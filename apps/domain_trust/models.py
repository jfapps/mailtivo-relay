from django.db import models


class DomainIdentity(models.Model):
    CERT_NONE = "none"
    CERT_CMC = "cmc"
    CERT_VMC = "vmc"
    CERT_CHOICES = [
        (CERT_NONE, "No certificate"),
        (CERT_CMC, "CMC"),
        (CERT_VMC, "VMC"),
    ]

    domain = models.CharField(max_length=253, unique=True)
    label = models.CharField(max_length=120, blank=True)
    dkim_selector = models.CharField(max_length=63, blank=True)
    certificate_type = models.CharField(max_length=8, choices=CERT_CHOICES, default=CERT_NONE)
    latest_state = models.JSONField(default=dict, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["domain"]

    def __str__(self) -> str:
        return self.domain
