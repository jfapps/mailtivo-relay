"""Tests for capture (sandbox) pools — the built-in testing relay.

A send routed through a capture pool is stored and shown in the Test Inbox but
never dispatched to a provider, and capture runs synchronously (no Q2 worker).
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.api_keys.models import APIKey
from apps.messages_api.models import Message
from apps.pools.models import Pool
from apps.suppressions.models import Suppression


def _post(client, url, body, *, bearer=""):
    headers = {"HTTP_AUTHORIZATION": bearer} if bearer else {}
    return client.post(url, data=json.dumps(body), content_type="application/json", **headers)


@pytest.fixture(autouse=True)
def _clear_rate_limit_cache():
    # The per-key rate-limit counter lives in the shared LocMem cache, which
    # isn't reset between tests; the rolled-back DB reuses the same key id, so
    # counts would otherwise accumulate across tests and false-429.
    from django.core.cache import cache

    cache.clear()
    yield


@pytest.fixture
def capture_pool(db) -> Pool:
    # A capture pool needs no connections/members.
    return Pool.objects.create(name="sandbox", mode=Pool.MODE_CAPTURE)


@pytest.fixture
def capture_bearer(db, capture_pool) -> str:
    _, secret = APIKey.issue(name="sandbox-key", scopes=["*"], default_pool=capture_pool)
    return f"Bearer {secret}"


@pytest.mark.django_db
def test_capture_pool_stores_without_sending(client, capture_bearer):
    with patch("apps.sending.tasks.enqueue_message") as enqueue:
        r = _post(
            client,
            reverse("messages_api:emails_create"),
            {"from": "me@test.dev", "to": ["you@test.dev"], "subject": "hi", "html": "<b>captured</b>"},
            bearer=capture_bearer,
        )
    assert r.status_code == 200, r.content
    msg = Message.objects.get(pk=r.json()["id"])
    assert msg.sandbox is True
    assert msg.status == Message.STATUS_CAPTURED
    assert msg.sent_at is not None
    assert msg.provider_message_id == f"captured-{msg.id}"
    assert msg.body() == {"html": "<b>captured</b>", "text": ""}
    # The whole point: no upstream dispatch.
    enqueue.assert_not_called()


@pytest.mark.django_db
def test_capture_pool_needs_no_members(client, capture_pool, capture_bearer):
    assert capture_pool.members.count() == 0
    r = _post(
        client,
        reverse("messages_api:emails_create"),
        {"from": "me@test.dev", "to": ["you@test.dev"], "subject": "x", "text": "."},
        bearer=capture_bearer,
    )
    assert r.status_code == 200, r.content


@pytest.mark.django_db
def test_capture_pool_captures_suppressed_recipients(client, capture_pool, capture_bearer):
    Suppression.objects.create(email="blocked@test.dev", reason="bounce")
    r = _post(
        client,
        reverse("messages_api:emails_create"),
        {"from": "me@test.dev", "to": ["blocked@test.dev"], "subject": "x", "text": "."},
        bearer=capture_bearer,
    )
    # Live pools reject an all-suppressed send (422); capture pools keep it.
    assert r.status_code == 200, r.content
    msg = Message.objects.get(pk=r.json()["id"])
    assert msg.to == ["blocked@test.dev"]
    assert msg.status == Message.STATUS_CAPTURED


# --- Panel: Test Inbox vs Messages -----------------------------------------


@pytest.fixture
def auth_client(client, db):
    owner = User.objects.create_user(
        email="o@a.test", password="ChangeMe-12345!", is_workspace_admin=True
    )
    client.force_login(owner)
    return client


def _msg(*, sandbox: bool, subject: str) -> Message:
    status = Message.STATUS_CAPTURED if sandbox else Message.STATUS_DELIVERED
    m = Message(from_address="a@x.test", to=["b@y.test"], subject=subject, sandbox=sandbox, status=status)
    m.set_body(html="<p>body</p>", text="body")
    m.save()
    return m


@pytest.mark.django_db
def test_test_inbox_shows_only_captured(auth_client):
    _msg(sandbox=True, subject="captured-one")
    _msg(sandbox=False, subject="live-one")
    r = auth_client.get(reverse("messages_panel:test_inbox"))
    assert r.status_code == 200
    assert b"captured-one" in r.content
    assert b"live-one" not in r.content


@pytest.mark.django_db
def test_messages_list_excludes_captured(auth_client):
    _msg(sandbox=True, subject="captured-one")
    _msg(sandbox=False, subject="live-one")
    r = auth_client.get(reverse("messages_panel:list"))
    assert r.status_code == 200
    assert b"live-one" in r.content
    assert b"captured-one" not in r.content


@pytest.mark.django_db
def test_captured_message_detail_renders(auth_client):
    m = _msg(sandbox=True, subject="captured-detail")
    r = auth_client.get(reverse("messages_panel:detail", args=[m.id]))
    assert r.status_code == 200
    assert b"captured-detail" in r.content
    assert b"Captured" in r.content
    assert b"Test" in r.content  # the sandbox pill


# --- Event simulation -------------------------------------------------------


@pytest.mark.django_db
def test_simulate_creates_event_and_advances_status():
    from apps.events.models import Event
    from apps.events.simulate import simulate_event

    m = _msg(sandbox=True, subject="sim")
    event = simulate_event(m, "delivered")
    assert event.simulated is True
    assert event.type == "delivered"
    assert event.recipient == "b@y.test"
    assert Event.objects.filter(message=m, type="delivered").exists()
    m.refresh_from_db()
    assert m.status == Message.STATUS_DELIVERED


@pytest.mark.django_db
def test_simulate_rejects_live_message():
    from apps.events.simulate import SimulateError, simulate_event

    m = _msg(sandbox=False, subject="live")
    with pytest.raises(SimulateError):
        simulate_event(m, "delivered")


@pytest.mark.django_db
def test_simulate_rejects_unknown_type():
    from apps.events.simulate import SimulateError, simulate_event

    m = _msg(sandbox=True, subject="sim")
    with pytest.raises(SimulateError):
        simulate_event(m, "exploded")


@pytest.mark.django_db
def test_simulate_bounce_does_not_suppress():
    # A test must never mutate the global (live) suppression list.
    from apps.events.simulate import simulate_event

    m = _msg(sandbox=True, subject="sim")
    simulate_event(m, "bounced")
    assert not Suppression.objects.filter(email="b@y.test").exists()


@pytest.mark.django_db
def test_simulate_fans_out_to_enabled_webhook():
    from apps.events.models import WebhookDelivery, WebhookEndpoint
    from apps.events.simulate import simulate_event

    WebhookEndpoint.objects.create(name="app", url="https://app.test/hook", enabled=True)
    m = _msg(sandbox=True, subject="sim")
    with patch("django_q.tasks.async_task"):
        simulate_event(m, "opened")
    assert WebhookDelivery.objects.count() == 1


@pytest.mark.django_db
def test_simulate_endpoint_requires_sandbox(auth_client):
    from apps.events.models import Event

    live = _msg(sandbox=False, subject="live")
    r = auth_client.post(reverse("messages_panel:simulate", args=[live.id]), {"type": "delivered"})
    assert r.status_code == 302  # redirects back with a flash error
    assert not Event.objects.filter(message=live).exists()


@pytest.mark.django_db
def test_simulate_endpoint_creates_event(auth_client):
    from apps.events.models import Event

    m = _msg(sandbox=True, subject="sim")
    with patch("django_q.tasks.async_task"):
        r = auth_client.post(reverse("messages_panel:simulate", args=[m.id]), {"type": "clicked"})
    assert r.status_code == 302
    assert Event.objects.filter(message=m, type="clicked", simulated=True).exists()


@pytest.mark.django_db
def test_detail_shows_simulate_controls_only_for_sandbox(auth_client):
    sb = _msg(sandbox=True, subject="sandbox-msg")
    live = _msg(sandbox=False, subject="live-msg")
    r_sb = auth_client.get(reverse("messages_panel:detail", args=[sb.id]))
    r_live = auth_client.get(reverse("messages_panel:detail", args=[live.id]))
    assert b"Simulate an event" in r_sb.content
    assert b"Simulate an event" not in r_live.content
