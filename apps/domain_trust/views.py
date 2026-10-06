from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from apps.audit.models import AuditLog

from .forms import DeliveryHeadersForm, DomainIdentityForm
from .delivery import inspect_delivery_headers
from .models import DomainIdentity
from .services import inspect_domain


def _admin_required(view):
    return login_required(login_url="/login/")(
        user_passes_test(lambda u: u.is_authenticated and u.is_workspace_admin, login_url="/login/")(view)
    )


@_admin_required
@require_http_methods(["GET", "POST"])
def index(request: HttpRequest) -> HttpResponse:
    form = DomainIdentityForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        identity = form.save()
        AuditLog.record(
            request.user,
            action="domain_trust.added",
            target=identity.domain,
            detail={"dkim_selector": identity.dkim_selector, "certificate_type": identity.certificate_type},
        )
        messages.success(request, f"{identity.domain} added. Run a check to inspect its public DNS.")
        return redirect(reverse("domain_trust:index"))
    return render(
        request,
        "domain_trust/index.html",
        {"form": form, "delivery_form": DeliveryHeadersForm(), "identities": DomainIdentity.objects.all()},
    )


@_admin_required
@require_http_methods(["POST"])
def refresh(request: HttpRequest, pk: int) -> HttpResponse:
    identity = get_object_or_404(DomainIdentity, pk=pk)
    try:
        identity.latest_state = inspect_domain(identity.domain, identity.dkim_selector)
        identity.last_checked_at = timezone.now()
        identity.save(update_fields=["latest_state", "last_checked_at", "updated_at"])
    except Exception as exc:
        messages.error(request, f"DNS check failed for {identity.domain}: {exc}")
        return redirect(reverse("domain_trust:index"))
    AuditLog.record(
        request.user,
        action="domain_trust.checked",
        target=identity.domain,
        detail={
            "bimi_infrastructure_ready": identity.latest_state.get("bimi_infrastructure_ready", False),
            "gmail_bimi_candidate": identity.latest_state.get("gmail_bimi_candidate", False),
        },
    )
    messages.success(request, f"DNS status refreshed for {identity.domain}.")
    return redirect(reverse("domain_trust:index"))


@_admin_required
@require_http_methods(["POST"])
def verify_delivery(request: HttpRequest, pk: int) -> HttpResponse:
    identity = get_object_or_404(DomainIdentity, pk=pk)
    form = DeliveryHeadersForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Paste valid message headers before running the delivery check.")
        return redirect(reverse("domain_trust:index"))
    try:
        state = inspect_delivery_headers(identity.domain, form.cleaned_data["headers"])
    except ValueError as exc:
        messages.error(request, f"Delivery check could not be completed: {exc}.")
        return redirect(reverse("domain_trust:index"))

    identity.latest_delivery_state = state
    identity.last_delivery_checked_at = timezone.now()
    identity.save(update_fields=["latest_delivery_state", "last_delivery_checked_at", "updated_at"])
    AuditLog.record(
        request.user,
        action="domain_trust.delivery_checked",
        target=identity.domain,
        detail={
            "status": state.get("status"),
            "spf": state.get("spf"),
            "dkim": state.get("dkim"),
            "dmarc": state.get("dmarc"),
            "dkim_selector": state.get("dkim_selector"),
            "delivery_auth_ready": state.get("delivery_auth_ready", False),
        },
    )
    if state.get("delivery_auth_ready"):
        messages.success(request, f"Real delivery authentication verified for {identity.domain}.")
    else:
        messages.warning(request, f"Delivery authentication is not ready for {identity.domain}: {state.get('detail', '')}")
    return redirect(reverse("domain_trust:index"))


@_admin_required
@require_http_methods(["POST"])
def delete(request: HttpRequest, pk: int) -> HttpResponse:
    identity = get_object_or_404(DomainIdentity, pk=pk)
    domain = identity.domain
    identity.delete()
    AuditLog.record(request.user, action="domain_trust.deleted", target=domain)
    messages.success(request, f"{domain} removed from Domain Trust.")
    return redirect(reverse("domain_trust:index"))
