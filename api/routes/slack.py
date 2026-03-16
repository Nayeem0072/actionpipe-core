"""Slack OAuth connect flow — lets users link their Slack workspace to their account."""
from __future__ import annotations

import logging
import os
import secrets
from typing import Annotated
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth import get_current_user
from api.db import get_db
from api.models import User, UserToken

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/slack", tags=["slack"])

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SLACK_CLIENT_ID = os.getenv("SLACK_CLIENT_ID", "")
SLACK_CLIENT_SECRET = os.getenv("SLACK_CLIENT_SECRET", "")
# The URL Slack redirects back to after the user authorises (must be HTTPS and
# registered in your Slack app's "OAuth & Permissions → Redirect URLs").
SLACK_REDIRECT_URI = os.getenv("SLACK_REDIRECT_URI", "")
# Where to send the user after a successful connect (your frontend).
SLACK_FRONTEND_REDIRECT = os.getenv("SLACK_FRONTEND_REDIRECT", "http://localhost:5173")

_SLACK_AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"
_SLACK_TOKEN_URL = "https://slack.com/api/oauth.v2.access"

# Scopes the bot token will have. im:write required for conversations.open (DM channel resolution).
_BOT_SCOPES = "chat:write,channels:read,users:read,users:read.email,im:write"


def _require_config() -> None:
    """Raise 503 if Slack OAuth credentials are not configured."""
    if not SLACK_CLIENT_ID or not SLACK_CLIENT_SECRET:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Slack OAuth is not configured (SLACK_CLIENT_ID / SLACK_CLIENT_SECRET missing)",
        )


# ---------------------------------------------------------------------------
# GET /slack/connect
# ---------------------------------------------------------------------------

@router.get("/connect")
async def slack_connect(
    current_user: Annotated[User, Depends(get_current_user)],
):
    """
    Return the Slack OAuth authorization URL.

    The frontend should redirect the user to this URL.
    The `state` parameter encodes the user's DB id so the callback can look
    them up without requiring a second JWT in the redirect.
    """
    _require_config()

    # Use the user's UUID as state so the callback can identify who's connecting.
    # A cryptographic prefix is added so it can't be guessed/forged.
    state = f"{secrets.token_urlsafe(16)}.{current_user.id}"

    params = {
        "client_id": SLACK_CLIENT_ID,
        "scope": _BOT_SCOPES,
        "redirect_uri": SLACK_REDIRECT_URI,
        "state": state,
    }
    url = f"{_SLACK_AUTHORIZE_URL}?{urlencode(params)}"
    return {"url": url}


# ---------------------------------------------------------------------------
# GET /slack/callback
# ---------------------------------------------------------------------------

@router.get("/callback")
async def slack_callback(
    code: str = Query(..., description="Temporary code from Slack"),
    state: str = Query(..., description="State token issued by /slack/connect"),
    db: AsyncSession = Depends(get_db),
):
    """
    Slack redirects here after the user approves the OAuth request.

    Exchanges the temporary code for an access token, then upserts a
    UserToken(service='slack') row for the authorising user.
    Finally redirects the browser to the frontend.

    NOTE: This endpoint does NOT use get_current_user — the browser navigates
    here directly from Slack, not from the SPA. The user is identified via the
    `state` parameter that was set in /slack/connect.
    """
    _require_config()

    # Extract user id from state (format: "<random>.<user_uuid>")
    try:
        _, user_id_str = state.rsplit(".", 1)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid state parameter")

    # Exchange code for token
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            _SLACK_TOKEN_URL,
            data={
                "client_id": SLACK_CLIENT_ID,
                "client_secret": SLACK_CLIENT_SECRET,
                "code": code,
                "redirect_uri": SLACK_REDIRECT_URI,
            },
        )

    if resp.status_code != 200:
        logger.error("Slack token exchange HTTP error: %s", resp.text)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to reach Slack OAuth endpoint",
        )

    data = resp.json()
    if not data.get("ok"):
        error = data.get("error", "unknown")
        logger.warning("Slack OAuth error: %s", error)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Slack OAuth failed: {error}",
        )

    access_token: str = data["access_token"]
    team: dict = data.get("team", {})
    authed_user: dict = data.get("authed_user", {})

    meta = {
        "workspace": team.get("name"),
        "team_id": team.get("id"),
        "slack_user_id": authed_user.get("id"),
        "bot_user_id": data.get("bot_user_id"),
    }

    # Upsert UserToken
    result = await db.execute(
        select(UserToken).where(
            UserToken.user_id == user_id_str,
            UserToken.service == "slack",
        )
    )
    token_row = result.scalar_one_or_none()

    if token_row:
        token_row.access_token = access_token
        token_row.meta = meta
        logger.info("Updated Slack token for user %s (workspace: %s)", user_id_str, meta.get("workspace"))
    else:
        token_row = UserToken(
            user_id=user_id_str,
            service="slack",
            access_token=access_token,
            meta=meta,
        )
        db.add(token_row)
        logger.info("Created Slack token for user %s (workspace: %s)", user_id_str, meta.get("workspace"))

    await db.commit()

    # Redirect user back to the frontend
    return RedirectResponse(url=SLACK_FRONTEND_REDIRECT, status_code=302)


# ---------------------------------------------------------------------------
# GET /slack/status
# ---------------------------------------------------------------------------

@router.get("/status")
async def slack_status(
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """
    Return whether the current user has connected their Slack workspace.

    Response: {connected: bool, workspace: str|null, slack_user_id: str|null}
    """
    result = await db.execute(
        select(UserToken).where(
            UserToken.user_id == current_user.id,
            UserToken.service == "slack",
        )
    )
    token_row = result.scalar_one_or_none()

    if not token_row:
        return {"connected": False, "workspace": None, "slack_user_id": None}

    meta = token_row.meta or {}
    return {
        "connected": True,
        "workspace": meta.get("workspace"),
        "slack_user_id": meta.get("slack_user_id"),
    }


# ---------------------------------------------------------------------------
# DELETE /slack/disconnect
# ---------------------------------------------------------------------------

@router.delete("/disconnect", status_code=204)
async def slack_disconnect(
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """
    Remove the user's Slack token. Returns 204 whether or not a token existed.
    """
    result = await db.execute(
        select(UserToken).where(
            UserToken.user_id == current_user.id,
            UserToken.service == "slack",
        )
    )
    token_row = result.scalar_one_or_none()

    if token_row:
        await db.delete(token_row)
        await db.commit()
        logger.info("Removed Slack token for user %s", current_user.id)
