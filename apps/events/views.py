"""Inbound webhook endpoints.

URL shape: /webhooks/<provider>/<connection_id>/
We require the connection_id in the URL so a single relay can host many
connections of the same provider without ambiguity in their webhook settings.

Signature verification + payload parsing live on the adapter (apps.connections.adapters)
— this view is just glue: lookup → verify → parse → persist → status update.
"""
from __future__ import annotations

import json
import logging

from django.db import IntegrityError, transaction
from django.http import HttpRequest, JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from apps.connections.adapters import AdapterError
from apps.connections.models import Connection
from apps.events.models import Event
from apps.messages_api.models import Message

log = logging.getLogger(__name__)


# Map normalized event types → Message.status. None means "do not update".
_STATUS_FOR_EVENT = {
    "queued": None,
    "sent": Message.STATUS_SENT,
    "delivered": Message.STATUS_DELIVERED,
    "deferred": None,
    "bounced": Message.STATUS_BOUNCED,
    "complained": Message.STATUS_COMPLAINED,
    "failed": Message.STATUS_FAILED,
    "opened": None,
    "clicked": None,
    "unknown": None,
}

# Status precedence: a delivered message that later opens/clicks must stay delivered,
# but a delivered message that later complains becomes complained.
_STATUS_RANK = {
    Message.STATUS_QUEUED: 0,
    Message.STATUS_SCHEDULED: 0,
    Message.STATUS_SENDING: 1,
    Message.STATUS_SENT: 2,
    Message.STATUS_DELIVERED: 3,
    Message.STATUS_FAILED: 5,
    Message.STATUS_BOUNCED: 5,
    Message.STATUS_COMPLAINED: 6,
}


def _error(message: str, *, status: int) -> JsonResponse:
    return JsonResponse({"error": message}, status=status)


def _find_message(provider_code: str, *, provider_message_id: str, recipient_token: str) -> Message | None:
    qs = Message.objects.all()
    if provider_message_id:
        m = qs.filter(provider_message_id=provider_message_id).first()
        if m:
            return m
    if recipient_token and provider_code == Connection.PROVIDER_POSTAL:
        # Postal: recipient_tokens is a {address: token} dict on Message.
        for m in qs.filter(recipient_tokens__isnull=False).exclude(recipient_tokens={}):
            if recipient_token in (m.recipient_tokens or {}).values():
                return m
    return None


def _apply_status(message: Message, new_status: str | None) -> None:
    if new_status is None:
        return
    current_rank = _STATUS_RANK.get(message.status, 0)
    new_rank = _STATUS_RANK.get(new_status, 0)
    if new_rank >= current_rank:
        message.status = new_status
        message.save(update_fields=["status"])


def _ingest(request: HttpRequest, connection: Connection) -> JsonResponse:
    body = request.body or b""
    if not body:
        return _error("Empty body.", status=400)

    adapter = connection.adapter()
    try:
        if not adapter.verify_webhook(headers=request.headers, body=body):
            return _error("Invalid signature.", status=401)
    except AdapterError as exc:
        return _error(f"Adapter configuration error: {exc}", status=500)

    try:
        normalized = adapter.parse_event(body=body)
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        log.warning("Failed to parse %s webhook: %s", connection.provider_code, exc)
        return _error(f"Could not parse payload: {exc}", status=422)

    # Override the parser's provider_event_id with the carrier-provided one when
    # the carrier supplies a better unique id (Svix's svix-id beats Resend's
    # body-composed fallback).
    svix_id = request.headers.get("svix-id") or request.headers.get("Svix-Id") or ""
    if svix_id and connection.provider_code == Connection.PROVIDER_RESEND:
        normalized.provider_event_id = svix_id

    message = _find_message(
        connection.provider_code,
        provider_message_id=normalized.provider_message_id,
        recipient_token=normalized.recipient_token,
    )

    with transaction.atomic():
        try:
            event = Event.objects.create(
                message=message,
                connection=connection,
                type=normalized.type,
                provider_event_id=normalized.provider_event_id,
                provider_message_id=normalized.provider_message_id,
                recipient=normalized.recipient,
                recipient_token=normalized.recipient_token,
                occurred_at=normalized.occurred_at,
                link_url=normalized.link_url[:8000] if normalized.link_url else "",
                user_agent=normalized.user_agent[:500] if normalized.user_agent else "",
                ip=normalized.ip or None,
                bounce_reason=normalized.bounce_reason[:500] if normalized.bounce_reason else "",
                raw=normalized.raw,
            )
        except IntegrityError:
            return JsonResponse({"ok": True, "deduped": True}, status=200)

        if message is not None:
            _apply_status(message, _STATUS_FOR_EVENT.get(normalized.type))

    return JsonResponse(
        {"ok": True, "event_id": event.id, "type": normalized.type, "matched_message": bool(message)},
        status=200,
    )


@csrf_exempt
@require_http_methods(["POST"])
def postal_webhook(request: HttpRequest, connection_id: int) -> JsonResponse:
    connection = get_object_or_404(Connection, pk=connection_id, provider_code=Connection.PROVIDER_POSTAL)
    return _ingest(request, connection)


@csrf_exempt
@require_http_methods(["POST"])
def resend_webhook(request: HttpRequest, connection_id: int) -> JsonResponse:
    connection = get_object_or_404(Connection, pk=connection_id, provider_code=Connection.PROVIDER_RESEND)
    return _ingest(request, connection)
