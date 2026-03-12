"""Authenticated dashboard summary endpoints."""
from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_user
from api.db import get_db
from api.models import AgentRunTask, RunRequestLog, RunResponseLog, TokenLimit, TokenUsage, User, UserToken

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

_DEFAULT_MONTHLY_LIMIT = int(os.getenv("TOKEN_LIMIT_MONTHLY_DEFAULT", "0"))
_MCP_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "mcp_config.json"
_KNOWN_SERVICES = ("slack", "notion", "jira", "gmail", "calendar", "general_task")
_KNOWN_AGENT_TYPES = ("extractor", "normalizer", "executor")
_KNOWN_AGENT_STATUSES = ("pending", "running", "completed", "failed", "permanently_failed")


def _load_tool_type_to_server_map() -> dict[str, str | None]:
    try:
        with _MCP_CONFIG_PATH.open("r", encoding="utf-8") as handle:
            config = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    mapping = config.get("toolTypeToServer")
    return mapping if isinstance(mapping, dict) else {}


def _month_start() -> datetime:
    return datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def _get_effective_monthly_limit(db: AsyncSession, user_id) -> int:
    result = await db.execute(
        select(TokenLimit)
        .where(TokenLimit.period == "monthly")
        .where(TokenLimit.agent_type.is_(None))
        .where((TokenLimit.user_id == user_id) | (TokenLimit.user_id.is_(None)))
        .order_by(TokenLimit.user_id.is_(None), TokenLimit.updated_at.desc())
    )
    limits = result.scalars().all()
    if not limits:
        return _DEFAULT_MONTHLY_LIMIT

    return int((limits[0].max_tokens or 0))


@router.get("/summary")
async def get_dashboard_summary(
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    month_start = _month_start()
    tool_type_to_server = _load_tool_type_to_server_map()

    token_totals_result = await db.execute(
        select(
            func.coalesce(func.sum(TokenUsage.prompt_tokens), 0),
            func.coalesce(func.sum(TokenUsage.completion_tokens), 0),
            func.coalesce(func.sum(TokenUsage.total_tokens), 0),
        ).where(TokenUsage.user_id == current_user.id)
    )
    prompt_total, completion_total, used_total = token_totals_result.one()

    token_month_result = await db.execute(
        select(func.coalesce(func.sum(TokenUsage.total_tokens), 0))
        .where(TokenUsage.user_id == current_user.id)
        .where(TokenUsage.created_at >= month_start)
    )
    used_this_month = int(token_month_result.scalar() or 0)

    token_by_agent_result = await db.execute(
        select(
            TokenUsage.agent_type,
            func.coalesce(func.sum(TokenUsage.total_tokens), 0),
        )
        .where(TokenUsage.user_id == current_user.id)
        .group_by(TokenUsage.agent_type)
    )
    tokens_by_agent = {agent_type: int(total) for agent_type, total in token_by_agent_result.all()}
    for agent_type in _KNOWN_AGENT_TYPES:
        tokens_by_agent.setdefault(agent_type, 0)

    allocated_this_month = await _get_effective_monthly_limit(db, current_user.id)
    remaining_this_month = None if allocated_this_month == 0 else max(allocated_this_month - used_this_month, 0)

    requested_result = await db.execute(
        select(func.count(RunRequestLog.id)).where(RunRequestLog.user_id == current_user.id)
    )
    requested_runs = int(requested_result.scalar() or 0)

    task_rows_result = await db.execute(
        select(AgentRunTask.run_id, AgentRunTask.agent_type, AgentRunTask.status)
        .where(AgentRunTask.user_id == current_user.id)
    )
    task_rows = task_rows_result.all()

    agent_stage_counts = {
        agent_type: {status: 0 for status in _KNOWN_AGENT_STATUSES}
        for agent_type in _KNOWN_AGENT_TYPES
    }
    statuses_by_run: dict[str, list[str]] = defaultdict(list)
    for run_id, agent_type, status in task_rows:
        if agent_type not in agent_stage_counts:
            agent_stage_counts[agent_type] = {known_status: 0 for known_status in _KNOWN_AGENT_STATUSES}
        if status not in agent_stage_counts[agent_type]:
            agent_stage_counts[agent_type][status] = 0
        agent_stage_counts[agent_type][status] += 1
        statuses_by_run[run_id].append(status)

    completed_runs = 0
    failed_runs = 0
    in_progress_runs = 0
    for statuses in statuses_by_run.values():
        if any(status in {"failed", "permanently_failed"} for status in statuses):
            failed_runs += 1
        elif statuses and len(statuses) >= 3 and all(status == "completed" for status in statuses):
            completed_runs += 1
        else:
            in_progress_runs += 1

    response_rows_result = await db.execute(
        select(
            RunResponseLog.request_id,
            RunResponseLog.actions_extracted,
            RunResponseLog.actions_normalized,
            RunResponseLog.actions_executed,
            RunResponseLog.response_data,
            RunResponseLog.created_at,
        )
        .join(RunRequestLog, RunResponseLog.request_id == RunRequestLog.id)
        .where(RunRequestLog.user_id == current_user.id)
        .where(RunResponseLog.status == "completed")
        .order_by(RunResponseLog.request_id, RunResponseLog.created_at.desc())
    )

    latest_completed_by_request: dict[Any, tuple[int | None, int | None, int | None, dict[str, Any] | None]] = {}
    for request_id, actions_extracted, actions_normalized, actions_executed, response_data, _created_at in response_rows_result.all():
        if request_id in latest_completed_by_request:
            continue
        latest_completed_by_request[request_id] = (
            actions_extracted,
            actions_normalized,
            actions_executed,
            response_data,
        )

    action_totals = {
        "extracted": 0,
        "normalized": 0,
        "executed": 0,
    }
    integrations_found = {service: 0 for service in _KNOWN_SERVICES}

    for actions_extracted, actions_normalized, actions_executed, response_data in latest_completed_by_request.values():
        action_totals["extracted"] += int(actions_extracted or 0)
        action_totals["normalized"] += int(actions_normalized or 0)
        action_totals["executed"] += int(actions_executed or 0)

        payload = response_data or {}
        executor_actions = payload.get("executor_actions") or []
        if not isinstance(executor_actions, list):
            continue

        for action in executor_actions:
            if not isinstance(action, dict):
                continue
            server = action.get("server")
            if not server:
                tool_type = action.get("tool_type")
                server = tool_type_to_server.get(tool_type) if tool_type else None
                if server is None:
                    server = tool_type or "general_task"
            if server not in integrations_found:
                integrations_found[server] = 0
            integrations_found[server] += 1

    connected_rows_result = await db.execute(
        select(UserToken.service, func.count(UserToken.id))
        .where(UserToken.user_id == current_user.id)
        .group_by(UserToken.service)
    )
    integrations_connected = {service: 0 for service in ("slack", "notion", "jira")}
    for service, count in connected_rows_result.all():
        integrations_connected[service] = int(count)

    return {
        "tokens": {
            "used_total": int(used_total or 0),
            "prompt_total": int(prompt_total or 0),
            "completion_total": int(completion_total or 0),
            "used_this_month": used_this_month,
            "allocated_this_month": int(allocated_this_month),
            "remaining_this_month": remaining_this_month,
            "is_unlimited": allocated_this_month == 0,
            "by_agent": tokens_by_agent,
        },
        "runs": {
            "requested": requested_runs,
            "success": completed_runs,
            "completed": completed_runs,
            "failed": failed_runs,
            "in_progress": in_progress_runs,
        },
        "agentStages": agent_stage_counts,
        "actions": action_totals,
        "integrationsFound": integrations_found,
        "integrationsConnected": integrations_connected,
    }
