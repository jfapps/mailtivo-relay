from __future__ import annotations

import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.api_keys.models import APIKey
from apps.audit.models import AuditLog


@pytest.fixture
def owner(db):
    return User.objects.create_user(email="o@a.test", password="ChangeMe-12345!", is_workspace_admin=True)


@pytest.fixture
def admin_client(client, owner):
    client.force_login(owner)
    return client


@pytest.mark.django_db
def test_apikey_issue_records_audit(admin_client, owner):
    admin_client.post(reverse("api_keys:list"), {"name": "k1", "scopes": "*"})
    assert AuditLog.objects.filter(action="apikey.issued", target="k1").exists()


@pytest.mark.django_db
def test_apikey_revoke_records_audit(admin_client, owner):
    key, _secret = APIKey.issue(name="k2", scopes=["*"])
    admin_client.post(reverse("api_keys:revoke", args=[key.id]))
    assert AuditLog.objects.filter(action="apikey.revoked", target="k2").exists()


@pytest.mark.django_db
def test_audit_log_list_view_filters(admin_client, owner):
    AuditLog.record(owner, action="apikey.issued", target="x")
    AuditLog.record(owner, action="pool.created", target="p")
    r = admin_client.get(reverse("audit:list") + "?action=apikey")
    assert r.status_code == 200
    assert b"apikey.issued" in r.content
    assert b"pool.created" not in r.content


@pytest.mark.django_db
def test_record_stores_actor_email_for_resilience(owner):
    log = AuditLog.record(owner, action="t.x", target="y")
    assert log.actor_email == owner.email
    owner.delete()
    log.refresh_from_db()
    assert log.actor is None
    assert log.actor_email == "o@a.test"
