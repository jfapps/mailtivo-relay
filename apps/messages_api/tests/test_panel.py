from __future__ import annotations

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.events.models import Event
from apps.messages_api.models import Message


@pytest.fixture
def owner(db):
    return User.objects.create_user(email="o@a.test", password="ChangeMe-12345!", is_workspace_admin=True)


@pytest.fixture
def auth_client(client, owner):
    client.force_login(owner)
    return client


def _make_message(**overrides) -> Message:
    defaults = dict(from_address="a@x.test", to=["b@y.test"], subject="hello")
    defaults.update(overrides)
    msg = Message(**defaults)
    msg.set_body(html="<p>body</p>", text="body")
    msg.save()
    return msg


@pytest.mark.django_db
def test_messages_list_requires_login(client):
    r = client.get(reverse("messages_panel:list"))
    assert r.status_code in (302, 301)
    assert "/login" in r["Location"]


@pytest.mark.django_db
def test_messages_list_renders(auth_client):
    _make_message(subject="hello world")
    _make_message(subject="another", status=Message.STATUS_DELIVERED)
    r = auth_client.get(reverse("messages_panel:list"))
    assert r.status_code == 200
    assert b"hello world" in r.content
    assert b"another" in r.content


@pytest.mark.django_db
def test_messages_list_filters_by_status(auth_client):
    _make_message(subject="bouncer", status=Message.STATUS_BOUNCED)
    _make_message(subject="deliveree", status=Message.STATUS_DELIVERED)
    r = auth_client.get(reverse("messages_panel:list") + "?status=bounced")
    assert r.status_code == 200
    assert b"bouncer" in r.content
    assert b"deliveree" not in r.content


@pytest.mark.django_db
def test_messages_list_search(auth_client):
    _make_message(subject="needle")
    _make_message(subject="haystack")
    r = auth_client.get(reverse("messages_panel:list") + "?q=needle")
    assert r.status_code == 200
    assert b"needle" in r.content
    assert b"haystack" not in r.content


@pytest.mark.django_db
def test_messages_list_htmx_returns_partial(auth_client):
    _make_message(subject="hello")
    r = auth_client.get(
        reverse("messages_panel:list"),
        HTTP_HX_REQUEST="true",
    )
    assert r.status_code == 200
    # Partial does not include the panel chrome.
    assert b"<aside" not in r.content
    assert b"hello" in r.content


@pytest.mark.django_db
def test_message_detail_renders_envelope_and_events(auth_client):
    msg = _make_message(subject="t", status=Message.STATUS_DELIVERED)
    Event.objects.create(
        message=msg,
        type="delivered",
        provider_event_id="evt-1",
        occurred_at=timezone.now(),
    )
    r = auth_client.get(reverse("messages_panel:detail", args=[msg.id]))
    assert r.status_code == 200
    assert b"a@x.test" in r.content
    assert b"Delivered" in r.content


@pytest.mark.django_db
def test_message_detail_404(auth_client):
    r = auth_client.get(reverse("messages_panel:detail", args=["01000000000000000000000000"]))
    assert r.status_code == 404


@pytest.mark.django_db
def test_spam_tab_shows_analyze_when_configured(auth_client):
    from apps.accounts.models import WorkspaceSettings
    from apps.core.encryption import encrypt

    ws = WorkspaceSettings.load()
    ws.ai_provider = "openai"
    ws.ai_api_key_encrypted = encrypt("sk-test")
    ws.save()
    msg = _make_message()
    r = auth_client.get(reverse("messages_panel:detail", args=[msg.id]))
    assert r.status_code == 200
    assert b"Analyze for spam" in r.content


@pytest.mark.django_db
def test_spam_tab_prompts_to_configure_when_not(auth_client):
    msg = _make_message()
    r = auth_client.get(reverse("messages_panel:detail", args=[msg.id]))
    assert r.status_code == 200
    assert b"Configure an AI provider" in r.content


@pytest.mark.django_db
def test_analyze_enqueues_and_returns_running_panel(auth_client):
    from unittest import mock

    msg = _make_message()
    with mock.patch("django_q.tasks.async_task") as async_task:
        r = auth_client.post(reverse("messages_panel:analyze", args=[msg.id]))
    assert r.status_code == 200
    async_task.assert_called_once_with("apps.spam_analysis.tasks.analyze_message", msg.id)
    assert b"Analyzing message" in r.content
    msg.refresh_from_db()
    assert msg.spam_report == {"state": "running"}


@pytest.mark.django_db
def test_spam_panel_renders_done_result(auth_client):
    msg = _make_message()
    msg.spam_score = 82
    msg.spam_verdict = "spam"
    msg.spam_report = {
        "state": "done",
        "score": 82,
        "verdict": "spam",
        "provider": "openai",
        "model": "gpt-4o-mini",
        "ai": {"score": 82, "summary": "Looks like phishing", "findings": [{"label": "Urgency", "detail": "acts now"}]},
        "links": {"checked": True, "error": "", "flagged_count": 1,
                  "results": [{"url": "http://bad.test/x", "flagged": True, "threats": ["MALWARE"]}]},
    }
    msg.spam_analyzed_at = timezone.now()
    msg.save()
    r = auth_client.get(reverse("messages_panel:spam_panel", args=[msg.id]))
    assert r.status_code == 200
    assert b"Looks like phishing" in r.content
    assert b"MALWARE" in r.content
    assert b"82" in r.content


@pytest.mark.django_db
def test_analyze_requires_login(client):
    msg = _make_message()
    r = client.post(reverse("messages_panel:analyze", args=[msg.id]))
    assert r.status_code in (301, 302)
    assert "/login" in r["Location"]


@pytest.mark.django_db
def test_dashboard_kpis_render(auth_client):
    _make_message(status=Message.STATUS_DELIVERED)
    _make_message(status=Message.STATUS_BOUNCED)
    r = auth_client.get(reverse("panel:dashboard"))
    assert r.status_code == 200
    assert b"Delivery rate" in r.content
    assert b"Bounce rate" in r.content


@pytest.mark.django_db
def test_dashboard_kpi_partial(auth_client):
    _make_message(status=Message.STATUS_DELIVERED)
    r = auth_client.get(reverse("panel:kpis"))
    assert r.status_code == 200
    assert b"Delivery rate" in r.content
    # Partial has no <aside> (sidebar) — it's the polled fragment.
    assert b"<aside" not in r.content
