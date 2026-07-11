from __future__ import annotations

import json

from django import forms

from apps.core.encryption import decrypt

from .models import Connection


class ConnectionForm(forms.ModelForm):
    """One form covers all providers — provider-specific fields render conditionally."""

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
    # SES credentials. access_key_id is non-secret (shown prefilled on edit); the
    # secret access key is write-only. Both are packed into credentials_encrypted.
    aws_access_key_id = forms.CharField(
        required=False,
        help_text="SES: AWS access key ID (e.g. AKIA...).",
    )
    aws_secret_access_key = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text="SES: AWS secret access key. Leave blank to keep the existing value.",
    )

    class Meta:
        model = Connection
        fields = ["name", "provider_code", "base_url", "aws_region", "ses_configuration_set", "sns_topic_arn", "daily_cap", "enabled"]

    def __init__(self, *args, instance: Connection | None = None, **kwargs):
        super().__init__(*args, instance=instance, **kwargs)
        if instance and instance.webhook_public_key_pem:
            self.fields["webhook_public_key_pem"].initial = instance.webhook_public_key_pem
        # Prefill the (non-secret) SES access key id when editing.
        if instance and instance.provider_code == Connection.PROVIDER_SES and instance.credentials_encrypted:
            try:
                creds = json.loads(decrypt(bytes(instance.credentials_encrypted)))
                self.fields["aws_access_key_id"].initial = creds.get("access_key_id", "")
            except (ValueError, TypeError):
                pass

    def clean(self) -> dict:
        cleaned = super().clean()
        provider = cleaned.get("provider_code")
        if provider == Connection.PROVIDER_POSTAL and not cleaned.get("base_url"):
            self.add_error("base_url", "Required for Postal.")
        if provider == Connection.PROVIDER_SES:
            if not cleaned.get("aws_region"):
                self.add_error("aws_region", "Required for SES.")
            if not self.instance.pk:
                if not cleaned.get("aws_access_key_id"):
                    self.add_error("aws_access_key_id", "Required for new SES connections.")
                if not cleaned.get("aws_secret_access_key"):
                    self.add_error("aws_secret_access_key", "Required for new SES connections.")
        # On create, api_key is required for Postal/Resend (we can't keep an empty one).
        if (
            not self.instance.pk
            and provider in (Connection.PROVIDER_POSTAL, Connection.PROVIDER_RESEND)
            and not cleaned.get("api_key")
        ):
            self.add_error("api_key", "Required for new connections.")
        return cleaned
