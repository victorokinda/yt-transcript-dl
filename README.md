# YouTube Transcript Downloader (`yt-transcript-dl`)

A containerized application with a responsive web UI to batch-download YouTube channel, playlist, or single video transcripts into cleanly organized local directories.

---

## Features

- **Dashboard UI**:
  - Single-page dashboard built with Tailwind CSS and FontAwesome (zero Node.js/npm build steps).
  - Configurable target URL (`@channel_name`, channel URL, playlist URL, or individual video URL).
  - Output format selector: `txt` (clean timestamped `[HH:MM:SS] Subtitle`), `srt` (SubRip), or `vtt` (WebVTT).
  - Preferred language selector with automatic caption fallback.
  - Interactive polite delay slider (0.0s – 10.0s) to prevent YouTube rate-limiting (`HTTP 429`).
  - Idempotent download mode: skips existing transcripts on disk by default unless "Overwrite" is checked.
- **Real-Time Telemetry & Logs**:
  - Live progress updates streamed via Server-Sent Events (SSE).
  - Metrics cards: Channel title, total detected videos, current video title marquee, completed, skipped, and failed counts.
  - Integrated terminal console with auto-scrolling log stream for warnings, errors, and download progress.
  - Built-in filesystem explorer showing saved channels and transcript files.
- **Engine**:
  - **Flat Playlist Extraction**: Extracts playlist and channel items via `yt-dlp` (`extract_flat='in_playlist'`) without buffering hundreds of video web pages into memory.
  - **No Media Downloads**: Only transcript/subtitle files are downloaded (`skip_download: True`); no video or audio files are downloaded.
  - **Smart Subtitle Normalization**: Converts raw YouTube auto-captions by stripping markup tags (`<c>`, `<font>`), decoding HTML entities, and deduplicating rolling ASR lines for readable plain text.
  - **Path Sanitization**: Cross-platform filesystem sanitization stripping `/ \ : * ? " < > |` and control characters.
- **Containerization**:
  - Multi-platform Docker support (`linux/amd64`, `linux/arm64`).
  - Runtime dependencies (`ffmpeg`, `nodejs` JavaScript runtime for modern YouTube player extraction, `curl-cffi`).
  - Configurable port mapping through `.env` file without hardcoded port collisions.
  - Persistent volume binding for transcripts output.

---

## Directory Structure

Downloaded transcripts are organized as:
```text
transcripts/
├── 3Blue1Brown/
│   ├── Essence of linear algebra preview.txt
│   └── Vectors, what even are they.txt
└── Veritasium/
    ├── Why No One Has Measured The Speed Of Light.txt
    └── The Simplest Math Problem No One Can Solve.txt
```

---

## Quick Start

### 1. Clone & Configure

Copy the environment template:
```bash
cp .env.example .env
```

Edit `.env` to configure your preferred port and default options:
```env
# Change to any available port (e.g. 8080 or 8000)
PORT=8080

# Host directory where transcripts will be saved (relative or absolute path)
HOST_TRANSCRIPTS_DIR=./transcripts

# Destination folder inside the container
OUTPUT_DIR=/data/transcripts

# Default subtitle language (manual first, automatic fallback)
DEFAULT_LANGUAGE=en

# Default delay in seconds between downloads
DEFAULT_DELAY=1.5
```

### 2. Run with Docker Compose

```bash
docker compose up --build -d
```

Open your browser at:
```
http://localhost:8080
```
*(or the port specified in `.env`)*

Transcripts will be saved locally into the `./transcripts` directory.

To stop the container:
```bash
docker compose down
```

---

## Local Development (Without Docker)

1. Create a Python 3.11+ virtual environment:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Run the application:
   ```bash
   export PORT=8080
   export OUTPUT_DIR=./transcripts
   uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
   ```

3. Run unit tests:
   ```bash
   pytest tests/
   ```

---

## API Reference

| Endpoint | Method | Description |
|---|---|---|
| `/` | `GET` | Single-page Web UI dashboard |
| `/health` | `GET` | Health check endpoint |
| `/api/config` | `GET` | Current server configuration (`port`, `output_dir`, defaults) |
| `/api/status` | `GET` | Active job progress metrics and current telemetry |
| `/api/download` | `POST` | Start a batch download job |
| `/api/cancel` | `POST` | Cancel the active download session |
| `/api/events` | `GET` | Server-Sent Events (SSE) live stream for progress & logs |
| `/api/history` | `GET` | History of recent download jobs |
| `/api/files` | `GET` | List saved channels and transcript files on disk |

### Example `POST /api/download` Payload

```json
{
  "url": "https://www.youtube.com/@veritasium",
  "format": "txt",
  "language": "en",
  "delay": 1.5,
  "overwrite": false
}
```

---

## Format Details

- **`txt`**: Human-readable text format with formatted timestamps and clean captions:
  ```text
  [00:00:01] Hello everyone, welcome to this lecture.
  [00:00:05] Today we will discuss linear algebra.
  ```
- **`srt`**: SubRip subtitle format (`.srt`) with cue counters and millisecond precision.
- **`vtt`**: WebVTT format (`.vtt`) as supplied by YouTube.
