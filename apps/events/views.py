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
from apps.events.outbound import fan_out
from apps.messages_api.models import Message
from apps.suppressions.models import Suppression

log = logging.getLogger(__name__)


_AUTO_SUPPRESS_REASON = {
    "bounced": Suppression.REASON_HARD_BOUNCE,
    "complained": Suppression.REASON_COMPLAINT,
}


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

        # Auto-suppress on hard bounce / complaint, using the event recipient.
        reason = _AUTO_SUPPRESS_REASON.get(normalized.type)
        if reason and normalized.recipient:
            Suppression.add(
                normalized.recipient,
                reason=reason,
                note=f"auto from {connection.provider_code} {normalized.type}",
                source_message=message,
            )

        fan_out(event, message=message)

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


# ---- panel views for outbound webhook configuration ----------------------

from django.contrib import messages as flash  # noqa: E402
from django.contrib.auth.decorators import login_required, user_passes_test  # noqa: E402
from django.http import HttpResponse  # noqa: E402
from django.shortcuts import redirect, render  # noqa: E402
from django.urls import reverse  # noqa: E402

from apps.audit.models import AuditLog  # noqa: E402
from apps.events.forms import WebhookEndpointForm  # noqa: E402
from apps.events.models import WebhookEndpoint  # noqa: E402


def _admin_required(view):
    return login_required(login_url="/login/")(
        user_passes_test(lambda u: u.is_authenticated and u.is_workspace_admin, login_url="/login/")(view)
    )


@_admin_required
def webhooks_list(request: HttpRequest) -> HttpResponse:
    endpoints = WebhookEndpoint.objects.all()
    return render(request, "events/webhooks_list.html", {"endpoints": endpoints})


@_admin_required
@require_http_methods(["GET", "POST"])
def webhooks_create(request: HttpRequest) -> HttpResponse:
    form = WebhookEndpointForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        endpoint = form.save()
        AuditLog.record(request.user, action="webhook.created", target=endpoint.url)
        flash.success(request, f"Webhook “{endpoint.name}” created. Copy the signing secret below.")
        return redirect(reverse("events:webhooks_edit", args=[endpoint.pk]))
    return render(request, "events/webhooks_edit.html", {"form": form, "creating": True})


@_admin_required
@require_http_methods(["GET", "POST"])
def webhooks_edit(request: HttpRequest, pk: int) -> HttpResponse:
    endpoint = get_object_or_404(WebhookEndpoint, pk=pk)
    form = WebhookEndpointForm(request.POST or None, instance=endpoint)
    if request.method == "POST" and form.is_valid():
        form.save()
        AuditLog.record(request.user, action="webhook.updated", target=endpoint.url)
        flash.success(request, "Webhook saved.")
        return redirect(reverse("events:webhooks_edit", args=[endpoint.pk]))
    return render(
        request,
        "events/webhooks_edit.html",
        {"form": form, "endpoint": endpoint, "creating": False},
    )


@_admin_required
@require_http_methods(["POST"])
def webhooks_rotate(request: HttpRequest, pk: int) -> HttpResponse:
    from apps.events.models import _gen_signing_secret

    endpoint = get_object_or_404(WebhookEndpoint, pk=pk)
    endpoint.signing_secret = _gen_signing_secret()
    endpoint.save(update_fields=["signing_secret", "updated_at"])
    AuditLog.record(request.user, action="webhook.secret_rotated", target=endpoint.url)
    flash.success(request, "Signing secret rotated.")
    return redirect(reverse("events:webhooks_edit", args=[endpoint.pk]))


@_admin_required
@require_http_methods(["POST"])
def webhooks_delete(request: HttpRequest, pk: int) -> HttpResponse:
    endpoint = get_object_or_404(WebhookEndpoint, pk=pk)
    url = endpoint.url
    endpoint.delete()
    AuditLog.record(request.user, action="webhook.deleted", target=url)
    flash.success(request, "Webhook deleted.")
    return redirect(reverse("events:webhooks_list"))
