"""Tests for the retention purge (apps.messages_api.tasks) and the
Data & storage panel page."""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User, WorkspaceSettings
from apps.events.models import Event
from apps.messages_api.models import Attachment, IdempotencyRecord, Message, PurgeRun
from apps.messages_api.tasks import delete_captured_messages, purge_expired


def _message(*, status=Message.STATUS_DELIVERED, expired=True, sandbox=False) -> Message:
    m = Message(
        from_address="a@x.test",
        to=["b@y.test"],
        subject="s",
        status=status,
        sandbox=sandbox,
    )
    m.set_body(html="<p>hi</p>", text="hi")
    m.retention_expires_at = timezone.now() + (timedelta(days=-1) if expired else timedelta(days=10))
    m.save()
    return m


@pytest.mark.django_db
def test_purge_clears_bodies_and_attachments_keeps_metadata_and_events():
    msg = _message(expired=True)
    Attachment.objects.create(message=msg, filename="a.pdf", content_b64="QUJD")
    Event.objects.create(
        message=msg, type="delivered", provider_event_id="ev-1", occurred_at=timezone.now()
    )

    result = purge_expired()

    assert result["messages_purged"] == 1
    assert result["attachments_purged"] == 1
    msg.refresh_from_db()
    assert msg.body_purged_at is not None
    assert bytes(msg.body_encrypted) == b""
    assert msg.body() == {"html": "", "text": ""}
    assert msg.attachments.count() == 0
    # Metadata + events survive.
    assert msg.subject == "s"
    assert msg.events.count() == 1
    run = PurgeRun.objects.get()
    assert run.trigger == PurgeRun.TRIGGER_SCHEDULED
    assert run.finished_at is not None


@pytest.mark.django_db
def test_purge_skips_unexpired_and_in_flight_messages():
    fresh = _message(expired=False)
    queued = _message(status=Message.STATUS_QUEUED, expired=True)
    scheduled = _message(status=Message.STATUS_SCHEDULED, expired=True)

    result = purge_expired()

    assert result["messages_purged"] == 0
    for m in (fresh, queued, scheduled):
        m.refresh_from_db()
        assert m.body_purged_at is None
        assert m.body()["text"] == "hi"


@pytest.mark.django_db
def test_purge_is_idempotent():
    _message(expired=True)
    assert purge_expired()["messages_purged"] == 1
    assert purge_expired()["messages_purged"] == 0


@pytest.mark.django_db
def test_scheduled_purge_honors_retention_toggle():
    ws = WorkspaceSettings.load()
    ws.retention_enabled = False
    ws.save()
    _message(expired=True)

    assert purge_expired() == {"skipped": True}
    # Manual runs ignore the toggle.
    assert purge_expired(trigger=PurgeRun.TRIGGER_MANUAL)["messages_purged"] == 1


@pytest.mark.django_db
def test_purge_older_than_overrides_retention_window():
    old = _message(expired=False)  # retention says keep...
    Message.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=100))
    recent = _message(expired=False)

    result = purge_expired(trigger=PurgeRun.TRIGGER_MANUAL, older_than_days=90)

    assert result["messages_purged"] == 1
    old.refresh_from_db()
    recent.refresh_from_db()
    assert old.body_purged_at is not None
    assert recent.body_purged_at is None


@pytest.mark.django_db
def test_purge_sweeps_expired_idempotency_records(api_key):
    msg = _message(expired=False)
    rec = IdempotencyRecord.objects.create(api_key=api_key, key="k", fingerprint="f", message=msg)
    IdempotencyRecord.objects.filter(pk=rec.pk).update(
        expires_at=timezone.now() - timedelta(hours=1)
    )

    assert purge_expired()["idempotency_purged"] == 1
    assert IdempotencyRecord.objects.count() == 0


@pytest.mark.django_db
def test_delete_captured_removes_test_data_only():
    live = _message(expired=False)
    captured = _message(status=Message.STATUS_CAPTURED, sandbox=True, expired=False)
    Attachment.objects.create(message=captured, filename="a.txt", content_b64="QUJD")
    Event.objects.create(
        message=captured, type="delivered", provider_event_id="sim-1", occurred_at=timezone.now()
    )

    assert delete_captured_messages() == 1

    assert Message.objects.filter(pk=live.pk).exists()
    assert not Message.objects.filter(pk=captured.pk).exists()
    assert Attachment.objects.count() == 0
    assert Event.objects.count() == 0


# ---- Data & storage panel page ---------------------------------------------


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
def test_data_page_renders_with_stats(admin_client):
    _message(expired=True)
    r = admin_client.get(reverse("panel:data"))
    assert r.status_code == 200
    assert b"Data &amp; storage" in r.content
    assert b"Pending purge" in r.content


@pytest.mark.django_db
def test_data_page_requires_login(client):
    r = client.get(reverse("panel:data"))
    assert r.status_code == 302
    assert "/login/" in r["Location"]


@pytest.mark.django_db
def test_purge_now_action(admin_client):
    _message(expired=True)
    r = admin_client.post(reverse("panel:data_purge_now"))
    assert r.status_code == 302
    assert Message.objects.filter(body_purged_at__isnull=False).count() == 1
    assert PurgeRun.objects.get().trigger == PurgeRun.TRIGGER_MANUAL


@pytest.mark.django_db
def test_purge_older_action_validates_days(admin_client):
    r = admin_client.post(reverse("panel:data_purge_older"), {"days": "not-a-number"})
    assert r.status_code == 302
    assert PurgeRun.objects.count() == 0


@pytest.mark.django_db
def test_delete_captured_action(admin_client):
    _message(status=Message.STATUS_CAPTURED, sandbox=True, expired=False)
    r = admin_client.post(reverse("panel:data_delete_captured"))
    assert r.status_code == 302
    assert Message.objects.count() == 0
