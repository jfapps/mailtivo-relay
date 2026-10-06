from __future__ import annotations

from django import forms

from .models import WebhookEndpoint

EVENT_TYPE_CHOICES = [
    ("sent", "Sent"),
    ("delivered", "Delivered"),
    ("deferred", "Deferred"),
    ("bounced", "Bounced"),
    ("complained", "Complained"),
    ("opened", "Opened"),
    ("clicked", "Clicked"),
    ("failed", "Failed"),
]


class WebhookEndpointForm(forms.ModelForm):
    event_types = forms.MultipleChoiceField(
        choices=EVENT_TYPE_CHOICES,
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="Leave empty to receive all event types.",
    )

    class Meta:
        model = WebhookEndpoint
        fields = ["name", "url", "enabled", "event_types"]
        widgets = {
            "url": forms.URLInput(attrs={"class": "input", "placeholder": "https://your-app.example.com/webhooks/mailtivo"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk and self.instance.event_types:
            self.fields["event_types"].initial = self.instance.event_types
