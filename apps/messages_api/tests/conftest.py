from __future__ import annotations

import pytest

from apps.api_keys.models import APIKey
from apps.connections.models import Connection
from apps.core.encryption import encrypt
from apps.pools.models import Pool, PoolMember


@pytest.fixture
def resend_connection(db) -> Connection:
    return Connection.objects.create(
        name="resend-main",
        provider_code="resend",
        credentials_encrypted=encrypt("re_test"),
        status=Connection.STATUS_HEALTHY,
    )


@pytest.fixture
def pool(db, resend_connection) -> Pool:
    p = Pool.objects.create(name="primary", routing_strategy=Pool.STRATEGY_FAILOVER)
    PoolMember.objects.create(pool=p, connection=resend_connection, priority=0)
    return p


@pytest.fixture
def issued_key(db, pool) -> tuple[APIKey, str]:
    return APIKey.issue(name="t", scopes=["*"], default_pool=pool)


@pytest.fixture
def api_key(issued_key) -> APIKey:
    return issued_key[0]


@pytest.fixture
def bearer(issued_key) -> str:
    return f"Bearer {issued_key[1]}"
