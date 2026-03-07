"""
Runs API: create pipeline runs (upload + details) and stream progress via SSE.

  POST /runs       — Create run (multipart: file, meetingDate, language), return runId + streamUrl.
  GET  /runs/{id}/stream — SSE stream for extractor → normalizer → executor progress.
"""
import asyncio
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.requests import Request
from fastapi.responses import StreamingResponse

from api.pipeline import run_pipeline_sync

MAX_FILE_SIZE_BYTES = 15 * 1024 * 1024  # 15 MB
ALLOWED_EXTENSIONS = {".csv", ".txt", ".doc", ".pdf"}
UPLOAD_DIR = Path(__file__).resolve().parent.parent / "uploads"

router = APIRouter(prefix="/runs", tags=["runs"])

# In-memory run store: run_id -> { "queue": asyncio.Queue, "status": "pending"|"running"|"completed"|"error" }
_runs: dict[str, dict[str, Any]] = {}


def _ensure_upload_dir() -> Path:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    return UPLOAD_DIR


def _sse_message(event_type: str | None, data: dict) -> str:
    """Format one SSE message (event type optional, data as JSON line)."""
    import json
    lines = []
    if event_type is not None:
        lines.append(f"event: {event_type}")
    lines.append(f"data: {json.dumps(data)}")
    return "\n".join(lines) + "\n\n"


async def _run_pipeline_task(run_id: str, transcript_path: str, meeting_date: str | None, language: str | None) -> None:
    """Run pipeline in thread and push events to the run's queue."""
    run_state = _runs.get(run_id)
    if not run_state:
        return
    queue: asyncio.Queue = run_state["queue"]
    loop = asyncio.get_event_loop()

    def put_event(event_type: str, data: dict) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, {"event": event_type, "data": data})

    def run_in_thread() -> None:
        run_pipeline_sync(
            transcript_path,
            meeting_date,
            language,
            put_event,
            dry_run=True,
            contacts_path=None,
        )
        # Signal stream consumer that run is finished (no more events)
        loop.call_soon_threadsafe(queue.put_nowait, None)

    run_state["status"] = "running"
    await asyncio.get_event_loop().run_in_executor(None, run_in_thread)
    run_state["status"] = "completed"


# --- POST /runs (multipart or JSON) ---


@router.post("", status_code=201)
async def create_run(
    request: Request,
    file: UploadFile | None = File(None),
    meetingDate: str | None = Form(None),
    language: str | None = Form(None),
) -> dict:
    """
    Create a new pipeline run: upload a meeting transcript (or pass by reference),
    start processing asynchronously, and return an id to subscribe to for SSE progress.

    Multipart: file (required if not using JSON), meetingDate (e.g. YYYY-MM-DD), language (e.g. en, bn).
    JSON: fileRef (path or id), meetingDate, language.
    """
    transcript_path: str
    meeting_date_str: str | None = meetingDate
    language_str: str | None = language

    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body = await request.json()
        ref = body.get("fileRef")
        if not ref:
            raise HTTPException(status_code=400, detail="fileRef required when using application/json")
        meeting_date_str = body.get("meetingDate") or meeting_date_str
        language_str = body.get("language") or language_str
        # fileRef can be an absolute path or a stored filename under uploads/
        p = Path(ref)
        if p.is_absolute() and p.exists():
            transcript_path = str(p)
        else:
            candidate = UPLOAD_DIR / ref
            if not candidate.exists():
                raise HTTPException(status_code=404, detail=f"File not found: {ref}")
            transcript_path = str(candidate)
    else:
        if not file or not file.filename:
            raise HTTPException(status_code=400, detail="file is required (multipart/form-data)")
        suffix = Path(file.filename).suffix.lower()
        if suffix not in ALLOWED_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=f"File type not allowed. Allowed: {', '.join(ALLOWED_EXTENSIONS)}",
            )
        content = await file.read()
        if len(content) > MAX_FILE_SIZE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"File too large. Maximum size: 15 MB (got {len(content) / (1024*1024):.2f} MB).",
            )
        _ensure_upload_dir()
        safe_name = f"{uuid.uuid4().hex}{suffix}"
        dest = UPLOAD_DIR / safe_name
        dest.write_bytes(content)
        transcript_path = str(dest)

    run_id = uuid.uuid4().hex
    queue: asyncio.Queue = asyncio.Queue()
    _runs[run_id] = {"queue": queue, "status": "pending"}

    asyncio.create_task(_run_pipeline_task(run_id, transcript_path, meeting_date_str, language_str))

    return {
        "runId": run_id,
        "streamUrl": f"/runs/{run_id}/stream",
    }


# --- GET /runs/:runId/stream (SSE) ---

@router.get("/{run_id}/stream")
async def stream_run(run_id: str) -> StreamingResponse:
    """
    Real-time progress for the pipeline (extractor → normalizer → executor).
    Connect with Accept: text/event-stream.
    """
    run_state = _runs.get(run_id)
    if not run_state:
        raise HTTPException(status_code=404, detail="Run not found")

    queue: asyncio.Queue = run_state["queue"]

    async def event_generator():
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=300.0)
                except asyncio.TimeoutError:
                    yield _sse_message("progress", {"agent": None, "step": "waiting", "status": "running"})
                    await asyncio.sleep(0)
                    continue
                if item is None:
                    break
                event_type = item.get("event")
                data = item.get("data", {})
                yield _sse_message(event_type, data)
                await asyncio.sleep(0)
        finally:
            pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
