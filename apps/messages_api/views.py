"""Resend-compatible /api/v1/emails endpoints."""
from __future__ import annotations

import json
import logging

from django.db import IntegrityError, transaction
from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from apps.accounts.models import WorkspaceSettings
from apps.messages_api.auth import require_api_key
from apps.messages_api.models import Attachment, IdempotencyRecord, Message
from apps.messages_api.ratelimit import check as ratelimit_check
from apps.messages_api.serializers import (
    ValidationError,
    fingerprint_request,
    parse_email_request,
    serialize_message,
)
from apps.suppressions.models import Suppression

log = logging.getLogger(__name__)


def _error(message: str, *, status: int, name: str = "validation_error") -> JsonResponse:
    return JsonResponse(
        {"statusCode": status, "name": name, "message": message},
        status=status,
    )


def _parse_body(request: HttpRequest) -> dict:
    try:
        return json.loads(request.body or b"{}")
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Body is not valid JSON: {exc}") from exc


@csrf_exempt
@require_http_methods(["POST"])
@require_api_key(scope="send")
def emails_create(request: HttpRequest) -> JsonResponse:
    api_key = request.api_auth.api_key  # type: ignore[attr-defined]

    rate = ratelimit_check(api_key.id)
    if not rate.allowed:
        response = _error(
            f"Rate limit exceeded — retry after {rate.retry_after}s.",
            status=429,
            name="rate_limit_exceeded",
        )
        response["Retry-After"] = str(rate.retry_after)
        response["RateLimit-Limit"] = str(rate.limit)
        response["RateLimit-Remaining"] = "0"
        return response

    try:
        body = _parse_body(request)
        clean = parse_email_request(body)
    except ValidationError as exc:
        return _error(exc.message, status=422, name=exc.code or "validation_error")

    pool = api_key.default_pool
    if pool is None:
        return _error(
            "API key has no default pool assigned. Assign one in the panel.",
            status=400,
            name="no_pool_assigned",
        )

    # Filter suppressed recipients. If *all* recipients are suppressed,
    # refuse the send up front rather than queueing a no-op.
    suppressed = Suppression.filter_suppressed(
        list(clean["to"]) + list(clean["cc"]) + list(clean["bcc"])
    )
    if suppressed:
        kept_to = [a for a in clean["to"] if a.strip().lower() not in suppressed]
        if not kept_to:
            return _error(
                f"All recipients are suppressed: {', '.join(sorted(suppressed))}",
                status=422,
                name="recipient_suppressed",
            )
        clean["to"] = kept_to
        clean["cc"] = [a for a in clean["cc"] if a.strip().lower() not in suppressed]
        clean["bcc"] = [a for a in clean["bcc"] if a.strip().lower() not in suppressed]

    idem_key = request.META.get("HTTP_IDEMPOTENCY_KEY", "").strip()
    fingerprint = fingerprint_request(body) if idem_key else ""

    # Idempotency replay check.
    if idem_key:
        existing = (
            IdempotencyRecord.objects.select_related("message")
            .filter(api_key=api_key, key=idem_key)
            .first()
        )
        if existing and not existing.is_expired:
            if existing.fingerprint != fingerprint:
                return _error(
                    "Idempotency-Key reused with a different request body.",
                    status=409,
                    name="idempotency_conflict",
                )
            return JsonResponse({"id": existing.message.id}, status=200)

    workspace = WorkspaceSettings.load()

    with transaction.atomic():
        message = Message(
            api_key=api_key,
            pool=pool,
            from_address=clean["from_address"],
            to=clean["to"],
            cc=clean["cc"],
            bcc=clean["bcc"],
            reply_to=clean["reply_to"],
            subject=clean["subject"],
            headers=clean["headers"],
            tags=clean["tags"],
            idempotency_key=idem_key,
            idempotency_fingerprint=fingerprint,
        )
        message.set_body(html=clean["html"], text=clean["text"])
        if clean["scheduled_at"]:
            from datetime import datetime

            message.scheduled_at = datetime.fromisoformat(
                clean["scheduled_at"].replace("Z", "+00:00")
            )
            message.status = Message.STATUS_SCHEDULED
        message.set_retention(workspace.retention_days)
        message.save()

        for a in clean["attachments"]:
            Attachment.objects.create(message=message, **a)

        if idem_key:
            try:
                IdempotencyRecord.objects.create(
                    api_key=api_key,
                    key=idem_key,
                    fingerprint=fingerprint,
                    message=message,
                )
            except IntegrityError:
                # Another concurrent request created the record first — re-fetch and return that one.
                existing = IdempotencyRecord.objects.select_related("message").get(
                    api_key=api_key, key=idem_key
                )
                transaction.set_rollback(True)
                return JsonResponse({"id": existing.message.id}, status=200)

    # Enqueue unless scheduled — schedule handling lives in v2.
    if message.status == Message.STATUS_QUEUED:
        try:
            from apps.sending.tasks import enqueue_message

            enqueue_message(message)
        except Exception as exc:  # noqa: BLE001 — never fail the request because Q2 isn't up
            log.exception("enqueue failed for %s: %s", message.id, exc)

    return JsonResponse({"id": message.id}, status=200)


@csrf_exempt
@require_http_methods(["GET"])
@require_api_key(scope="read")
def emails_retrieve(request: HttpRequest, message_id: str) -> JsonResponse:
    try:
        message = Message.objects.get(pk=message_id)
    except Message.DoesNotExist:
        return _error("Message not found.", status=404, name="not_found")
    return JsonResponse(serialize_message(message), status=200)
