"""Retention purge — drops expired message bodies while keeping metadata + events.

Runs daily on django-q (registered in apps.core.apps) and on demand from the
Data & storage page. Purging clears `body_encrypted`, deletes the message's
attachments, and stamps `body_purged_at`; the Message row itself, its status
history, and its events are kept, which is exactly what the Settings page
promises ("bodies and attachments are purged; metadata + events are retained").
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from apps.accounts.models import WorkspaceSettings
from apps.messages_api.models import Attachment, IdempotencyRecord, Message, PurgeRun

log = logging.getLogger(__name__)

BATCH_SIZE = 500

# Never purge a message that hasn't finished its lifecycle — a scheduled send
# may legitimately outlive a short retention window and still needs its body.
_IN_FLIGHT_STATUSES = (
    Message.STATUS_QUEUED,
    Message.STATUS_SCHEDULED,
    Message.STATUS_SENDING,
)


def purge_expired(
    *,
    trigger: str = PurgeRun.TRIGGER_SCHEDULED,
    older_than_days: int | None = None,
    user_id: str | None = None,
) -> dict:
    """Purge bodies + attachments of expired messages. Returns run counters.

    Scheduled runs honor the WorkspaceSettings.retention_enabled switch;
    manual runs (from the panel) always execute. `older_than_days` overrides
    the per-message `retention_expires_at` with a created_at cutoff — used by
    the panel's "purge older than N days" action.
    """
    ws = WorkspaceSettings.load()
    if trigger == PurgeRun.TRIGGER_SCHEDULED and not ws.retention_enabled:
        log.info("retention purge skipped: disabled in workspace settings")
        return {"skipped": True}

    now = timezone.now()
    if older_than_days is not None:
        cutoff = now - timedelta(days=older_than_days)
        qs = Message.objects.filter(created_at__lt=cutoff)
    else:
        cutoff = now
        qs = Message.objects.filter(retention_expires_at__lt=cutoff)
    qs = qs.filter(body_purged_at__isnull=True).exclude(status__in=_IN_FLIGHT_STATUSES)

    run = PurgeRun.objects.create(trigger=trigger, cutoff=cutoff, created_by_id=user_id)

    messages_purged = 0
    attachments_purged = 0
    while True:
        batch = list(qs.values_list("id", flat=True)[:BATCH_SIZE])
        if not batch:
            break
        deleted, _ = Attachment.objects.filter(message_id__in=batch).delete()
        attachments_purged += deleted
        messages_purged += Message.objects.filter(id__in=batch).update(
            body_encrypted=b"", body_purged_at=now,
        )

    # Housekeeping in the same run: idempotency records past their 24h TTL.
    idempotency_purged, _ = IdempotencyRecord.objects.filter(expires_at__lte=now).delete()

    run.finished_at = timezone.now()
    run.messages_purged = messages_purged
    run.attachments_purged = attachments_purged
    run.idempotency_purged = idempotency_purged
    run.save(update_fields=[
        "finished_at", "messages_purged", "attachments_purged", "idempotency_purged",
    ])
    log.info(
        "retention purge (%s): %d messages, %d attachments, %d idempotency records",
        trigger, messages_purged, attachments_purged, idempotency_purged,
    )
    return {
        "messages_purged": messages_purged,
        "attachments_purged": attachments_purged,
        "idempotency_purged": idempotency_purged,
    }


def delete_captured_messages() -> int:
    """Hard-delete all captured (test inbox) messages, their attachments and
    simulated events. Test data only — live messages are never touched."""
    from apps.events.models import Event

    qs = Message.objects.filter(sandbox=True)
    ids = list(qs.values_list("id", flat=True))
    if not ids:
        return 0
    # Event.message is SET_NULL; delete explicitly so simulated events don't
    # linger as orphans in the events views.
    Event.objects.filter(message_id__in=ids).delete()
    deleted, per_model = qs.delete()
    return per_model.get("messages_api.Message", 0)
