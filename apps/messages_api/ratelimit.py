"""Per-API-key sliding-window rate limit.

Backed by django.core.cache so a single-process LocMem default works in tests,
and operators can swap to Redis with one settings change. The window math is
deliberately approximate — Resend's published limit (5/s/team) is more about
shielding the upstream than enforcing fairness, and the cost of an extra
allowed send is much lower than blocking a legitimate burst.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache

DEFAULT_PER_SECOND = 10
DEFAULT_PER_MINUTE = 600


@dataclass(slots=True)
class RateLimitResult:
    allowed: bool
    retry_after: int  # seconds; 0 when allowed
    remaining: int  # remaining in the current 1s window
    limit: int  # 1s limit


def _key(api_key_id: int, bucket: str) -> str:
    return f"mr:rl:{api_key_id}:{bucket}"


def _get_limits() -> tuple[int, int]:
    per_s = int(getattr(settings, "API_RATE_LIMIT_PER_SECOND", DEFAULT_PER_SECOND))
    per_m = int(getattr(settings, "API_RATE_LIMIT_PER_MINUTE", DEFAULT_PER_MINUTE))
    return per_s, per_m


def check(api_key_id: int) -> RateLimitResult:
    """Check + increment the per-key rate limit windows."""
    per_s, per_m = _get_limits()
    now = int(time.time())
    sec_key = _key(api_key_id, f"s{now}")
    min_key = _key(api_key_id, f"m{now // 60}")

    try:
        cache.add(sec_key, 0, timeout=2)
        cache.add(min_key, 0, timeout=120)
        n_sec = cache.incr(sec_key)
        n_min = cache.incr(min_key)
    except ValueError:
        # Some cache backends raise if the key was evicted between add/incr.
        # Treat that as a fresh window — better to allow than to false-429.
        n_sec, n_min = 1, 1

    if n_sec > per_s:
        return RateLimitResult(allowed=False, retry_after=1, remaining=0, limit=per_s)
    if n_min > per_m:
        return RateLimitResult(allowed=False, retry_after=60 - (now % 60), remaining=0, limit=per_s)
    return RateLimitResult(
        allowed=True,
        retry_after=0,
        remaining=max(0, per_s - n_sec),
        limit=per_s,
    )
