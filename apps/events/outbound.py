"""Outbound webhook delivery — fan-out our normalized events to customer apps.

Signature scheme (kept simple — matches the SDK example in docs/api.md):
  Mailtivo-Timestamp: <unix seconds>
  Mailtivo-Signature: t=<unix>,v1=<hex HMAC-SHA256>
  signed payload = f"{t}.{raw_body}"
  secret = WebhookEndpoint.signing_secret (the part after "whsec_" prefix)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from datetime import timedelta

import requests
from django.utils import timezone

from apps.events.models import Event, WebhookDelivery, WebhookEndpoint

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 6
HTTP_TIMEOUT = 10
# Exponential-ish backoff in seconds: 5s, 30s, 2m, 10m, 1h, 6h.
_BACKOFFS = [5, 30, 120, 600, 3600, 21600]


def _payload_for(event: Event, *, message=None) -> dict:
    """The body we POST to outbound endpoints — provider-agnostic shape."""
    return {
        "id": f"evt_{event.id}",
        "type": event.type,
        "test": event.simulated,
        "occurred_at": event.occurred_at.isoformat() if event.occurred_at else None,
        "data": {
            "message_id": message.id if message else None,
            "provider_message_id": event.provider_message_id,
            "recipient": event.recipient,
            "link_url": event.link_url or None,
            "user_agent": event.user_agent or None,
            "ip": event.ip,
            "bounce_reason": event.bounce_reason or None,
        },
    }


def sign_body(secret: str, body: bytes, *, ts: int | None = None) -> tuple[str, str]:
    """Return (timestamp, signature_header)."""
    if ts is None:
        ts = int(time.time())
    key = secret.removeprefix("whsec_").encode("utf-8")
    signed = f"{ts}.".encode("utf-8") + body
    mac = hmac.new(key, signed, hashlib.sha256).hexdigest()
    return str(ts), f"t={ts},v1={mac}"


def deliver_one(delivery_id: int) -> str:
    """Q2 task entrypoint. Returns the new status."""
    try:
        delivery = WebhookDelivery.objects.select_related("endpoint", "event").get(pk=delivery_id)
    except WebhookDelivery.DoesNotExist:
        log.warning("deliver_one: delivery %s vanished", delivery_id)
        return "missing"

    if delivery.status == WebhookDelivery.STATUS_DELIVERED:
        return delivery.status

    endpoint = delivery.endpoint
    if not endpoint.enabled:
        delivery.status = WebhookDelivery.STATUS_FAILED
        delivery.last_error = "Endpoint disabled."
        delivery.save(update_fields=["status", "last_error"])
        return delivery.status

    event = delivery.event
    message = event.message
    body = json.dumps(_payload_for(event, message=message)).encode("utf-8")
    ts, sig = sign_body(endpoint.signing_secret, body)

    delivery.attempts += 1
    delivery.save(update_fields=["attempts"])

    try:
        resp = requests.post(
            endpoint.url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Mailtivo-Relay/1.0",
                "Mailtivo-Timestamp": ts,
                "Mailtivo-Signature": sig,
                "Mailtivo-Event": event.type,
                "Mailtivo-Test": "true" if event.simulated else "false",
            },
            timeout=HTTP_TIMEOUT,
        )
        delivery.last_status_code = resp.status_code
        ok = 200 <= resp.status_code < 300
        if ok:
            delivery.status = WebhookDelivery.STATUS_DELIVERED
            delivery.delivered_at = timezone.now()
            delivery.last_error = ""
            delivery.save(update_fields=["status", "delivered_at", "last_status_code", "last_error"])
            WebhookEndpoint.objects.filter(pk=endpoint.pk).update(
                last_attempted_at=timezone.now(),
                last_succeeded_at=timezone.now(),
                consecutive_failures=0,
            )
            return delivery.status
        delivery.last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
    except requests.RequestException as exc:
        delivery.last_error = f"network error: {exc}"[:500]

    # Failed — schedule next attempt unless we're past MAX_ATTEMPTS.
    if delivery.attempts < MAX_ATTEMPTS:
        try:
            from django_q.tasks import schedule
            from django_q.models import Schedule

            wait_s = _BACKOFFS[min(delivery.attempts - 1, len(_BACKOFFS) - 1)]
            schedule(
                "apps.events.outbound.deliver_one",
                delivery.id,
                name=f"webhook-retry-{delivery.id}-{delivery.attempts}",
                schedule_type=Schedule.ONCE,
                next_run=timezone.now() + timedelta(seconds=wait_s),
            )
        except Exception as exc:  # noqa: BLE001 — retry scheduling is best-effort
            log.exception("could not schedule retry for delivery %s: %s", delivery.id, exc)
    else:
        delivery.status = WebhookDelivery.STATUS_FAILED

    delivery.save(update_fields=["status", "last_status_code", "last_error"])
    WebhookEndpoint.objects.filter(pk=endpoint.pk).update(
        last_attempted_at=timezone.now(),
        consecutive_failures=endpoint.consecutive_failures + 1,
    )
    return delivery.status


def fan_out(event: Event, *, message=None) -> list[int]:
    """Create + enqueue one WebhookDelivery per interested endpoint. Returns
    the delivery ids."""
    delivery_ids: list[int] = []
    endpoints = WebhookEndpoint.objects.filter(enabled=True)
    for ep in endpoints:
        if not ep.wants(event.type):
            continue
        delivery = WebhookDelivery.objects.create(endpoint=ep, event=event)
        delivery_ids.append(delivery.id)
        try:
            from django_q.tasks import async_task

            async_task("apps.events.outbound.deliver_one", delivery.id)
        except Exception as exc:  # noqa: BLE001
            log.exception("could not enqueue outbound delivery %s: %s", delivery.id, exc)
    return delivery_ids
