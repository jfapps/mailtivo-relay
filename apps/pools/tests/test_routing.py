from __future__ import annotations

import random
from collections import Counter
from datetime import date, timedelta

import pytest
from django.utils import timezone

from apps.connections.models import Connection
from apps.core.encryption import encrypt
from apps.pools.models import Pool, PoolMember, WarmupPlan
from apps.pools.routing import NoMemberAvailable, record_send, select_member


def _conn(name: str, *, status: str = Connection.STATUS_HEALTHY, enabled: bool = True, daily_cap: int = 0) -> Connection:
    return Connection.objects.create(
        name=name,
        provider_code="resend",
        credentials_encrypted=encrypt("re_test"),
        status=status,
        enabled=enabled,
        daily_cap=daily_cap,
    )


def _pool(strategy: str = Pool.STRATEGY_WEIGHTED, *, health_skip: bool = True) -> Pool:
    return Pool.objects.create(
        name=f"p-{strategy}-{random.randint(0, 1_000_000)}",
        routing_strategy=strategy,
        health_skip_enabled=health_skip,
    )


@pytest.mark.django_db
def test_select_weighted_picks_proportionally():
    random.seed(42)
    pool = _pool(Pool.STRATEGY_WEIGHTED)
    PoolMember.objects.create(pool=pool, connection=_conn("a"), weight=1)
    PoolMember.objects.create(pool=pool, connection=_conn("b"), weight=9)
    picks: Counter[str] = Counter()
    for _ in range(2000):
        m = select_member(pool)
        picks[m.connection.name] += 1
    # Expect roughly 10:90 ratio; assert within wide tolerance.
    assert 0.05 < picks["a"] / 2000 < 0.20
    assert 0.80 < picks["b"] / 2000 < 0.95


@pytest.mark.django_db
def test_select_weighted_zero_weight_is_skipped():
    random.seed(0)
    pool = _pool(Pool.STRATEGY_WEIGHTED)
    PoolMember.objects.create(pool=pool, connection=_conn("a"), weight=0)
    PoolMember.objects.create(pool=pool, connection=_conn("b"), weight=5)
    for _ in range(50):
        assert select_member(pool).connection.name == "b"


@pytest.mark.django_db
def test_select_failover_prefers_lowest_priority():
    pool = _pool(Pool.STRATEGY_FAILOVER)
    PoolMember.objects.create(pool=pool, connection=_conn("primary"), priority=0)
    PoolMember.objects.create(pool=pool, connection=_conn("backup"), priority=10)
    assert select_member(pool).connection.name == "primary"
    # Excluding primary falls over to backup.
    primary_id = pool.members.get(connection__name="primary").id
    assert select_member(pool, exclude=[primary_id]).connection.name == "backup"


@pytest.mark.django_db
def test_select_round_robin_rotates():
    pool = _pool(Pool.STRATEGY_ROUND_ROBIN)
    PoolMember.objects.create(pool=pool, connection=_conn("a"))
    PoolMember.objects.create(pool=pool, connection=_conn("b"))
    PoolMember.objects.create(pool=pool, connection=_conn("c"))
    names = [select_member(pool).connection.name for _ in range(6)]
    assert names == ["a", "b", "c", "a", "b", "c"]


@pytest.mark.django_db
def test_health_skip_filters_down_connections():
    pool = _pool(Pool.STRATEGY_FAILOVER)
    PoolMember.objects.create(pool=pool, connection=_conn("primary", status=Connection.STATUS_DOWN), priority=0)
    backup = PoolMember.objects.create(pool=pool, connection=_conn("backup"), priority=5)
    assert select_member(pool).id == backup.id


@pytest.mark.django_db
def test_health_skip_respects_skip_until_cooldown():
    pool = _pool(Pool.STRATEGY_FAILOVER)
    cool = _conn("cool")
    cool.skip_until = timezone.now() + timedelta(minutes=2)
    cool.save(update_fields=["skip_until"])
    PoolMember.objects.create(pool=pool, connection=cool, priority=0)
    backup = PoolMember.objects.create(pool=pool, connection=_conn("backup"), priority=1)
    assert select_member(pool).id == backup.id


@pytest.mark.django_db
def test_health_skip_disabled_returns_down_connection():
    pool = _pool(Pool.STRATEGY_FAILOVER, health_skip=False)
    primary = PoolMember.objects.create(pool=pool, connection=_conn("p", status=Connection.STATUS_DOWN), priority=0)
    PoolMember.objects.create(pool=pool, connection=_conn("b"), priority=10)
    assert select_member(pool).id == primary.id


@pytest.mark.django_db
def test_no_member_available_raises():
    pool = _pool(Pool.STRATEGY_FAILOVER)
    PoolMember.objects.create(pool=pool, connection=_conn("only", status=Connection.STATUS_DOWN))
    with pytest.raises(NoMemberAvailable):
        select_member(pool)


@pytest.mark.django_db
def test_daily_cap_skips_capped_members():
    pool = _pool(Pool.STRATEGY_FAILOVER)
    m = PoolMember.objects.create(pool=pool, connection=_conn("capped", daily_cap=2), priority=0)
    PoolMember.objects.create(pool=pool, connection=_conn("free"), priority=10)
    # Two sends fill the cap; third routes to backup.
    record_send(m)
    record_send(m)
    m.refresh_from_db()
    assert m.daily_sent_count == 2
    assert select_member(pool).connection.name == "free"


@pytest.mark.django_db
def test_warmup_plan_applies_daily_cap():
    pool = _pool(Pool.STRATEGY_FAILOVER)
    plan = WarmupPlan.objects.create(
        pool=pool,
        curve=WarmupPlan.CURVE_CUSTOM,
        start_date=date.today(),
        daily_caps_json=[1, 5, 20],
        enabled=True,
    )
    assert plan.cap_for(date.today()) == 1
    assert plan.cap_for(date.today() + timedelta(days=1)) == 5
    assert plan.cap_for(date.today() + timedelta(days=10)) == 0  # off the curve = unlimited

    m = PoolMember.objects.create(pool=pool, connection=_conn("m1"))
    # First send fills today's warmup cap of 1; member is no longer usable today.
    record_send(m)
    m.refresh_from_db()
    assert m.at_cap()
    with pytest.raises(NoMemberAvailable):
        select_member(pool)
