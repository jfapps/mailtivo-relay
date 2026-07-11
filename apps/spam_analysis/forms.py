from __future__ import annotations

from django import forms

from apps.accounts.models import WorkspaceSettings


class AIProviderForm(forms.Form):
    provider = forms.ChoiceField(
        required=False,
        choices=[("", "— None —")] + WorkspaceSettings.AI_PROVIDER_CHOICES,
    )
    model = forms.CharField(required=False, max_length=80)
    api_key = forms.CharField(required=False, widget=forms.PasswordInput, max_length=255)

    def clean(self) -> dict:
        cleaned = super().clean()
        provider = cleaned.get("provider")
        # Require a key the first time a provider is selected; the view preserves
        # an existing stored key when this is left blank on a later edit.
        if provider and not cleaned.get("api_key") and not self.initial.get("has_key"):
            self.add_error("api_key", "An API key is required for the selected provider.")
        return cleaned


class SafeBrowsingForm(forms.Form):
    enabled = forms.BooleanField(required=False)
    api_key = forms.CharField(required=False, widget=forms.PasswordInput, max_length=255)

    def clean(self) -> dict:
        cleaned = super().clean()
        if cleaned.get("enabled") and not cleaned.get("api_key") and not self.initial.get("has_key"):
            self.add_error("api_key", "An API key is required to enable Safe Browsing.")
        return cleaned
