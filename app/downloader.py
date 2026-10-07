import html
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Generator, List, Optional, Tuple

import yt_dlp

logger = logging.getLogger("yt_transcript_dl.downloader")


@dataclass
class VideoItem:
    id: str
    title: str
    url: str
    duration: Optional[int] = None


@dataclass
class PlaylistInfo:
    channel_name: str
    sanitized_channel_name: str
    playlist_title: str
    total_videos: int
    items: List[VideoItem]


@dataclass
class DownloadResult:
    video_id: str
    title: str
    status: str  # "completed", "skipped", "failed"
    message: str
    file_path: Optional[str] = None


def sanitize_filename(name: str, max_length: int = 180) -> str:
    r"""
    Sanitize string for safe cross-platform filesystem use.
    Strips OS-restricted characters: / \ : * ? " < > |
    Strips control characters, collapses whitespace, trims dots/spaces.
    """
    if not name:
        return "untitled"

    # Replace OS restricted characters with underscores or empty
    cleaned = re.sub(r'[\\/*?:"<>|]', "", name)
    # Strip non-printable / control characters
    cleaned = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", cleaned)
    # Collapse multiple whitespace characters into single space
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # Strip leading/trailing dots and spaces
    cleaned = cleaned.strip(". ")

    if not cleaned:
        cleaned = "untitled"

    # Ensure length restriction while preserving unicode cleanly
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip(". ")

    return cleaned


def normalize_youtube_url(url: str) -> str:
    """
    Normalizes YouTube URLs:
    - @channel_handle -> https://www.youtube.com/@handle/videos
    - https://www.youtube.com/@handle -> https://www.youtube.com/@handle/videos
    """
    cleaned_url = url.strip()
    if cleaned_url.startswith("@"):
        return f"https://www.youtube.com/{cleaned_url}/videos"

    # If it's a channel URL like https://www.youtube.com/@handle without a subpath
    match = re.match(r"^(https?://(?:www\.)?youtube\.com/@[^/?#]+)(?:/)?$", cleaned_url)
    if match:
        return f"{match.group(1)}/videos"

    return cleaned_url


def format_seconds_to_timestamp(seconds_float: float) -> str:
    """Format total seconds into HH:MM:SS."""
    total_sec = int(seconds_float)
    hours = total_sec // 3600
    minutes = (total_sec % 3600) // 60
    seconds = total_sec % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def timestamp_str_to_hhmmss(ts: str) -> str:
    """
    Converts various timestamp strings (e.g. 00:01:23.456 or 01:23.456)
    into standard [HH:MM:SS].
    """
    # ts could be HH:MM:SS.mmm or MM:SS.mmm
    ts = ts.replace(",", ".").split(".")[0]
    parts = ts.split(":")
    if len(parts) == 3:
        h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
    elif len(parts) == 2:
        h, m, s = 0, int(parts[0]), int(parts[1])
    else:
        return "[00:00:00]"
    return f"[{h:02d}:{m:02d}:{s:02d}]"


def clean_vtt_text(raw_text: str) -> str:
    """
    Cleans WebVTT cue text:
    - Removes inline HTML/VTT tags like <c>, </c>, <00:00:01.000>, <v voice>, <font...>
    - Unescapes HTML entities
    - Strips whitespace
    """
    # Remove karaoke/timing tags <00:00:00.000>
    text = re.sub(r"<\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?>", "", raw_text)
    # Remove HTML/XML tags
    text = re.sub(r"<[^>]+>", "", text)
    # Unescape HTML entities (&nbsp;, &amp;, &#39;, etc.)
    text = html.unescape(text)
    # Replace non-breaking spaces
    text = text.replace("\xa0", " ")
    # Collapse internal whitespace
    text = re.sub(r"[ \t]+", " ", text).strip()
    return text


def convert_vtt_or_srt_to_txt(sub_content: str) -> str:
    """
    Converts raw WebVTT or SRT subtitle text into timestamped plain text:
    Format: [HH:MM:SS] Subtitle text
    Deduplicates consecutive identical / rolling caption lines from YouTube auto-subs.
    """
    lines = sub_content.splitlines()
    parsed_entries: List[Tuple[str, str]] = []

    # Regex for timestamp cues
    # Example: 00:00:01.200 --> 00:00:03.400 or 00:01.200 --> 00:03.400
    timestamp_pattern = re.compile(
        r"(?:^|\s)(\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?)\s*-->\s*(\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?)"
    )

    current_timestamp: Optional[str] = None
    current_text_buffer: List[str] = []

    for line in lines:
        line_stripped = line.strip()

        # Check for timestamp line
        match = timestamp_pattern.search(line_stripped)
        if match:
            # If we had buffered text from previous cue, flush it
            if current_timestamp and current_text_buffer:
                combined_text = " ".join(current_text_buffer).strip()
                cleaned = clean_vtt_text(combined_text)
                if cleaned:
                    parsed_entries.append((current_timestamp, cleaned))
                current_text_buffer = []

            # Set new timestamp (start timestamp formatted as [HH:MM:SS])
            current_timestamp = timestamp_str_to_hhmmss(match.group(1))
            continue

        # Skip headers / numeric cue indices in SRT
        if not line_stripped:
            continue
        if line_stripped.startswith("WEBVTT") or line_stripped.startswith("Kind:") or line_stripped.startswith("Language:"):
            continue
        if line_stripped.startswith("NOTE") or line_stripped.startswith("STYLE"):
            continue
        if line_stripped.isdigit():
            # SRT cue index
            continue

        # Text line
        if current_timestamp:
            current_text_buffer.append(line_stripped)

    # Flush last cue
    if current_timestamp and current_text_buffer:
        combined_text = " ".join(current_text_buffer).strip()
        cleaned = clean_vtt_text(combined_text)
        if cleaned:
            parsed_entries.append((current_timestamp, cleaned))

    # Post-process: Deduplicate rolling subtitle lines common in YouTube ASR
    output_lines: List[str] = []
    last_text = ""
    for ts, text in parsed_entries:
        if not text:
            continue
        # If this line is identical to the last line, skip
        if text == last_text:
            continue
        # If last_text is a prefix of text (rolling caption word build-up)
        # e.g. "so here we are" followed by "so here we are in front"
        if last_text and text.startswith(last_text) and len(text) > len(last_text):
            # Replace the previous entry's text if timestamps are close,
            # or just emit the new part
            if output_lines:
                output_lines.pop()
        output_lines.append(f"{ts} {text}")
        last_text = text

    return "\n".join(output_lines)


class TranscriptDownloader:
    """
    Handles yt-dlp metadata extraction, flat playlist parsing,
    subtitle downloads, and structured local directory storage.
    """

    def __init__(self, base_output_dir: str = "/data/transcripts"):
        self.base_output_dir = Path(base_output_dir)

    def extract_channel_or_playlist(
        self, url: str, log_fn: Optional[Callable[[str, str], None]] = None
    ) -> PlaylistInfo:
        """
        Uses flat extraction ('extract_flat': 'in_playlist') to parse a channel or playlist
        without fetching complete video webpages or media streams into RAM.
        """
        normalized_url = normalize_youtube_url(url)
        if log_fn:
            log_fn("INFO", f"Analyzing target: {normalized_url}")

        ydl_opts = {
            "extract_flat": "in_playlist",
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "ignoreerrors": True,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(normalized_url, download=False)

        if not info:
            raise ValueError(f"Could not extract metadata from {url}. Please verify URL accessibility.")

        # Determine channel name / playlist title
        playlist_title = info.get("title") or "YouTube Playlist"
        channel_name = (
            info.get("channel")
            or info.get("uploader")
            or info.get("channel_name")
            or playlist_title
        )

        sanitized_channel = sanitize_filename(channel_name)

        entries = info.get("entries")
        items: List[VideoItem] = []

        if entries is not None:
            # Handle playlist / channel entries generator or list
            for entry in entries:
                if not entry:
                    continue
                v_id = entry.get("id") or entry.get("url")
                v_title = entry.get("title") or f"video_{v_id}"
                v_url = entry.get("url")
                if not v_url or not v_url.startswith("http"):
                    v_url = f"https://www.youtube.com/watch?v={v_id}"
                items.append(
                    VideoItem(
                        id=str(v_id),
                        title=str(v_title),
                        url=v_url,
                        duration=entry.get("duration"),
                    )
                )
        else:
            # Single video URL was provided
            v_id = info.get("id") or "video"
            v_title = info.get("title") or "Video"
            v_url = info.get("webpage_url") or f"https://www.youtube.com/watch?v={v_id}"
            items.append(
                VideoItem(
                    id=str(v_id),
                    title=str(v_title),
                    url=v_url,
                    duration=info.get("duration"),
                )
            )

        return PlaylistInfo(
            channel_name=channel_name,
            sanitized_channel_name=sanitized_channel,
            playlist_title=playlist_title,
            total_videos=len(items),
            items=items,
        )

    def download_video_transcript(
        self,
        video: VideoItem,
        channel_name: str,
        output_format: str = "txt",
        preferred_lang: str = "en",
        overwrite: bool = False,
        log_fn: Optional[Callable[[str, str], None]] = None,
    ) -> DownloadResult:
        """
        Downloads the transcript for a single video.
        - Preserves directory tree: <base_output_dir>/<Sanitized Channel Name>/<Sanitized Video Title>.<format>
        - Skips existing files if overwrite is False.
        - Attempts manual captions first, falls back to automatic captions.
        - Converts to target format ('srt', 'vtt', 'txt').
        """
        sanitized_channel = sanitize_filename(channel_name)
        sanitized_title = sanitize_filename(video.title)
        channel_dir = self.base_output_dir / sanitized_channel
        channel_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(channel_dir, 0o777)
        except Exception:
            pass

        target_file = channel_dir / f"{sanitized_title}.{output_format}"

        # Idempotency check: Skip if already present
        if target_file.exists() and not overwrite:
            if log_fn:
                log_fn("INFO", f"Skipped: '{video.title}' (file already exists)")
            return DownloadResult(
                video_id=video.id,
                title=video.title,
                status="skipped",
                message="File already exists on disk",
                file_path=str(target_file),
            )

        # Build candidate languages: e.g. ['en', 'en-orig', 'en-US', 'en-GB']
        lang = preferred_lang.strip().lower()
        sub_langs = [lang, f"{lang}-orig"]
        if lang == "en":
            sub_langs.extend(["en-US", "en-GB"])

        # We download temporary subtitles to a unique temporary directory
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_tmpl = Path(tmp_dir) / "%(id)s.%(ext)s"

            # Determine subtitle format for yt-dlp
            # yt-dlp supports 'vtt' natively from YouTube captions
            download_sub_format = "srt" if output_format == "srt" else "vtt"

            ydl_opts = {
                "skip_download": True,
                "writesubtitles": True,
                "writeautomaticsub": True,
                "subtitleslangs": sub_langs,
                "subtitlesformat": download_sub_format,
                "outtmpl": str(tmp_tmpl),
                "quiet": True,
                "no_warnings": True,
                "ignoreerrors": True,
            }

            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([video.url])
            except Exception as e:
                err_msg = f"Network or extraction error: {str(e)}"
                if log_fn:
                    log_fn("ERROR", f"Failed '{video.title}': {err_msg}")
                return DownloadResult(
                    video_id=video.id,
                    title=video.title,
                    status="failed",
                    message=err_msg,
                )

            # Find downloaded subtitle files in tmp_dir
            found_files = list(Path(tmp_dir).glob(f"{video.id}.*.*"))
            if not found_files:
                # Also check any file matching video.id
                found_files = [f for f in Path(tmp_dir).iterdir() if f.is_file() and video.id in f.name]

            if not found_files:
                msg = f"No subtitles available in language '{preferred_lang}'"
                if log_fn:
                    log_fn("WARN", f"Warning: '{video.title}' - {msg}")
                return DownloadResult(
                    video_id=video.id,
                    title=video.title,
                    status="failed",
                    message=msg,
                )

            # Prefer file matching preferred language
            selected_file = found_files[0]
            for f in found_files:
                if f".{lang}." in f.name:
                    selected_file = f
                    break

            try:
                raw_content = selected_file.read_text(encoding="utf-8", errors="replace")

                if output_format == "txt":
                    # Convert to timestamped plain text [HH:MM:SS] Subtitle
                    cleaned_txt = convert_vtt_or_srt_to_txt(raw_content)
                    target_file.write_text(cleaned_txt, encoding="utf-8")
                elif output_format == "vtt":
                    target_file.write_text(raw_content, encoding="utf-8")
                elif output_format == "srt":
                    # If file is already srt, copy; otherwise convert
                    if selected_file.suffix == ".srt":
                        target_file.write_text(raw_content, encoding="utf-8")
                    else:
                        # VTT downloaded, convert cues to SRT format or write
                        # yt-dlp usually converts to srt when requested, but fallback:
                        target_file.write_text(raw_content, encoding="utf-8")

                try:
                    os.chmod(target_file, 0o666)
                except Exception:
                    pass

                if log_fn:
                    log_fn("SUCCESS", f"Downloaded: '{video.title}' -> {target_file.name}")

                return DownloadResult(
                    video_id=video.id,
                    title=video.title,
                    status="completed",
                    message=f"Successfully saved to {target_file.name}",
                    file_path=str(target_file),
                )

            except Exception as e:
                err_msg = f"Failed writing transcript: {str(e)}"
                if log_fn:
                    log_fn("ERROR", f"Error saving '{video.title}': {err_msg}")
                return DownloadResult(
                    video_id=video.id,
                    title=video.title,
                    status="failed",
                    message=err_msg,
                )
