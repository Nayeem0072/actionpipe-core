"""LangGraph PostgreSQL checkpointer factory.

Uses langgraph-checkpoint-postgres with a *synchronous* psycopg connection so
it can be called from Celery workers (which run in plain threads, not async).

Thread-ID convention
--------------------
Each agent step uses a stable thread ID:  "{run_id}:{agent_type}"

Because the thread ID never changes across retries, LangGraph always resumes
from the last successfully completed node rather than starting from scratch.

Connection strategy
-------------------
Two separate connections are used deliberately:

1. Setup connection (autocommit=True) — used only for PostgresSaver.setup().
   CREATE INDEX CONCURRENTLY cannot run inside a transaction block, so
   autocommit=True is mandatory here. This connection is closed immediately
   after setup completes and runs at most once per worker process.

2. Checkpointing connection (autocommit=False, the default) — used for all
   actual graph checkpoint reads/writes. PostgresSaver issues explicit
   BEGIN/COMMIT around each checkpoint operation; using autocommit=True on
   this connection would cause those statements to be rejected by psycopg3,
   resulting in a silent hang.

Usage
-----
    with build_checkpointer() as checkpointer:
        app = graph.compile(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": thread_id}}
        result = app.invoke(initial_state, config=config)
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Generator

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_SYNC_DATABASE_URL = os.getenv(
    "SYNC_DATABASE_URL",
    "postgresql://myuser:mypassword@localhost:5432/agentdb",
)

# Process-level flag: setup only needs to run once per worker process.
_setup_done: bool = False


def make_thread_id(run_id: str, agent_type: str) -> str:
    """Return the stable LangGraph thread ID for a given run + agent."""
    return f"{run_id}:{agent_type}"


def _ensure_setup() -> None:
    """Run PostgresSaver.setup() exactly once per worker process.

    Uses a short-lived autocommit connection so that CREATE INDEX CONCURRENTLY
    can execute outside a transaction block.
    """
    global _setup_done
    if _setup_done:
        return
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
        import psycopg
    except ImportError as exc:
        raise ImportError(
            "langgraph-checkpoint-postgres and psycopg[binary] are required. "
            "Install them with: pip install langgraph-checkpoint-postgres psycopg[binary]"
        ) from exc

    with psycopg.connect(_SYNC_DATABASE_URL, autocommit=True) as setup_conn:
        PostgresSaver(setup_conn).setup()
        logger.info("PostgresSaver.setup() complete")

    _setup_done = True


@contextmanager
def build_checkpointer() -> Generator:
    """Context manager that yields a ready-to-use PostgresSaver.

    Opens a standard (non-autocommit) psycopg connection for checkpointing so
    that PostgresSaver's internal BEGIN/COMMIT pairs work correctly.
    """
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
        import psycopg
    except ImportError as exc:
        raise ImportError(
            "langgraph-checkpoint-postgres and psycopg[binary] are required. "
            "Install them with: pip install langgraph-checkpoint-postgres psycopg[binary]"
        ) from exc

    _ensure_setup()

    # Standard connection — autocommit=False (default).
    # PostgresSaver wraps checkpoint reads/writes in explicit transactions;
    # this mode is required for those to work correctly.
    with psycopg.connect(_SYNC_DATABASE_URL) as conn:
        checkpointer = PostgresSaver(conn)
        logger.debug("PostgresSaver ready for checkpointing (conn=%s)", conn)
        yield checkpointer
