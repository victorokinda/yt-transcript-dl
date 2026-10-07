import os
import unittest
from pathlib import Path
from app.downloader import (
    sanitize_filename,
    normalize_youtube_url,
    convert_vtt_or_srt_to_txt,
    TranscriptDownloader,
    VideoItem,
)


class TestDownloader(unittest.TestCase):
    def test_sanitize_filename(self):
        self.assertEqual(
            sanitize_filename('Hello: World / Test? * < > | "'),
            "Hello World Test",
        )
        self.assertEqual(
            sanitize_filename("... leading and trailing dots ..."),
            "leading and trailing dots",
        )
        self.assertEqual(sanitize_filename(""), "untitled")
        self.assertEqual(sanitize_filename("   "), "untitled")
        # Unicode characters preservation
        self.assertEqual(sanitize_filename("日本語のタイトル"), "日本語のタイトル")
        # Long string truncation
        long_str = "a" * 250
        sanitized_long = sanitize_filename(long_str, max_length=150)
        self.assertLessEqual(len(sanitized_long), 150)

    def test_normalize_url(self):
        self.assertEqual(
            normalize_youtube_url("@hubermanlab"),
            "https://www.youtube.com/@hubermanlab/videos",
        )
        self.assertEqual(
            normalize_youtube_url("https://www.youtube.com/@hubermanlab"),
            "https://www.youtube.com/@hubermanlab/videos",
        )
        self.assertEqual(
            normalize_youtube_url("https://www.youtube.com/@hubermanlab/playlists"),
            "https://www.youtube.com/@hubermanlab/playlists",
        )
        playlist_url = "https://www.youtube.com/playlist?list=PL12345"
        self.assertEqual(normalize_youtube_url(playlist_url), playlist_url)

    def test_convert_vtt_to_txt(self):
        sample_vtt = """WEBVTT
Kind: captions
Language: en

00:00:01.200 --> 00:00:03.360
All right, so <c>here we are</c>,

00:00:03.360 --> 00:00:05.500
All right, so here we are, in front of the elephants

00:00:05.500 --> 00:00:08.100
and the cool thing about them &amp; their trunks
"""
        txt = convert_vtt_or_srt_to_txt(sample_vtt)
        lines = txt.splitlines()
        self.assertTrue(len(lines) >= 2)
        # Check timestamp format [HH:MM:SS]
        self.assertTrue(lines[0].startswith("[00:00:01]") or lines[0].startswith("[00:00:03]"))
        # Check HTML unescaped
        self.assertIn("them & their trunks", txt)
        # Check cue tags stripped
        self.assertNotIn("<c>", txt)
        self.assertNotIn("</c>", txt)

    def test_idempotency_skip(self):
        output_dir = Path("/tmp/test_transcripts")
        output_dir.mkdir(parents=True, exist_ok=True)
        channel_dir = output_dir / "Test_Channel"
        channel_dir.mkdir(parents=True, exist_ok=True)

        existing_file = channel_dir / "Existing_Video.txt"
        existing_file.write_text("[00:00:00] Pre-existing transcript", encoding="utf-8")

        downloader = TranscriptDownloader(base_output_dir=str(output_dir))
        video = VideoItem(id="test1234", title="Existing_Video", url="https://youtube.com/watch?v=test1234")

        result = downloader.download_video_transcript(
            video=video,
            channel_name="Test_Channel",
            output_format="txt",
            overwrite=False,
        )

        self.assertEqual(result.status, "skipped")
        self.assertIn("already exists", result.message.lower())


if __name__ == "__main__":
    unittest.main()
