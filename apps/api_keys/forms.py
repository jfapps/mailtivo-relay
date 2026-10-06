from __future__ import annotations

from django import forms

from apps.pools.models import Pool

from .models import APIKey

SCOPE_CHOICES = [
    (APIKey.SCOPE_ALL, "Full access (read + send)"),
    (APIKey.SCOPE_SEND, "Send only"),
    (APIKey.SCOPE_READ, "Read only"),
]


class IssueKeyForm(forms.Form):
    name = forms.CharField(max_length=120)
    scopes = forms.ChoiceField(choices=SCOPE_CHOICES, initial=APIKey.SCOPE_ALL)
    default_pool = forms.ModelChoiceField(
        queryset=Pool.objects.all(),
        required=False,
        empty_label="— none (key cannot send until assigned) —",
    )

    def clean_scopes(self) -> list[str]:
        return [self.cleaned_data["scopes"]]
