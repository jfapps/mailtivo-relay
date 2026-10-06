from __future__ import annotations

from django import forms
from django.contrib.auth import authenticate
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from .models import User


class LoginForm(forms.Form):
    email = forms.EmailField()
    password = forms.CharField(widget=forms.PasswordInput, required=False)

    def __init__(self, *args: object, request=None, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.request = request
        self.user: User | None = None

    def clean(self) -> dict:
        cleaned = super().clean()
        email = cleaned.get("email")
        password = cleaned.get("password") or ""
        if email and password:
            self.user = authenticate(self.request, username=email, password=password)
            if self.user is None:
                raise ValidationError("Invalid email or password.")
        return cleaned


class MagicLinkRequestForm(forms.Form):
    email = forms.EmailField()


class OnboardingForm(forms.Form):
    workspace_name = forms.CharField(max_length=120)
    email = forms.EmailField()
    display_name = forms.CharField(max_length=120, required=False)
    password = forms.CharField(widget=forms.PasswordInput)
    password_confirm = forms.CharField(widget=forms.PasswordInput)

    def clean(self) -> dict:
        cleaned = super().clean()
        pw = cleaned.get("password") or ""
        confirm = cleaned.get("password_confirm") or ""
        if pw != confirm:
            # Validation copy, not a credential literal.
            raise ValidationError({"password_confirm": "Passwords do not match."})  # nosec B105
        validate_password(pw)
        return cleaned
