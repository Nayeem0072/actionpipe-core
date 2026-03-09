# Auth0 implementation

This document describes how Auth0 is integrated with the API: configuration, request flow, and how to protect routes.

## Overview

- The **frontend** uses Auth0 to log users in and obtains an **access token** (JWT).
- The **API** validates the JWT using Auth0’s public keys (JWKS) and, on first use, **creates or updates** the user in PostgreSQL (and assigns them to an organization).
- In development, all new users can be placed into a **single shared org** so they automatically share contacts (controlled by `USE_SHARED_ORG` / `SHARED_ORG_NAME`).
- Any route that needs the current user uses the `get_current_user` dependency (or `get_user_details` when claims are also needed); the user is looked up by Auth0 `sub` and returned from the DB.

## Backend configuration

Set these in `.env` (see `.env.example`):

| Variable | Description | Example |
|----------|-------------|---------|
| `AUTH0_DOMAIN` | Auth0 tenant host (no `https://`) | `your-tenant.auth0.com` |
| `AUTH0_AUDIENCE` | API Identifier from Auth0 Dashboard | `https://your-api-identifier` or `https://api.yourapp.com` |
| `USE_SHARED_ORG` | When `1`, all new users go into a single shared org | `1` (dev) |
| `SHARED_ORG_NAME` | Name of the shared org when `USE_SHARED_ORG=1` | `Development` |
| `DISABLE_AUTH0_USERINFO` | When `1`, never call Auth0 `/userinfo`; use JWT claims only | `1` (dev) |

If either is missing, the API returns `503 Service Unavailable` for protected routes with detail: `Auth0 is not configured`.

## Auth0 Dashboard setup

1. **Create an API** (Auth0 Dashboard → APIs → Create API):
   - **Name**: e.g. “Agent AI API”
   - **Identifier**: set this to the value you use for `AUTH0_AUDIENCE` (e.g. `https://localhost:8000` or a custom identifier).
   - **Signing Algorithm**: RS256 (default).

2. **Application** (Auth0 Dashboard → Applications):
   - Use the same Application that your frontend uses for login.
   - Ensure the frontend requests an **access token** with **audience** equal to `AUTH0_AUDIENCE` when calling `getAccessTokenSilently` / `loginWithRedirect` (or , in this development case, it should be: https://localhost:8000). Without the correct audience, the backend will reject the token.

3. **Optional**: In the API’s Settings, you can enable “Allow Offline Access” if you need refresh tokens; the current implementation only uses the access token.

## Request flow

1. User logs in on the frontend via Auth0 (e.g. redirect or popup).
2. Frontend receives an **access token** (JWT) and stores it (e.g. in memory or secure storage).
3. Frontend calls the `/me` API with:
   ```http
   Authorization: Bearer <access_token>
   ```
4. `/me` API:
   - Fetches Auth0 JWKS from `https://{AUTH0_DOMAIN}/.well-known/jwks.json` (cached).
   - Verifies the JWT (signature, `iss`, `aud`, expiry).
   - Optionally calls Auth0 `/userinfo` **once per user** (cached by `sub`) to enrich `email` / `name` / `picture` when `DISABLE_AUTH0_USERINFO=0`. In dev we normally set `DISABLE_AUTH0_USERINFO=1` to avoid this network call.
   - Reads `sub`, `email`, `name`, `picture` from the resulting payload.
   - Looks up the user in the DB by `auth0_id` (= `sub`). If not found, creates an **Organization** and a **User**; if found, updates `name` / `email` / `picture` if changed.
   - Returns the `User` from the DB plus the full identity **claims** to the route.

## Endpoints

### `GET /me`

Returns the current authenticated user. Creates or updates the user in the DB on first request after login.

**Headers**

- `Authorization: Bearer <access_token>` (required)

**Response (200)**

```json
{
  "id": "uuid",
  "org_id": "uuid",
  "org_name": "Development",
  "email": "user@example.com",
  "name": "User Name",
  "picture": "https://...",
  "created_at": "2025-03-08T12:00:00.000000",
  "org_person_id": "uuid-or-null",
  "claims": {
    "...": "all decoded token claims (plus /userinfo fields if enabled)"
  }
}
```

The **frontend should typically call `/me` once after login**, store the pieces it needs (e.g. `name`, `picture`, and anything from `claims`), and reuse them without calling `/me` again on every page.

**Errors**

- `401 Unauthorized` — Missing/invalid `Authorization` header, invalid/expired token, or token not meant for this API (wrong audience).
- `503 Service Unavailable` — Auth0 not configured or JWKS could not be loaded.

## Protecting other routes

Use the same dependency on any route that should require a logged-in user:

```python
from fastapi import APIRouter, Depends
from api.auth import get_current_user
from api.models import User

router = APIRouter()

@router.get("/protected")
async def protected_route(user: User = Depends(get_current_user)):
    # user is the DB User (with id, org_id, email, etc.)
    return {"user_id": str(user.id)}
```

- If the request has no token or an invalid token, FastAPI returns `401` before the route runs.
- If the token is valid, the user is fetched or created and passed in as `user`.

## Token claims used

The backend reads these from the verified JWT (and optionally from Auth0 profile when `/userinfo` is enabled):

| Claim   | Use |
|---------|-----|
| `sub`   | Stored as `User.auth0_id`; used to find or create the user. |
| `email` | Stored as `User.email`. Fallback: `{sub}@auth0.user` if missing. |
| `name`  | Stored as `User.name`. If missing but email is present, we derive a simple display name from the email local part. |
| `picture` | Stored as `User.picture`. |

`iss` and `aud` are validated but not stored; they must match `https://{AUTH0_DOMAIN}/` and `AUTH0_AUDIENCE` respectively.

## Database behavior

- **First login**:
  - If `USE_SHARED_ORG=1`: the user is assigned to (and, if needed, creates) a shared **Organization** named `SHARED_ORG_NAME`.
  - Otherwise: a new **Organization** is created (name = email local part or `"Personal"`).
  - In both cases, a new **User** is created with `org_id`, `auth0_id`, `email`, `name`, `picture`.
- **Subsequent requests**: The user is found by `auth0_id`. If `name`, `email`, or `picture` differ from the token (or `/userinfo`), the user row is updated.

## Userinfo calls and performance

- JWT validation is done **locally** using JWKS and is required on every request.
- Auth0 `/userinfo` is **optional** and controlled by:
  - `DISABLE_AUTH0_USERINFO=1` (default in dev): never call `/userinfo`; `claims` come only from the JWT.
  - `DISABLE_AUTH0_USERINFO=0`: call `/userinfo` **once per user** (cached by `sub`) when email/name look placeholder.
- For local development, keep `DISABLE_AUTH0_USERINFO=1` for fast requests and rely on `/me.claims` for any profile fields your frontend needs.

## Error responses

| Status | Cause |
|--------|--------|
| `401 Unauthorized` | No `Authorization: Bearer` header, invalid/expired JWT, wrong audience, or missing `sub`. |
| `503 Service Unavailable` | `AUTH0_DOMAIN` or `AUTH0_AUDIENCE` not set, or JWKS fetch failed. |

## Code references

- **JWT verification and user resolution**: `api/auth.py` (`verify_auth0_token`, `get_or_create_user`, `get_current_user`).
- **User/org models**: `api/models.py` (`User`, `Organization`).
- **`GET /me`**: `api/main.py`.
