"""Message model — every send becomes a row here.

Bodies (html + text) are stored Fernet-encrypted at rest. The plaintext is only
ever decrypted at send time inside the Q2 worker.
"""
from __future__ import annotations

import json
from datetime import timedelta
from typing import Iterable

from django.db import models
from django.utils import timezone
from ulid import ULID

from apps.core.encryption import decrypt, encrypt


def _new_ulid() -> str:
    return str(ULID())


class Message(models.Model):
    STATUS_QUEUED = "queued"
    STATUS_SENDING = "sending"
    STATUS_SENT = "sent"
    STATUS_DELIVERED = "delivered"
    STATUS_FAILED = "failed"
    STATUS_BOUNCED = "bounced"
    STATUS_COMPLAINED = "complained"
    STATUS_SCHEDULED = "scheduled"
    STATUS_CAPTURED = "captured"  # test-mode send: stored, never dispatched
    STATUS_CHOICES = [
        (STATUS_QUEUED, "Queued"),
        (STATUS_SCHEDULED, "Scheduled"),
        (STATUS_SENDING, "Sending"),
        (STATUS_SENT, "Sent"),
        (STATUS_DELIVERED, "Delivered"),
        (STATUS_FAILED, "Failed"),
        (STATUS_BOUNCED, "Bounced"),
        (STATUS_COMPLAINED, "Complained"),
        (STATUS_CAPTURED, "Captured"),
    ]

    TERMINAL_STATUSES = {
        STATUS_DELIVERED, STATUS_FAILED, STATUS_BOUNCED, STATUS_COMPLAINED, STATUS_CAPTURED,
    }

    id = models.CharField(primary_key=True, max_length=26, default=_new_ulid, editable=False)

    api_key = models.ForeignKey(
        "api_keys.APIKey", null=True, blank=True, on_delete=models.SET_NULL, related_name="messages",
    )
    pool = models.ForeignKey(
        "pools.Pool", null=True, blank=True, on_delete=models.SET_NULL, related_name="messages",
    )
    # The connection that ultimately accepted the send. Null while queued.
    connection = models.ForeignKey(
        "connections.Connection", null=True, blank=True, on_delete=models.SET_NULL, related_name="messages",
    )

    from_address = models.CharField(max_length=255)
    to = models.JSONField(default=list)
    cc = models.JSONField(default=list, blank=True)
    bcc = models.JSONField(default=list, blank=True)
    reply_to = models.JSONField(default=list, blank=True)

    subject = models.CharField(max_length=998, blank=True)  # RFC 5322 line length limit
    body_encrypted = models.BinaryField(blank=True, default=b"")
    headers = models.JSONField(default=dict, blank=True)
    tags = models.JSONField(default=list, blank=True)

    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_QUEUED, db_index=True)
    # True when the message was captured by a test/sandbox (capture) pool rather
    # than dispatched to a provider. Denormalized so the Test Inbox filter and
    # history survive the pool FK being nulled (SET_NULL).
    sandbox = models.BooleanField(default=False, db_index=True)
    provider_message_id = models.CharField(max_length=255, blank=True, db_index=True)
    # Postal returns per-recipient tokens; we keep them to correlate webhooks.
    recipient_tokens = models.JSONField(default=dict, blank=True)

    idempotency_key = models.CharField(max_length=255, blank=True, db_index=True)
    idempotency_fingerprint = models.CharField(max_length=64, blank=True)

    scheduled_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=500, blank=True)
    attempts = models.JSONField(default=list, blank=True)

    # Spam analysis (see apps.spam_analysis). Latest run only — re-analysing
    # overwrites. spam_report holds the full breakdown plus a "state" key
    # ("running" / "done" / "error") that drives the detail-page panel.
    spam_score = models.PositiveSmallIntegerField(null=True, blank=True)
    spam_verdict = models.CharField(max_length=12, blank=True)
    spam_report = models.JSONField(default=dict, blank=True)
    spam_analyzed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    retention_expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    # Set when the retention purge cleared body_encrypted + attachments.
    # Metadata and events are kept; only content is dropped.
    body_purged_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["api_key", "idempotency_key"], name="msg_idem_key_idx"),
        ]

    def __str__(self) -> str:
        return f"Message<{self.id} {self.status}>"

    # ---- body helpers ----------------------------------------------------
    def set_body(self, *, html: str = "", text: str = "") -> None:
        payload = json.dumps({"html": html or "", "text": text or ""})
        self.body_encrypted = encrypt(payload)

    def body(self) -> dict[str, str]:
        if not self.body_encrypted:
            return {"html": "", "text": ""}
        try:
            data = json.loads(decrypt(bytes(self.body_encrypted)))
        except Exception:
            return {"html": "", "text": ""}
        return {"html": data.get("html", ""), "text": data.get("text", "")}

    # ---- lifecycle helpers -----------------------------------------------
    def set_retention(self, days: int) -> None:
        self.retention_expires_at = timezone.now() + timedelta(days=days)

    def append_attempt(self, *, connection_id: int | None, ok: bool, error: str = "", provider_id: str = "") -> None:
        entry = {
            "at": timezone.now().isoformat(),
            "connection_id": connection_id,
            "ok": ok,
            "error": error[:300] if error else "",
            "provider_message_id": provider_id,
        }
        attempts = list(self.attempts or [])
        attempts.append(entry)
        self.attempts = attempts

    def recipient_list(self) -> list[str]:
        out: list[str] = []
        for bucket in (self.to, self.cc, self.bcc):
            if isinstance(bucket, Iterable):
                out.extend(str(x) for x in bucket if x)
        return out

    # ---- adapter payload --------------------------------------------------
    def to_adapter_payload(self) -> dict:
        body = self.body()
        payload: dict = {
            "from": self.from_address,
            "to": list(self.to or []),
            "subject": self.subject,
            "html": body["html"],
            "text": body["text"],
        }
        if self.cc:
            payload["cc"] = list(self.cc)
        if self.bcc:
            payload["bcc"] = list(self.bcc)
        if self.reply_to:
            payload["reply_to"] = list(self.reply_to)
        if self.headers:
            payload["headers"] = dict(self.headers)
        if self.tags:
            payload["tags"] = list(self.tags)
        if self.scheduled_at:
            payload["scheduled_at"] = self.scheduled_at.isoformat()
        if self.idempotency_key:
            payload["idempotency_key"] = self.idempotency_key
        atts = list(self.attachments.all())
        if atts:
            payload["attachments"] = [a.to_adapter_dict() for a in atts]
        return payload


class Attachment(models.Model):
    message = models.ForeignKey(Message, related_name="attachments", on_delete=models.CASCADE)
    filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=120, blank=True)
    content_id = models.CharField(max_length=120, blank=True)
    # Base64-encoded payload — usually small (<25 MB). For v1 we keep it inline.
    content_b64 = models.TextField()

    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self) -> str:
        return self.filename

    def to_adapter_dict(self) -> dict:
        d: dict = {
            "filename": self.filename,
            "content_b64": self.content_b64,
        }
        if self.content_type:
            d["content_type"] = self.content_type
        if self.content_id:
            d["content_id"] = self.content_id
        return d


class PurgeRun(models.Model):
    """One execution of the retention purge — scheduled or manually triggered.

    Kept as its own tiny table (rather than audit-log entries) so the
    Data & storage page can show purge history and counts cheaply.
    """

    TRIGGER_SCHEDULED = "scheduled"
    TRIGGER_MANUAL = "manual"
    TRIGGER_CHOICES = [
        (TRIGGER_SCHEDULED, "Scheduled"),
        (TRIGGER_MANUAL, "Manual"),
    ]

    started_at = models.DateTimeField(default=timezone.now, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    trigger = models.CharField(max_length=12, choices=TRIGGER_CHOICES, default=TRIGGER_SCHEDULED)
    # The retention cutoff this run applied (messages expiring before it were purged).
    cutoff = models.DateTimeField(null=True, blank=True)
    messages_purged = models.PositiveIntegerField(default=0)
    attachments_purged = models.PositiveIntegerField(default=0)
    idempotency_purged = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )

    class Meta:
        ordering = ("-started_at",)

    def __str__(self) -> str:
        return f"PurgeRun<{self.trigger} {self.started_at:%Y-%m-%d %H:%M} n={self.messages_purged}>"


class IdempotencyRecord(models.Model):
    """Per-API-key idempotency cache. 24-hour TTL."""

    TTL_HOURS = 24

    api_key = models.ForeignKey("api_keys.APIKey", on_delete=models.CASCADE, related_name="idempotency_records")
    key = models.CharField(max_length=255)
    fingerprint = models.CharField(max_length=64)
    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["api_key", "key"], name="uniq_apikey_idem"),
        ]
        indexes = [models.Index(fields=["expires_at"])]

    def save(self, *args, **kwargs):
        if not self.expires_at:
            self.expires_at = timezone.now() + timedelta(hours=self.TTL_HOURS)
        super().save(*args, **kwargs)

    @property
    def is_expired(self) -> bool:
        return self.expires_at <= timezone.now()
