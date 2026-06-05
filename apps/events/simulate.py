"""Test Inbox event simulator.

Lets an operator synthesize a delivery event (delivered / opened / clicked /
bounced / complained) for a *sandbox* (captured) message and fan it out to the
configured outbound webhook endpoints — so you can exercise your webhook-handling
code end-to-end without a real provider.

This deliberately mirrors apps.events.views._ingest, minus the signature /
provider-parsing glue, and with two intentional differences:
  * events are flagged simulated=True (and marked test in the outbound payload), and
  * we do NOT auto-suppress on bounce/complaint — a test must not mutate the
    global (live) suppression list.
"""
from __future__ import annotations

import uuid

from django.db import transaction
from django.utils import timezone

from apps.events.models import Event
from apps.events.outbound import fan_out
from apps.events.views import _STATUS_FOR_EVENT, _apply_status
from apps.messages_api.models import Message

# Event types an operator can simulate, in lifecycle order. A subset of
# NormalizedEvent.EVENT_TYPES — the post-send events that are meaningful to test.
SIMULATABLE_TYPES = ("delivered", "opened", "clicked", "bounced", "complained")


class SimulateError(ValueError):
    """Raised when a simulation request is invalid (bad type / non-sandbox message)."""


def simulate_event(message: Message, event_type: str) -> Event:
    """Create a simulated `event_type` Event for a sandbox message, update its
    status, and fan out to outbound webhooks. Returns the created Event."""
    if not message.sandbox:
        raise SimulateError("Events can only be simulated for sandbox (captured) messages.")
    if event_type not in SIMULATABLE_TYPES:
        raise SimulateError(f"Cannot simulate event type {event_type!r}.")

    recipient = next((r for r in (message.to or []) if r), "")
    now = timezone.now()

    with transaction.atomic():
        event = Event.objects.create(
            message=message,
            connection=None,
            type=event_type,
            provider_event_id=f"sim-{uuid.uuid4().hex}",
            provider_message_id=message.provider_message_id,
            recipient=recipient,
            occurred_at=now,
            link_url="https://example.com/simulated-link" if event_type == "clicked" else "",
            user_agent="Mailtivo-Relay Simulator" if event_type in ("opened", "clicked") else "",
            bounce_reason="Simulated hard bounce" if event_type == "bounced" else "",
            raw={"simulated": True, "source": "test-inbox"},
            simulated=True,
        )
        _apply_status(message, _STATUS_FOR_EVENT.get(event_type))
        fan_out(event, message=message)

    return event
