from __future__ import annotations

from unittest import mock

import pytest
from allauth.socialaccount.models import SocialApp
from django.urls import reverse

from apps.accounts.models import Invitation, User, WorkspaceSettings
from apps.core.encryption import decrypt


@pytest.fixture
def owner(db):
    return User.objects.create_user(
        email="owner@acme.test", password="ChangeMe-12345!", is_workspace_admin=True
    )


@pytest.fixture
def admin_client(client, owner):
    client.force_login(owner)
    return client


@pytest.mark.django_db
def test_dashboard_renders(admin_client):
    r = admin_client.get(reverse("panel:dashboard"))
    assert r.status_code == 200
    assert b"Dashboard" in r.content
    assert b"Get sending in three steps" in r.content


@pytest.mark.django_db
def test_settings_save_general(admin_client):
    r = admin_client.post(
        reverse("panel:settings"),
        {
            "section": "workspace",
            "ws-name": "Renamed",
            "ws-from_email_default": "hi@acme.test",
            "ws-retention_days": 14,
        },
    )
    assert r.status_code == 302
    ws = WorkspaceSettings.load()
    assert ws.name == "Renamed"
    assert ws.from_email_default == "hi@acme.test"
    assert ws.retention_days == 14


@pytest.mark.django_db
def test_settings_rejects_invalid_retention(admin_client):
    r = admin_client.post(
        reverse("panel:settings"),
        {"section": "workspace", "ws-name": "OK", "ws-from_email_default": "", "ws-retention_days": 0},
    )
    assert r.status_code == 200
    assert b"between 1 and 365" in r.content


@pytest.mark.django_db
def test_enabling_google_oauth_creates_social_app(admin_client):
    r = admin_client.post(
        reverse("panel:settings"),
        {
            "section": "google",
            "google-enabled": "on",
            "google-client_id": "1234.apps.googleusercontent.com",
            "google-client_secret": "GOCSPX-test-secret",
        },
    )
    assert r.status_code == 302
    ws = WorkspaceSettings.load()
    assert ws.google_oauth_enabled is True
    assert ws.google_oauth_client_id == "1234.apps.googleusercontent.com"
    assert decrypt(bytes(ws.google_oauth_client_secret_encrypted)) == "GOCSPX-test-secret"

    app = SocialApp.objects.get(provider="google")
    assert app.client_id == "1234.apps.googleusercontent.com"
    assert app.secret == "GOCSPX-test-secret"


@pytest.mark.django_db
def test_disabling_google_removes_social_app(admin_client):
    # Enable, then disable.
    admin_client.post(
        reverse("panel:settings"),
        {
            "section": "google",
            "google-enabled": "on",
            "google-client_id": "1234.apps.googleusercontent.com",
            "google-client_secret": "GOCSPX-test-secret",
        },
    )
    assert SocialApp.objects.filter(provider="google").exists()

    r = admin_client.post(reverse("panel:settings"), {"section": "google"})
    assert r.status_code == 302
    assert not SocialApp.objects.filter(provider="google").exists()
    assert WorkspaceSettings.load().google_oauth_enabled is False


@pytest.mark.django_db
def test_team_invite_creates_pending_invitation(admin_client, owner):
    r = admin_client.post(reverse("panel:team"), {"email": "invitee@acme.test", "is_admin": "on"})
    assert r.status_code == 302
    inv = Invitation.objects.get(email="invitee@acme.test")
    assert inv.invited_by == owner
    assert inv.is_admin is True
    assert inv.accepted_at is None
    assert inv.revoked_at is None


@pytest.mark.django_db
def test_invitation_accept_creates_user(client, owner):
    invitation, raw = Invitation.issue(email="invitee@acme.test", invited_by=owner)
    r = client.post(
        reverse("accounts:invitation_accept", args=[raw]),
        {"display_name": "Invitee", "password": "ChangeMe-12345!", "password_confirm": "ChangeMe-12345!"},
    )
    assert r.status_code == 302
    user = User.objects.get(email="invitee@acme.test")
    assert user.check_password("ChangeMe-12345!")
    assert user.display_name == "Invitee"
    invitation.refresh_from_db()
    assert invitation.accepted_by == user
    assert invitation.accepted_at is not None


@pytest.mark.django_db
def test_revoke_invitation(admin_client, owner):
    invitation, _ = Invitation.issue(email="x@acme.test", invited_by=owner)
    r = admin_client.post(reverse("panel:invitation_revoke", args=[invitation.id]))
    assert r.status_code == 302
    invitation.refresh_from_db()
    assert invitation.revoked_at is not None


@pytest.mark.django_db
def test_member_remove_blocks_self(admin_client, owner):
    r = admin_client.post(reverse("panel:member_remove", args=[owner.id]))
    assert r.status_code == 302
    owner.refresh_from_db()
    assert owner.is_active is True


@pytest.mark.django_db
def test_sidebar_shows_version(admin_client):
    from mailtivo_relay import __version__

    r = admin_client.get(reverse("panel:dashboard"))
    assert f"v{__version__}".encode() in r.content


@pytest.mark.django_db
def test_update_available_badge_shows_when_behind(admin_client):
    ws = WorkspaceSettings.load()
    ws.latest_version = "99.0.0"
    ws.save()
    r = admin_client.get(reverse("panel:dashboard"))
    assert b"Update available" in r.content


@pytest.mark.django_db
def test_settings_updates_toggle_persists(admin_client):
    r = admin_client.post(reverse("panel:settings"), {"section": "updates"})  # checkbox unchecked
    assert r.status_code == 302
    assert WorkspaceSettings.load().update_check_enabled is False

    r = admin_client.post(reverse("panel:settings"), {"section": "updates", "update_check_enabled": "on"})
    assert r.status_code == 302
    assert WorkspaceSettings.load().update_check_enabled is True


@pytest.mark.django_db
def test_settings_check_now_enqueues_task(admin_client):
    with mock.patch("django_q.tasks.async_task") as async_task:
        r = admin_client.post(
            reverse("panel:settings"),
            {"section": "updates", "update_check_enabled": "on", "check_now": "1"},
        )
    assert r.status_code == 302
    async_task.assert_called_once_with("apps.core.updates.check_for_update")


@pytest.mark.django_db
def test_non_admin_cannot_open_settings(client, owner):
    member = User.objects.create_user(email="m@acme.test", password="ChangeMe-12345!")
    client.force_login(member)
    r = client.get(reverse("panel:settings"))
    assert r.status_code == 302
    assert "/login/" in r["Location"]
