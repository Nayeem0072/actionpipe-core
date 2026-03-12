"""
Runs API: create pipeline runs (upload + details) and stream progress via SSE.

  POST /runs       — Create run (multipart: file, meetingDate, language), return runId + streamUrl.
  GET  /runs/{id}/stream — SSE stream for extractor → normalizer → executor progress.

Pipeline execution is offloaded to Celery workers. Progress events are published
to a Redis Pub/Sub channel (``run:{run_id}:events``) and forwarded to SSE clients.
"""
import asyncio
import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, AsyncGenerator

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.requests import Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import UserDetails, get_user_details
from api.db import async_session_factory, get_db
from api.models import AgentRunTask, RunRequestLog, RunResponseLog

MAX_FILE_SIZE_BYTES = 15 * 1024 * 1024  # 15 MB
ALLOWED_EXTENSIONS = {".csv", ".txt", ".doc", ".pdf"}
UPLOAD_DIR = Path(__file__).resolve().parent.parent / "uploads"

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CELERY_MAX_RETRIES = int(os.getenv("CELERY_MAX_RETRIES", "3"))

router = APIRouter(prefix="/runs", tags=["runs"])


def _ensure_upload_dir() -> Path:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    return UPLOAD_DIR


def _sse_message(event_type: str | None, data: dict) -> str:
    """Format one SSE frame."""
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


async def _create_agent_run_tasks(db: AsyncSession, run_id: str, user_id: uuid.UUID | None) -> None:
    """Pre-create AgentRunTask rows for all three agent steps in pending state."""
    from api.models import AgentRunTask

    for agent_type in ("extractor", "normalizer", "executor"):
        task = AgentRunTask(
            run_id=run_id,
            user_id=user_id,
            agent_type=agent_type,
            checkpoint_thread_id=f"{run_id}:{agent_type}",
            status="pending",
            attempt_count=0,
            max_attempts=CELERY_MAX_RETRIES,
        )
        db.add(task)
    await db.flush()


# ---------------------------------------------------------------------------
# POST /runs
# ---------------------------------------------------------------------------


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
    start processing via Celery workers, and return an id to subscribe to for SSE.

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

    # Persist run request log
    meeting_dt = None
    if meeting_date_str:
        try:
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

    # Pre-create the three AgentRunTask tracking rows
    await _create_agent_run_tasks(db, run_id, user_details.user.id)
    await db.commit()

    # Dispatch the extractor Celery task — it will chain normalizer → executor on success
    from worker.tasks import run_extractor_task

    run_extractor_task.apply_async(
        args=[
            run_id,
            str(user_details.user.id),
            transcript_path,
            meeting_date_str,
            language_str,
            True,  # dry_run
        ],
        queue="extractor",
    )

    return {
        "runId": run_id,
        "streamUrl": f"/runs/{run_id}/stream",
    }


# ---------------------------------------------------------------------------
# GET /runs/:runId/stream  (SSE via Redis Pub/Sub)
# ---------------------------------------------------------------------------


@router.get("/{run_id}/stream")
async def stream_run(
    run_id: str,
    user_details: Annotated[UserDetails, Depends(get_user_details)],
) -> StreamingResponse:
    """
    Real-time progress for the pipeline (extractor → normalizer → executor).
    Connect with Accept: text/event-stream.

    Events are published to Redis Pub/Sub by the Celery workers and forwarded
    here as SSE frames.  The stream closes when a ``run_complete`` or ``error``
    event arrives, or after a 5-minute timeout.
    """

    async def event_generator() -> AsyncGenerator[str, None]:
        redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
        pubsub = redis_client.pubsub()
        channel = f"run:{run_id}:events"
        await pubsub.subscribe(channel)

        try:
            deadline = asyncio.get_event_loop().time() + 300  # 5-minute overall timeout

            async for raw_message in pubsub.listen():
                if asyncio.get_event_loop().time() > deadline:
                    yield _sse_message("progress", {"agent": None, "step": "timeout", "status": "error"})
                    break

                if raw_message["type"] != "message":
                    continue

                try:
                    parsed = json.loads(raw_message["data"])
                except (json.JSONDecodeError, TypeError):
                    continue

                event_type: str = parsed.get("event", "")
                data: dict = parsed.get("data", {})

                # Internal signal to close the stream — not forwarded to the client
                if event_type == "__stream_end__":
                    break

                yield _sse_message(event_type, data)

                # Persist final summary / error to the DB
                if event_type in ("run_complete", "error"):
                    asyncio.create_task(_log_run_response(run_id, event_type, data))
                    # Give the client the final frame then close
                    if event_type == "run_complete":
                        break

                await asyncio.sleep(0)

        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.close()
            await redis_client.aclose()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
