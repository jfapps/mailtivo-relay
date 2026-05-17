from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
import responses

from apps.connections.models import Connection
from apps.core.encryption import encrypt
from apps.events.models import Event, WebhookDelivery, WebhookEndpoint
from apps.events.outbound import deliver_one, fan_out, sign_body
from apps.messages_api.models import Message


@pytest.fixture
def endpoint(db) -> WebhookEndpoint:
    return WebhookEndpoint.objects.create(
        name="my-app",
        url="https://app.example.test/webhooks/mailtivo",
        signing_secret="whsec_test1234567890",
        enabled=True,
    )


@pytest.fixture
def event(db):
    msg = Message(from_address="a@x.test", to=["b@y.test"], subject="hi", provider_message_id="prov-1")
    msg.set_body(text=".")
    msg.save()
    return Event.objects.create(
        message=msg,
        type="delivered",
        provider_event_id="evt-1",
        provider_message_id="prov-1",
        recipient="b@y.test",
        occurred_at=datetime.now(tz=timezone.utc),
    )


def _verify_sig(secret: str, body: bytes, header: str) -> bool:
    """Reimplement verification independently — proves the receiver can verify."""
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    ts = parts.get("t", "")
    v1 = parts.get("v1", "")
    key = secret.removeprefix("whsec_").encode()
    signed = f"{ts}.".encode() + body
    expected = hmac.new(key, signed, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, v1)


def test_sign_body_roundtrips():
    secret = "whsec_abcdef"
    ts, header = sign_body(secret, b'{"hi":1}', ts=1747500000)
    assert ts == "1747500000"
    assert _verify_sig(secret, b'{"hi":1}', header)


@pytest.mark.django_db
@responses.activate
def test_deliver_one_signs_and_marks_delivered(endpoint, event):
    responses.add(responses.POST, endpoint.url, status=200, json={"ok": True})
    delivery = WebhookDelivery.objects.create(endpoint=endpoint, event=event)
    status = deliver_one(delivery.id)
    assert status == WebhookDelivery.STATUS_DELIVERED
    request = responses.calls[0].request
    header = request.headers["Mailtivo-Signature"]
    assert _verify_sig(endpoint.signing_secret, request.body, header)
    endpoint.refresh_from_db()
    assert endpoint.consecutive_failures == 0
    assert endpoint.last_succeeded_at is not None


@pytest.mark.django_db
@responses.activate
def test_deliver_one_schedules_retry_on_failure(endpoint, event):
    responses.add(responses.POST, endpoint.url, status=503, json={"err": "boom"})
    delivery = WebhookDelivery.objects.create(endpoint=endpoint, event=event)
    with patch("apps.events.outbound.schedule") if False else patch("django_q.tasks.schedule") as sched:
        status = deliver_one(delivery.id)
    assert status == WebhookDelivery.STATUS_PENDING
    delivery.refresh_from_db()
    assert delivery.last_status_code == 503
    assert "503" in delivery.last_error
    sched.assert_called_once()
    endpoint.refresh_from_db()
    assert endpoint.consecutive_failures == 1


@pytest.mark.django_db
def test_disabled_endpoint_short_circuits(endpoint, event):
    endpoint.enabled = False
    endpoint.save(update_fields=["enabled"])
    delivery = WebhookDelivery.objects.create(endpoint=endpoint, event=event)
    assert deliver_one(delivery.id) == WebhookDelivery.STATUS_FAILED


@pytest.mark.django_db
def test_fan_out_respects_event_type_filter(event):
    matching = WebhookEndpoint.objects.create(
        name="dlv-only",
        url="https://a.example.test/hook",
        signing_secret="whsec_x",
        event_types=["delivered"],
    )
    non_matching = WebhookEndpoint.objects.create(
        name="bounced-only",
        url="https://b.example.test/hook",
        signing_secret="whsec_y",
        event_types=["bounced"],
    )
    with patch("django_q.tasks.async_task") as q:
        ids = fan_out(event)
    assert len(ids) == 1
    delivery = WebhookDelivery.objects.get()
    assert delivery.endpoint_id == matching.id
    # And the other endpoint got nothing.
    assert not WebhookDelivery.objects.filter(endpoint=non_matching).exists()
    q.assert_called_once()


# ---- auto-suppression on hard bounce / complaint --------------------------

@pytest.mark.django_db
def test_resend_bounce_webhook_auto_suppresses(client):
    secret = "whsec_" + base64.b64encode(b"a-very-secret-key").decode()
    conn = Connection.objects.create(
        name="r", provider_code="resend",
        credentials_encrypted=encrypt("re_test"),
        webhook_secret_encrypted=encrypt(secret),
    )
    raw = json.dumps({
        "type": "email.bounced",
        "created_at": "2026-05-17T12:01:30.000Z",
        "data": {"email_id": "prov-bnc", "to": ["bouncer@x.test"], "bounce": {"message": "no such user"}},
    }).encode()
    svix_id, svix_ts = "msg_bnc_1", "1747500000"
    key = base64.b64decode(secret.removeprefix("whsec_"))
    signed = f"{svix_id}.{svix_ts}.".encode() + raw
    sig = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    from django.urls import reverse

    r = client.post(
        reverse("events:resend", args=[conn.id]),
        data=raw,
        content_type="application/json",
        HTTP_SVIX_ID=svix_id,
        HTTP_SVIX_TIMESTAMP=svix_ts,
        HTTP_SVIX_SIGNATURE=f"v1,{sig}",
    )
    assert r.status_code == 200, r.content

    from apps.suppressions.models import Suppression

    s = Suppression.objects.get(email="bouncer@x.test")
    assert s.reason == Suppression.REASON_HARD_BOUNCE
