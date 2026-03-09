"""Database connection for FastAPI — async SQLAlchemy + asyncpg."""
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
import os

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from api.models import Base

load_dotenv()

# Default matches: docker run ... -e POSTGRES_USER=myuser -e POSTGRES_PASSWORD=mypassword -e POSTGRES_DB=agentdb
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://myuser:mypassword@localhost:5432/agentdb",
)

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
    async with async_session_factory() as session:
        await session.execute(text("SELECT 1"))
    yield
    await engine.dispose()
