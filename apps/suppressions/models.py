"""Suppression list — emails that must never be sent to again.

A v1 install is single-workspace, so we keep the table flat (no workspace FK).
Reasons are open-ended strings but the constants below are what the relay
itself writes; humans can use anything via the UI.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone


class Suppression(models.Model):
    REASON_HARD_BOUNCE = "hard_bounce"
    REASON_COMPLAINT = "complaint"
    REASON_MANUAL = "manual"
    REASON_IMPORTED = "imported"
    REASON_UNSUBSCRIBE = "unsubscribe"

    REASON_CHOICES = [
        (REASON_HARD_BOUNCE, "Hard bounce"),
        (REASON_COMPLAINT, "Complaint"),
        (REASON_MANUAL, "Manual"),
        (REASON_IMPORTED, "Imported"),
        (REASON_UNSUBSCRIBE, "Unsubscribed"),
    ]

    email = models.EmailField(unique=True, db_index=True)
    reason = models.CharField(max_length=32, default=REASON_MANUAL)
    note = models.CharField(max_length=255, blank=True)
    source_message = models.ForeignKey(
        "messages_api.Message",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="suppressions",
    )
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="suppressions_created",
    )

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"{self.email} ({self.reason})"

    @classmethod
    def is_suppressed(cls, email: str) -> bool:
        return cls.objects.filter(email__iexact=email.strip()).exists()

    @classmethod
    def add(
        cls,
        email: str,
        *,
        reason: str = REASON_MANUAL,
        note: str = "",
        source_message=None,
        created_by=None,
    ) -> Suppression | None:
        """Idempotent insert; returns the row or None if the email is invalid."""
        cleaned = (email or "").strip().lower()
        if "@" not in cleaned:
            return None
        obj, _ = cls.objects.get_or_create(
            email=cleaned,
            defaults={
                "reason": reason,
                "note": note,
                "source_message": source_message,
                "created_by": created_by,
            },
        )
        return obj

    @classmethod
    def filter_suppressed(cls, emails: list[str]) -> set[str]:
        """Return the set (lowercased) of suppressed addresses from `emails`."""
        if not emails:
            return set()
        cleaned = [e.strip().lower() for e in emails if e]
        return set(
            cls.objects.filter(email__in=cleaned).values_list("email", flat=True)
        )
