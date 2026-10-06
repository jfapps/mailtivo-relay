from __future__ import annotations

from unittest.mock import patch

import pytest

from apps.connections.adapters import PERMANENT_FAILURE, TEMPORARY_FAILURE, AdapterError, AdapterResult
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


def _message(pool: Pool) -> Message:
    msg = Message(
        pool=pool,
        from_address="a@x.test",
        to=["b@y.test"],
        subject="hi",
    )
    msg.set_body(text="hello")
    msg.save()
    return msg


@pytest.fixture
def pool_with_two_members(db) -> tuple[Pool, PoolMember, PoolMember]:
    p = Pool.objects.create(name="p", routing_strategy=Pool.STRATEGY_FAILOVER)
    primary = PoolMember.objects.create(pool=p, connection=_conn("primary"), priority=0)
    backup = PoolMember.objects.create(pool=p, connection=_conn("backup"), priority=10)
    return p, primary, backup


@pytest.mark.django_db
def test_send_message_happy_path(pool_with_two_members):
    pool, primary, _ = pool_with_two_members
    msg = _message(pool)

    with patch("apps.connections.adapters.resend.ResendAdapter.send") as mock_send:
        mock_send.return_value = AdapterResult(
            provider_message_id="resend-123", recipient_tokens={}, raw_response={}
        )
        status = send_message(msg.id)

    assert status == Message.STATUS_SENT
    msg.refresh_from_db()
    assert msg.connection_id == primary.connection_id
    assert msg.provider_message_id == "resend-123"
    assert msg.sent_at is not None
    assert msg.attempts and msg.attempts[-1]["ok"] is True
    # Pool member daily counter incremented.
    primary.refresh_from_db()
    assert primary.daily_sent_count == 1


@pytest.mark.django_db
def test_send_message_falls_through_on_temporary_failure(pool_with_two_members):
    pool, primary, backup = pool_with_two_members
    msg = _message(pool)

    calls = []

    def fake_send(self, *, message):
        calls.append(self.connection.name)
        if self.connection.name == "primary":
            raise AdapterError("upstream 503", kind=TEMPORARY_FAILURE, status_code=503)
        return AdapterResult(provider_message_id="b-1", raw_response={})

    with patch("apps.connections.adapters.resend.ResendAdapter.send", new=fake_send):
        status = send_message(msg.id)

    assert status == Message.STATUS_SENT
    assert calls == ["primary", "backup"]
    msg.refresh_from_db()
    assert msg.connection_id == backup.connection_id
    assert len(msg.attempts) == 2
    assert msg.attempts[0]["ok"] is False
    assert msg.attempts[1]["ok"] is True
    primary.connection.refresh_from_db()
    # Primary got a 503 → skip_until set + status degraded.
    assert primary.connection.status == Connection.STATUS_DEGRADED
    assert primary.connection.skip_until is not None


@pytest.mark.django_db
def test_send_message_permanent_failure_does_not_retry(pool_with_two_members):
    pool, _, _ = pool_with_two_members
    msg = _message(pool)

    call_count = 0

    def fake_send(self, *, message):
        nonlocal call_count
        call_count += 1
        raise AdapterError("bad request", kind=PERMANENT_FAILURE, status_code=422)

    with patch("apps.connections.adapters.resend.ResendAdapter.send", new=fake_send):
        status = send_message(msg.id)

    assert status == Message.STATUS_FAILED
    # Should not try the backup — permanent errors are body-level.
    assert call_count == 1
    msg.refresh_from_db()
    assert msg.last_error
    assert "bad request" in msg.last_error


@pytest.mark.django_db
def test_send_message_all_members_fail(pool_with_two_members):
    pool, _, _ = pool_with_two_members
    msg = _message(pool)

    def fake_send(self, *, message):
        raise AdapterError("network", kind=TEMPORARY_FAILURE, status_code=502)

    with patch("apps.connections.adapters.resend.ResendAdapter.send", new=fake_send):
        status = send_message(msg.id)

    assert status == Message.STATUS_FAILED
    msg.refresh_from_db()
    assert len(msg.attempts) == 2
    assert all(a["ok"] is False for a in msg.attempts)


@pytest.mark.django_db
def test_send_message_no_pool_marks_failed(db):
    msg = Message(from_address="a@x", to=["b@y"], subject="x")
    msg.set_body(text=".")
    msg.save()
    assert send_message(msg.id) == Message.STATUS_FAILED
    msg.refresh_from_db()
    assert msg.last_error == "No pool assigned"


@pytest.mark.django_db
def test_send_message_missing_id_returns_missing():
    assert send_message("01ABCDEFGHIJKLMNOPQRSTUVWZ") == "missing"
