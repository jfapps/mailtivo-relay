from __future__ import annotations

from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.connections.adapters import PERMANENT_FAILURE, AdapterError, AdapterResult
from apps.connections.models import Connection
from apps.core.encryption import encrypt
from apps.messages_api.models import Message
from apps.pools.models import Pool, PoolMember


@pytest.fixture
def owner(db):
    return User.objects.create_user(
        email="owner@acme.test", password="ChangeMe-12345!", is_workspace_admin=True
    )


@pytest.fixture
def auth_client(client, owner):
    client.force_login(owner)
    return client


@pytest.fixture
def pool(db) -> Pool:
    p = Pool.objects.create(name="primary", routing_strategy=Pool.STRATEGY_FAILOVER)
    conn = Connection.objects.create(
        name="resend-main",
        provider_code="resend",
        credentials_encrypted=encrypt("re_test"),
        status=Connection.STATUS_HEALTHY,
    )
    PoolMember.objects.create(pool=p, connection=conn, priority=0)
    return p


@pytest.mark.django_db
def test_tools_index_renders(auth_client):
    r = auth_client.get(reverse("tools:index"))
    assert r.status_code == 200
    assert b"Test send" in r.content


@pytest.mark.django_db
def test_tools_require_login(client):
    r = client.get(reverse("tools:index"))
    assert r.status_code == 302
    assert "/login/" in r["Location"]


@pytest.mark.django_db
def test_test_send_page_prefills_user_email(auth_client, owner, pool):
    r = auth_client.get(reverse("tools:test_send"))
    assert r.status_code == 200
    assert owner.email.encode() in r.content


@pytest.mark.django_db
def test_test_send_without_pool_warns(auth_client):
    r = auth_client.get(reverse("tools:test_send"))
    assert r.status_code == 200
    assert b"Create a pool" in r.content


@pytest.mark.django_db
def test_test_send_success(auth_client, pool):
    with patch("apps.connections.adapters.resend.ResendAdapter.send") as mock_send:
        mock_send.return_value = AdapterResult(provider_message_id="resend-xyz")
        r = auth_client.post(
            reverse("tools:test_send"),
            {
                "pool": pool.pk,
                "from_address": "Acme <hi@acme.test>",
                "to_address": "dest@example.test",
                "subject": "Hi there",
                "body": "Test body line one.\nLine two.",
            },
        )

    assert r.status_code == 200
    assert b"Sent" in r.content

    message = Message.objects.get()
    assert message.status == Message.STATUS_SENT
    assert message.to == ["dest@example.test"]
    assert message.from_address == "Acme <hi@acme.test>"
    assert message.provider_message_id == "resend-xyz"
    # Body stored for both parts.
    body = message.body()
    assert "Test body line one." in body["text"]
    assert "Test body line one." in body["html"]


@pytest.mark.django_db
def test_test_send_records_audit_log(auth_client, pool):
    with patch("apps.connections.adapters.resend.ResendAdapter.send") as mock_send:
        mock_send.return_value = AdapterResult(provider_message_id="resend-xyz")
        auth_client.post(
            reverse("tools:test_send"),
            {
                "pool": pool.pk,
                "from_address": "hi@acme.test",
                "to_address": "dest@example.test",
                "subject": "Hi",
                "body": "body",
            },
        )
    entry = AuditLog.objects.get(action="tools.test_send")
    assert entry.target == "dest@example.test"
    assert entry.detail["status"] == Message.STATUS_SENT


@pytest.mark.django_db
def test_test_send_surfaces_provider_failure(auth_client, pool):
    def fake_send(self, *, message):
        raise AdapterError("bad sender domain", kind=PERMANENT_FAILURE, status_code=422)

    with patch("apps.connections.adapters.resend.ResendAdapter.send", new=fake_send):
        r = auth_client.post(
            reverse("tools:test_send"),
            {
                "pool": pool.pk,
                "from_address": "hi@acme.test",
                "to_address": "dest@example.test",
                "subject": "Hi",
                "body": "body",
            },
        )

    assert r.status_code == 200
    assert b"bad sender domain" in r.content
    assert Message.objects.get().status == Message.STATUS_FAILED


@pytest.mark.django_db
def test_test_send_rejects_invalid_email(auth_client, pool):
    r = auth_client.post(
        reverse("tools:test_send"),
        {
            "pool": pool.pk,
            "from_address": "hi@acme.test",
            "to_address": "not-an-email",
            "subject": "Hi",
            "body": "body",
        },
    )
    assert r.status_code == 200
    assert b"valid to email address" in r.content
    # No message should have been created.
    assert not Message.objects.exists()
