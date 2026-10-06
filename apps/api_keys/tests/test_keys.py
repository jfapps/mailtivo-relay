from __future__ import annotations

import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.api_keys.models import KEY_PREFIX, APIKey


@pytest.fixture
def owner(db):
    return User.objects.create_user(email="owner@acme.test", password="ChangeMe-12345!", is_workspace_admin=True)


@pytest.fixture
def admin_client(client, owner):
    client.force_login(owner)
    return client


@pytest.mark.django_db
def test_issue_returns_plaintext_once(admin_client):
    r = admin_client.post(reverse("api_keys:list"), {"name": "my key", "scopes": "*"})
    assert r.status_code == 200
    # The freshly issued plaintext key is rendered exactly once.
    assert KEY_PREFIX.encode() in r.content
    assert APIKey.objects.filter(name="my key").exists()


@pytest.mark.django_db
def test_authenticate_roundtrip(db):
    key, plaintext = APIKey.issue(name="t", scopes=["*"])
    assert APIKey.authenticate(plaintext) == key
    assert APIKey.authenticate("garbage") is None
    assert APIKey.authenticate(plaintext + "x") is None


@pytest.mark.django_db
def test_revoke_blocks_auth(admin_client, db):
    key, plaintext = APIKey.issue(name="t", scopes=["*"])
    r = admin_client.post(reverse("api_keys:revoke", args=[key.id]))
    assert r.status_code == 302
    assert APIKey.authenticate(plaintext) is None


@pytest.mark.django_db
def test_scope_checks():
    key, _ = APIKey.issue(name="t", scopes=["send"])
    assert key.has_scope("send")
    assert key.has_scope("read") is False

    key2, _ = APIKey.issue(name="t2", scopes=["*"])
    assert key2.has_scope("send")
    assert key2.has_scope("read")


@pytest.mark.django_db
def test_set_pool_reassigns_key(admin_client):
    from apps.pools.models import Pool

    live = Pool.objects.create(name="live", mode=Pool.MODE_LIVE)
    capture = Pool.objects.create(name="sandbox", mode=Pool.MODE_CAPTURE)
    key, _ = APIKey.issue(name="app", scopes=["*"], default_pool=live)

    r = admin_client.post(reverse("api_keys:set_pool", args=[key.id]), {"default_pool": capture.id})
    assert r.status_code == 302
    key.refresh_from_db()
    assert key.default_pool_id == capture.id


@pytest.mark.django_db
def test_set_pool_clears_to_none(admin_client):
    from apps.pools.models import Pool

    live = Pool.objects.create(name="live", mode=Pool.MODE_LIVE)
    key, _ = APIKey.issue(name="app", scopes=["*"], default_pool=live)

    r = admin_client.post(reverse("api_keys:set_pool", args=[key.id]), {"default_pool": ""})
    assert r.status_code == 302
    key.refresh_from_db()
    assert key.default_pool_id is None


@pytest.mark.django_db
def test_set_pool_404_on_revoked_key(admin_client):
    from django.utils import timezone

    from apps.pools.models import Pool

    capture = Pool.objects.create(name="sandbox", mode=Pool.MODE_CAPTURE)
    key, _ = APIKey.issue(name="app", scopes=["*"])
    key.revoked_at = timezone.now()
    key.save(update_fields=["revoked_at"])

    r = admin_client.post(reverse("api_keys:set_pool", args=[key.id]), {"default_pool": capture.id})
    assert r.status_code == 404
