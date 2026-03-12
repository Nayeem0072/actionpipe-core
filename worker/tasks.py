"""Celery tasks for the three agent steps: extractor → normalizer → executor.

Each task follows the same lifecycle:
  1. Guard: load AgentRunTask from DB. If attempt_count >= max_attempts
     mark permanently_failed and stop (no more retries).
  2. Increment attempt_count, set status = running.
  3. Check token limits for the user.
  4. Apply per-user + per-agent rate limiting.
  5. Run the LangGraph graph with PostgresSaver checkpointer + token callback.
  6. On success: persist token usage, set status = completed, publish SSE
     events, chain to the next task.
  7. On a provider error (429 / 5xx): compute backoff+jitter, sleep, re-raise
     so Celery retries.
  8. On any other unexpected error: set status = failed, publish error SSE,
     re-raise so Celery can retry (or mark permanently_failed next attempt).

SSE events are published to Redis Pub/Sub channel ``run:{run_id}:events`` as
JSON-serialised dicts matching the existing event schema used by the SSE stream.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from celery import Task
from celery.exceptions import Ignore
from dotenv import load_dotenv

load_dotenv()

# Make project root importable inside worker processes
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from worker.celery_app import celery_app
from worker.checkpointer import build_checkpointer, make_thread_id
from worker.rate_limiter import RateLimitExceeded, backoff_jitter, get_rate_limiter
from worker.token_tracker import (
    TokenLimitExceeded,
    TokenTrackingCallback,
    check_token_limit,
    persist_token_usage,
)

logger = logging.getLogger(__name__)

CELERY_MAX_RETRIES = int(os.getenv("CELERY_MAX_RETRIES", "3"))
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Exceptions that indicate a transient provider error and should trigger
# exponential backoff + retry.
_PROVIDER_ERROR_NAMES = frozenset(
    {
        "RateLimitError",
        "APIStatusError",
        "InternalServerError",
        "ServiceUnavailableError",
        "APIConnectionError",
        "APITimeoutError",
        "TooManyRequestsError",
    }
)


def _is_provider_error(exc: BaseException) -> bool:
    return type(exc).__name__ in _PROVIDER_ERROR_NAMES or (
        hasattr(exc, "status_code") and getattr(exc, "status_code", 0) in (429, 500, 502, 503, 504)
    )


# ---------------------------------------------------------------------------
# Redis Pub/Sub helpers
# ---------------------------------------------------------------------------


def _get_redis_client():
    import redis
    return redis.from_url(REDIS_URL, decode_responses=True)


def _publish_event(run_id: str, event_type: str, data: dict) -> None:
    """Publish one SSE-shaped event to the run's Redis channel."""
    try:
        r = _get_redis_client()
        payload = json.dumps({"event": event_type, "data": data})
        r.publish(f"run:{run_id}:events", payload)
    except Exception as exc:
        logger.warning("Failed to publish SSE event for run %s: %s", run_id, exc)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _get_agent_task(db, run_id: str, agent_type: str):
    """Return the AgentRunTask row or None."""
    from sqlalchemy import select
    from api.models import AgentRunTask

    return db.execute(
        select(AgentRunTask).where(
            AgentRunTask.run_id == run_id,
            AgentRunTask.agent_type == agent_type,
        )
    ).scalars().first()


def _update_agent_task(db, row, **kwargs) -> None:
    for k, v in kwargs.items():
        setattr(row, k, v)
    db.commit()


def _persist_run_response(db, run_id: str, event_type: str, data: dict) -> None:
    """Persist the terminal run summary independently of SSE subscribers."""
    from sqlalchemy import select

    from api.models import RunRequestLog, RunResponseLog

    request_log = db.execute(
        select(RunRequestLog).where(RunRequestLog.run_id == run_id)
    ).scalars().first()
    if request_log is None:
        logger.warning("Could not persist run response; missing request log for run=%s", run_id)
        return

    summary: dict[str, Any] = data.get("summary") or {}
    status = "completed" if event_type == "run_complete" else data.get("status") or event_type
    response_log = db.execute(
        select(RunResponseLog)
        .where(RunResponseLog.request_id == request_log.id)
        .where(RunResponseLog.status == status)
        .order_by(RunResponseLog.created_at.desc())
    ).scalars().first()

    if response_log is None:
        response_log = RunResponseLog(
            request_id=request_log.id,
            status=status,
        )
        db.add(response_log)

    response_log.actions_extracted = summary.get("actions_extracted")
    response_log.actions_normalized = summary.get("actions_normalized")
    response_log.actions_executed = summary.get("actions_executed")
    response_log.response_data = data
    db.commit()


# ---------------------------------------------------------------------------
# Shared task pre/post logic
# ---------------------------------------------------------------------------


def _task_start(db, run_id: str, agent_type: str, celery_task_id: str) -> Any:
    """Guard + increment attempt_count.  Returns the AgentRunTask row.

    Raises Ignore (stops task silently) if max_attempts is reached.
    """
    from api.models import AgentRunTask

    row = _get_agent_task(db, run_id, agent_type)
    if row is None:
        # Row missing — create a minimal one so we can track this attempt
        thread_id = make_thread_id(run_id, agent_type)
        row = AgentRunTask(
            run_id=run_id,
            agent_type=agent_type,
            checkpoint_thread_id=thread_id,
            status="pending",
            attempt_count=0,
            max_attempts=CELERY_MAX_RETRIES,
        )
        db.add(row)
        db.commit()
        db.refresh(row)

    if row.status == "permanently_failed":
        logger.warning("Task %s/%s is permanently_failed — ignoring", run_id, agent_type)
        raise Ignore()

    if row.attempt_count >= row.max_attempts:
        _update_agent_task(db, row, status="permanently_failed", error_message="Max attempts reached")
        _publish_event(
            run_id,
            "error",
            {
                "agent": agent_type,
                "message": f"Agent {agent_type} permanently failed after {row.attempt_count} attempt(s).",
                "code": "max_attempts_reached",
            },
        )
        logger.error(
            "run=%s agent=%s permanently failed after %d attempt(s)",
            run_id,
            agent_type,
            row.attempt_count,
        )
        raise Ignore()

    _update_agent_task(
        db,
        row,
        attempt_count=row.attempt_count + 1,
        celery_task_id=celery_task_id,
        status="running",
        error_message=None,
    )
    # row.attempt_count is now the incremented value (setattr updated it in-memory)
    logger.info(
        "run=%s agent=%s attempt %d/%d started",
        run_id,
        agent_type,
        row.attempt_count,
        row.max_attempts,
    )
    return row


def _task_success(db, row, callback: TokenTrackingCallback, user_id: str | None) -> None:
    _update_agent_task(db, row, status="completed")
    try:
        persist_token_usage(db, callback, user_id)
    except Exception as exc:
        logger.warning("Failed to persist token usage: %s", exc)


def _task_failure(db, row, exc: BaseException) -> None:
    _update_agent_task(db, row, status="failed", error_message=str(exc)[:2000])


# ---------------------------------------------------------------------------
# Extractor task
# ---------------------------------------------------------------------------


@celery_app.task(
    bind=True,
    name="worker.tasks.run_extractor_task",
    max_retries=CELERY_MAX_RETRIES,
    queue="extractor",
)
def run_extractor_task(
    self: Task,
    run_id: str,
    user_id: str | None,
    transcript_path: str,
    meeting_date: str | None = None,
    language: str | None = None,
    dry_run: bool = True,
) -> dict:
    """Extract action items from the transcript.

    On success publishes SSE events and chains to run_normalizer_task.
    """
    from api.db import get_sync_db

    agent_type = "extractor"
    thread_id = make_thread_id(run_id, agent_type)

    _publish_event(run_id, "progress", {"agent": agent_type, "step": "load_transcript", "status": "running"})

    with get_sync_db() as db:
        row = _task_start(db, run_id, agent_type, self.request.id or "")

        # Token limit check (before we burn any tokens)
        try:
            if user_id:
                check_token_limit(user_id, agent_type, db)
        except TokenLimitExceeded as exc:
            _task_failure(db, row, exc)
            _publish_event(run_id, "error", {"agent": agent_type, "message": str(exc), "code": "token_limit_exceeded"})
            raise Ignore() from exc

    # Rate limiting (outside DB session — may sleep)
    try:
        provider = os.getenv("ACTIVE_PROVIDER", "unknown")
        if user_id:
            get_rate_limiter().check_all(user_id, agent_type, provider)
    except RateLimitExceeded as exc:
        logger.warning("Rate limit hit for run=%s: %s", run_id, exc)

    # --- Run the extractor graph ---
    try:
        from src.action_extractor.main import load_transcript
        from src.action_extractor.workflow import extract_actions_with_progress_checkpointed

        _publish_event(run_id, "step_done", {"agent": agent_type, "step": "load_transcript"})
        transcript = load_transcript(transcript_path)

        callback = TokenTrackingCallback(run_id=run_id, agent_type=agent_type, provider=provider)

        def emit(event_type: str, data: dict) -> None:
            _publish_event(run_id, event_type, data)

        with build_checkpointer() as checkpointer:
            actions = extract_actions_with_progress_checkpointed(
                transcript,
                emit,
                checkpointer=checkpointer,
                thread_id=thread_id,
                callbacks=[callback],
            )

    except Exception as exc:
        attempt = self.request.retries + 1
        with get_sync_db() as db:
            row = _get_agent_task(db, run_id, agent_type)
            if row:
                _task_failure(db, row, exc)

        if _is_provider_error(exc):
            delay = backoff_jitter(attempt)
            logger.warning(
                "Provider error on run=%s agent=%s (attempt %d) — retry in %.1fs: %s",
                run_id, agent_type, attempt, delay, exc,
            )
            raise self.retry(exc=exc, countdown=delay)

        _publish_event(run_id, "error", {"agent": agent_type, "message": str(exc), "step": "extraction"})
        raise self.retry(exc=exc, countdown=backoff_jitter(attempt))

    # Success
    with get_sync_db() as db:
        row = _get_agent_task(db, run_id, agent_type)
        if row:
            _task_success(db, row, callback, user_id)

    _publish_event(run_id, "agent_done", {"agent": agent_type})

    # Chain to normalizer
    run_normalizer_task.apply_async(
        args=[run_id, user_id, actions, len(actions), meeting_date, language, dry_run],
        queue="normalizer",
    )
    return {"run_id": run_id, "actions_count": len(actions)}


# ---------------------------------------------------------------------------
# Normalizer task
# ---------------------------------------------------------------------------


@celery_app.task(
    bind=True,
    name="worker.tasks.run_normalizer_task",
    max_retries=CELERY_MAX_RETRIES,
    queue="normalizer",
)
def run_normalizer_task(
    self: Task,
    run_id: str,
    user_id: str | None,
    actions: list,
    extracted_count: int = 0,
    meeting_date: str | None = None,
    language: str | None = None,
    dry_run: bool = True,
) -> dict:
    """Normalise extracted actions (deadlines, verbs, dedup, tool classification).

    On success chains to run_executor_task.
    """
    from api.db import get_sync_db

    agent_type = "normalizer"
    thread_id = make_thread_id(run_id, agent_type)

    _publish_event(run_id, "progress", {"agent": agent_type, "step": "deadline_normalizer", "status": "running"})

    with get_sync_db() as db:
        row = _task_start(db, run_id, agent_type, self.request.id or "")

        try:
            if user_id:
                check_token_limit(user_id, agent_type, db)
        except TokenLimitExceeded as exc:
            _task_failure(db, row, exc)
            _publish_event(run_id, "error", {"agent": agent_type, "message": str(exc), "code": "token_limit_exceeded"})
            raise Ignore() from exc

    try:
        provider = os.getenv("ACTIVE_PROVIDER", "unknown")
        if user_id:
            get_rate_limiter().check_all(user_id, agent_type, provider)
    except RateLimitExceeded as exc:
        logger.warning("Rate limit hit for run=%s: %s", run_id, exc)

    try:
        from src.action_normalizer.workflow import normalize_actions_with_progress_checkpointed

        callback = TokenTrackingCallback(run_id=run_id, agent_type=agent_type, provider=provider)

        def emit(event_type: str, data: dict) -> None:
            _publish_event(run_id, event_type, data)

        with build_checkpointer() as checkpointer:
            normalized = normalize_actions_with_progress_checkpointed(
                actions,
                emit,
                meeting_date=meeting_date,
                checkpointer=checkpointer,
                thread_id=thread_id,
                callbacks=[callback],
            )

    except Exception as exc:
        attempt = self.request.retries + 1
        with get_sync_db() as db:
            row = _get_agent_task(db, run_id, agent_type)
            if row:
                _task_failure(db, row, exc)

        if _is_provider_error(exc):
            delay = backoff_jitter(attempt)
            logger.warning(
                "Provider error on run=%s agent=%s (attempt %d) — retry in %.1fs: %s",
                run_id, agent_type, attempt, delay, exc,
            )
            raise self.retry(exc=exc, countdown=delay)

        _publish_event(run_id, "error", {"agent": agent_type, "message": str(exc), "step": "normalize"})
        raise self.retry(exc=exc, countdown=backoff_jitter(attempt))

    with get_sync_db() as db:
        row = _get_agent_task(db, run_id, agent_type)
        if row:
            _task_success(db, row, callback, user_id)

    _publish_event(run_id, "agent_done", {"agent": agent_type})

    run_executor_task.apply_async(
        args=[run_id, user_id, normalized, extracted_count, len(normalized), dry_run],
        queue="executor",
    )
    return {"run_id": run_id, "normalized_count": len(normalized)}


# ---------------------------------------------------------------------------
# Executor task
# ---------------------------------------------------------------------------


@celery_app.task(
    bind=True,
    name="worker.tasks.run_executor_task",
    max_retries=CELERY_MAX_RETRIES,
    queue="executor",
)
def run_executor_task(
    self: Task,
    run_id: str,
    user_id: str | None,
    normalized: list,
    extracted_count: int = 0,
    normalized_count: int = 0,
    dry_run: bool = True,
) -> dict:
    """Dispatch normalised actions to MCP tools (dry-run or live).

    On success publishes the final run_complete SSE event.
    """
    from api.db import get_sync_db

    agent_type = "executor"
    thread_id = make_thread_id(run_id, agent_type)

    _publish_event(run_id, "progress", {"agent": agent_type, "step": "contact_resolver", "status": "running"})

    with get_sync_db() as db:
        row = _task_start(db, run_id, agent_type, self.request.id or "")

        try:
            if user_id:
                check_token_limit(user_id, agent_type, db)
        except TokenLimitExceeded as exc:
            _task_failure(db, row, exc)
            _publish_event(run_id, "error", {"agent": agent_type, "message": str(exc), "code": "token_limit_exceeded"})
            raise Ignore() from exc

    try:
        provider = os.getenv("ACTIVE_PROVIDER", "unknown")
        if user_id:
            get_rate_limiter().check_all(user_id, agent_type, provider)
    except RateLimitExceeded as exc:
        logger.warning("Rate limit hit for run=%s: %s", run_id, exc)

    try:
        from src.action_executor.workflow import execute_actions_with_progress_checkpointed

        callback = TokenTrackingCallback(run_id=run_id, agent_type=agent_type, provider=provider)

        def emit(event_type: str, data: dict) -> None:
            _publish_event(run_id, event_type, data)

        with build_checkpointer() as checkpointer:
            results = execute_actions_with_progress_checkpointed(
                normalized,
                emit,
                dry_run=dry_run,
                contacts_path=None,
                checkpointer=checkpointer,
                thread_id=thread_id,
                callbacks=[callback],
            )

    except Exception as exc:
        attempt = self.request.retries + 1
        with get_sync_db() as db:
            row = _get_agent_task(db, run_id, agent_type)
            if row:
                _task_failure(db, row, exc)

        if _is_provider_error(exc):
            delay = backoff_jitter(attempt)
            logger.warning(
                "Provider error on run=%s agent=%s (attempt %d) — retry in %.1fs: %s",
                run_id, agent_type, attempt, delay, exc,
            )
            raise self.retry(exc=exc, countdown=delay)

        _publish_event(run_id, "error", {"agent": agent_type, "message": str(exc), "step": "execute"})
        raise self.retry(exc=exc, countdown=backoff_jitter(attempt))

    with get_sync_db() as db:
        row = _get_agent_task(db, run_id, agent_type)
        if row:
            _task_success(db, row, callback, user_id)

    _publish_event(run_id, "agent_done", {"agent": agent_type})
    run_complete_payload = {
        "summary": {
            "actions_extracted": extracted_count,
            "actions_normalized": normalized_count or len(normalized),
            "actions_executed": len(results),
        },
        "executor_actions": results,
    }
    _publish_event(
        run_id,
        "run_complete",
        run_complete_payload,
    )
    with get_sync_db() as db:
        _persist_run_response(db, run_id, "run_complete", run_complete_payload)
    # Signal end of stream
    _publish_event(run_id, "__stream_end__", {})

    return {"run_id": run_id, "executed_count": len(results)}
