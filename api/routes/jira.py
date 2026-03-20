"""Jira OAuth connect flow — lets users link their Atlassian/Jira account to their account."""
from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_user
from api.db import get_db
from api.models import User, UserToken

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jira", tags=["jira"])

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

JIRA_CLIENT_ID = os.getenv("JIRA_CLIENT_ID", "")
JIRA_CLIENT_SECRET = os.getenv("JIRA_CLIENT_SECRET", "")
# The URL Atlassian redirects back to after the user authorises (must be
# registered as a Callback URL in your Atlassian app settings).
JIRA_REDIRECT_URI = os.getenv("JIRA_REDIRECT_URI", "")
# Where to send the user after a successful connect (your frontend).
JIRA_FRONTEND_REDIRECT = os.getenv("JIRA_FRONTEND_REDIRECT", "http://localhost:5173")

_ATLASSIAN_AUTHORIZE_URL = "https://auth.atlassian.com/authorize"
_ATLASSIAN_TOKEN_URL = "https://auth.atlassian.com/oauth/token"
_ATLASSIAN_RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"

# offline_access grants a refresh token; the Jira scopes cover reading/writing issues.
_JIRA_SCOPES = "read:jira-user write:jira-work read:jira-work offline_access"


def _require_config() -> None:
    """Raise 503 if Jira OAuth credentials are not configured."""
    if not JIRA_CLIENT_ID or not JIRA_CLIENT_SECRET:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Jira OAuth is not configured (JIRA_CLIENT_ID / JIRA_CLIENT_SECRET missing)",
        )


# ---------------------------------------------------------------------------
# GET /jira/connect
# ---------------------------------------------------------------------------

@router.get("/connect")
async def jira_connect(
    current_user: Annotated[User, Depends(get_current_user)],
):
    """
    Return the Atlassian OAuth authorization URL.

    The frontend should redirect the user to this URL.
    The `state` parameter encodes the user's DB id so the callback can look
    them up without requiring a second JWT in the redirect.

    `prompt=consent` ensures Atlassian always issues a refresh token.
    """
    _require_config()

    # Use the user's UUID as state so the callback can identify who's connecting.
    # A cryptographic prefix is added so it can't be guessed/forged.
    state = f"{secrets.token_urlsafe(16)}.{current_user.id}"

    params = {
        "client_id": JIRA_CLIENT_ID,
        "redirect_uri": JIRA_REDIRECT_URI,
        "response_type": "code",
        "scope": _JIRA_SCOPES,
        "audience": "api.atlassian.com",
        "prompt": "consent",
        "state": state,
    }
    url = f"{_ATLASSIAN_AUTHORIZE_URL}?{urlencode(params)}"
    return {"url": url}


# ---------------------------------------------------------------------------
# GET /jira/callback
# ---------------------------------------------------------------------------

@router.get("/callback")
async def jira_callback(
    code: str = Query(..., description="Temporary authorization code from Atlassian"),
    state: str = Query(..., description="State token issued by /jira/connect"),
    db: AsyncSession = Depends(get_db),
):
    """
    Atlassian redirects here after the user approves the OAuth request.

    Exchanges the temporary code for an access token and refresh token, then
    calls the accessible-resources endpoint to resolve the Jira cloud_id and
    site_url for the user's Atlassian site. Upserts a UserToken(service='jira')
    row for the authorising user, then redirects the browser to the frontend.

    NOTE: This endpoint does NOT use get_current_user — the browser navigates
    here directly from Atlassian, not from the SPA. The user is identified via
    the `state` parameter that was set in /jira/connect.
    """
    _require_config()

    # Extract user id from state (format: "<random>.<user_uuid>")
    try:
        _, user_id_str = state.rsplit(".", 1)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid state parameter")

    # Exchange code for tokens
    async with httpx.AsyncClient(timeout=15.0) as client:
        token_resp = await client.post(
            _ATLASSIAN_TOKEN_URL,
            json={
                "client_id": JIRA_CLIENT_ID,
                "client_secret": JIRA_CLIENT_SECRET,
                "code": code,
                "redirect_uri": JIRA_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
        )

    if token_resp.status_code != 200:
        logger.error("Atlassian token exchange HTTP error: %s", token_resp.text)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to reach Atlassian OAuth token endpoint",
        )

    token_data = token_resp.json()

    if "error" in token_data:
        error = token_data.get("error", "unknown")
        error_description = token_data.get("error_description", "")
        logger.warning("Jira OAuth error: %s — %s", error, error_description)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Jira OAuth failed: {error}",
        )

    access_token: str = token_data["access_token"]
    refresh_token: str | None = token_data.get("refresh_token")
    expires_in: int = token_data.get("expires_in", 3600)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    # Resolve the Jira cloud_id and site_url for this user's Atlassian organisation.
    # Atlassian can have multiple accessible resources; we use the first one.
    cloud_id: str | None = None
    site_url: str | None = None
    site_name: str | None = None
    async with httpx.AsyncClient(timeout=10.0) as client:
        resources_resp = await client.get(
            _ATLASSIAN_RESOURCES_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if resources_resp.status_code == 200:
        resources = resources_resp.json()
        if resources:
            first = resources[0]
            cloud_id = first.get("id")
            site_url = first.get("url")
            site_name = first.get("name")
    else:
        logger.warning("Could not fetch Atlassian accessible-resources (status %s)", resources_resp.status_code)

    meta = {
        "cloud_id": cloud_id,
        "site_url": site_url,
        "site_name": site_name,
        "scope": token_data.get("scope"),
    }

    # Upsert UserToken
    result = await db.execute(
        select(UserToken).where(
            UserToken.user_id == user_id_str,
            UserToken.service == "jira",
        )
    )
    token_row = result.scalar_one_or_none()

    if token_row:
        token_row.access_token = access_token
        if refresh_token:
            token_row.refresh_token = refresh_token
        token_row.expires_at = expires_at
        token_row.meta = meta
        logger.info("Updated Jira token for user %s (site: %s)", user_id_str, site_name)
    else:
        token_row = UserToken(
            user_id=user_id_str,
            service="jira",
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            meta=meta,
        )
        db.add(token_row)
        logger.info("Created Jira token for user %s (site: %s)", user_id_str, site_name)

    await db.commit()

    # Redirect user back to the frontend
    return RedirectResponse(url=JIRA_FRONTEND_REDIRECT, status_code=302)


# ---------------------------------------------------------------------------
# GET /jira/status
# ---------------------------------------------------------------------------

@router.get("/status")
async def jira_status(
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """
    Return whether the current user has connected their Jira account.

    Response: {connected: bool, site_url: str|null, site_name: str|null, scope: str|null}
    """
    result = await db.execute(
        select(UserToken).where(
            UserToken.user_id == current_user.id,
            UserToken.service == "jira",
        )
    )
    token_row = result.scalar_one_or_none()

    if not token_row:
        return {
            "connected": False,
            "site_url": None,
            "site_name": None,
            "scope": None,
            "project_key": None,
        }

    meta = token_row.meta or {}
    return {
        "connected": True,
        "site_url": meta.get("site_url"),
        "site_name": meta.get("site_name"),
        "scope": meta.get("scope"),
        "project_key": meta.get("project_key"),
    }


# ---------------------------------------------------------------------------
# DELETE /jira/disconnect
# ---------------------------------------------------------------------------

@router.delete("/disconnect", status_code=204)
async def jira_disconnect(
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """
    Remove the user's Jira token. Returns 204 whether or not a token existed.
    """
    result = await db.execute(
        select(UserToken).where(
            UserToken.user_id == current_user.id,
            UserToken.service == "jira",
        )
    )
    token_row = result.scalar_one_or_none()

    if token_row:
        await db.delete(token_row)
        await db.commit()
        logger.info("Removed Jira token for user %s", current_user.id)


# ---------------------------------------------------------------------------
# GET /jira/projects
# ---------------------------------------------------------------------------

@router.get("/projects")
async def jira_list_projects(
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """
    Return all Jira projects the user has access to on their connected Atlassian site.

    Calls the Atlassian REST API using the user's stored OAuth token.
    The frontend should call this after /jira/connect to let the user pick
    a default project for issue creation.

    Response: { "projects": [{ "key": "KAN", "name": "Kanban", "type": "software" }, ...] }
    """
    result = await db.execute(
        select(UserToken).where(
            UserToken.user_id == current_user.id,
            UserToken.service == "jira",
        )
    )
    token_row = result.scalar_one_or_none()
    if not token_row or not token_row.access_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Jira is not connected. Connect your Atlassian account first via /jira/connect.",
        )

    from api.routes.runs import _refresh_jira_token_if_needed
    access_token = await _refresh_jira_token_if_needed(token_row, db)
    if not access_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Could not obtain a valid Jira access token. Please reconnect via /jira/connect.",
        )

    meta = token_row.meta or {}
    cloud_id = meta.get("cloud_id", "")
    if not cloud_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Jira cloud_id missing from token metadata. Please reconnect via /jira/connect.",
        )

    url = f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/project/search"
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers=headers, params={"maxResults": 100, "orderBy": "name"})
    except Exception as exc:
        logger.error("Jira project list request failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not reach Atlassian API to fetch projects.",
        )

    if resp.status_code != 200:
        logger.error("Jira project list HTTP %s: %s", resp.status_code, resp.text[:300])
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Atlassian API returned HTTP {resp.status_code} when listing projects.",
        )

    data = resp.json()
    projects = [
        {
            "key": p["key"],
            "name": p["name"],
            "type": p.get("projectTypeKey", ""),
            "style": p.get("style", ""),
        }
        for p in data.get("values", [])
    ]

    saved_key = meta.get("project_key")
    return {"projects": projects, "saved_project_key": saved_key}


# ---------------------------------------------------------------------------
# PATCH /jira/settings
# ---------------------------------------------------------------------------

class JiraSettingsBody(BaseModel):
    project_key: str = Field(..., description="Jira project key to save as the default (e.g. 'KAN')")


@router.patch("/settings")
async def jira_save_settings(
    body: JiraSettingsBody,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """
    Save the user's default Jira project key.

    Stores project_key in user_tokens.meta so that POST /runs/{id}/jira_actions/execute
    can use it automatically without requiring projectKey in every request body.

    Response: { "project_key": "KAN" }
    """
    result = await db.execute(
        select(UserToken).where(
            UserToken.user_id == current_user.id,
            UserToken.service == "jira",
        )
    )
    token_row = result.scalar_one_or_none()
    if not token_row:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Jira is not connected. Connect your Atlassian account first via /jira/connect.",
        )

    project_key = body.project_key.strip().upper()
    if not project_key:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="project_key cannot be empty.",
        )

    meta = dict(token_row.meta or {})
    meta["project_key"] = project_key
    token_row.meta = meta
    await db.commit()

    logger.info("Saved default Jira project key '%s' for user %s", project_key, current_user.id)
    return {"project_key": project_key}


# ---------------------------------------------------------------------------
# Test endpoints — validate the Jira MCP server end-to-end
# ---------------------------------------------------------------------------


class TestCreateIssueBody(BaseModel):
    project_key: str = Field(default="KAN", description="Jira project key (e.g. 'KAN')")
    summary: str = Field(..., description="Issue title / summary")
    description: Optional[str] = Field(default=None, description="Issue body text")
    assignee_account_id: Optional[str] = Field(default=None, description="Atlassian account ID of assignee")
    priority: Optional[str] = Field(default=None, description="Priority: high, medium, low, etc.")
    due_date: Optional[str] = Field(default=None, description="Due date in YYYY-MM-DD format")
    labels: Optional[list[str]] = Field(default=None, description="Label strings")
    issue_type: str = Field(default="Task", description="Issue type, e.g. Task, Bug, Story")


class TestUpdateIssueBody(BaseModel):
    issue_key: str = Field(..., description="Existing issue key to update, e.g. 'KAN-5'")
    summary: Optional[str] = Field(default=None)
    description: Optional[str] = Field(default=None)
    assignee_account_id: Optional[str] = Field(default=None)
    priority: Optional[str] = Field(default=None)
    due_date: Optional[str] = Field(default=None, description="YYYY-MM-DD")
    labels: Optional[list[str]] = Field(default=None)


async def _get_jira_credentials(user: User, db: AsyncSession) -> tuple[str, str]:
    """
    Return (access_token, cloud_id) for the user's connected Jira account.
    Refreshes the access token if it is near expiry.
    Raises 403/422 if the account is not connected or metadata is missing.
    """
    result = await db.execute(
        select(UserToken).where(
            UserToken.user_id == user.id,
            UserToken.service == "jira",
        )
    )
    token_row = result.scalar_one_or_none()
    if not token_row or not token_row.access_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Jira is not connected. Connect your Atlassian account first via /jira/connect.",
        )

    from api.routes.runs import _refresh_jira_token_if_needed
    access_token = await _refresh_jira_token_if_needed(token_row, db)
    if not access_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Could not obtain a valid Jira access token. Please reconnect via /jira/connect.",
        )

    meta = token_row.meta or {}
    cloud_id = meta.get("cloud_id", "")
    if not cloud_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Jira cloud_id missing from token metadata. Please reconnect via /jira/connect.",
        )

    return access_token, cloud_id


async def _call_jira_mcp_tool(
    tool_name: str,
    params: dict,
    access_token: str,
    cloud_id: str,
) -> Any:
    """
    Invoke a single tool on the Jira MCP server and return the raw MCP response.
    Raises HTTPException on tool-not-found or MCP-level errors.
    """
    import sys as _sys
    import json as _json
    import asyncio
    from pathlib import Path
    from langchain_mcp_adapters.client import MultiServerMCPClient  # type: ignore

    _project_root = Path(__file__).parent.parent.parent
    config = _json.loads((_project_root / "mcp_config.json").read_text())
    server_cfg = config["mcpServers"]["jira"]

    # Use sys.executable so the subprocess runs in the same venv (has mcp, httpx, etc.)
    command = server_cfg["command"]
    if command == "python":
        command = _sys.executable

    # Resolve relative script paths to absolute only when the file exists on disk.
    # Package specifiers (e.g. @scope/pkg) and flags (-y) are left unchanged.
    args = []
    for a in server_cfg.get("args", []):
        if not a.startswith("-") and not Path(a).is_absolute():
            resolved = _project_root / a
            if resolved.exists():
                args.append(str(resolved))
                continue
        args.append(a)

    server_spec = {
        "jira": {
            "command": command,
            "args": args,
            "env": {"JIRA_ACCESS_TOKEN": access_token, "JIRA_CLOUD_ID": cloud_id},
            "transport": "stdio",
        }
    }

    client = MultiServerMCPClient(server_spec)
    try:
        tools = await client.get_tools()
        tool = next((t for t in tools if t.name == tool_name), None)
        if tool is None:
            available = [t.name for t in tools]
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Tool '{tool_name}' not found in Jira MCP server. Available: {available}",
            )
        return await tool.ainvoke(params)
    finally:
        close = getattr(client, "aclose", None) or getattr(client, "close", None)
        if close:
            import asyncio as _asyncio
            if _asyncio.iscoroutinefunction(close):
                await close()
            else:
                close()


def _parse_jira_mcp_text(response: Any) -> dict:
    """Extract and JSON-parse the text payload from an MCP response list."""
    import json as _json
    raw_text: Optional[str] = None
    if isinstance(response, list):
        for item in response:
            if isinstance(item, dict) and item.get("type") == "text":
                raw_text = item.get("text")
                break
    elif isinstance(response, str):
        raw_text = response
    if not raw_text:
        return {}
    try:
        return _json.loads(raw_text)
    except Exception:
        return {"raw": raw_text}


@router.post("/test/create-issue")
async def test_create_jira_issue(
    body: TestCreateIssueBody,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Test endpoint — create a Jira issue via the custom MCP server.

    Calls jira_create_issue on the MCP server using your connected Jira account.
    Use this to verify the MCP server works end-to-end before it is used in the
    full pipeline.

    Example:
        POST /jira/test/create-issue
        { "project_key": "KAN", "summary": "Test issue from MCP" }

    Returns the created issue key on success, e.g. { "issue_key": "KAN-7", ... }
    """
    access_token, cloud_id = await _get_jira_credentials(current_user, db)

    params = {
        "project_key": body.project_key.strip().upper(),
        "summary": body.summary,
        "issue_type": body.issue_type,
    }
    if body.description:
        params["description"] = body.description
    if body.assignee_account_id:
        params["assignee_account_id"] = body.assignee_account_id
    if body.priority:
        params["priority"] = body.priority
    if body.due_date:
        params["due_date"] = body.due_date
    if body.labels:
        params["labels"] = body.labels

    response = await _call_jira_mcp_tool("jira_create_issue", params, access_token, cloud_id)
    logger.info("jira_create_issue MCP response: %s", response)

    data = _parse_jira_mcp_text(response)
    if data.get("ok") is False:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Jira API error: {data.get('error', 'unknown')}",
        )

    return {
        "status": "success",
        "issue_key": data.get("key"),
        "issue_id": data.get("id"),
        "issue_url": data.get("self"),
        "params_sent": params,
    }


@router.post("/test/update-issue")
async def test_update_jira_issue(
    body: TestUpdateIssueBody,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Test endpoint — update an existing Jira issue via the custom MCP server.

    Calls jira_update_issue on the MCP server using your connected Jira account.

    Example:
        POST /jira/test/update-issue
        { "issue_key": "KAN-5", "summary": "Revised title", "priority": "high" }
    """
    access_token, cloud_id = await _get_jira_credentials(current_user, db)

    params: dict = {"issue_key": body.issue_key.strip().upper()}
    if body.summary is not None:
        params["summary"] = body.summary
    if body.description is not None:
        params["description"] = body.description
    if body.assignee_account_id is not None:
        params["assignee_account_id"] = body.assignee_account_id
    if body.priority is not None:
        params["priority"] = body.priority
    if body.due_date is not None:
        params["due_date"] = body.due_date
    if body.labels is not None:
        params["labels"] = body.labels

    response = await _call_jira_mcp_tool("jira_update_issue", params, access_token, cloud_id)
    logger.info("jira_update_issue MCP response for %s: %s", body.issue_key, response)

    data = _parse_jira_mcp_text(response)
    if data.get("ok") is False:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Jira API error: {data.get('error', 'unknown')}",
        )

    return {
        "status": "success",
        "issue_key": body.issue_key.strip().upper(),
    }
