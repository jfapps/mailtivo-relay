from __future__ import annotations

import pytest
from django.core import mail
from django.urls import reverse

from apps.accounts.models import MagicLinkToken, User, WorkspaceSettings


@pytest.fixture
def client_db(db, client):
    return client


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
