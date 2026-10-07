import asyncio
import datetime
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.downloader import PlaylistInfo, TranscriptDownloader, VideoItem

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("yt_transcript_dl")

# Configuration from environment variables
PORT = int(os.environ.get("PORT", "8000"))
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/data/transcripts")
DEFAULT_LANGUAGE = os.environ.get("DEFAULT_LANGUAGE", "en")
DEFAULT_DELAY = float(os.environ.get("DEFAULT_DELAY", "1.5"))

# Ensure base directory exists
output_path = Path(OUTPUT_DIR)
output_path.mkdir(parents=True, exist_ok=True)


class DownloadRequest(BaseModel):
    url: str = Field(..., description="Channel URL, handle @name, or playlist URL")
    format: str = Field(default="txt", pattern="^(srt|vtt|txt)$", description="Output subtitle format")
    language: str = Field(default=DEFAULT_LANGUAGE, description="Language code (e.g. en, es, fr)")
    delay: float = Field(default=DEFAULT_DELAY, ge=0.0, le=30.0, description="Polite request delay in seconds")
    overwrite: bool = Field(default=False, description="Whether to overwrite existing transcript files")


class ReorderRequest(BaseModel):
    job_ids: List[str]


class MoveRequest(BaseModel):
    direction: str = Field(..., pattern="^(up|down)$")


class SkipRequest(BaseModel):
    requeue: bool = True


class JobState:
    def __init__(self, job_id: str, request: DownloadRequest):
        self.job_id: str = job_id
        self.url: str = request.url
        self.format: str = request.format
        self.language: str = request.language
        self.delay: float = request.delay
        self.overwrite: bool = request.overwrite
        self.status: str = "queued"  # queued, extracting, downloading, completed, cancelled, skipped, failed
        self.channel_name: str = ""
        self.sanitized_channel_name: str = ""
        self.playlist_title: str = ""
        self.total_videos: int = 0
        self.current_index: int = 0
        self.current_video_title: str = ""
        self.completed_count: int = 0
        self.skipped_count: int = 0
        self.failed_count: int = 0
        self.progress_percent: float = 0.0
        self.created_at: str = datetime.datetime.now().isoformat()
        self.completed_at: Optional[str] = None
        self.error_message: Optional[str] = None
        self.is_cancelled: bool = False
        self.is_skipped: bool = False
        self.logs: List[Dict[str, str]] = []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "url": self.url,
            "format": self.format,
            "language": self.language,
            "delay": self.delay,
            "overwrite": self.overwrite,
            "status": self.status,
            "channel_name": self.channel_name,
            "sanitized_channel_name": self.sanitized_channel_name,
            "playlist_title": self.playlist_title,
            "total_videos": self.total_videos,
            "current_index": self.current_index,
            "current_video_title": self.current_video_title,
            "completed_count": self.completed_count,
            "skipped_count": self.skipped_count,
            "failed_count": self.failed_count,
            "progress_percent": round(self.progress_percent, 1),
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "error_message": self.error_message,
            "is_cancelled": self.is_cancelled,
            "is_skipped": self.is_skipped,
        }


class JobManager:
    def __init__(self):
        self.active_job: Optional[JobState] = None
        self.queue: List[JobState] = []
        self.history: List[Dict[str, Any]] = []
        self.sse_subscribers: Set[asyncio.Queue] = set()
        self.worker_task: Optional[asyncio.Task] = None
        self.queue_event = asyncio.Event()
        self.downloader = TranscriptDownloader(base_output_dir=OUTPUT_DIR)
        self.loop: Optional[asyncio.AbstractEventLoop] = None

    def get_queue_state(self) -> Dict[str, Any]:
        return {
            "active": self.active_job.to_dict() if self.active_job else None,
            "queue": [j.to_dict() for j in self.queue],
            "total_pending": len(self.queue),
        }

    async def broadcast(self, event_type: str, data: Any):
        """Send SSE payload to all connected clients."""
        payload = json.dumps({"type": event_type, "data": data})
        message = f"event: {event_type}\ndata: {payload}\n\n"
        dead_subscribers = set()
        for q in self.sse_subscribers:
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                dead_subscribers.add(q)
        for dead in dead_subscribers:
            self.sse_subscribers.discard(dead)

    async def broadcast_queue(self):
        await self.broadcast("queue", self.get_queue_state())

    def log(self, level: str, message: str):
        """Append log message and broadcast thread-safely."""
        now_str = datetime.datetime.now().strftime("%H:%M:%S")
        log_entry = {
            "timestamp": now_str,
            "level": level.upper(),
            "message": message,
        }
        if self.active_job:
            self.active_job.logs.append(log_entry)
            if len(self.active_job.logs) > 1000:
                self.active_job.logs = self.active_job.logs[-1000:]
        logger.info(f"[{level.upper()}] {message}")

        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self.broadcast("log", log_entry), self.loop)

    def enqueue_job(self, request: DownloadRequest) -> JobState:
        """Adds a new job to the pending queue and wakes the worker."""
        job_id = str(uuid.uuid4())[:8]
        job = JobState(job_id=job_id, request=request)
        self.queue.append(job)
        self.log("INFO", f"Enqueued [{job_id}]: '{request.url}' (position #{len(self.queue)})")
        self.queue_event.set()
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self.broadcast_queue(), self.loop)
        return job

    def reorder_queue(self, job_ids: List[str]) -> bool:
        """Reorders the queue to match the provided list of job IDs."""
        current_map = {j.job_id: j for j in self.queue}
        new_queue = [current_map[jid] for jid in job_ids if jid in current_map]
        for j in self.queue:
            if j.job_id not in job_ids:
                new_queue.append(j)
        self.queue = new_queue
        self.log("INFO", f"Queue order updated ({len(self.queue)} items pending)")
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self.broadcast_queue(), self.loop)
        return True

    def move_job(self, job_id: str, direction: str) -> bool:
        """Moves a queued job up or down one position."""
        for i, j in enumerate(self.queue):
            if j.job_id == job_id:
                if direction == "up" and i > 0:
                    self.queue[i], self.queue[i - 1] = self.queue[i - 1], self.queue[i]
                elif direction == "down" and i < len(self.queue) - 1:
                    self.queue[i], self.queue[i + 1] = self.queue[i + 1], self.queue[i]
                else:
                    return False
                self.log("INFO", f"Moved job [{job_id}] {direction} in queue.")
                if self.loop and self.loop.is_running():
                    asyncio.run_coroutine_threadsafe(self.broadcast_queue(), self.loop)
                return True
        return False

    def remove_from_queue(self, job_id: str) -> bool:
        """Removes a job from the queue or cancels it if currently active."""
        if self.active_job and self.active_job.job_id == job_id:
            self.active_job.is_cancelled = True
            self.active_job.status = "cancelled"
            self.log("WARN", f"Active job [{job_id}] cancelled.")
            if self.loop and self.loop.is_running():
                asyncio.run_coroutine_threadsafe(self.broadcast_queue(), self.loop)
            return True

        for i, j in enumerate(self.queue):
            if j.job_id == job_id:
                removed = self.queue.pop(i)
                removed.status = "cancelled"
                self.log("INFO", f"Removed job [{job_id}] ('{removed.url}') from queue.")
                self._record_history(removed)
                if self.loop and self.loop.is_running():
                    asyncio.run_coroutine_threadsafe(self.broadcast_queue(), self.loop)
                return True
        return False

    def skip_active_job(self, requeue: bool = True) -> bool:
        """Skips the active job and optionally re-queues it at the end to resume later."""
        if self.active_job and self.active_job.status in ("extracting", "downloading", "queued"):
            old = self.active_job
            old.is_cancelled = True
            old.is_skipped = True
            old.status = "skipped"
            self.log("WARN", f"Skipping active job [{old.job_id}] ('{old.channel_name or old.url}').")

            if requeue:
                req = DownloadRequest(
                    url=old.url,
                    format=old.format,
                    language=old.language,
                    delay=old.delay,
                    overwrite=old.overwrite,
                )
                new_job = JobState(job_id=str(uuid.uuid4())[:8], request=req)
                self.queue.append(new_job)
                self.log("INFO", f"Re-queued skipped job as [{new_job.job_id}] to resume later.")

            if self.loop and self.loop.is_running():
                asyncio.run_coroutine_threadsafe(self.broadcast_queue(), self.loop)
            return True
        return False

    async def worker_loop(self):
        """Sequential queue processing worker."""
        while True:
            try:
                while not self.queue:
                    self.active_job = None
                    await self.broadcast_queue()
                    await self.queue_event.wait()
                    self.queue_event.clear()

                if not self.queue:
                    continue

                self.active_job = self.queue.pop(0)
                await self.broadcast_queue()
                await self._run_job(self.active_job)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"Unexpected error in queue worker: {e}")
                await asyncio.sleep(1)
            finally:
                self.active_job = None
                await self.broadcast_queue()

    async def _run_job(self, job: JobState):
        try:
            job.status = "extracting"
            await self.broadcast("state", job.to_dict())
            await self.broadcast_queue()
            self.log("INFO", f"Starting flat playlist extraction (format: {job.format}, lang: {job.language})...")

            try:
                playlist_info: PlaylistInfo = await asyncio.to_thread(
                    self.downloader.extract_channel_or_playlist,
                    job.url,
                    lambda lvl, msg: self.log(lvl, msg),
                )
            except Exception as e:
                err_msg = f"Failed to extract playlist/channel: {str(e)}"
                job.status = "failed"
                job.error_message = err_msg
                self.log("ERROR", err_msg)
                await self.broadcast("state", job.to_dict())
                self._record_history(job)
                return

            if job.is_cancelled:
                job.status = "skipped" if job.is_skipped else "cancelled"
                await self.broadcast("state", job.to_dict())
                self._record_history(job)
                return

            job.channel_name = playlist_info.channel_name
            job.sanitized_channel_name = playlist_info.sanitized_channel_name
            job.playlist_title = playlist_info.playlist_title
            job.total_videos = playlist_info.total_videos
            job.status = "downloading"
            await self.broadcast("state", job.to_dict())
            await self.broadcast_queue()

            self.log(
                "INFO",
                f"Target '{job.channel_name}' detected. Total videos in queue: {job.total_videos}",
            )

            if job.total_videos == 0:
                self.log("WARN", "No videos found in specified target.")
                job.status = "completed"
                job.progress_percent = 100.0
                await self.broadcast("state", job.to_dict())
                self._record_history(job)
                return

            for idx, item in enumerate(playlist_info.items):
                if job.is_cancelled:
                    job.status = "skipped" if job.is_skipped else "cancelled"
                    self.log("WARN", f"Job halted ({job.status}). Processed {idx}/{job.total_videos} videos.")
                    break

                job.current_index = idx + 1
                job.current_video_title = item.title
                job.progress_percent = (idx / job.total_videos) * 100.0
                await self.broadcast("state", job.to_dict())

                result = await asyncio.to_thread(
                    self.downloader.download_video_transcript,
                    video=item,
                    channel_name=job.channel_name,
                    output_format=job.format,
                    preferred_lang=job.language,
                    overwrite=job.overwrite,
                    log_fn=lambda lvl, msg: self.log(lvl, msg),
                )

                if result.status == "completed":
                    job.completed_count += 1
                elif result.status == "skipped":
                    job.skipped_count += 1
                else:
                    job.failed_count += 1

                job.progress_percent = ((idx + 1) / job.total_videos) * 100.0
                await self.broadcast("state", job.to_dict())

                if idx < job.total_videos - 1 and not job.is_cancelled and job.delay > 0:
                    await asyncio.sleep(job.delay)

            if not job.is_cancelled:
                job.status = "completed"
                job.progress_percent = 100.0
                self.log(
                    "SUCCESS",
                    f"Batch download finished! Completed: {job.completed_count}, Skipped: {job.skipped_count}, Failed: {job.failed_count}",
                )

        except Exception as e:
            job.status = "failed"
            job.error_message = str(e)
            self.log("ERROR", f"Unexpected job failure: {str(e)}")

        finally:
            job.completed_at = datetime.datetime.now().isoformat()
            await self.broadcast("state", job.to_dict())
            self._record_history(job)

    def _record_history(self, job: JobState):
        record = job.to_dict()
        record["logs_count"] = len(job.logs)
        self.history.insert(0, record)
        if len(self.history) > 50:
            self.history = self.history[:50]


job_manager = JobManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    job_manager.loop = asyncio.get_running_loop()
    job_manager.worker_task = asyncio.create_task(job_manager.worker_loop())
    yield
    if job_manager.worker_task:
        job_manager.worker_task.cancel()


app = FastAPI(
    title="YouTube Transcript Downloader",
    description="Batch download YouTube transcripts with queue and directory management",
    version="1.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=FileResponse)
@app.head("/", response_class=FileResponse)
async def serve_index():
    index_file = STATIC_DIR / "index.html"
    if not index_file.exists():
        return HTMLResponse("<h1>Index file missing</h1>", status_code=404)
    return FileResponse(str(index_file))


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "output_dir": OUTPUT_DIR,
        "port": PORT,
    }


@app.get("/api/config")
async def get_config():
    return {
        "port": PORT,
        "output_dir": OUTPUT_DIR,
        "default_language": DEFAULT_LANGUAGE,
        "default_delay": DEFAULT_DELAY,
    }


@app.get("/api/status")
async def get_status():
    if job_manager.active_job:
        data = job_manager.active_job.to_dict()
        data["logs"] = job_manager.active_job.logs[-100:]
        return data
    return {"status": "idle"}


@app.get("/api/queue")
async def get_queue():
    return job_manager.get_queue_state()


@app.post("/api/download")
async def start_download(request: DownloadRequest):
    """Enqueues a new channel or playlist download job."""
    job = job_manager.enqueue_job(request)
    return {
        "job": job.to_dict(),
        "queue_length": len(job_manager.queue),
        "is_active": (job_manager.active_job == job),
    }


@app.post("/api/queue/reorder")
async def reorder_queue(req: ReorderRequest):
    success = job_manager.reorder_queue(req.job_ids)
    return {"success": success, "queue": job_manager.get_queue_state()}


@app.post("/api/queue/{job_id}/move")
async def move_queue_item(job_id: str, req: MoveRequest):
    success = job_manager.move_job(job_id, req.direction)
    return {"success": success, "queue": job_manager.get_queue_state()}


@app.delete("/api/queue/{job_id}")
async def remove_queue_item(job_id: str):
    removed = job_manager.remove_from_queue(job_id)
    return {"success": removed, "queue": job_manager.get_queue_state()}


@app.post("/api/queue/skip-active")
async def skip_active_item(req: SkipRequest):
    skipped = job_manager.skip_active_job(requeue=req.requeue)
    return {"skipped": skipped, "queue": job_manager.get_queue_state()}


@app.post("/api/cancel")
async def cancel_download():
    """Cancels the active job."""
    if job_manager.active_job:
        cancelled = job_manager.remove_from_queue(job_manager.active_job.job_id)
        return {"cancelled": cancelled}
    return {"cancelled": False}


@app.get("/api/history")
async def get_history():
    return job_manager.history


@app.get("/api/files")
async def list_files():
    """List downloaded channel folders and transcript files."""
    try:
        base = Path(OUTPUT_DIR)
        if not base.exists():
            return {"channels": []}

        channels = []
        for channel_folder in sorted(base.iterdir()):
            if channel_folder.is_dir():
                files = []
                for f in sorted(channel_folder.iterdir()):
                    if f.is_file() and f.suffix in (".txt", ".srt", ".vtt"):
                        files.append(
                            {
                                "name": f.name,
                                "size": f.stat().st_size,
                                "modified": datetime.datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
                            }
                        )
                channels.append(
                    {
                        "channel": channel_folder.name,
                        "file_count": len(files),
                        "files": files,
                    }
                )
        return {"channels": channels}
    except Exception as e:
        logger.error(f"Error listing files: {e}")
        return {"channels": [], "error": str(e)}


@app.get("/api/events")
async def sse_events(request: Request):
    """
    Server-Sent Events endpoint streaming real-time queue, job status, and logs.
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    job_manager.sse_subscribers.add(queue)

    async def event_generator():
        try:
            # Initial Queue state
            queue_json = json.dumps({"type": "queue", "data": job_manager.get_queue_state()})
            yield f"event: queue\ndata: {queue_json}\n\n"

            # Initial Job state
            if job_manager.active_job:
                initial_state = json.dumps({"type": "state", "data": job_manager.active_job.to_dict()})
                yield f"event: state\ndata: {initial_state}\n\n"
                for log_item in job_manager.active_job.logs[-30:]:
                    log_data = json.dumps({"type": "log", "data": log_item})
                    yield f"event: log\ndata: {log_data}\n\n"
            else:
                initial_state = json.dumps({"type": "state", "data": {"status": "idle"}})
                yield f"event: state\ndata: {initial_state}\n\n"

            while True:
                if await request.is_disconnected():
                    break
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield message
                except asyncio.TimeoutError:
                    yield ": ping\n\n"

        except asyncio.CancelledError:
            pass
        finally:
            job_manager.sse_subscribers.discard(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=PORT, reload=False)
