"""Background worker: downloads subtitles, summarises them with Haiku and stores the result.

One daemon thread with an in-memory queue (lost on restart; the next sync_channel re-queues what is missing).
"""

import queue
import random
import sys
import threading
import time

import yt_dlp

import db
import summarizer
import youtube

# Pause between videos that actually hit YouTube - lowers the chance of an IP block.
THROTTLE_SECONDS = (3.0, 8.0)

_jobs: "queue.Queue[str]" = queue.Queue()
_queued_ids: set[str] = set()
_queue_lock = threading.Lock()
_worker: threading.Thread | None = None


def _log(message: str) -> None:
    print(f"[indexer] {message}", file=sys.stderr, flush=True)


def _process_video(video_id: str) -> bool:
    """Fetch the transcript, summarise it with Haiku and store the result.
    Returns True if YouTube was contacted (so the caller should throttle)."""
    row = db.get_video(video_id)
    if row is None:
        return False
    if row["status"] == "done" and row["summary_version"] == db.SUMMARY_VERSION:
        return False

    try:
        with yt_dlp.YoutubeDL({"skip_download": True, "quiet": True}) as ydl:
            info = ydl.extract_info(row["url"], download=False) or {}
            language, transcript = youtube.fetch_transcript(ydl, info, "pl")
        if not transcript.strip():
            raise RuntimeError("Subtitles were empty")

        title = info.get("title") or row["title"]
        upload_date = info.get("upload_date")
        summarised = summarizer.summarize(title, upload_date, transcript)
        result = summarised.summary
        db.save_summary(
            video_id,
            channel=info.get("channel") or info.get("uploader"),
            title=title,
            upload_date=upload_date,
            language=language,
            summary=result.summary,
            topics=result.topics,
            stances=[s.model_dump() for s in result.stances],
            input_tokens=summarised.input_tokens,
            output_tokens=summarised.output_tokens,
        )
        _log(f"OK {video_id} ({language}) {title} [haiku tokens in/out: "
             f"{summarised.input_tokens}/{summarised.output_tokens}]")
    except Exception as exc:
        db.mark_error(video_id, f"{type(exc).__name__}: {exc}")
        _log(f"ERROR {video_id}: {exc}")
    return True


def _worker_loop() -> None:
    while True:
        video_id = _jobs.get()
        try:
            contacted_youtube = _process_video(video_id)
        except Exception as exc:  # never let the worker die
            db.mark_error(video_id, f"{type(exc).__name__}: {exc}")
            contacted_youtube = False
        finally:
            with _queue_lock:
                _queued_ids.discard(video_id)
            _jobs.task_done()
        if contacted_youtube:
            time.sleep(random.uniform(*THROTTLE_SECONDS))


def enqueue(video_id: str, url: str, channel: str | None, title: str | None) -> str:
    """Register a video and queue it if needed. Returns 'queued' | 'already_done' | 'in_queue'."""
    global _worker
    needs_work = db.upsert_pending(video_id, url, channel, title)
    if not needs_work:
        return "already_done"
    with _queue_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_worker_loop, name="indexer", daemon=True)
            _worker.start()
        if video_id in _queued_ids:
            return "in_queue"
        _queued_ids.add(video_id)
    _jobs.put(video_id)
    return "queued"


def queue_length() -> int:
    return _jobs.qsize()


def wait_for(ids: list[str], wait_seconds: int) -> list:
    """Poll the database until none of the videos is 'pending' or the time budget (max 120 s) runs out."""
    deadline = time.monotonic() + max(0, min(wait_seconds, 120))
    while True:
        rows = [db.get_video(i) for i in ids]
        if not any(r and r["status"] == "pending" for r in rows) or time.monotonic() >= deadline:
            return rows
        time.sleep(1)
