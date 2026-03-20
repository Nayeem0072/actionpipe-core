"""
Runs API: create pipeline runs (upload + details) and stream progress via SSE.

  POST /runs       — Create run (multipart: file, meetingDate, language), return runId + streamUrl.
  GET  /runs/{id}/stream — SSE stream for extractor → normalizer → executor progress.
  POST /runs/{id}/actions/execute      — Execute selected Slack actions from a completed run.
  POST /runs/{id}/jira_actions/execute — Execute selected Jira actions from a completed run.
"""
import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, AsyncGenerator, Optional

import httpx

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.requests import Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import UserDetails, get_user_details
from api.db import get_db
from api.models import AgentRunTask, RunRequestLog, RunResponseLog, UserToken

logger = logging.getLogger(__name__)

MAX_FILE_SIZE_BYTES = 15 * 1024 * 1024  # 15 MB
ALLOWED_EXTENSIONS = {".csv", ".txt", ".doc", ".pdf"}
UPLOAD_DIR = Path(__file__).resolve().parent.parent / "uploads"

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CELERY_MAX_RETRIES = int(os.getenv("CELERY_MAX_RETRIES", "3"))
# Per-user rate limit for executing Slack actions (per minute)
SLACK_EXECUTE_LIMIT_PER_MINUTE = int(os.getenv("SLACK_EXECUTE_LIMIT_PER_MINUTE", "10"))

router = APIRouter(prefix="/runs", tags=["runs"])


async def _check_slack_execute_rate_limit(user_id: str, count: int = 1) -> None:
    """
    Sliding-window rate limit for Slack action execution per user.
    Raises HTTPException 429 if the user would exceed SLACK_EXECUTE_LIMIT_PER_MINUTE in 60s.
    """
    if SLACK_EXECUTE_LIMIT_PER_MINUTE <= 0:
        return
    key = f"ratelimit:slack_execute:{user_id}"
    window = 60
    now = asyncio.get_event_loop().time()
    window_start = now - window
    try:
        redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
        try:
            await redis_client.zremrangebyscore(key, "-inf", window_start)
            current = await redis_client.zcard(key)
            if current + count > SLACK_EXECUTE_LIMIT_PER_MINUTE:
                await redis_client.aclose()
                raise HTTPException(
                    status_code=429,
                    detail=f"Rate limit exceeded: max {SLACK_EXECUTE_LIMIT_PER_MINUTE} Slack executions per minute",
                )
            # Record this request (one member per action executed)
            for i in range(count):
                await redis_client.zadd(key, {f"{now}:{i}:{uuid.uuid4().hex}": now})
            await redis_client.expire(key, window + 10)
        finally:
            await redis_client.aclose()
    except HTTPException:
        raise
    except Exception:
        # If Redis is down, allow the request (fail open for availability)
        pass


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

                if event_type in ("run_complete", "error"):
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


# ---------------------------------------------------------------------------
# POST /runs/{run_id}/actions/execute — Execute selected Slack actions
# ---------------------------------------------------------------------------


class ExecuteActionsBody(BaseModel):
    """Request body for executing selected actions from a run."""

    actionIds: list[str] = Field(..., min_length=1, description="Action ids from executor_actions to execute (Slack only)")


@router.post("/{run_id}/actions/execute")
async def execute_run_actions(
    run_id: str,
    body: ExecuteActionsBody,
    user_details: Annotated[UserDetails, Depends(get_user_details)],
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Execute selected Slack actions from a completed run.

    Loads the run's stored executor_actions, filters to Slack actions whose id
    is in actionIds, then invokes the Slack MCP server for each (with sandbox
    and optional user Slack token). Returns per-action results.
    """
    # 1. Resolve run_id -> RunRequestLog, ensure user owns the run
    request_result = await db.execute(
        select(RunRequestLog).where(
            RunRequestLog.run_id == run_id,
            RunRequestLog.user_id == user_details.user.id,
        )
    )
    request_log = request_result.scalars().first()
    if not request_log:
        raise HTTPException(status_code=404, detail="Run not found or access denied")

    # 2. Load latest completed run response
    response_result = await db.execute(
        select(RunResponseLog)
        .where(
            RunResponseLog.request_id == request_log.id,
            RunResponseLog.status == "completed",
        )
        .order_by(RunResponseLog.created_at.desc())
        .limit(1)
    )
    response_log = response_result.scalars().first()
    if not response_log or not response_log.response_data:
        raise HTTPException(
            status_code=404,
            detail="Run has no completed response yet; wait for the pipeline to finish",
        )

    executor_actions = response_log.response_data.get("executor_actions") or []
    if not isinstance(executor_actions, list):
        raise HTTPException(status_code=500, detail="Invalid run response data")

    action_ids_set = set(body.actionIds)
    # 3. Filter to requested actions that are Slack
    slack_actions = [
        a for a in executor_actions
        if isinstance(a, dict)
        and a.get("id") in action_ids_set
        and (a.get("server") == "slack" or a.get("tool_type") == "send_notification")
    ]
    found_ids = {a["id"] for a in slack_actions}
    missing = action_ids_set - found_ids
    if missing:
        # Check if they exist but are not Slack
        all_ids = {a.get("id") for a in executor_actions if isinstance(a, dict) and a.get("id")}
        not_found = missing - all_ids
        if not_found:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown action id(s): {sorted(not_found)}",
            )
        non_slack = missing & all_ids
        raise HTTPException(
            status_code=400,
            detail=f"Only Slack actions can be executed. Non-Slack action id(s): {sorted(non_slack)}",
        )

    # Rate limit: max N Slack executions per minute per user
    await _check_slack_execute_rate_limit(str(user_details.user.id), count=len(slack_actions))

    # 4. Build action list for MCPDispatcher (id, tool_type, tool_params)
    actions_for_dispatch = [
        {
            "id": a["id"],
            "tool_type": a.get("tool_type", "send_notification"),
            "tool_params": a.get("params", {}),
        }
        for a in slack_actions
    ]

    # Use the user's Slack token from user_tokens for the MCP server
    server_env_overrides: dict[str, dict[str, str]] = {}
    token_result = await db.execute(
        select(UserToken).where(
            UserToken.user_id == user_details.user.id,
            UserToken.service == "slack",
        )
    )
    slack_token_row = token_result.scalars().first()
    if not slack_token_row or not slack_token_row.access_token:
        raise HTTPException(
            status_code=403,
            detail="Slack is not connected. Connect your Slack workspace first (e.g. via /slack/connect).",
        )
    server_env_overrides["slack"] = {
        "SLACK_BOT_TOKEN": slack_token_row.access_token,
    }
    meta = slack_token_row.meta or {}
    if meta.get("team_id"):
        server_env_overrides["slack"]["SLACK_TEAM_ID"] = meta["team_id"]

    # 5. Dispatch via MCP (sandbox is applied inside MCPDispatcher)
    from src.action_executor.mcp_clients import MCPDispatcher

    dispatcher = MCPDispatcher(
        dry_run=False,
        server_env_overrides=server_env_overrides if server_env_overrides else None,
    )
    results = await dispatcher.dispatch_all(actions_for_dispatch)

    return {"executor_actions": results}


# ---------------------------------------------------------------------------
# Jira token refresh helper
# ---------------------------------------------------------------------------

_ATLASSIAN_TOKEN_URL = "https://auth.atlassian.com/oauth/token"
# Refresh if the token expires within this window
_JIRA_REFRESH_WINDOW_SECONDS = 300


async def _refresh_jira_token_if_needed(
    token_row: UserToken, db: AsyncSession
) -> Optional[str]:
    """
    Refresh the user's Jira OAuth access token if it is expired or about to expire
    within the next _JIRA_REFRESH_WINDOW_SECONDS seconds.

    Updates token_row.access_token and token_row.expires_at in-place and commits
    the change to the DB.

    Returns the (possibly refreshed) access token, or None if the refresh failed
    and the existing token may still be valid.
    """
    if not token_row.refresh_token:
        logger.warning("Jira token row has no refresh_token; using existing access token as-is")
        return token_row.access_token

    now = datetime.now(timezone.utc)
    expires_at = token_row.expires_at
    if expires_at and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if expires_at and (expires_at - now).total_seconds() > _JIRA_REFRESH_WINDOW_SECONDS:
        return token_row.access_token

    client_id = os.environ.get("JIRA_CLIENT_ID", "")
    client_secret = os.environ.get("JIRA_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        logger.warning("JIRA_CLIENT_ID / JIRA_CLIENT_SECRET not set; skipping token refresh")
        return token_row.access_token

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                _ATLASSIAN_TOKEN_URL,
                json={
                    "grant_type": "refresh_token",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": token_row.refresh_token,
                },
            )

        if resp.status_code != 200:
            logger.error(
                "Jira token refresh failed (HTTP %s): %s",
                resp.status_code,
                resp.text[:300],
            )
            return token_row.access_token

        data = resp.json()
        if "error" in data:
            logger.error("Jira token refresh error: %s — %s", data["error"], data.get("error_description"))
            return token_row.access_token

        new_access_token: str = data["access_token"]
        expires_in: int = data.get("expires_in", 3600)
        new_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

        token_row.access_token = new_access_token
        token_row.expires_at = new_expires_at
        if data.get("refresh_token"):
            token_row.refresh_token = data["refresh_token"]

        await db.commit()
        logger.info("Refreshed Jira access token for user %s", token_row.user_id)
        return new_access_token

    except Exception as exc:  # noqa: BLE001
        logger.warning("Jira token refresh exception: %s; using existing token", exc)
        return token_row.access_token


# ---------------------------------------------------------------------------
# POST /runs/{run_id}/jira_actions/execute
# ---------------------------------------------------------------------------


class ExecuteJiraActionsBody(BaseModel):
    """Request body for executing selected Jira actions from a run."""

    actionIds: list[str] = Field(
        ..., min_length=1, description="Action IDs from executor_actions to execute (Jira only)"
    )
    projectKey: Optional[str] = Field(
        default=None,
        description=(
            "Jira project key to create issues in (e.g. 'ENG', 'PROJ'). "
            "Takes precedence over the project_key stored in your Jira token settings. "
            "Required if no default project key has been saved for your account."
        ),
    )


@router.post("/{run_id}/jira_actions/execute")
async def execute_jira_run_actions(
    run_id: str,
    body: ExecuteJiraActionsBody,
    user_details: Annotated[UserDetails, Depends(get_user_details)],
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Execute selected Jira actions from a completed run.

    Loads the run's stored executor_actions, filters to Jira (create_jira_task)
    actions whose id is in actionIds, then invokes the Jira MCP server for each
    using the user's stored OAuth access token. Returns per-action results.

    The user must have connected their Jira account via /jira/connect before calling
    this endpoint.
    """
    # 1. Resolve run_id -> RunRequestLog, ensure user owns the run
    request_result = await db.execute(
        select(RunRequestLog).where(
            RunRequestLog.run_id == run_id,
            RunRequestLog.user_id == user_details.user.id,
        )
    )
    request_log = request_result.scalars().first()
    if not request_log:
        raise HTTPException(status_code=404, detail="Run not found or access denied")

    # 2. Load latest completed run response
    response_result = await db.execute(
        select(RunResponseLog)
        .where(
            RunResponseLog.request_id == request_log.id,
            RunResponseLog.status == "completed",
        )
        .order_by(RunResponseLog.created_at.desc())
        .limit(1)
    )
    response_log = response_result.scalars().first()
    if not response_log or not response_log.response_data:
        raise HTTPException(
            status_code=404,
            detail="Run has no completed response yet; wait for the pipeline to finish",
        )

    executor_actions = response_log.response_data.get("executor_actions") or []
    if not isinstance(executor_actions, list):
        raise HTTPException(status_code=500, detail="Invalid run response data")

    # 3. Filter to requested Jira actions
    action_ids_set = set(body.actionIds)
    jira_actions = [
        a for a in executor_actions
        if isinstance(a, dict)
        and a.get("id") in action_ids_set
        and (a.get("server") == "jira" or a.get("tool_type") == "create_jira_task")
    ]
    found_ids = {a["id"] for a in jira_actions}
    missing = action_ids_set - found_ids
    if missing:
        all_ids = {a.get("id") for a in executor_actions if isinstance(a, dict) and a.get("id")}
        not_found = missing - all_ids
        if not_found:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown action id(s): {sorted(not_found)}",
            )
        non_jira = missing & all_ids
        raise HTTPException(
            status_code=400,
            detail=f"Only Jira actions can be executed here. Non-Jira action id(s): {sorted(non_jira)}",
        )

    # 4. Fetch and (if needed) refresh the user's Jira token
    token_result = await db.execute(
        select(UserToken).where(
            UserToken.user_id == user_details.user.id,
            UserToken.service == "jira",
        )
    )
    jira_token_row = token_result.scalars().first()
    if not jira_token_row or not jira_token_row.access_token:
        raise HTTPException(
            status_code=403,
            detail="Jira is not connected. Connect your Atlassian account first via /jira/connect.",
        )

    access_token = await _refresh_jira_token_if_needed(jira_token_row, db)
    if not access_token:
        raise HTTPException(
            status_code=403,
            detail="Could not obtain a valid Jira access token. Please reconnect via /jira/connect.",
        )

    meta = jira_token_row.meta or {}
    cloud_id = meta.get("cloud_id", "")
    if not cloud_id:
        raise HTTPException(
            status_code=422,
            detail="Jira cloud_id is missing from your token metadata. Please reconnect via /jira/connect.",
        )

    # Resolve project_key: request body > user_tokens.meta > error
    project_key = (
        (body.projectKey or "").strip().upper()
        or (meta.get("project_key") or "").strip().upper()
    )
    if not project_key:
        raise HTTPException(
            status_code=422,
            detail=(
                "A Jira project key is required. Supply it as 'projectKey' in the request body "
                "or save a default via PATCH /jira/settings."
            ),
        )

    # 5. Build action list for MCPDispatcher, injecting project_key into each action's tool_params
    actions_for_dispatch = [
        {
            "id": a["id"],
            "tool_type": a.get("tool_type", "create_jira_task"),
            "tool_params": {**a.get("params", {}), "project_key": project_key},
        }
        for a in jira_actions
    ]

    # 6. Inject credentials via server_env_overrides
    server_env_overrides: dict[str, dict[str, str]] = {
        "jira": {
            "JIRA_ACCESS_TOKEN": access_token,
            "JIRA_CLOUD_ID": cloud_id,
        }
    }

    # 7. Dispatch via MCP (param validation and sandboxing applied inside MCPDispatcher)
    from src.action_executor.mcp_clients import MCPDispatcher

    dispatcher = MCPDispatcher(
        dry_run=False,
        server_env_overrides=server_env_overrides,
    )
    results = await dispatcher.dispatch_all(actions_for_dispatch)

    return {"executor_actions": results}
