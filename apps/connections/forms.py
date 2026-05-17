from __future__ import annotations

from django import forms

from .models import Connection


class ConnectionForm(forms.ModelForm):
    """One form covers both providers — provider-specific fields render conditionally."""

    api_key = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="Leave blank to keep the existing key.",
    )
    webhook_secret = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="Resend webhook signing secret (starts with whsec_...). Leave blank to keep the existing value.",
    )
    webhook_public_key_pem = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 6, "class": "input font-mono text-xs"}),
        help_text="Postal: paste the RSA PUBLIC KEY PEM of your Postal instance (config/signing.key public half).",
    )

    class Meta:
        model = Connection
        fields = ["name", "provider_code", "base_url", "daily_cap", "enabled"]

    def __init__(self, *args, instance: Connection | None = None, **kwargs):
        super().__init__(*args, instance=instance, **kwargs)
        if instance and instance.webhook_public_key_pem:
            self.fields["webhook_public_key_pem"].initial = instance.webhook_public_key_pem

    def clean(self) -> dict:
        cleaned = super().clean()
        provider = cleaned.get("provider_code")
        if provider == Connection.PROVIDER_POSTAL and not cleaned.get("base_url"):
            self.add_error("base_url", "Required for Postal.")
        if provider == Connection.PROVIDER_RESEND:
            # base_url is optional (defaults to api.resend.com); webhook_public_key_pem irrelevant
            pass
        # On create, api_key is required (we can't keep an empty one).
        if not self.instance.pk and not cleaned.get("api_key"):
            self.add_error("api_key", "Required for new connections.")
        return cleaned
