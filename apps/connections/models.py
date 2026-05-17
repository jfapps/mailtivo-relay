from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone


class Connection(models.Model):
    """One upstream provider account (e.g. one Resend API key, one Postal server)."""

    PROVIDER_POSTAL = "postal"
    PROVIDER_RESEND = "resend"
    PROVIDER_CHOICES = [
        (PROVIDER_RESEND, "Resend"),
        (PROVIDER_POSTAL, "Postal"),
    ]

    STATUS_UNKNOWN = "unknown"
    STATUS_HEALTHY = "healthy"
    STATUS_DEGRADED = "degraded"
    STATUS_DOWN = "down"
    STATUS_CHOICES = [
        (STATUS_UNKNOWN, "Unknown"),
        (STATUS_HEALTHY, "Healthy"),
        (STATUS_DEGRADED, "Degraded"),
        (STATUS_DOWN, "Down"),
    ]

    name = models.CharField(max_length=120, unique=True)
    provider_code = models.CharField(max_length=32, choices=PROVIDER_CHOICES)
    base_url = models.URLField(blank=True, help_text="Postal: required (e.g. https://postal.example.com). Resend: optional override.")

    # Per-Connection credentials — encrypted with Fernet (apps.core.encryption).
    credentials_encrypted = models.BinaryField(blank=True, default=b"")
    # Webhook verification material:
    #   - Resend: HMAC signing secret ("whsec_..." Svix shared secret) — encrypted.
    #   - Postal: RSA public key PEM (not secret, but stored alongside for symmetry).
    webhook_secret_encrypted = models.BinaryField(blank=True, default=b"")
    webhook_public_key_pem = models.TextField(blank=True)

    daily_cap = models.PositiveIntegerField(default=0, help_text="Optional. 0 = unlimited.")
    enabled = models.BooleanField(default=True)

    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_UNKNOWN)
    last_health_check_at = models.DateTimeField(null=True, blank=True)
    last_health_message = models.CharField(max_length=255, blank=True)

    # Rolling-window counters that the router consults for health-aware skip.
    # The window resets every WINDOW_SECONDS via tick_window().
    recent_5xx_count = models.PositiveIntegerField(default=0)
    recent_total = models.PositiveIntegerField(default=0)
    recent_window_start = models.DateTimeField(null=True, blank=True)
    skip_until = models.DateTimeField(null=True, blank=True)
    WINDOW_SECONDS = 60

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="connections_created",
    )

    class Meta:
        ordering = ("name",)

    def __str__(self) -> str:
        return f"{self.name} [{self.get_provider_code_display()}]"

    @property
    def is_healthy(self) -> bool:
        return self.status == self.STATUS_HEALTHY

    def adapter(self):
        """Return an instantiated adapter for this connection."""
        from .adapters import get_adapter_class

        return get_adapter_class(self.provider_code)(self)

    def tick_window(self) -> None:
        """Reset rolling counters if the current window has expired. Mutates
        the in-memory instance; callers persist via Connection.objects.update."""
        from datetime import timedelta

        now = timezone.now()
        start = self.recent_window_start
        if start is None or (now - start) >= timedelta(seconds=self.WINDOW_SECONDS):
            self.recent_5xx_count = 0
            self.recent_total = 0
            self.recent_window_start = now

    def error_rate_pct(self) -> float:
        if not self.recent_total:
            return 0.0
        return (self.recent_5xx_count / self.recent_total) * 100.0
