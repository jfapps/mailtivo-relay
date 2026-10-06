from __future__ import annotations

import re

from django import forms

from .models import DomainIdentity

_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def normalize_domain(value: str) -> str:
    value = (value or "").strip().lower().rstrip(".")
    if not value or len(value) > 253:
        raise forms.ValidationError("Enter a valid domain.")
    try:
        ascii_domain = value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise forms.ValidationError("Enter a valid domain.") from exc
    labels = ascii_domain.split(".")
    if len(labels) < 2 or any(not _LABEL_RE.fullmatch(label) for label in labels):
        raise forms.ValidationError("Enter a valid domain.")
    return ascii_domain


class DomainIdentityForm(forms.ModelForm):
    class Meta:
        model = DomainIdentity
        fields = ["domain", "label", "dkim_selector", "certificate_type"]
        widgets = {
            "domain": forms.TextInput(attrs={"placeholder": "example.com", "class": "input"}),
            "label": forms.TextInput(attrs={"placeholder": "Brand or product", "class": "input"}),
            "dkim_selector": forms.TextInput(attrs={"placeholder": "e.g. s1", "class": "input"}),
            "certificate_type": forms.Select(attrs={"class": "input"}),
        }

    def clean_domain(self) -> str:
        return normalize_domain(self.cleaned_data["domain"])

    def clean_dkim_selector(self) -> str:
        value = (self.cleaned_data.get("dkim_selector") or "").strip().lower()
        if value and not _LABEL_RE.fullmatch(value):
            raise forms.ValidationError("Use only letters, numbers and hyphens.")
        return value
