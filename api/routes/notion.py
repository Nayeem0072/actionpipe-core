"""Notion OAuth connect flow — lets users link their Notion workspace to their account."""
from __future__ import annotations

import base64
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

router = APIRouter(prefix="/notion", tags=["notion"])

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

NOTION_CLIENT_ID = os.getenv("NOTION_CLIENT_ID", "")
NOTION_CLIENT_SECRET = os.getenv("NOTION_CLIENT_SECRET", "")
# The URL Notion redirects back to after the user authorises (must be registered
# in your Notion integration's OAuth settings).
NOTION_REDIRECT_URI = os.getenv("NOTION_REDIRECT_URI", "")
# Where to send the user after a successful connect (your frontend).
NOTION_FRONTEND_REDIRECT = os.getenv("NOTION_FRONTEND_REDIRECT", "http://localhost:5173")

_NOTION_AUTHORIZE_URL = "https://api.notion.com/v1/oauth/authorize"
_NOTION_TOKEN_URL = "https://api.notion.com/v1/oauth/token"


def _require_config() -> None:
    """Raise 503 if Notion OAuth credentials are not configured."""
    if not NOTION_CLIENT_ID or not NOTION_CLIENT_SECRET:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Notion OAuth is not configured (NOTION_CLIENT_ID / NOTION_CLIENT_SECRET missing)",
        )


def _basic_auth_header() -> str:
    """Return the HTTP Basic Auth header value for the Notion token endpoint."""
    credentials = f"{NOTION_CLIENT_ID}:{NOTION_CLIENT_SECRET}"
    encoded = base64.b64encode(credentials.encode()).decode()
    return f"Basic {encoded}"


# ---------------------------------------------------------------------------
# GET /notion/connect
# ---------------------------------------------------------------------------

@router.get("/connect")
async def notion_connect(
    current_user: Annotated[User, Depends(get_current_user)],
):
    """
    Return the Notion OAuth authorization URL.

    The frontend should redirect the user to this URL.
    The `state` parameter encodes the user's DB id so the callback can look
    them up without requiring a second JWT in the redirect.
    """
    _require_config()

    # Use the user's UUID as state so the callback can identify who's connecting.
    # A cryptographic prefix is added so it can't be guessed/forged.
    state = f"{secrets.token_urlsafe(16)}.{current_user.id}"

    params = {
        "client_id": NOTION_CLIENT_ID,
        "redirect_uri": NOTION_REDIRECT_URI,
        "response_type": "code",
        "owner": "user",
        "state": state,
    }
    url = f"{_NOTION_AUTHORIZE_URL}?{urlencode(params)}"
    return {"url": url}


# ---------------------------------------------------------------------------
# GET /notion/callback
# ---------------------------------------------------------------------------

@router.get("/callback")
async def notion_callback(
    code: str = Query(..., description="Temporary code from Notion"),
    state: str = Query(..., description="State token issued by /notion/connect"),
    db: AsyncSession = Depends(get_db),
):
    """
    Notion redirects here after the user approves the OAuth request.

    Exchanges the temporary code for an access token, then upserts a
    UserToken(service='notion') row for the authorising user.
    Finally redirects the browser to the frontend.

    NOTE: This endpoint does NOT use get_current_user — the browser navigates
    here directly from Notion, not from the SPA. The user is identified via the
    `state` parameter that was set in /notion/connect.

    Notion's token endpoint requires HTTP Basic Auth using the client id and
    secret, rather than passing them in the request body.
    """
    _require_config()

    # Extract user id from state (format: "<random>.<user_uuid>")
    try:
        _, user_id_str = state.rsplit(".", 1)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid state parameter")

    # Exchange code for token — Notion requires Basic Auth, not body params
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            _NOTION_TOKEN_URL,
            headers={
                "Authorization": _basic_auth_header(),
                "Content-Type": "application/json",
            },
            json={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": NOTION_REDIRECT_URI,
            },
        )

    if resp.status_code != 200:
        logger.error("Notion token exchange HTTP error %s: %s", resp.status_code, resp.text)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to reach Notion OAuth endpoint",
        )

    data = resp.json()

    if "error" in data:
        error = data.get("error", "unknown")
        logger.warning("Notion OAuth error: %s", error)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Notion OAuth failed: {error}",
        )

    access_token: str = data["access_token"]

    meta = {
        "workspace_id": data.get("workspace_id"),
        "workspace_name": data.get("workspace_name"),
        "workspace_icon": data.get("workspace_icon"),
        "bot_id": data.get("bot_id"),
    }

    # Upsert UserToken
    result = await db.execute(
        select(UserToken).where(
            UserToken.user_id == user_id_str,
            UserToken.service == "notion",
        )
    )
    token_row = result.scalar_one_or_none()

    if token_row:
        token_row.access_token = access_token
        token_row.meta = meta
        logger.info("Updated Notion token for user %s (workspace: %s)", user_id_str, meta.get("workspace_name"))
    else:
        token_row = UserToken(
            user_id=user_id_str,
            service="notion",
            access_token=access_token,
            meta=meta,
        )
        db.add(token_row)
        logger.info("Created Notion token for user %s (workspace: %s)", user_id_str, meta.get("workspace_name"))

    await db.commit()

    # Redirect user back to the frontend
    return RedirectResponse(url=NOTION_FRONTEND_REDIRECT, status_code=302)


# ---------------------------------------------------------------------------
# GET /notion/status
# ---------------------------------------------------------------------------

@router.get("/status")
async def notion_status(
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """
    Return whether the current user has connected their Notion workspace.

    Response: {connected: bool, workspace_name: str|null, workspace_id: str|null}
    """
    result = await db.execute(
        select(UserToken).where(
            UserToken.user_id == current_user.id,
            UserToken.service == "notion",
        )
    )
    token_row = result.scalar_one_or_none()

    if not token_row:
        return {"connected": False, "workspace_name": None, "workspace_id": None}

    meta = token_row.meta or {}
    return {
        "connected": True,
        "workspace_name": meta.get("workspace_name"),
        "workspace_id": meta.get("workspace_id"),
    }


# ---------------------------------------------------------------------------
# DELETE /notion/disconnect
# ---------------------------------------------------------------------------

@router.delete("/disconnect", status_code=204)
async def notion_disconnect(
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """
    Remove the user's Notion token. Returns 204 whether or not a token existed.
    """
    result = await db.execute(
        select(UserToken).where(
            UserToken.user_id == current_user.id,
            UserToken.service == "notion",
        )
    )
    token_row = result.scalar_one_or_none()

    if token_row:
        await db.delete(token_row)
        await db.commit()
        logger.info("Removed Notion token for user %s", current_user.id)
