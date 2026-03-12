"""Database connection for FastAPI — async SQLAlchemy + asyncpg.

Also exposes a sync session factory for Celery workers, which run in plain
threads and cannot use asyncpg / async SQLAlchemy.
"""
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, contextmanager
import os
from typing import Generator

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from api.models import Base, TokenLimit

load_dotenv()

# Default matches: docker run ... -e POSTGRES_USER=myuser -e POSTGRES_PASSWORD=mypassword -e POSTGRES_DB=agentdb
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://myuser:mypassword@localhost:5432/agentdb",
)

# Sync URL for Celery workers (psycopg2, no asyncpg)
SYNC_DATABASE_URL = os.getenv(
    "SYNC_DATABASE_URL",
    "postgresql://myuser:mypassword@localhost:5432/agentdb",
)
INITIAL_MONTHLY_TOKEN_LIMIT = int(os.getenv("INITIAL_MONTHLY_TOKEN_LIMIT", "100000"))

engine = create_async_engine(
    DATABASE_URL,
    echo=os.getenv("SQL_ECHO", "0").lower() in ("1", "true", "yes"),
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

# Sync engine / session factory used by Celery workers
sync_engine = create_engine(
    SYNC_DATABASE_URL,
    echo=os.getenv("SQL_ECHO", "0").lower() in ("1", "true", "yes"),
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
)

sync_session_factory = sessionmaker(
    sync_engine,
    class_=Session,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


@contextmanager
def get_sync_db() -> Generator[Session, None, None]:
    """Context manager that yields a sync session for Celery workers."""
    session = sync_session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: yield an async session and close it when done."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def _backfill_initial_monthly_token_limits() -> None:
    """Grant the default monthly quota to existing users that do not have one."""
    async with async_session_factory() as session:
        users_missing_limit = (
            await session.execute(
                text(
                    """
                    SELECT u.id
                    FROM users u
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM token_limits tl
                        WHERE tl.user_id = u.id
                          AND tl.agent_type IS NULL
                          AND tl.period = 'monthly'
                    )
                    """
                )
            )
        ).scalars().all()

        if not users_missing_limit:
            return

        session.add_all(
            [
                TokenLimit(
                    user_id=user_id,
                    agent_type=None,
                    period="monthly",
                    max_tokens=INITIAL_MONTHLY_TOKEN_LIMIT,
                )
                for user_id in users_missing_limit
            ]
        )
        await session.commit()


@asynccontextmanager
async def db_lifespan():
    """Lifespan context: create tables, verify connection, dispose engine on shutdown."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Add columns to users if they were added after initial create (PostgreSQL 12+)
        await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS auth0_id VARCHAR(255)"))
        await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS picture VARCHAR(512)"))
        await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()"))
        # Link org_people to users (when a contact has a login account)
        await conn.execute(text("ALTER TABLE org_people ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id) ON DELETE SET NULL"))
        # Ensure run_request_logs / run_response_logs have latest columns
        await conn.execute(text("ALTER TABLE run_request_logs ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id) ON DELETE SET NULL"))
        await conn.execute(text("ALTER TABLE run_request_logs ADD COLUMN IF NOT EXISTS user_auth0_sub VARCHAR(255)"))
        await conn.execute(text("ALTER TABLE run_request_logs ADD COLUMN IF NOT EXISTS meeting_date TIMESTAMPTZ"))
        await conn.execute(text("ALTER TABLE run_request_logs ADD COLUMN IF NOT EXISTS language VARCHAR(64)"))
        await conn.execute(text("ALTER TABLE run_request_logs ADD COLUMN IF NOT EXISTS original_file_name VARCHAR(512)"))
        await conn.execute(text("ALTER TABLE run_request_logs ADD COLUMN IF NOT EXISTS stored_file_name VARCHAR(512)"))
        await conn.execute(text("ALTER TABLE run_response_logs ADD COLUMN IF NOT EXISTS status VARCHAR(64)"))
        await conn.execute(text("ALTER TABLE run_response_logs ADD COLUMN IF NOT EXISTS actions_extracted INTEGER"))
        await conn.execute(text("ALTER TABLE run_response_logs ADD COLUMN IF NOT EXISTS actions_normalized INTEGER"))
        await conn.execute(text("ALTER TABLE run_response_logs ADD COLUMN IF NOT EXISTS actions_executed INTEGER"))
        await conn.execute(text("ALTER TABLE run_response_logs ADD COLUMN IF NOT EXISTS response_data JSONB"))
        # user_tokens: extra metadata per service (e.g. Slack workspace name, user id)
        await conn.execute(text("ALTER TABLE user_tokens ADD COLUMN IF NOT EXISTS meta JSONB"))
    await _backfill_initial_monthly_token_limits()
    async with async_session_factory() as session:
        await session.execute(text("SELECT 1"))
    yield
    await engine.dispose()
