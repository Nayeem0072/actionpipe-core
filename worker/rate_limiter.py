"""Redis-backed rate limiter with exponential backoff + jitter.

Rate limiting strategy
----------------------
Two independent sliding-window rate limiters protect against API bans:

  1. Per-user   — key: ``ratelimit:user:{user_id}``
  2. Per-agent  — key: ``ratelimit:agent:{agent_type}:{provider}``

Each window is implemented as a Redis sorted set:
  - Member:  unique request ID (uuid4 hex)
  - Score:   current UNIX timestamp (float)
  - On each check: prune scores older than the window, then count members.

Exponential backoff with full jitter
-------------------------------------
Used when a provider returns a rate-limit / server error (HTTP 429/503):

    sleep = min(cap, base * 2^attempt) * uniform(0.5, 1.0)

The jitter avoids thundering-herd retries when many workers hit the same limit
simultaneously.
"""
from __future__ import annotations

import logging
import os
import random
import time
import uuid
from typing import TYPE_CHECKING

from dotenv import load_dotenv

if TYPE_CHECKING:
    pass

load_dotenv()

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Rate limit defaults (overridable via env)
_DEFAULT_USER_LIMIT = int(os.getenv("RATE_LIMIT_USER_PER_MINUTE", "60"))
_DEFAULT_AGENT_LIMIT = int(os.getenv("RATE_LIMIT_AGENT_PER_MINUTE", "30"))

# Backoff config
_BACKOFF_BASE = 1.0   # seconds
_BACKOFF_CAP = 60.0   # max seconds before a retry


def backoff_jitter(attempt: int, base: float = _BACKOFF_BASE, cap: float = _BACKOFF_CAP) -> float:
    """Return a randomised exponential backoff delay (full-jitter variant).

    Formula: uniform(0.5, 1.0) * min(cap, base * 2^attempt)
    """
    ceiling = min(cap, base * (2 ** attempt))
    return ceiling * random.uniform(0.5, 1.0)


class RateLimitExceeded(Exception):
    """Raised when the caller has exceeded the configured rate limit."""

    def __init__(self, key: str, limit: int, window: int) -> None:
        self.key = key
        self.limit = limit
        self.window = window
        super().__init__(
            f"Rate limit exceeded: {key} — {limit} calls/{window}s allowed"
        )


class RedisRateLimiter:
    """Sliding-window rate limiter backed by Redis sorted sets.

    Parameters
    ----------
    redis_url:
        Redis connection string (defaults to ``REDIS_URL`` env var).
    user_limit:
        Max calls per user per ``user_window`` seconds.
    user_window:
        Window size in seconds for per-user limit (default 60).
    agent_limit:
        Max calls per agent+provider combination per ``agent_window`` seconds.
    agent_window:
        Window size in seconds for per-agent limit (default 60).
    """

    def __init__(
        self,
        redis_url: str = REDIS_URL,
        user_limit: int = _DEFAULT_USER_LIMIT,
        user_window: int = 60,
        agent_limit: int = _DEFAULT_AGENT_LIMIT,
        agent_window: int = 60,
    ) -> None:
        import redis as redis_lib

        self._redis = redis_lib.from_url(redis_url, decode_responses=True)
        self.user_limit = user_limit
        self.user_window = user_window
        self.agent_limit = agent_limit
        self.agent_window = agent_window

    # ------------------------------------------------------------------
    # Core sliding-window check
    # ------------------------------------------------------------------

    def _check(self, key: str, limit: int, window: int, *, block: bool = True) -> None:
        """Check (and record) one call against a sliding window.

        If ``block`` is True (default) the method *sleeps* until a slot opens
        rather than raising immediately — useful for short bursts.
        If ``block`` is False it raises :class:`RateLimitExceeded` immediately.
        """
        if limit == 0:
            # 0 means unlimited
            return

        now = time.time()
        window_start = now - window
        member = uuid.uuid4().hex

        pipe = self._redis.pipeline()
        pipe.zremrangebyscore(key, "-inf", window_start)
        pipe.zcard(key)
        pipe.zadd(key, {member: now})
        pipe.expire(key, window + 10)  # small grace period
        _, current_count, *_ = pipe.execute()

        if current_count >= limit:
            # Remove the member we just added since the call won't proceed
            self._redis.zrem(key, member)
            if not block:
                raise RateLimitExceeded(key, limit, window)
            # Sleep until the oldest entry expires and retry
            oldest_score = self._redis.zrange(key, 0, 0, withscores=True)
            if oldest_score:
                sleep_for = max(0.0, (oldest_score[0][1] + window) - time.time() + 0.1)
                logger.info("Rate limit hit on %s — sleeping %.2fs", key, sleep_for)
                time.sleep(sleep_for)
            # Re-attempt after the sleep
            self._check(key, limit, window, block=block)

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def check_user(self, user_id: str, *, block: bool = True) -> None:
        """Record + check one call for *user_id*."""
        self._check(
            f"ratelimit:user:{user_id}",
            self.user_limit,
            self.user_window,
            block=block,
        )

    def check_agent(self, agent_type: str, provider: str, *, block: bool = True) -> None:
        """Record + check one call for *agent_type* + *provider* combination."""
        self._check(
            f"ratelimit:agent:{agent_type}:{provider}",
            self.agent_limit,
            self.agent_window,
            block=block,
        )

    def check_all(self, user_id: str, agent_type: str, provider: str, *, block: bool = True) -> None:
        """Convenience: check both user and agent limits in one call."""
        self.check_user(user_id, block=block)
        self.check_agent(agent_type, provider, block=block)


# Module-level singleton — constructed lazily so tests can monkey-patch
_limiter: RedisRateLimiter | None = None


def get_rate_limiter() -> RedisRateLimiter:
    """Return the module-level :class:`RedisRateLimiter` singleton."""
    global _limiter
    if _limiter is None:
        _limiter = RedisRateLimiter()
    return _limiter
