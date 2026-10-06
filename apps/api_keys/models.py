from __future__ import annotations

import hashlib
import hmac
import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone

KEY_PREFIX = "mr_live_"
KEY_SECRET_LEN = 36  # base32 chars after the prefix; ~180 bits of entropy


def _hash_secret(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _gen_secret() -> tuple[str, str, str]:
    """Return (full_key, prefix_lookup, hash). prefix_lookup is the public hint shown in lists."""
    token = secrets.token_urlsafe(KEY_SECRET_LEN).rstrip("=")
    full = f"{KEY_PREFIX}{token}"
    # The first 6 chars of the suffix double as a non-secret "fingerprint" for the UI.
    return full, token[:6], _hash_secret(full)


class APIKey(models.Model):
    SCOPE_SEND = "send"
    SCOPE_READ = "read"
    SCOPE_ALL = "*"

    name = models.CharField(max_length=120)
    # The 6-character display fingerprint (e.g. "mr_live_•••a7Xc")
    prefix = models.CharField(max_length=24, db_index=True)
    hash = models.CharField(max_length=64, unique=True, db_index=True)
    scopes = models.JSONField(default=list)  # ["send", "read"] or ["*"]

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="api_keys_created",
    )
    created_at = models.DateTimeField(default=timezone.now)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    # Optional pool assignment (Day 4 wires this up).
    default_pool = models.ForeignKey(
        "pools.Pool",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="default_for_keys",
    )

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"{self.name} ({self.prefix})"

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None

    @property
    def display_secret(self) -> str:
        return f"{KEY_PREFIX}•••{self.prefix}"

    def has_scope(self, scope: str) -> bool:
        if not self.scopes:
            return False
        return self.SCOPE_ALL in self.scopes or scope in self.scopes

    # ---- factory / lookup ------------------------------------------------
    @classmethod
    def issue(
        cls,
        *,
        name: str,
        scopes: list[str],
        created_by=None,
        default_pool=None,
    ) -> tuple[APIKey, str]:
        """Create a new APIKey; return (instance, plaintext_secret).

        The plaintext is shown to the user once and never persisted.
        """
        full, prefix, h = _gen_secret()
        key = cls.objects.create(
            name=name,
            prefix=prefix,
            hash=h,
            scopes=scopes,
            created_by=created_by,
            default_pool=default_pool,
        )
        return key, full

    @classmethod
    def authenticate(cls, raw: str | None) -> APIKey | None:
        """Look up an APIKey by plaintext bearer secret. Returns None on miss / revoked."""
        if not raw or not raw.startswith(KEY_PREFIX):
            return None
        h = _hash_secret(raw)
        try:
            key = cls.objects.get(hash=h, revoked_at__isnull=True)
        except cls.DoesNotExist:
            return None
        # Cheap rate-limit-free "last used" tracker — minute-granular update.
        now = timezone.now()
        if not key.last_used_at or (now - key.last_used_at).total_seconds() > 60:
            cls.objects.filter(pk=key.pk).update(last_used_at=now)
        return key

    @staticmethod
    def constant_time_eq(a: str, b: str) -> bool:
        return hmac.compare_digest(a, b)
