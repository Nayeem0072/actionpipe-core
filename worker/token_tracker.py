"""Token usage tracking for LangChain / LangGraph agent runs.

Components
----------
TokenTrackingCallback
    A LangChain ``BaseCallbackHandler`` that accumulates prompt_tokens,
    completion_tokens and total_tokens from every LLM call in a graph run.
    Attach it to a graph via ``config={"callbacks": [callback]}``.

check_token_limit(user_id, agent_type, db)
    Raises :class:`TokenLimitExceeded` if the user has exhausted their daily
    or monthly token budget.  Checks the most specific limit row first
    (user + agent_type), then falls back to progressively broader defaults.

persist_token_usage(...)
    Writes a ``TokenUsage`` row to the database after an agent completes.
"""
from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

load_dotenv()

logger = logging.getLogger(__name__)

_DEFAULT_DAILY_LIMIT = int(os.getenv("TOKEN_LIMIT_DAILY_DEFAULT", "0"))
_DEFAULT_MONTHLY_LIMIT = int(os.getenv("TOKEN_LIMIT_MONTHLY_DEFAULT", "0"))
_ANSI_ORANGE = "\033[38;5;214m"
_ANSI_RESET = "\033[0m"


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class TokenLimitExceeded(Exception):
    """Raised when a user's token budget would be exceeded."""

    def __init__(self, user_id: str, period: str, used: int, limit: int) -> None:
        self.user_id = user_id
        self.period = period
        self.used = used
        self.limit = limit
        super().__init__(
            f"Token limit exceeded for user {user_id}: "
            f"{used:,} / {limit:,} {period} tokens used"
        )


# ---------------------------------------------------------------------------
# Callback handler
# ---------------------------------------------------------------------------


@dataclass(eq=False)
class TokenTrackingCallback(BaseCallbackHandler):
    """Accumulates token counts from every LLM call in a LangGraph run.

    Usage::

        cb = TokenTrackingCallback(run_id="abc", agent_type="extractor")
        app.invoke(state, config={"callbacks": [cb]})
        print(cb.total_tokens)  # total across all nodes
    """

    run_id: str
    agent_type: str
    provider: str = ""
    model: str = ""
    prompt_tokens: int = field(default=0, init=False)
    completion_tokens: int = field(default=0, init=False)
    total_tokens: int = field(default=0, init=False)

    # track which model / provider was actually used (last seen wins)
    _last_model: str = field(default="", init=False, repr=False)
    _last_provider: str = field(default="", init=False, repr=False)

    # ------------------------------------------------------------------
    # LangChain callback hooks
    # ------------------------------------------------------------------

    def on_chat_model_end(self, response: LLMResult, **kwargs: Any) -> None:  # type: ignore[override]
        """Handle chat-model completions by reusing the standard LLM parser."""
        self._record_response(response)

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:  # type: ignore[override]
        self._record_response(response)

    def _record_response(self, response: LLMResult) -> None:
        usage = self._extract_usage(response)
        self.prompt_tokens += usage.get("prompt_tokens", 0)
        self.completion_tokens += usage.get("completion_tokens", 0)
        self.total_tokens += usage.get("total_tokens", 0)

        # Try to capture model/provider info from the response metadata
        if response.llm_output:
            if model := response.llm_output.get("model_name") or response.llm_output.get("model"):
                self._last_model = str(model)
            if provider := response.llm_output.get("provider"):
                self._last_provider = str(provider)

        if response.generations:
            first_gen = response.generations[0][0] if response.generations[0] else None
            message = getattr(first_gen, "message", None) if first_gen else None
            response_meta = getattr(message, "response_metadata", None) or {}
            if not self._last_model:
                model = response_meta.get("model_name") or response_meta.get("model")
                if model:
                    self._last_model = str(model)
            if not self._last_provider:
                provider = response_meta.get("provider")
                if provider:
                    self._last_provider = str(provider)

        logger.debug(
            "[token_tracker] run=%s agent=%s +prompt=%d +completion=%d total=%d",
            self.run_id,
            self.agent_type,
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
            self.total_tokens,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_usage(response: LLMResult) -> dict[str, int]:
        """Extract token counts from various provider response formats."""
        def _usage_value(container: Any, *keys: str) -> int:
            for key in keys:
                if isinstance(container, dict):
                    value = container.get(key)
                else:
                    value = getattr(container, key, None)
                if value is not None:
                    return int(value)
            return 0

        # OpenAI / Anthropic / Google all put token info in llm_output
        if response.llm_output:
            raw = response.llm_output.get("token_usage") or response.llm_output.get("usage") or {}
            if raw:
                return {
                    "prompt_tokens": int(raw.get("prompt_tokens") or raw.get("input_tokens") or 0),
                    "completion_tokens": int(
                        raw.get("completion_tokens") or raw.get("output_tokens") or 0
                    ),
                    "total_tokens": int(raw.get("total_tokens") or 0),
                }

        # Fall back to per-generation usage_metadata (LangChain ≥0.2)
        total_p = total_c = 0
        for gen_list in response.generations:
            for gen in gen_list:
                meta = getattr(gen, "generation_info", None) or {}
                um = getattr(gen, "message", None)
                um = getattr(um, "usage_metadata", None) if um else None
                if um:
                    total_p += _usage_value(um, "input_tokens", "prompt_tokens")
                    total_c += _usage_value(um, "output_tokens", "completion_tokens")
                elif meta:
                    total_p += int(meta.get("prompt_token_count") or 0)
                    total_c += int(meta.get("candidates_token_count") or 0)

        return {
            "prompt_tokens": total_p,
            "completion_tokens": total_c,
            "total_tokens": total_p + total_c,
        }

    @property
    def effective_model(self) -> str:
        return self._last_model or self.model

    @property
    def effective_provider(self) -> str:
        return self._last_provider or self.provider


# ---------------------------------------------------------------------------
# Token limit enforcement
# ---------------------------------------------------------------------------


def check_token_limit(
    user_id: str | uuid.UUID,
    agent_type: str,
    db: Any,  # sqlalchemy.orm.Session
) -> None:
    """Raise :class:`TokenLimitExceeded` if the user is over their budget.

    Checks both daily and monthly limits.  The most specific matching
    ``TokenLimit`` row wins (user+agent > user > global+agent > global).
    A ``max_tokens`` value of 0 means unlimited.
    """
    from sqlalchemy import func, select

    from api.models import TokenLimit, TokenUsage

    uid = str(user_id)

    for period in ("daily", "monthly"):
        limit_tokens = _resolve_limit(db, uid, agent_type, period)
        if limit_tokens == 0:
            continue

        # Sum tokens used in this period
        now = datetime.now(tz=timezone.utc)
        if period == "daily":
            period_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        used_q = (
            select(func.coalesce(func.sum(TokenUsage.total_tokens), 0))
            .where(TokenUsage.user_id == uuid.UUID(uid) if isinstance(uid, str) else user_id)
            .where(TokenUsage.created_at >= period_start)
        )
        if agent_type:
            used_q = used_q.where(TokenUsage.agent_type == agent_type)

        used: int = db.execute(used_q).scalar() or 0

        if used >= limit_tokens:
            raise TokenLimitExceeded(uid, period, used, limit_tokens)


def _resolve_limit(db: Any, user_id: str, agent_type: str, period: str) -> int:
    """Return the effective token limit for user + agent_type + period.

    Priority (highest to lowest):
      1. user_id + agent_type
      2. user_id + NULL agent_type
      3. NULL user_id + agent_type  (global per-agent default)
      4. NULL user_id + NULL agent_type  (global default)
    Falls back to env defaults if no DB row exists.
    """
    from sqlalchemy import select

    from api.models import TokenLimit

    try:
        uid_val = uuid.UUID(user_id)
    except (ValueError, AttributeError):
        uid_val = None

    candidates = db.execute(
        select(TokenLimit)
        .where(TokenLimit.period == period)
        .where(
            (TokenLimit.user_id == uid_val) | (TokenLimit.user_id.is_(None))
        )
    ).scalars().all()

    def specificity(row: TokenLimit) -> int:
        score = 0
        if row.user_id is not None:
            score += 2
        if row.agent_type is not None:
            score += 1
        return score

    matching = [
        r for r in candidates
        if (r.agent_type is None or r.agent_type == agent_type)
    ]
    if not matching:
        # No DB row — use env defaults
        return _DEFAULT_DAILY_LIMIT if period == "daily" else _DEFAULT_MONTHLY_LIMIT

    return max(matching, key=specificity).max_tokens


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def persist_token_usage(
    db: Any,  # sqlalchemy.orm.Session
    callback: TokenTrackingCallback,
    user_id: str | uuid.UUID | None,
) -> None:
    """Write a :class:`~api.models.TokenUsage` row for the completed agent run."""
    from api.models import TokenUsage

    if callback.total_tokens == 0:
        logger.info(
            f"{_ANSI_ORANGE}[token_tracker] skipping persist run=%s agent=%s prompt=%d completion=%d total=%d provider=%s model=%s{_ANSI_RESET}",
            callback.run_id,
            callback.agent_type,
            callback.prompt_tokens,
            callback.completion_tokens,
            callback.total_tokens,
            callback.effective_provider or "unknown",
            callback.effective_model or "unknown",
        )
        return

    uid: uuid.UUID | None = None
    if user_id:
        try:
            uid = uuid.UUID(str(user_id))
        except (ValueError, AttributeError):
            uid = None

    row = TokenUsage(
        user_id=uid,
        run_id=callback.run_id,
        agent_type=callback.agent_type,
        provider=callback.effective_provider or None,
        model=callback.effective_model or None,
        prompt_tokens=callback.prompt_tokens,
        completion_tokens=callback.completion_tokens,
        total_tokens=callback.total_tokens,
    )
    db.add(row)
    db.commit()
    logger.info(
        f"{_ANSI_ORANGE}[token_tracker] persisted run=%s agent=%s prompt=%d completion=%d total=%d provider=%s model=%s{_ANSI_RESET}",
        callback.run_id,
        callback.agent_type,
        callback.prompt_tokens,
        callback.completion_tokens,
        callback.total_tokens,
        callback.effective_provider or "unknown",
        callback.effective_model or "unknown",
    )
