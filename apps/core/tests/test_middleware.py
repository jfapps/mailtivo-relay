from __future__ import annotations

import pytest
from django.core.cache import cache
from django.urls import reverse

from apps.accounts.models import User


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.mark.django_db
def test_security_headers_on_panel_pages(client):
    user = User.objects.create_user(email="o@a.test", password="ChangeMe-12345!", is_workspace_admin=True)
    client.force_login(user)
    r = client.get(reverse("panel:dashboard"))
    assert r.status_code == 200
    csp = r["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert r["Referrer-Policy"] == "same-origin"
    assert r["X-Content-Type-Options"] == "nosniff"
    assert "camera=()" in r["Permissions-Policy"]


@pytest.mark.django_db
def test_security_headers_skipped_for_api(client):
    """API responses are machine-consumed JSON; CSP is irrelevant and just
    adds noise to integration test fixtures."""
    r = client.post(reverse("messages_api:emails_create"), data="{}", content_type="application/json")
    assert r.status_code == 401
    assert "Content-Security-Policy" not in r


@pytest.mark.django_db
def test_login_lockout_blocks_after_threshold(client):
    User.objects.create_user(email="real@a.test", password="ChangeMe-12345!", is_workspace_admin=True)
    from apps.core.middleware import LOCKOUT_FAILURES

    for _ in range(LOCKOUT_FAILURES):
        r = client.post(reverse("accounts:login"), {"email": "real@a.test", "password": "wrong"})
        # The form renders the page with errors (200) — failures get registered.
        assert r.status_code in (200, 403)

    # Next attempt must be locked out.
    r = client.post(reverse("accounts:login"), {"email": "real@a.test", "password": "wrong"})
    assert r.status_code == 403
    assert "Retry-After" in r


@pytest.mark.django_db
def test_login_lockout_clears_on_success(client):
    User.objects.create_user(email="real@a.test", password="ChangeMe-12345!", is_workspace_admin=True)

    # 3 failures, then a success.
    for _ in range(3):
        client.post(reverse("accounts:login"), {"email": "real@a.test", "password": "wrong"})
    r = client.post(
        reverse("accounts:login"),
        {"email": "real@a.test", "password": "ChangeMe-12345!"},
    )
    # Should redirect to /app/ on success.
    assert r.status_code in (200, 302)

    # After a successful login, the failure counter must be zero, so 10+ bad
    # passwords in a row should still be allowed (and not 403).
    from apps.core.middleware import login_lockout_count

    # session-bound; check via cache helper.
    assert login_lockout_count("127.0.0.1") == 0


@pytest.mark.django_db
def test_healthz_returns_ok(client):
    r = client.get("/healthz/")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


@pytest.mark.django_db
def test_healthz_does_not_require_login(client):
    """Critical: container healthchecks aren't authenticated."""
    r = client.get("/healthz/")
    assert r.status_code == 200
