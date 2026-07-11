"""Tests for scheduled sends (`scheduled_at`).

A future timestamp defers dispatch via a one-shot django-q Schedule that fires
apps.sending.tasks.send_message; a past timestamp sends immediately.
"""
from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.urls import reverse
from django.utils import timezone
from django_q.models import Schedule

from apps.messages_api.models import Message
from apps.pools.models import Pool
from apps.sending.tasks import send_message


def _post(client, body, *, bearer=""):
    headers = {"HTTP_AUTHORIZATION": bearer} if bearer else {}
    return client.post(
        reverse("messages_api:emails_create"),
        data=json.dumps(body),
        content_type="application/json",
        **headers,
    )


@pytest.fixture(autouse=True)
def _clear_rate_limit_cache():
    from django.core.cache import cache

    cache.clear()
    yield


@pytest.mark.django_db
def test_future_scheduled_at_creates_once_schedule(client, bearer):
    run_at = timezone.now() + timedelta(hours=2)
    with patch("apps.sending.tasks.enqueue_message") as enqueue:
        r = _post(
            client,
            {
                "from": "me@test.dev",
                "to": ["you@test.dev"],
                "subject": "later",
                "text": "soon",
                "scheduled_at": run_at.isoformat(),
            },
            bearer=bearer,
        )
    assert r.status_code == 200, r.content
    msg = Message.objects.get(pk=r.json()["id"])
    assert msg.status == Message.STATUS_SCHEDULED
    assert msg.scheduled_at == run_at
    enqueue.assert_not_called()

    sched = Schedule.objects.get(name=f"scheduled-send-{msg.id}")
    assert sched.func == "apps.sending.tasks.send_message"
    assert sched.schedule_type == Schedule.ONCE
    assert sched.next_run == run_at


@pytest.mark.django_db
def test_past_scheduled_at_sends_immediately(client, bearer):
    past = timezone.now() - timedelta(minutes=5)
    with patch("apps.sending.tasks.enqueue_message") as enqueue:
        r = _post(
            client,
            {
                "from": "me@test.dev",
                "to": ["you@test.dev"],
                "subject": "now",
                "text": "x",
                "scheduled_at": past.isoformat(),
            },
            bearer=bearer,
        )
    assert r.status_code == 200, r.content
    msg = Message.objects.get(pk=r.json()["id"])
    assert msg.status == Message.STATUS_QUEUED
    assert msg.scheduled_at == past  # recorded for reference, but sent now
    enqueue.assert_called_once()
    assert not Schedule.objects.filter(name=f"scheduled-send-{msg.id}").exists()


@pytest.mark.django_db
def test_scheduled_at_too_far_ahead_rejected(client, bearer):
    r = _post(
        client,
        {
            "from": "me@test.dev",
            "to": ["you@test.dev"],
            "subject": "x",
            "text": "x",
            "scheduled_at": (timezone.now() + timedelta(days=45)).isoformat(),
        },
        bearer=bearer,
    )
    assert r.status_code == 422
    assert "30 days" in r.json()["message"]
    assert Message.objects.count() == 0


@pytest.mark.django_db
def test_capture_pool_ignores_scheduling(client, db):
    """Test-inbox sends capture immediately even with a future scheduled_at."""
    from apps.api_keys.models import APIKey

    capture_pool = Pool.objects.create(name="sandbox", mode=Pool.MODE_CAPTURE)
    _, secret = APIKey.issue(name="sb", scopes=["*"], default_pool=capture_pool)
    r = _post(
        client,
        {
            "from": "me@test.dev",
            "to": ["you@test.dev"],
            "subject": "x",
            "text": "x",
            "scheduled_at": (timezone.now() + timedelta(hours=1)).isoformat(),
        },
        bearer=f"Bearer {secret}",
    )
    assert r.status_code == 200, r.content
    msg = Message.objects.get(pk=r.json()["id"])
    assert msg.status == Message.STATUS_CAPTURED
    assert not Schedule.objects.filter(name=f"scheduled-send-{msg.id}").exists()


@pytest.mark.django_db
def test_send_message_captures_when_pool_switched_to_capture(pool):
    """A scheduled message whose pool flipped to capture mode is captured, not sent."""
    msg = Message.objects.create(
        pool=pool,
        from_address="me@test.dev",
        to=["you@test.dev"],
        subject="x",
        status=Message.STATUS_SCHEDULED,
    )
    pool.mode = Pool.MODE_CAPTURE
    pool.save(update_fields=["mode"])

    assert send_message(msg.id) == Message.STATUS_CAPTURED
    msg.refresh_from_db()
    assert msg.sandbox is True
    assert msg.status == Message.STATUS_CAPTURED
