from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from apps.audit.models import AuditLog
from apps.pools.models import Pool

from .forms import IssueKeyForm
from .models import APIKey


def _admin_required(view):
    return login_required(login_url="/login/")(
        user_passes_test(lambda u: u.is_authenticated and u.is_workspace_admin, login_url="/login/")(view)
    )


@_admin_required
@require_http_methods(["GET", "POST"])
def list_view(request: HttpRequest) -> HttpResponse:
    form = IssueKeyForm(request.POST or None)
    issued_secret: str | None = None
    issued_key: APIKey | None = None
    if request.method == "POST" and form.is_valid():
        issued_key, issued_secret = APIKey.issue(
            name=form.cleaned_data["name"],
            scopes=form.cleaned_data["scopes"],
            created_by=request.user,
            default_pool=form.cleaned_data.get("default_pool"),
        )
        AuditLog.record(
            request.user,
            action="apikey.issued",
            target=issued_key.name,
            detail={"scopes": issued_key.scopes, "default_pool": getattr(issued_key.default_pool, "name", None)},
        )
        # Don't redirect — we need to render the page once to surface the secret.
        form = IssueKeyForm()

    keys = APIKey.objects.select_related("default_pool").all()
    return render(
        request,
        "api_keys/list.html",
        {
            "form": form,
            "keys": keys,
            "pools": Pool.objects.all(),
            "issued_secret": issued_secret,
            "issued_key": issued_key,
        },
    )


@_admin_required
@require_http_methods(["POST"])
def set_pool_view(request: HttpRequest, pk: int) -> HttpResponse:
    """Reassign an active key's default pool — e.g. flip it between a live pool
    and a capture (sandbox) pool without revoking and reissuing the secret."""
    key = get_object_or_404(APIKey, pk=pk, revoked_at__isnull=True)
    pool_id = request.POST.get("default_pool", "").strip()
    new_pool = get_object_or_404(Pool, pk=pool_id) if pool_id else None

    old_name = getattr(key.default_pool, "name", None)
    key.default_pool = new_pool
    key.save(update_fields=["default_pool"])
    AuditLog.record(
        request.user,
        action="apikey.pool_changed",
        target=key.name,
        detail={"from": old_name, "to": getattr(new_pool, "name", None)},
    )
    messages.success(request, f"Updated pool for “{key.name}”.")
    return redirect(reverse("api_keys:list"))


@_admin_required
@require_http_methods(["POST"])
def revoke_view(request: HttpRequest, pk: int) -> HttpResponse:
    key = get_object_or_404(APIKey, pk=pk, revoked_at__isnull=True)
    key.revoked_at = timezone.now()
    key.save(update_fields=["revoked_at"])
    AuditLog.record(request.user, action="apikey.revoked", target=key.name)
    messages.success(request, f"Revoked “{key.name}”.")
    return redirect(reverse("api_keys:list"))
