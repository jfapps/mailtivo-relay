from __future__ import annotations

from django import forms

from apps.messages_api.serializers import EMAIL_RE, _strip_angle
from apps.pools.models import Pool


class TestSendForm(forms.Form):
    """Compose a one-off test email and push it through the relay pipeline.

    Mirrors the validation the public /api/v1/emails endpoint applies (see
    apps.messages_api.serializers) so a test send behaves exactly like a real one.
    """

    pool = forms.ModelChoiceField(
        queryset=Pool.objects.all(),
        empty_label=None,
        widget=forms.Select(attrs={"class": "input"}),
        help_text="Which pool routes this send. Members are picked by the pool's strategy.",
    )
    from_address = forms.CharField(
        max_length=255,
        label="From",
        widget=forms.TextInput(attrs={"class": "input", "placeholder": "Acme <hi@yourdomain.com>"}),
        help_text="Sender address. May be \"Name <addr@domain>\". Must be a domain your provider is allowed to send for.",
    )
    to_address = forms.CharField(
        max_length=255,
        label="To",
        widget=forms.TextInput(attrs={"class": "input", "placeholder": "you@example.com"}),
        help_text="Where the test lands. Defaults to your account email.",
    )
    subject = forms.CharField(
        max_length=998,
        required=False,
        widget=forms.TextInput(attrs={"class": "input"}),
    )
    body = forms.CharField(
        widget=forms.Textarea(attrs={"class": "input", "rows": 6}),
        help_text="Plain-text body. Sent as both the text and HTML parts.",
    )

    def _validate_email(self, value: str, *, field: str) -> str:
        cleaned = (value or "").strip()
        if not EMAIL_RE.match(_strip_angle(cleaned)):
            raise forms.ValidationError(f"Enter a valid {field} email address.")
        return cleaned

    def clean_from_address(self) -> str:
        return self._validate_email(self.cleaned_data["from_address"], field="from")

    def clean_to_address(self) -> str:
        return self._validate_email(self.cleaned_data["to_address"], field="to")
