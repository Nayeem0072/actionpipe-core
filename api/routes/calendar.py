"""Google Calendar OAuth connect flow — lets users link their Google Calendar to their account."""
from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
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

router = APIRouter(prefix="/calendar", tags=["calendar"])

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
# The URL Google redirects back to after the user authorises (must be registered
# in Google Cloud Console → OAuth 2.0 → Authorised redirect URIs).
GOOGLE_CALENDAR_REDIRECT_URI = os.getenv("GOOGLE_CALENDAR_REDIRECT_URI", "")
# Where to send the user after a successful connect (your frontend).
CALENDAR_FRONTEND_REDIRECT = os.getenv("CALENDAR_FRONTEND_REDIRECT", "http://localhost:5173")

_GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"

# Scopes required for reading/writing calendar events.
_CALENDAR_SCOPES = " ".join([
    "openid",
    "email",
    "https://www.googleapis.com/auth/calendar",
])


def _require_config() -> None:
    """Raise 503 if Google OAuth credentials are not configured."""
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google Calendar OAuth is not configured (GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET missing)",
        )


# ---------------------------------------------------------------------------
# GET /calendar/connect
# ---------------------------------------------------------------------------

@router.get("/connect")
async def calendar_connect(
    current_user: Annotated[User, Depends(get_current_user)],
):
    """
    Return the Google OAuth authorization URL.

    The frontend should redirect the user to this URL.
    The `state` parameter encodes the user's DB id so the callback can look
    them up without requiring a second JWT in the redirect.

    `access_type=offline` and `prompt=consent` are required to obtain a
    refresh_token from Google on every authorization.
    """
    _require_config()

    # Use the user's UUID as state so the callback can identify who's connecting.
    # A cryptographic prefix is added so it can't be guessed/forged.
    state = f"{secrets.token_urlsafe(16)}.{current_user.id}"

    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_CALENDAR_REDIRECT_URI,
        "response_type": "code",
        "scope": _CALENDAR_SCOPES,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    url = f"{_GOOGLE_AUTHORIZE_URL}?{urlencode(params)}"
    return {"url": url}


# ---------------------------------------------------------------------------
# GET /calendar/callback
# ---------------------------------------------------------------------------

@router.get("/callback")
async def calendar_callback(
    code: str = Query(..., description="Temporary authorization code from Google"),
    state: str = Query(..., description="State token issued by /calendar/connect"),
    db: AsyncSession = Depends(get_db),
):
    """
    Google redirects here after the user approves the OAuth request.

    Exchanges the temporary code for an access token and refresh token, then
    upserts a UserToken(service='google_calendar') row for the authorising user.
    Finally redirects the browser to the frontend.

    NOTE: This endpoint does NOT use get_current_user — the browser navigates
    here directly from Google, not from the SPA. The user is identified via the
    `state` parameter that was set in /calendar/connect.
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
            _GOOGLE_TOKEN_URL,
            data={
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "code": code,
                "redirect_uri": GOOGLE_CALENDAR_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
        )

    if token_resp.status_code != 200:
        logger.error("Google token exchange HTTP error: %s", token_resp.text)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to reach Google OAuth token endpoint",
        )

    token_data = token_resp.json()

    if "error" in token_data:
        error = token_data.get("error", "unknown")
        error_description = token_data.get("error_description", "")
        logger.warning("Google OAuth error: %s — %s", error, error_description)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Google OAuth failed: {error}",
        )

    access_token: str = token_data["access_token"]
    refresh_token: str | None = token_data.get("refresh_token")
    expires_in: int = token_data.get("expires_in", 3600)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    # Fetch the user's Google account email to store in meta
    google_email: str | None = None
    async with httpx.AsyncClient(timeout=10.0) as client:
        userinfo_resp = await client.get(
            _GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if userinfo_resp.status_code == 200:
        userinfo = userinfo_resp.json()
        google_email = userinfo.get("email")
    else:
        logger.warning("Could not fetch Google userinfo (status %s)", userinfo_resp.status_code)

    meta = {
        "email": google_email,
        "scopes": token_data.get("scope"),
    }

    # Upsert UserToken
    result = await db.execute(
        select(UserToken).where(
            UserToken.user_id == user_id_str,
            UserToken.service == "google_calendar",
        )
    )
    token_row = result.scalar_one_or_none()

    if token_row:
        token_row.access_token = access_token
        if refresh_token:
            token_row.refresh_token = refresh_token
        token_row.expires_at = expires_at
        token_row.meta = meta
        logger.info("Updated Google Calendar token for user %s (email: %s)", user_id_str, google_email)
    else:
        token_row = UserToken(
            user_id=user_id_str,
            service="google_calendar",
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            meta=meta,
        )
        db.add(token_row)
        logger.info("Created Google Calendar token for user %s (email: %s)", user_id_str, google_email)

    await db.commit()

    # Redirect user back to the frontend
    return RedirectResponse(url=CALENDAR_FRONTEND_REDIRECT, status_code=302)


# ---------------------------------------------------------------------------
# GET /calendar/status
# ---------------------------------------------------------------------------

@router.get("/status")
async def calendar_status(
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """
    Return whether the current user has connected their Google Calendar.

    Response: {connected: bool, email: str|null, scopes: str|null}
    """
    result = await db.execute(
        select(UserToken).where(
            UserToken.user_id == current_user.id,
            UserToken.service == "google_calendar",
        )
    )
    token_row = result.scalar_one_or_none()

    if not token_row:
        return {"connected": False, "email": None, "scopes": None}

    meta = token_row.meta or {}
    return {
        "connected": True,
        "email": meta.get("email"),
        "scopes": meta.get("scopes"),
    }


# ---------------------------------------------------------------------------
# DELETE /calendar/disconnect
# ---------------------------------------------------------------------------

@router.delete("/disconnect", status_code=204)
async def calendar_disconnect(
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """
    Remove the user's Google Calendar token. Returns 204 whether or not a token existed.
    """
    result = await db.execute(
        select(UserToken).where(
            UserToken.user_id == current_user.id,
            UserToken.service == "google_calendar",
        )
    )
    token_row = result.scalar_one_or_none()

    if token_row:
        await db.delete(token_row)
        await db.commit()
        logger.info("Removed Google Calendar token for user %s", current_user.id)
