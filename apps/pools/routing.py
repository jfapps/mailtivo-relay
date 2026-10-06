"""Pool router. Picks the next PoolMember for a delivery attempt.

Strategies:
  - weighted     : Random weighted choice (weights act as probabilities).
  - failover     : Order by priority asc, then by id. Always try the lowest-priority
                   reachable member first; only fall through on failure.
  - round_robin  : Rotate through enabled members using Pool.rr_cursor.

A health filter is layered on top of all three strategies. A member is "skippable"
when:
  - member.enabled is False
  - the connection is disabled, marked down, or inside its skip_until cool-down
  - the member is at its daily cap (per-connection cap or warmup cap)
  - the member id is in `exclude` (already tried this attempt)
"""
from __future__ import annotations

import random
from collections.abc import Iterable
from dataclasses import dataclass

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .models import Pool, PoolMember


class NoMemberAvailable(Exception):
    """Raised when the router cannot find a usable pool member."""


@dataclass(slots=True)
class RoutingContext:
    """Optional caller-supplied context. `attempt` is 0 for the first try, then 1, 2, ..."""

    attempt: int = 0
    exclude_member_ids: frozenset[int] = frozenset()


def _is_healthy(member: PoolMember, *, health_skip_enabled: bool) -> bool:
    if not member.enabled:
        return False
    conn = member.connection
    if not conn.enabled:
        return False
    if health_skip_enabled:
        if conn.status == conn.STATUS_DOWN:
            return False
        if conn.skip_until and conn.skip_until > timezone.now():
            return False
    if member.at_cap():
        return False
    return True


def _candidates(pool: Pool, exclude: Iterable[int]) -> list[PoolMember]:
    excluded = set(exclude)
    out: list[PoolMember] = []
    for m in pool.members.select_related("connection").all():
        if m.id in excluded:
            continue
        if not _is_healthy(m, health_skip_enabled=pool.health_skip_enabled):
            continue
        out.append(m)
    return out


def _pick_weighted(members: list[PoolMember]) -> PoolMember:
    weights = [max(int(m.weight), 0) for m in members]
    total = sum(weights)
    if total == 0:
        return members[0]
    r = random.uniform(0, total)
    upto = 0.0
    for m, w in zip(members, weights, strict=True):
        if w == 0:
            continue
        upto += w
        if r <= upto:
            return m
    return members[-1]


def _pick_failover(members: list[PoolMember]) -> PoolMember:
    return min(members, key=lambda m: (m.priority, m.id))


def _pick_round_robin(pool: Pool, members: list[PoolMember]) -> PoolMember:
    """Advance the pool's rr_cursor across the *full* member list, not just the
    healthy subset, so a degraded slot still consumes its turn (skipped, not
    starved). After picking, persist the advanced cursor."""
    full = list(pool.members.order_by("priority", "id").values_list("id", flat=True))
    if not full:
        raise NoMemberAvailable("pool is empty")
    healthy_ids = {m.id: m for m in members}
    n = len(full)
    start = pool.rr_cursor % n
    for offset in range(n):
        idx = (start + offset) % n
        member_id = full[idx]
        if member_id in healthy_ids:
            with transaction.atomic():
                Pool.objects.filter(pk=pool.pk).update(rr_cursor=(idx + 1) % n)
                pool.rr_cursor = (idx + 1) % n
            return healthy_ids[member_id]
    raise NoMemberAvailable("no healthy member in round-robin pass")


def select_member(
    pool: Pool,
    *,
    attempt: int = 0,
    exclude: Iterable[int] = (),
) -> PoolMember:
    """Pick the next PoolMember for `pool`. Raise NoMemberAvailable if none qualify."""
    candidates = _candidates(pool, exclude)
    if not candidates:
        raise NoMemberAvailable(f"no usable member in pool '{pool.name}' (attempt={attempt})")

    strategy = pool.routing_strategy
    if strategy == Pool.STRATEGY_WEIGHTED:
        return _pick_weighted(candidates)
    if strategy == Pool.STRATEGY_FAILOVER:
        return _pick_failover(candidates)
    if strategy == Pool.STRATEGY_ROUND_ROBIN:
        return _pick_round_robin(pool, candidates)
    raise NoMemberAvailable(f"unknown routing strategy: {strategy}")


def record_send(member: PoolMember) -> None:
    """Increment daily counters atomically. Call after a successful upstream send."""
    member.reset_daily_counter_if_needed()
    PoolMember.objects.filter(pk=member.pk).update(
        daily_sent_count=F("daily_sent_count") + 1,
        daily_counter_date=member.daily_counter_date,
    )
