from __future__ import annotations

from django import forms

from apps.connections.models import Connection

from .models import Pool, PoolMember, WarmupPlan


class PoolForm(forms.ModelForm):
    class Meta:
        model = Pool
        fields = [
            "name",
            "mode",
            "routing_strategy",
            "health_skip_enabled",
            "recent_5xx_threshold",
            "recent_bounce_rate_threshold_pct",
        ]


class PoolMemberForm(forms.ModelForm):
    connection = forms.ModelChoiceField(queryset=Connection.objects.filter(enabled=True))

    class Meta:
        model = PoolMember
        # `enabled` is intentionally absent here so the model default (True) wins
        # on create — toggling lives on the per-row HTMX endpoint (member_update).
        fields = ["connection", "weight", "priority"]

    def __init__(self, *args, pool: Pool | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._pool = pool
        if pool is not None:
            already = pool.members.values_list("connection_id", flat=True)
            qs = self.fields["connection"].queryset.exclude(id__in=already)
            if self.instance and self.instance.pk:
                qs = qs | Connection.objects.filter(pk=self.instance.connection_id)
            self.fields["connection"].queryset = qs.distinct()


class WarmupForm(forms.ModelForm):
    daily_caps_csv = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3, "class": "input font-mono text-xs"}),
        help_text="Comma-separated daily caps for the 'Custom' curve.",
    )

    class Meta:
        model = WarmupPlan
        fields = ["curve", "start_date", "enabled"]
        widgets = {
            "start_date": forms.DateInput(attrs={"type": "date", "class": "input"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.daily_caps_json:
            self.fields["daily_caps_csv"].initial = ", ".join(str(n) for n in self.instance.daily_caps_json)

    def clean(self) -> dict:
        cleaned = super().clean()
        curve = cleaned.get("curve")
        if curve == WarmupPlan.CURVE_CUSTOM:
            raw = (cleaned.get("daily_caps_csv") or "").strip()
            if not raw:
                self.add_error("daily_caps_csv", "Custom curve needs at least one cap.")
            else:
                caps: list[int] = []
                for part in raw.split(","):
                    part = part.strip()
                    if not part:
                        continue
                    try:
                        n = int(part)
                    except ValueError:
                        self.add_error("daily_caps_csv", f"'{part}' is not an integer.")
                        return cleaned
                    if n < 0:
                        self.add_error("daily_caps_csv", "Caps must be non-negative.")
                        return cleaned
                    caps.append(n)
                cleaned["_caps_parsed"] = caps
        return cleaned

    def save(self, commit: bool = True) -> WarmupPlan:
        plan: WarmupPlan = super().save(commit=False)
        caps = self.cleaned_data.get("_caps_parsed")
        if caps is not None:
            plan.daily_caps_json = caps
        elif plan.curve != WarmupPlan.CURVE_CUSTOM:
            plan.daily_caps_json = []
        if commit:
            plan.save()
        return plan
