from __future__ import annotations

from unittest import mock

import pytest
from django.urls import reverse

from apps.accounts.models import User, WorkspaceSettings
from apps.core.encryption import decrypt, encrypt


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
def test_integrations_page_renders(admin_client):
    r = admin_client.get(reverse("integrations:index"))
    assert r.status_code == 200
    assert b"AI provider" in r.content
    assert b"Safe Browsing" in r.content


@pytest.mark.django_db
def test_non_admin_cannot_open_integrations(client):
    member = User.objects.create_user(email="m@acme.test", password="ChangeMe-12345!")
    client.force_login(member)
    r = client.get(reverse("integrations:index"))
    assert r.status_code == 302
    assert "/login/" in r["Location"]


@pytest.mark.django_db
def test_save_ai_provider_encrypts_key(admin_client):
    r = admin_client.post(
        reverse("integrations:index"),
        {"section": "ai", "ai-provider": "openai", "ai-model": "gpt-4o-mini", "ai-api_key": "sk-secret"},
    )
    assert r.status_code == 302
    ws = WorkspaceSettings.load()
    assert ws.ai_provider == "openai"
    assert ws.ai_model == "gpt-4o-mini"
    assert decrypt(bytes(ws.ai_api_key_encrypted)) == "sk-secret"


@pytest.mark.django_db
def test_ai_provider_requires_key_on_first_save(admin_client):
    r = admin_client.post(
        reverse("integrations:index"),
        {"section": "ai", "ai-provider": "openai", "ai-model": "", "ai-api_key": ""},
    )
    assert r.status_code == 200
    assert b"API key is required" in r.content
    assert WorkspaceSettings.load().ai_provider == ""


@pytest.mark.django_db
def test_ai_key_preserved_when_blank_on_edit(admin_client):
    admin_client.post(
        reverse("integrations:index"),
        {"section": "ai", "ai-provider": "openai", "ai-api_key": "sk-first"},
    )
    # Re-save with a different model, blank key — key must be kept.
    admin_client.post(
        reverse("integrations:index"),
        {"section": "ai", "ai-provider": "openai", "ai-model": "gpt-4o", "ai-api_key": ""},
    )
    ws = WorkspaceSettings.load()
    assert ws.ai_model == "gpt-4o"
    assert decrypt(bytes(ws.ai_api_key_encrypted)) == "sk-first"


@pytest.mark.django_db
def test_save_safe_browsing_encrypts_key(admin_client):
    r = admin_client.post(
        reverse("integrations:index"),
        {"section": "safe_browsing", "sb-enabled": "on", "sb-api_key": "AIza-secret"},
    )
    assert r.status_code == 302
    ws = WorkspaceSettings.load()
    assert ws.safe_browsing_enabled is True
    assert decrypt(bytes(ws.safe_browsing_api_key_encrypted)) == "AIza-secret"


@pytest.mark.django_db
def test_ai_test_reports_success(admin_client):
    ws = WorkspaceSettings.load()
    ws.ai_provider = "openai"
    ws.ai_api_key_encrypted = encrypt("sk-x")
    ws.save()
    with mock.patch("apps.spam_analysis.ai_provider.test_connection", return_value="OK"):
        r = admin_client.post(reverse("integrations:ai_test"))
    assert r.status_code == 200
    assert b"Connected" in r.content


@pytest.mark.django_db
def test_ai_test_reports_failure(admin_client):
    ws = WorkspaceSettings.load()
    ws.ai_provider = "openai"
    ws.ai_api_key_encrypted = encrypt("sk-x")
    ws.save()
    with mock.patch("apps.spam_analysis.ai_provider.test_connection", side_effect=RuntimeError("bad key")):
        r = admin_client.post(reverse("integrations:ai_test"))
    assert r.status_code == 200
    assert b"Failed" in r.content


@pytest.mark.django_db
def test_safe_browsing_test_flags_test_url(admin_client):
    ws = WorkspaceSettings.load()
    ws.safe_browsing_api_key_encrypted = encrypt("AIza-x")
    ws.save()
    with mock.patch("apps.spam_analysis.safe_browsing.test_connection", return_value=True):
        r = admin_client.post(reverse("integrations:safe_browsing_test"))
    assert r.status_code == 200
    assert b"correctly flagged" in r.content
