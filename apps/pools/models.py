"""Pool models. Day 4 — full routing strategies + warmup."""
from __future__ import annotations

from datetime import date

from django.db import models
from django.utils import timezone


class Pool(models.Model):
    STRATEGY_WEIGHTED = "weighted"
    STRATEGY_FAILOVER = "failover"
    STRATEGY_ROUND_ROBIN = "round_robin"
    STRATEGY_CHOICES = [
        (STRATEGY_WEIGHTED, "Weighted"),
        (STRATEGY_FAILOVER, "Failover"),
        (STRATEGY_ROUND_ROBIN, "Round-robin"),
    ]

    MODE_LIVE = "live"
    MODE_CAPTURE = "capture"
    MODE_CHOICES = [
        (MODE_LIVE, "Live — route to upstream providers"),
        (MODE_CAPTURE, "Capture — store in the Test Inbox, never send"),
    ]

    name = models.CharField(max_length=120, unique=True)
    # Capture pools swallow mail into the internal Test Inbox instead of routing
    # it to a provider — a built-in sandbox for development/testing (no members,
    # no real delivery). Routing is bypassed entirely for these.
    mode = models.CharField(max_length=12, choices=MODE_CHOICES, default=MODE_LIVE)
    routing_strategy = models.CharField(max_length=24, choices=STRATEGY_CHOICES, default=STRATEGY_WEIGHTED)
    health_skip_enabled = models.BooleanField(default=True)
    recent_5xx_threshold = models.PositiveIntegerField(default=5)
    recent_bounce_rate_threshold_pct = models.PositiveIntegerField(default=10)

    # Round-robin cursor — points to the index of the next member to try.
    rr_cursor = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name",)

    def __str__(self) -> str:
        return self.name

    @property
    def is_capture(self) -> bool:
        return self.mode == self.MODE_CAPTURE


class PoolMember(models.Model):
    pool = models.ForeignKey(Pool, related_name="members", on_delete=models.CASCADE)
    connection = models.ForeignKey(
        "connections.Connection",
        related_name="pool_memberships",
        on_delete=models.CASCADE,
    )
    weight = models.PositiveIntegerField(default=1, help_text="Used by the weighted strategy. 0 disables the member.")
    priority = models.PositiveIntegerField(default=0, help_text="Lower = preferred for failover. Members at the same priority tier round-robin.")
    enabled = models.BooleanField(default=True)

    # Daily counters used to enforce warmup + per-connection daily_cap.
    daily_sent_count = models.PositiveIntegerField(default=0)
    daily_counter_date = models.DateField(default=date.today)

    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ("priority", "id")
        constraints = [
            models.UniqueConstraint(fields=["pool", "connection"], name="uniq_pool_connection"),
        ]

    def __str__(self) -> str:
        return f"{self.pool.name} → {self.connection.name}"

    def reset_daily_counter_if_needed(self) -> None:
        today = timezone.now().date()
        if self.daily_counter_date != today:
            self.daily_sent_count = 0
            self.daily_counter_date = today

    def todays_cap(self) -> int:
        """Return the effective daily cap. 0 = unlimited."""
        plan: WarmupPlan | None = getattr(self.pool, "warmup_plan", None)
        warmup_cap = plan.cap_for(timezone.now().date()) if plan and plan.enabled else 0
        connection_cap = self.connection.daily_cap or 0
        caps = [c for c in (warmup_cap, connection_cap) if c > 0]
        return min(caps) if caps else 0

    def at_cap(self) -> bool:
        self.reset_daily_counter_if_needed()
        cap = self.todays_cap()
        return cap > 0 and self.daily_sent_count >= cap


class WarmupPlan(models.Model):
    CURVE_SENDGRID = "sendgrid-standard"
    CURVE_AGGRESSIVE = "aggressive"
    CURVE_CUSTOM = "custom"
    CURVE_CHOICES = [
        (CURVE_SENDGRID, "SendGrid standard (30 days)"),
        (CURVE_AGGRESSIVE, "Aggressive (14 days)"),
        (CURVE_CUSTOM, "Custom"),
    ]

    # 30-day SendGrid-style curve: warm up volume gradually.
    SENDGRID_CAPS = [
        50, 100, 500, 1000, 2000, 5000, 10000, 20000, 40000, 70000,
        100000, 150000, 200000, 300000, 400000, 500000, 750000,
        1_000_000, 1_500_000, 2_000_000, 2_500_000, 3_000_000, 3_500_000,
        4_000_000, 4_500_000, 5_000_000, 5_500_000, 6_000_000, 6_500_000, 7_000_000,
    ]
    AGGRESSIVE_CAPS = [
        500, 2000, 5000, 10000, 25000, 50000, 100000,
        250000, 500000, 1_000_000, 2_000_000, 3_500_000, 5_000_000, 7_500_000,
    ]

    pool = models.OneToOneField(Pool, related_name="warmup_plan", on_delete=models.CASCADE)
    curve = models.CharField(max_length=32, choices=CURVE_CHOICES, default=CURVE_SENDGRID)
    start_date = models.DateField(default=date.today)
    daily_caps_json = models.JSONField(default=list, blank=True, help_text="Custom curve caps. Ignored for preset curves.")
    enabled = models.BooleanField(default=False)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Warmup({self.pool.name}, {self.curve})"

    def caps(self) -> list[int]:
        if self.curve == self.CURVE_SENDGRID:
            return self.SENDGRID_CAPS
        if self.curve == self.CURVE_AGGRESSIVE:
            return self.AGGRESSIVE_CAPS
        return list(self.daily_caps_json or [])

    def cap_for(self, on_date: date) -> int:
        """Return the daily cap for a given date. Past the curve = unlimited (0)."""
        if not self.enabled:
            return 0
        caps = self.caps()
        if not caps:
            return 0
        offset = (on_date - self.start_date).days
        if offset < 0:
            return caps[0]
        if offset >= len(caps):
            return 0  # post-warmup -> unlimited
        return caps[offset]
