"""AuditLog — append-only journal of admin/operator actions.

Stays intentionally minimal: who, what, target (free-form string), an optional
JSON detail blob, and a timestamp. Reads happen via the Settings → Audit log
panel page.
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone


class AuditLog(models.Model):
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_actions",
    )
    actor_email = models.EmailField(blank=True)  # cached so deletes don't erase history
    action = models.CharField(max_length=64, db_index=True)
    target = models.CharField(max_length=255, blank=True)
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["action", "created_at"])]

    def __str__(self) -> str:
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.actor_email or 'system'} {self.action} {self.target}"

    @classmethod
    def record(
        cls,
        actor,
        *,
        action: str,
        target: str = "",
        detail: dict | None = None,
    ) -> AuditLog:
        return cls.objects.create(
            actor=actor if getattr(actor, "is_authenticated", False) else None,
            actor_email=getattr(actor, "email", "") or "",
            action=action,
            target=target,
            detail=detail or {},
        )
