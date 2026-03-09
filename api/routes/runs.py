"""
Runs API: create pipeline runs (upload + details) and stream progress via SSE.

  POST /runs       — Create run (multipart: file, meetingDate, language), return runId + streamUrl.
  GET  /runs/{id}/stream — SSE stream for extractor → normalizer → executor progress.
"""
import asyncio
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.requests import Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import UserDetails, get_user_details
from api.db import async_session_factory, get_db
from api.models import RunRequestLog, RunResponseLog
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


async def _log_run_response(run_id: str, event_type: str, data: dict) -> None:
    """Persist a run response log for the given run_id."""
    async with async_session_factory() as session:
        try:
            result = await session.execute(
                select(RunRequestLog).where(RunRequestLog.run_id == run_id)
            )
            request_log = result.scalars().first()
            if not request_log:
                return
            summary: dict[str, Any] = data.get("summary") or {}
            status = "completed" if event_type == "run_complete" else data.get("status") or event_type
            response_log = RunResponseLog(
                request_id=request_log.id,
                status=status,
                actions_extracted=summary.get("actions_extracted"),
                actions_normalized=summary.get("actions_normalized"),
                actions_executed=summary.get("actions_executed"),
                response_data=data,
            )
            session.add(response_log)
            await session.commit()
        except Exception:
            await session.rollback()


async def _run_pipeline_task(run_id: str, transcript_path: str, meeting_date: str | None, language: str | None) -> None:
    """Run pipeline in thread and push events to the run's queue."""
    run_state = _runs.get(run_id)
    if not run_state:
        return
    queue: asyncio.Queue = run_state["queue"]
    loop = asyncio.get_event_loop()

    def put_event(event_type: str, data: dict) -> None:
        # Queue SSE event
        loop.call_soon_threadsafe(queue.put_nowait, {"event": event_type, "data": data})
        # Persist final summary or error when available
        if event_type in ("run_complete", "error"):
            def _schedule_log() -> None:
                asyncio.create_task(_log_run_response(run_id, event_type, data))
            loop.call_soon_threadsafe(_schedule_log)

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
    user_details: Annotated[UserDetails, Depends(get_user_details)],
    db: AsyncSession = Depends(get_db),
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
    original_filename: str | None = None
    stored_filename: str | None = None
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
        original_filename = ref
        stored_filename = Path(transcript_path).name
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
        original_filename = file.filename
        stored_filename = safe_name

    run_id = uuid.uuid4().hex
    queue: asyncio.Queue = asyncio.Queue()
    _runs[run_id] = {"queue": queue, "status": "pending"}

    # Persist run request log
    meeting_dt = None
    if meeting_date_str:
        try:
            # Support both date-only (YYYY-MM-DD) and full ISO datetime strings
            if "T" in meeting_date_str:
                meeting_dt = datetime.fromisoformat(meeting_date_str)
            else:
                meeting_dt = datetime.fromisoformat(meeting_date_str + "T00:00:00")
        except ValueError:
            meeting_dt = None

    request_log = RunRequestLog(
        user_id=user_details.user.id,
        user_auth0_sub=user_details.claims.get("sub"),
        run_id=run_id,
        meeting_date=meeting_dt,
        language=language_str,
        original_file_name=original_filename,
        stored_file_name=stored_filename,
    )
    db.add(request_log)
    await db.flush()

    asyncio.create_task(_run_pipeline_task(run_id, transcript_path, meeting_date_str, language_str))

    return {
        "runId": run_id,
        "streamUrl": f"/runs/{run_id}/stream",
    }


# --- GET /runs/:runId/stream (SSE) ---

@router.get("/{run_id}/stream")
async def stream_run(
    run_id: str,
    user_details: Annotated[UserDetails, Depends(get_user_details)],
) -> StreamingResponse:
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
