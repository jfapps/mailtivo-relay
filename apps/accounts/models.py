from __future__ import annotations

import secrets
from datetime import timedelta
from hashlib import sha256

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class UserManager(BaseUserManager["User"]):
    use_in_migrations = True

    def _create_user(self, email: str, password: str | None, **extra: object) -> "User":
        if not email:
            raise ValueError("Users must have an email address")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_user(self, email: str, password: str | None = None, **extra: object) -> "User":
        extra.setdefault("is_workspace_admin", False)
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra)

    def create_superuser(self, email: str, password: str | None = None, **extra: object) -> "User":
        extra["is_workspace_admin"] = True
        extra["is_staff"] = True
        extra["is_superuser"] = True
        return self._create_user(email, password, **extra)


class User(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(unique=True)
    display_name = models.CharField(max_length=120, blank=True)
    is_workspace_admin = models.BooleanField(default=False)
    is_staff = models.BooleanField(default=False)  # only true for the very first owner
    is_active = models.BooleanField(default=True)
    date_joined = models.DateTimeField(default=timezone.now)
    last_login_ip = models.GenericIPAddressField(null=True, blank=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    objects = UserManager()

    class Meta:
        ordering = ("email",)

    def __str__(self) -> str:
        return self.email

    def get_short_name(self) -> str:
        return self.display_name or self.email.split("@", 1)[0]


def _hash_token(raw: str) -> str:
    return sha256(raw.encode("utf-8")).hexdigest()


class MagicLinkToken(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="magic_links")
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    ip_issued_from = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["user", "used_at"])]

    @classmethod
    def issue(cls, user: User, *, ttl_minutes: int | None = None, ip: str | None = None) -> tuple["MagicLinkToken", str]:
        raw = secrets.token_urlsafe(48)
        ttl = ttl_minutes if ttl_minutes is not None else getattr(settings, "MAGIC_LINK_TTL_MINUTES", 15)
        token = cls.objects.create(
            user=user,
            token_hash=_hash_token(raw),
            expires_at=timezone.now() + timedelta(minutes=ttl),
            ip_issued_from=ip,
        )
        return token, raw

    @classmethod
    def consume(cls, raw: str) -> User | None:
        try:
            token = cls.objects.select_related("user").get(token_hash=_hash_token(raw))
        except cls.DoesNotExist:
            return None
        if token.used_at is not None:
            return None
        if token.expires_at <= timezone.now():
            return None
        token.used_at = timezone.now()
        token.save(update_fields=["used_at"])
        return token.user


class WorkspaceSettings(models.Model):
    """Singleton row holding workspace-wide configuration."""

    SINGLETON_PK = 1

    name = models.CharField(max_length=120, default="My Workspace")
    from_email_default = models.EmailField(blank=True)
    retention_days = models.PositiveIntegerField(default=30)
    # Master switch for the daily retention purge (apps.messages_api.tasks).
    # When off, expired bodies are kept until purged manually from Data & storage.
    retention_enabled = models.BooleanField(default=True)

    google_oauth_enabled = models.BooleanField(default=False)
    google_oauth_client_id = models.CharField(max_length=255, blank=True)
    google_oauth_client_secret_encrypted = models.BinaryField(blank=True, default=b"")

    # Spam analysis integrations (see apps.spam_analysis). One active AI provider
    # for content scoring + an optional Google Safe Browsing key for link checks.
    AI_PROVIDER_OPENAI = "openai"
    AI_PROVIDER_ANTHROPIC = "anthropic"
    AI_PROVIDER_GEMINI = "gemini"
    AI_PROVIDER_CHOICES = [
        (AI_PROVIDER_OPENAI, "OpenAI"),
        (AI_PROVIDER_ANTHROPIC, "Anthropic"),
        (AI_PROVIDER_GEMINI, "Google Gemini"),
    ]

    ai_provider = models.CharField(max_length=20, blank=True, choices=AI_PROVIDER_CHOICES)
    ai_model = models.CharField(max_length=80, blank=True)
    ai_api_key_encrypted = models.BinaryField(blank=True, default=b"")

    safe_browsing_enabled = models.BooleanField(default=False)
    safe_browsing_api_key_encrypted = models.BinaryField(blank=True, default=b"")

    branding_logo = models.ImageField(upload_to="branding/", blank=True, null=True)

    # Opt-in update check (see apps.core.updates). Cached results of the daily
    # GitHub Releases lookup; no telemetry is sent.
    update_check_enabled = models.BooleanField(default=True)
    latest_version = models.CharField(max_length=40, blank=True)
    latest_release_url = models.URLField(blank=True)
    update_checked_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Workspace settings"
        verbose_name_plural = "Workspace settings"

    def __str__(self) -> str:
        return self.name

    def clean(self) -> None:
        if self.pk and self.pk != self.SINGLETON_PK:
            raise ValidationError("Only one WorkspaceSettings row is allowed.")

    def save(self, *args: object, **kwargs: object) -> None:
        self.pk = self.SINGLETON_PK
        super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> "WorkspaceSettings":
        obj, _ = cls.objects.get_or_create(pk=cls.SINGLETON_PK)
        return obj


class Invitation(models.Model):
    email = models.EmailField()
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    invited_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="invitations_sent")
    is_admin = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    accepted_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="invitations_accepted")
    accepted_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)

    @classmethod
    def issue(cls, email: str, invited_by: User, *, is_admin: bool = False, ttl_days: int = 7) -> tuple["Invitation", str]:
        raw = secrets.token_urlsafe(48)
        inv = cls.objects.create(
            email=cls._meta.get_field("email").to_python(email),
            token_hash=_hash_token(raw),
            invited_by=invited_by,
            is_admin=is_admin,
            expires_at=timezone.now() + timedelta(days=ttl_days),
        )
        return inv, raw

    @classmethod
    def lookup(cls, raw: str) -> "Invitation | None":
        try:
            inv = cls.objects.get(token_hash=_hash_token(raw))
        except cls.DoesNotExist:
            return None
        if inv.revoked_at or inv.accepted_at or inv.expires_at <= timezone.now():
            return None
        return inv
