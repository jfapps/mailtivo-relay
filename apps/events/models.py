"""Unified Event model — every webhook from every provider lands here in a
single normalized shape."""
from __future__ import annotations

from django.db import models
from django.utils import timezone


class Event(models.Model):
    # Mirrors NormalizedEvent.EVENT_TYPES — kept as a plain CharField so future
    # provider additions don't require migrations.
    message = models.ForeignKey(
        "messages_api.Message",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="events",
    )
    connection = models.ForeignKey(
        "connections.Connection",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="events",
    )

    type = models.CharField(max_length=32, db_index=True)
    provider_event_id = models.CharField(max_length=255, unique=True, db_index=True)
    provider_message_id = models.CharField(max_length=255, blank=True, db_index=True)
    recipient = models.CharField(max_length=320, blank=True)
    recipient_token = models.CharField(max_length=120, blank=True, db_index=True)
    occurred_at = models.DateTimeField(db_index=True)

    link_url = models.TextField(blank=True)
    user_agent = models.CharField(max_length=500, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    bounce_reason = models.CharField(max_length=500, blank=True)

    raw = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ("-occurred_at", "-created_at")
        indexes = [
            models.Index(fields=["message", "occurred_at"]),
            models.Index(fields=["type", "occurred_at"]),
        ]

    def __str__(self) -> str:
        return f"Event<{self.type} {self.provider_event_id[:20]}>"
