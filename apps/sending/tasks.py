"""Django-Q2 task that drives a Message through the pool router to an adapter.

Lifecycle:
  1. Load the queued Message.
  2. Pick a PoolMember via apps.pools.routing.select_member().
  3. Call the adapter's send(). On success: persist + return.
  4. On AdapterError(temporary): mark connection degraded, record attempt,
     fall through to the next member up to MAX_ATTEMPTS.
  5. On AdapterError(permanent): record attempt; do *not* retry on a different
     member (the request body is bad — try-next won't help).
  6. If we exhaust all members or hit MAX_ATTEMPTS, mark Message failed.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.connections.adapters import AdapterError, PERMANENT_FAILURE
from apps.connections.models import Connection
from apps.messages_api.models import Message
from apps.pools.models import PoolMember
from apps.pools.routing import NoMemberAvailable, record_send, select_member

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
DEGRADED_COOLDOWN = timedelta(minutes=5)


def _mark_connection_failure(connection: Connection, *, status_code: int | None) -> None:
    """Bump 5xx counters and apply a cool-down on the connection."""
    connection.tick_window()
    fields: dict[str, object] = {
        "recent_total": connection.recent_total + 1,
        "recent_window_start": connection.recent_window_start,
        "status": Connection.STATUS_DEGRADED,
        "last_health_check_at": timezone.now(),
    }
    if status_code is not None and status_code >= 500:
        fields["recent_5xx_count"] = connection.recent_5xx_count + 1
        fields["skip_until"] = timezone.now() + DEGRADED_COOLDOWN
    Connection.objects.filter(pk=connection.pk).update(**fields)


def _mark_connection_success(connection: Connection) -> None:
    connection.tick_window()
    Connection.objects.filter(pk=connection.pk).update(
        recent_total=connection.recent_total + 1,
        recent_window_start=connection.recent_window_start,
        status=Connection.STATUS_HEALTHY,
        last_health_check_at=timezone.now(),
        last_health_message="OK",
        skip_until=None,
    )


def send_message(message_id: str) -> str:
    """Q2 entrypoint. Returns the final Message status."""
    try:
        message = Message.objects.select_related("pool").get(pk=message_id)
    except Message.DoesNotExist:
        log.warning("send_message: message_id %s not found", message_id)
        return "missing"

    if message.status in Message.TERMINAL_STATUSES or message.status == Message.STATUS_SENT:
        return message.status

    pool = message.pool
    if pool is None:
        message.status = Message.STATUS_FAILED
        message.last_error = "No pool assigned"
        message.save(update_fields=["status", "last_error"])
        return message.status

    Message.objects.filter(pk=message.pk).update(status=Message.STATUS_SENDING)
    message.status = Message.STATUS_SENDING

    tried: set[int] = set()
    last_error = ""

    for attempt in range(MAX_ATTEMPTS):
        try:
            member: PoolMember = select_member(pool, attempt=attempt, exclude=tried)
        except NoMemberAvailable as exc:
            last_error = str(exc)
            break

        tried.add(member.id)
        connection = member.connection
        adapter = connection.adapter()

        try:
            result = adapter.send(message=message.to_adapter_payload())
        except AdapterError as exc:
            last_error = str(exc)
            message.append_attempt(
                connection_id=connection.id, ok=False, error=last_error,
            )
            _mark_connection_failure(connection, status_code=exc.status_code)
            if exc.kind == PERMANENT_FAILURE:
                # Body is bad — no point trying another member.
                break
            # TEMPORARY_FAILURE — fall through to the next member.
            continue
        except Exception as exc:  # noqa: BLE001
            last_error = f"Unexpected adapter error: {exc}"
            message.append_attempt(connection_id=connection.id, ok=False, error=last_error)
            _mark_connection_failure(connection, status_code=None)
            continue

        # Success — persist and exit.
        with transaction.atomic():
            message.append_attempt(
                connection_id=connection.id,
                ok=True,
                provider_id=result.provider_message_id,
            )
            message.connection = connection
            message.provider_message_id = result.provider_message_id
            message.recipient_tokens = dict(result.recipient_tokens or {})
            message.status = Message.STATUS_SENT
            message.sent_at = timezone.now()
            message.last_error = ""
            message.save(update_fields=[
                "connection", "provider_message_id", "recipient_tokens",
                "status", "sent_at", "last_error", "attempts",
            ])
            record_send(member)
            _mark_connection_success(connection)
        return message.status

    # All attempts exhausted.
    message.status = Message.STATUS_FAILED
    message.last_error = last_error[:500]
    message.save(update_fields=["status", "last_error", "attempts"])
    return message.status


def enqueue_message(message: Message) -> str:
    """Enqueue a freshly-persisted Message onto Django-Q2 and return the task id."""
    from django_q.tasks import async_task

    return async_task(
        "apps.sending.tasks.send_message",
        str(message.id),
        task_name=f"send-{message.id}",
    )


def capture_message(message: Message) -> str:
    """Test-mode 'send': store the email, never dispatch it to a provider.

    Used for sends routed through a capture (sandbox) pool. Runs synchronously in
    the request path — no Q2 worker required — so the captured email shows up in
    the Test Inbox immediately. Returns the final Message status.
    """
    message.sandbox = True
    message.status = Message.STATUS_CAPTURED
    message.sent_at = timezone.now()
    message.provider_message_id = f"captured-{message.id}"
    message.last_error = ""
    message.append_attempt(
        connection_id=None, ok=True, provider_id=message.provider_message_id,
    )
    message.save(update_fields=[
        "sandbox", "status", "sent_at", "provider_message_id", "last_error", "attempts",
    ])
    return message.status
