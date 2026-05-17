from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.connections.adapters import AdapterError, AdapterResult
from apps.connections.models import Connection
from apps.core.encryption import encrypt
from apps.messages_api.models import Message
from apps.pools.models import Pool, PoolMember
from apps.sending.tasks import send_message


def _conn(name: str) -> Connection:
    return Connection.objects.create(
        name=name,
        provider_code="resend",
        credentials_encrypted=encrypt("re_test"),
        status=Connection.STATUS_HEALTHY,
    )


@pytest.mark.django_db
def test_tick_window_resets_after_expiry():
    c = _conn("a")
    c.recent_total = 50
    c.recent_5xx_count = 30
    c.recent_window_start = timezone.now() - timedelta(seconds=120)
    c.tick_window()
    assert c.recent_total == 0
    assert c.recent_5xx_count == 0
    assert c.recent_window_start > timezone.now() - timedelta(seconds=2)


@pytest.mark.django_db
def test_tick_window_keeps_running_counts_inside_window():
    c = _conn("a")
    c.recent_total = 10
    c.recent_5xx_count = 5
    c.recent_window_start = timezone.now() - timedelta(seconds=10)
    c.tick_window()
    assert c.recent_total == 10
    assert c.recent_5xx_count == 5


@pytest.mark.django_db
def test_error_rate_pct():
    c = _conn("a")
    assert c.error_rate_pct() == 0.0
    c.recent_total = 4
    c.recent_5xx_count = 1
    assert c.error_rate_pct() == 25.0


@pytest.mark.django_db
def test_5xx_sets_skip_until_and_degrades():
    pool = Pool.objects.create(name="p", routing_strategy=Pool.STRATEGY_FAILOVER)
    primary = PoolMember.objects.create(pool=pool, connection=_conn("primary"), priority=0)
    backup = PoolMember.objects.create(pool=pool, connection=_conn("backup"), priority=10)

    msg = Message(pool=pool, from_address="a@x.test", to=["b@y.test"], subject="x")
    msg.set_body(text=".")
    msg.save()

    def fake_send(self, *, message):
        if self.connection.name == "primary":
            raise AdapterError("502", kind="temporary", status_code=502)
        return AdapterResult(provider_message_id="ok", raw_response={})

    with patch("apps.connections.adapters.resend.ResendAdapter.send", new=fake_send):
        status = send_message(msg.id)
    assert status == Message.STATUS_SENT

    primary.connection.refresh_from_db()
    assert primary.connection.status == Connection.STATUS_DEGRADED
    assert primary.connection.skip_until is not None
    assert primary.connection.recent_5xx_count >= 1


@pytest.mark.django_db
def test_success_after_failure_clears_skip_until():
    pool = Pool.objects.create(name="p")
    only = PoolMember.objects.create(pool=pool, connection=_conn("only"), priority=0)
    only.connection.skip_until = timezone.now() + timedelta(minutes=5)
    only.connection.status = Connection.STATUS_DEGRADED
    only.connection.save(update_fields=["skip_until", "status"])

    msg = Message(pool=pool, from_address="a@x.test", to=["b@y.test"], subject="x")
    msg.set_body(text=".")
    msg.save()

    # Bypass routing's skip-until filter by disabling health-skip for this pool.
    pool.health_skip_enabled = False
    pool.save(update_fields=["health_skip_enabled"])

    with patch("apps.connections.adapters.resend.ResendAdapter.send") as send:
        send.return_value = AdapterResult(provider_message_id="ok", raw_response={})
        assert send_message(msg.id) == Message.STATUS_SENT

    only.connection.refresh_from_db()
    assert only.connection.skip_until is None
    assert only.connection.status == Connection.STATUS_HEALTHY
