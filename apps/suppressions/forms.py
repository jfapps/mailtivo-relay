from __future__ import annotations

from django import forms

from .models import Suppression


class AddSuppressionForm(forms.Form):
    email = forms.EmailField()
    reason = forms.ChoiceField(choices=Suppression.REASON_CHOICES, initial=Suppression.REASON_MANUAL)
    note = forms.CharField(required=False, max_length=255)


class ImportSuppressionsForm(forms.Form):
    csv_file = forms.FileField(
        help_text="One email per line, or a CSV with an 'email' column.",
    )
    reason = forms.ChoiceField(
        choices=Suppression.REASON_CHOICES, initial=Suppression.REASON_IMPORTED
    )
