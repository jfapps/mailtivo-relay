from __future__ import annotations

import importlib

import pytest
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.middleware import SessionMiddleware
from django.core import mail
from django.http import Http404
from django.test import RequestFactory, override_settings
from django.urls import clear_url_caches, reverse

from apps.accounts.models import MagicLinkToken, User, WorkspaceSettings
from apps.accounts.views import dev_login


@pytest.fixture
def client_db(db, client):
    return client


def _dev_login_request(path: str = "/dev-login/", **params: str):
    """Build a GET request wired with the session + message storage that
    ``login()`` and ``messages.error()`` rely on."""
    request = RequestFactory().get(path, params)
    SessionMiddleware(lambda r: None).process_request(request)
    request.session.save()
    request._messages = FallbackStorage(request)
    return request


@pytest.fixture
def debug_urls():
    """Re-import the URLconf under DEBUG=True so the dev-login route registers."""
    with override_settings(DEBUG=True):
        clear_url_caches()
        import apps.accounts.urls as accounts_urls
        import mailtivo_relay.urls as root_urls

        importlib.reload(accounts_urls)
        importlib.reload(root_urls)
        yield
    clear_url_caches()
    importlib.reload(importlib.import_module("apps.accounts.urls"))
    importlib.reload(importlib.import_module("mailtivo_relay.urls"))


@pytest.mark.django_db
def test_root_redirects_to_onboarding_when_no_users(client_db):
    # `/` -> `/app/` (302) -> `/login/` (302) -> `/onboarding/` (302) when no owners yet.
    r = client_db.get("/", follow=True)
    assert r.status_code == 200
    assert b"Get Mailtivo-Relay running" in r.content


@pytest.mark.django_db
def test_onboarding_creates_owner_and_workspace(client_db):
    r = client_db.post(
        reverse("accounts:onboarding"),
        {
            "workspace_name": "Acme Mail",
            "email": "owner@acme.test",
            "display_name": "Owner",
            "password": "ChangeMe-12345!",
            "password_confirm": "ChangeMe-12345!",
        },
    )
    assert r.status_code == 302
    assert r["Location"] == "/app/"

    owner = User.objects.get(email="owner@acme.test")
    assert owner.is_workspace_admin is True
    assert owner.is_staff is True
    assert owner.check_password("ChangeMe-12345!")

    ws = WorkspaceSettings.load()
    assert ws.name == "Acme Mail"


@pytest.mark.django_db
def test_onboarding_blocked_once_owner_exists(client_db):
    User.objects.create_user(
        email="owner@acme.test", password="ChangeMe-12345!", is_workspace_admin=True
    )
    r = client_db.get(reverse("accounts:onboarding"))
    assert r.status_code == 302
    assert r["Location"].endswith("/login/")


@pytest.mark.django_db
def test_login_with_password(client_db):
    User.objects.create_user(
        email="owner@acme.test", password="ChangeMe-12345!", is_workspace_admin=True
    )
    r = client_db.post(
        reverse("accounts:login"),
        {"email": "owner@acme.test", "password": "ChangeMe-12345!"},
    )
    assert r.status_code == 302
    assert r["Location"] == "/app/"


@pytest.mark.django_db
def test_login_rejects_bad_password(client_db):
    User.objects.create_user(
        email="owner@acme.test", password="ChangeMe-12345!", is_workspace_admin=True
    )
    r = client_db.post(
        reverse("accounts:login"),
        {"email": "owner@acme.test", "password": "wrong"},
    )
    assert r.status_code == 200
    assert b"Invalid email or password" in r.content


@pytest.mark.django_db
def test_magic_link_full_loop(client_db):
    user = User.objects.create_user(
        email="owner@acme.test", password="ChangeMe-12345!", is_workspace_admin=True
    )

    # Request the link.
    mail.outbox.clear()
    r = client_db.post(reverse("accounts:magic_link_request"), {"email": user.email})
    assert r.status_code == 302
    assert len(mail.outbox) == 1
    body = mail.outbox[0].body
    # The plain-text message contains the absolute URL.
    assert "/login/magic/" in body

    raw_token = body.split("/login/magic/")[1].split("/")[0].rstrip()
    assert MagicLinkToken.objects.filter(user=user, used_at__isnull=True).count() == 1

    # Consume the link.
    r = client_db.get(reverse("accounts:magic_link_consume", args=[raw_token]))
    assert r.status_code == 302
    assert r["Location"] == "/app/"
    assert MagicLinkToken.objects.get(user=user).used_at is not None

    # Replaying the same link is rejected.
    r = client_db.get(reverse("accounts:magic_link_consume", args=[raw_token]))
    assert r.status_code == 302
    assert "/login/" in r["Location"]


@pytest.mark.django_db
def test_magic_link_request_for_unknown_email_does_not_leak(client_db):
    mail.outbox.clear()
    r = client_db.post(
        reverse("accounts:magic_link_request"), {"email": "nobody@nowhere.test"}
    )
    assert r.status_code == 302
    # No mail sent for unknown users, but UI shows the same generic message.
    assert mail.outbox == []


@pytest.mark.django_db
def test_app_requires_login(client_db):
    User.objects.create_user(
        email="owner@acme.test", password="ChangeMe-12345!", is_workspace_admin=True
    )
    r = client_db.get("/app/")
    assert r.status_code == 302
    assert "/login/" in r["Location"]


@pytest.mark.django_db
def test_app_dashboard_loads_when_signed_in(client_db):
    User.objects.create_user(
        email="owner@acme.test", password="ChangeMe-12345!", is_workspace_admin=True
    )
    client_db.login(username="owner@acme.test", password="ChangeMe-12345!")
    r = client_db.get("/app/")
    assert r.status_code == 200
    assert b"Dashboard" in r.content


@pytest.mark.django_db
@override_settings(DEBUG=False)
def test_dev_login_404s_when_not_debug():
    # Even if the request reaches the view, it must refuse outside DEBUG.
    with pytest.raises(Http404):
        dev_login(_dev_login_request())


@pytest.mark.django_db
def test_dev_login_route_absent_when_not_debug(client_db):
    # With DEBUG off (the test default) the route isn't even registered.
    assert client_db.get("/dev-login/").status_code == 404


@pytest.mark.django_db
@override_settings(DEBUG=True)
def test_dev_login_signs_in_first_admin():
    admin = User.objects.create_user(
        email="owner@acme.test", password="ChangeMe-12345!", is_workspace_admin=True
    )
    request = _dev_login_request()
    resp = dev_login(request)
    assert resp.status_code == 302
    assert resp["Location"] == "/app/"
    assert request.session["_auth_user_id"] == str(admin.pk)


@pytest.mark.django_db
@override_settings(DEBUG=True)
def test_dev_login_impersonates_by_email():
    User.objects.create_user(
        email="owner@acme.test", password="ChangeMe-12345!", is_workspace_admin=True
    )
    member = User.objects.create_user(email="member@acme.test", password="ChangeMe-12345!")
    request = _dev_login_request(email="member@acme.test")
    resp = dev_login(request)
    assert resp.status_code == 302
    assert request.session["_auth_user_id"] == str(member.pk)


@pytest.mark.django_db
@override_settings(DEBUG=True)
def test_dev_login_redirects_to_login_when_no_user():
    request = _dev_login_request()
    resp = dev_login(request)
    assert resp.status_code == 302
    assert resp["Location"].endswith("/login/")


@pytest.mark.django_db
def test_dev_login_route_resolves_end_to_end(client_db, debug_urls):
    admin = User.objects.create_user(
        email="owner@acme.test", password="ChangeMe-12345!", is_workspace_admin=True
    )
    r = client_db.get("/dev-login/")
    assert r.status_code == 302
    assert r["Location"] == "/app/"
    assert client_db.session["_auth_user_id"] == str(admin.pk)
