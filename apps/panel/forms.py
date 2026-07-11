from __future__ import annotations

from django import forms

from apps.accounts.models import WorkspaceSettings


class WorkspaceSettingsForm(forms.ModelForm):
    class Meta:
        model = WorkspaceSettings
        fields = ["name", "from_email_default", "retention_days", "retention_enabled"]

    def clean_retention_days(self) -> int:
        value = self.cleaned_data["retention_days"]
        if value < 1 or value > 365:
            raise forms.ValidationError("Retention must be between 1 and 365 days.")
        return value


class GoogleOAuthForm(forms.Form):
    enabled = forms.BooleanField(required=False)
    client_id = forms.CharField(required=False, max_length=255)
    client_secret = forms.CharField(required=False, widget=forms.PasswordInput, max_length=255)

    def clean(self) -> dict:
        cleaned = super().clean()
        if cleaned.get("enabled"):
            if not cleaned.get("client_id"):
                self.add_error("client_id", "Required when Google sign-in is enabled.")
            # Secret only required at first enable — if blank on edit, we preserve the stored value.
        return cleaned


class InvitationForm(forms.Form):
    email = forms.EmailField()
    is_admin = forms.BooleanField(required=False)
