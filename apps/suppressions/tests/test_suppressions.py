from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.api_keys.models import APIKey
from apps.connections.models import Connection
from apps.core.encryption import encrypt
from apps.messages_api.models import Message
from apps.pools.models import Pool, PoolMember
from apps.suppressions.models import Suppression


@pytest.fixture
def owner(db):
    return User.objects.create_user(email="o@a.test", password="ChangeMe-12345!", is_workspace_admin=True)


@pytest.fixture
def admin_client(client, owner):
    client.force_login(owner)
    return client


def _make_api_setup():
    conn = Connection.objects.create(
        name="r", provider_code="resend",
        credentials_encrypted=encrypt("re_test"),
        status=Connection.STATUS_HEALTHY,
    )
    pool = Pool.objects.create(name="p", routing_strategy=Pool.STRATEGY_FAILOVER)
    PoolMember.objects.create(pool=pool, connection=conn, priority=0)
    key, secret = APIKey.issue(name="t", scopes=["*"], default_pool=pool)
    return key, secret


@pytest.mark.django_db
def test_is_suppressed_case_insensitive():
    Suppression.objects.create(email="hello@x.test")
    assert Suppression.is_suppressed("Hello@X.Test") is True
    assert Suppression.is_suppressed("other@x.test") is False


@pytest.mark.django_db
def test_add_normalizes_and_dedupes():
    s1 = Suppression.add("a@x.test")
    s2 = Suppression.add("A@X.Test")
    assert s1 == s2
    assert Suppression.objects.count() == 1


@pytest.mark.django_db
def test_add_rejects_invalid():
    assert Suppression.add("not-an-email") is None
    assert Suppression.objects.count() == 0


@pytest.mark.django_db
def test_filter_suppressed_returns_set():
    Suppression.objects.create(email="a@x.test")
    Suppression.objects.create(email="b@x.test")
    out = Suppression.filter_suppressed(["A@X.Test", "c@x.test", "b@x.test"])
    assert out == {"a@x.test", "b@x.test"}


@pytest.mark.django_db
def test_send_api_filters_suppressed_recipients(client):
    key, secret = _make_api_setup()
    Suppression.objects.create(email="bad@x.test")
    body = {
        "from": "a@x.test",
        "to": ["bad@x.test", "good@x.test"],
        "subject": "hi",
        "text": ".",
    }
    with patch("apps.sending.tasks.enqueue_message"):
        r = client.post(
            reverse("messages_api:emails_create"),
            data=json.dumps(body),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {secret}",
        )
    assert r.status_code == 200, r.content
    msg = Message.objects.get(pk=r.json()["id"])
    assert msg.to == ["good@x.test"]


@pytest.mark.django_db
def test_send_api_rejects_when_all_recipients_suppressed(client):
    _, secret = _make_api_setup()
    Suppression.objects.create(email="bad@x.test")
    r = client.post(
        reverse("messages_api:emails_create"),
        data=json.dumps({"from": "a@x.test", "to": ["bad@x.test"], "subject": "hi", "text": "."}),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {secret}",
    )
    assert r.status_code == 422
    assert r.json()["name"] == "recipient_suppressed"


@pytest.mark.django_db
def test_panel_add_creates_audit_entry(admin_client):
    from apps.audit.models import AuditLog

    r = admin_client.post(
        reverse("suppressions:list"),
        {"section": "add", "email": "manual@x.test", "reason": Suppression.REASON_MANUAL, "note": "marked"},
    )
    assert r.status_code == 302
    assert Suppression.objects.filter(email="manual@x.test").exists()
    assert AuditLog.objects.filter(action="suppression.added").exists()


@pytest.mark.django_db
def test_panel_import_csv(admin_client):
    csv = b"email\nfoo@x.test\nbar@x.test\nnot-an-email\nfoo@x.test\n"
    f = io.BytesIO(csv)
    f.name = "u.csv"
    r = admin_client.post(
        reverse("suppressions:list"),
        {"section": "import", "reason": Suppression.REASON_IMPORTED, "csv_file": f},
    )
    assert r.status_code == 302
    assert Suppression.objects.filter(email="foo@x.test").exists()
    assert Suppression.objects.filter(email="bar@x.test").exists()
    # Dedup + bad email skipped.
    assert Suppression.objects.count() == 2


@pytest.mark.django_db
def test_panel_export_csv(admin_client):
    Suppression.objects.create(email="x@y.test", reason="manual", note="why")
    r = admin_client.get(reverse("suppressions:export"))
    assert r.status_code == 200
    content = b"".join(r.streaming_content).decode()
    assert "x@y.test" in content
    assert "manual" in content


@pytest.mark.django_db
def test_remove_records_audit(admin_client):
    from apps.audit.models import AuditLog

    s = Suppression.objects.create(email="a@x.test")
    r = admin_client.post(reverse("suppressions:remove", args=[s.id]))
    assert r.status_code == 302
    assert not Suppression.objects.filter(pk=s.id).exists()
    assert AuditLog.objects.filter(action="suppression.removed", target="a@x.test").exists()
