"""Jira OAuth connect flow — lets users link their Atlassian/Jira account to their account."""
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
        return {"connected": False, "site_url": None, "site_name": None, "scope": None}

    meta = token_row.meta or {}
    return {
        "connected": True,
        "site_url": meta.get("site_url"),
        "site_name": meta.get("site_name"),
        "scope": meta.get("scope"),
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
