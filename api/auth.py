"""Auth0 JWT verification and user get-or-create for FastAPI."""
import logging
import os
from dataclasses import dataclass
from typing import Annotated

import httpx
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db import get_db
from api.models import Organization, User, OrgPerson

logger = logging.getLogger(__name__)

HTTP_BEARER = HTTPBearer(auto_error=False)


@dataclass
class UserDetails:
    """JWT claims plus optional DB user for the current request."""
    claims: dict
    user: User


def get_token(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(HTTP_BEARER)],
) -> str:
    """
    Extract JWT from Authorization: Bearer … or from query param `token` (for SSE).
    Raises 401 if neither is present.
    """
    if credentials and credentials.credentials:
        return credentials.credentials
    token = request.query_params.get("token")
    if token:
        return token
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Missing or invalid authorization (use Authorization: Bearer <token> or ?token=<token>)",
        headers={"WWW-Authenticate": "Bearer"},
    )

AUTH0_DOMAIN = os.getenv("AUTH0_DOMAIN", "").rstrip("/").replace("https://", "").replace("http://", "")
AUTH0_AUDIENCE = os.getenv("AUTH0_AUDIENCE", "")
AUTH0_ISSUER = f"https://{AUTH0_DOMAIN}/" if AUTH0_DOMAIN else ""

# Development: put all new users in one shared org so they share contacts (people/teams).
USE_SHARED_ORG = os.getenv("USE_SHARED_ORG", "1").lower() in ("1", "true", "yes")
SHARED_ORG_NAME = os.getenv("SHARED_ORG_NAME", "Development")

# Development: avoid calling Auth0 /userinfo on every request.
# When True, we never call /userinfo; we just trust whatever is in the JWT.
DISABLE_AUTH0_USERINFO = os.getenv("DISABLE_AUTH0_USERINFO", "1").lower() in ("1", "true", "yes")

# In-memory cache of JWKS (key by kid)
_jwks_cache: dict[str, dict] = {}
_jwks_cache_issuer: str | None = None

# In-memory cache of userinfo responses: sub -> payload dict
_userinfo_cache: dict[str, dict] = {}


def _get_jwks() -> dict:
    """Fetch Auth0 JWKS (cached)."""
    global _jwks_cache, _jwks_cache_issuer
    if not AUTH0_DOMAIN:
        return {}
    if _jwks_cache_issuer != AUTH0_ISSUER:
        _jwks_cache.clear()
        _jwks_cache_issuer = AUTH0_ISSUER
    if _jwks_cache:
        return _jwks_cache
    url = f"https://{AUTH0_DOMAIN}/.well-known/jwks.json"
    try:
        resp = httpx.get(url, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        for key in data.get("keys", []):
            kid = key.get("kid")
            if kid:
                _jwks_cache[kid] = key
        return _jwks_cache
    except Exception as e:
        logger.warning("Failed to fetch Auth0 JWKS: %s", e)
        return _jwks_cache


def verify_auth0_token(token: str) -> dict:
    """
    Verify Auth0 JWT and return payload (sub, email, name, picture).
    Raises HTTPException if invalid or Auth0 not configured.
    """
    if not AUTH0_DOMAIN or not AUTH0_AUDIENCE:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth0 is not configured (AUTH0_DOMAIN / AUTH0_AUDIENCE)",
        )
    jwks = _get_jwks()
    if not jwks:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not load Auth0 signing keys",
        )
    try:
        unverified = jwt.get_unverified_header(token)
        kid = unverified.get("kid")
        if not kid or kid not in jwks:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token key id")
        key = jwt.algorithms.RSAAlgorithm.from_jwk(jwks[kid])
        payload = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=AUTH0_AUDIENCE,
            issuer=AUTH0_ISSUER,
        )
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    except Exception as e:
        logger.warning("Token verification error: %s", e)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


def _is_placeholder_name(name: str | None) -> bool:
    """True if name is missing or looks like Auth0 sub/placeholder (e.g. google-oauth2|123@auth0.user)."""
    if not name or not (name := name.strip()):
        return True
    return "@auth0.user" in name or "|" in name


def _is_placeholder_email(email: str | None) -> bool:
    """True if email is missing or the placeholder we use for missing email."""
    if not email or not email.strip():
        return True
    return email.strip().endswith("@auth0.user")


async def _fetch_auth0_userinfo(access_token: str, sub: str | None) -> dict | None:
    """
    Fetch user profile from Auth0 /userinfo (email, name, picture). Returns None on failure.
    Cached in-memory by sub so we only hit Auth0 once per user per process.
    """
    if not AUTH0_DOMAIN or DISABLE_AUTH0_USERINFO:
        return None
    if sub and sub in _userinfo_cache:
        return _userinfo_cache[sub]
    url = f"https://{AUTH0_DOMAIN}/userinfo"
    try:
        resp = httpx.get(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
        if sub:
            _userinfo_cache[sub] = data
        return data
    except Exception as e:
        logger.warning("Auth0 userinfo fetch failed: %s", e)
        return None


async def get_or_create_user(db: AsyncSession, payload: dict) -> User:
    """
    Find user by auth0_id (sub); if not found, create Organization and User.
    Update name/email/picture if changed.
    Payload can be from JWT and/or Auth0 /userinfo (email, name, picture).
    """
    sub = payload.get("sub")
    if not sub:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token missing sub")
    email = (payload.get("email") or "").strip() or (payload.get("https://your-app/email") or "").strip()
    name = (payload.get("name") or payload.get("nickname") or "").strip() or None
    if _is_placeholder_name(name):
        name = None
        # Prefer given_name + family_name from userinfo when name was placeholder
        given = (payload.get("given_name") or "").strip()
        family = (payload.get("family_name") or "").strip()
        if given or family:
            name = f"{given} {family}".strip() or None
    # Dev/production-safe fallback: if we still don't have a usable name but do have a real email,
    # derive a human-readable name from the email local-part (e.g. "john.doe" -> "John Doe").
    if not name and email and not _is_placeholder_email(email):
        local = email.split("@")[0]
        parts = (
            local.replace(".", " ")
            .replace("_", " ")
            .replace("-", " ")
            .split()
        )
        if parts:
            name = " ".join(p.capitalize() for p in parts)
    picture = (payload.get("picture") or "").strip() or None
    if _is_placeholder_email(email):
        email = f"{sub}@auth0.user"  # fallback only if still missing or placeholder

    result = await db.execute(select(User).where(User.auth0_id == sub))
    user = result.scalars().first()
    if user:
        changed = False
        if user.name != name:
            user.name = name
            changed = True
        if user.email != email:
            user.email = email
            changed = True
        if user.picture != picture:
            user.picture = picture
            changed = True
        if changed:
            await db.flush()
        await _link_user_to_org_person(db, user, email)
        return user

    # Create user — either in shared org (dev) or in a new org per user
    if USE_SHARED_ORG:
        result = await db.execute(select(Organization).where(Organization.name == SHARED_ORG_NAME))
        org = result.scalars().first()
        if not org:
            org = Organization(name=SHARED_ORG_NAME)
            db.add(org)
            await db.flush()
            logger.info("Created shared org: %s", SHARED_ORG_NAME)
    else:
        if name:
            org_name = name
        elif email and not _is_placeholder_email(email):
            local = email.split("@")[0]
            org_name = local if "|" not in local else "Personal"
        else:
            org_name = "Personal"
        org = Organization(name=org_name)
        db.add(org)
        await db.flush()
    user = User(
        org_id=org.id,
        auth0_id=sub,
        email=email,
        name=name,
        picture=picture,
    )
    db.add(user)
    await db.flush()
    await db.refresh(user)
    logger.info("Created user from Auth0: auth0_id=%s email=%s org_id=%s", sub, email, org.id)
    await _link_user_to_org_person(db, user, email)
    return user


def _normalize_email_for_match(email: str | None) -> str | None:
    """Return lowercased trimmed email for matching, or None if placeholder/missing."""
    if not email or not (e := email.strip()):
        return None
    if e.endswith("@auth0.user"):
        return None
    return e.lower()


async def _link_user_to_org_person(db: AsyncSession, user: User, login_email: str) -> None:
    """
    If this user is not yet linked to an OrgPerson, try to find one in the same org
    with matching email and link them. Skips if login_email is placeholder.
    """
    if _is_placeholder_email(login_email):
        return
    norm = _normalize_email_for_match(login_email)
    if not norm:
        return
    # Already linked?
    result = await db.execute(select(OrgPerson).where(OrgPerson.user_id == user.id))
    if result.scalars().first():
        return
    # Find an unlinked OrgPerson in same org with same email (case-insensitive)
    result = await db.execute(
        select(OrgPerson).where(
            OrgPerson.org_id == user.org_id,
            OrgPerson.user_id.is_(None),
        )
    )
    candidates = result.scalars().all()
    match = None
    for p in candidates:
        if p.email and _normalize_email_for_match(p.email) == norm:
            match = p
            break
    if match:
        match.user_id = user.id
        await db.flush()
        logger.info("Linked user %s to org_person %s (email match)", user.id, match.id)


async def get_user_details(
    db: Annotated[AsyncSession, Depends(get_db)],
    token: Annotated[str, Depends(get_token)],
) -> UserDetails:
    """
    FastAPI dependency: validate JWT (from Bearer or query param `token`), return UserDetails (claims + DB user).
    Use on every protected route; user is created/updated on first use.
    """
    payload = verify_auth0_token(token)
    # Access tokens for an API often omit email/name or have placeholder; fetch from Auth0 /userinfo if needed.
    # For performance, this is cached by sub and can be fully disabled in dev via DISABLE_AUTH0_USERINFO.
    email = (payload.get("email") or "").strip()
    name = (payload.get("name") or payload.get("nickname") or "").strip() or None
    if not DISABLE_AUTH0_USERINFO and (_is_placeholder_email(email) or _is_placeholder_name(name)):
        userinfo = await _fetch_auth0_userinfo(token, payload.get("sub"))
        if userinfo:
            payload = {**payload, **userinfo}
    user = await get_or_create_user(db, payload)
    return UserDetails(claims=payload, user=user)


async def get_current_user(
    user_details: Annotated[UserDetails, Depends(get_user_details)],
) -> User:
    """
    FastAPI dependency: require valid JWT (Bearer or ?token=), verify with Auth0, return DB user.
    Use on routes that only need the User model; for claims + user use get_user_details.
    """
    return user_details.user
