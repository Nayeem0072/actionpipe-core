"""FastAPI application — APIs live under the api folder."""
import logging
import sys

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from api.db import db_lifespan, get_db
from api.auth import UserDetails, get_user_details
from api.models import OrgPerson, Organization, User
from api.routes import runs as runs_routes
from api.routes import network as network_routes
from api.routes import slack as slack_routes
from api.routes import calendar as calendar_routes
from api.routes import jira as jira_routes
from api.routes import notion as notion_routes
from api.routes import dashboard as dashboard_routes

# Ensure api and src loggers (pipeline, executor, etc.) output to console when running under uvicorn
_root = logging.getLogger()
_root.setLevel(logging.INFO)
if not _root.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    _root.addHandler(_handler)

app = FastAPI(
    title="Agent AI API",
    description="ActionPipe and other APIs.",
    version="0.1.0",
    lifespan=lambda app: db_lifespan(),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://[::1]:5173",
    ],
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|\[::1\]):5173$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

app.include_router(runs_routes.router)
app.include_router(network_routes.router)
app.include_router(slack_routes.router)
app.include_router(calendar_routes.router)
app.include_router(jira_routes.router)
app.include_router(notion_routes.router)
app.include_router(dashboard_routes.router)


@app.get("/me")
async def me(
    user_details: UserDetails = Depends(get_user_details),
    db: AsyncSession = Depends(get_db),
):
    """Return the current authenticated user (from Auth0). Creates/updates user in DB on first login."""
    user = user_details.user
    result = await db.execute(select(OrgPerson.id).where(OrgPerson.user_id == user.id))
    org_person_id = result.scalar_one_or_none()
    org_name_result = await db.execute(
        select(Organization.name).where(Organization.id == user.org_id)
    )
    org_name = org_name_result.scalar_one_or_none()
    return {
        "id": str(user.id),
        "org_id": str(user.org_id),
        "org_name": org_name,
        "email": user.email,
        "name": user.name,
        "picture": user.picture,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "org_person_id": str(org_person_id) if org_person_id else None,
        # Full identity payload from the token (and Auth0 /userinfo if enabled).
        # Frontend can cache this on first load and reuse it without calling /me again.
        "claims": user_details.claims,
    }


@app.get("/health")
async def health(db: AsyncSession = Depends(get_db)):
    """Check API and database connectivity."""
    await db.execute(text("SELECT 1"))
    return {"status": "ok", "database": "connected"}
