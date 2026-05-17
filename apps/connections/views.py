from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from apps.connections.adapters import PROVIDER_LABELS, AdapterError
from apps.core.encryption import encrypt

from .forms import ConnectionForm
from .models import Connection


def _admin_required(view):
    return login_required(login_url="/login/")(
        user_passes_test(lambda u: u.is_authenticated and u.is_workspace_admin, login_url="/login/")(view)
    )


@_admin_required
def list_view(request: HttpRequest) -> HttpResponse:
    connections = Connection.objects.all()
    return render(
        request,
        "connections/list.html",
        {"connections": connections, "provider_labels": PROVIDER_LABELS},
    )


@_admin_required
@require_http_methods(["GET", "POST"])
def create_view(request: HttpRequest) -> HttpResponse:
    form = ConnectionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        conn: Connection = form.save(commit=False)
        if api_key := form.cleaned_data.get("api_key"):
            conn.credentials_encrypted = encrypt(api_key)
        if secret := form.cleaned_data.get("webhook_secret"):
            conn.webhook_secret_encrypted = encrypt(secret)
        if pem := form.cleaned_data.get("webhook_public_key_pem"):
            conn.webhook_public_key_pem = pem
        conn.created_by = request.user
        conn.save()
        messages.success(request, f"Created connection “{conn.name}”.")
        return redirect(reverse("connections:edit", args=[conn.pk]))
    return render(request, "connections/edit.html", {"form": form, "creating": True})


@_admin_required
@require_http_methods(["GET", "POST"])
def edit_view(request: HttpRequest, pk: int) -> HttpResponse:
    conn = get_object_or_404(Connection, pk=pk)
    form = ConnectionForm(request.POST or None, instance=conn)
    if request.method == "POST" and form.is_valid():
        conn = form.save(commit=False)
        if api_key := form.cleaned_data.get("api_key"):
            conn.credentials_encrypted = encrypt(api_key)
        if secret := form.cleaned_data.get("webhook_secret"):
            conn.webhook_secret_encrypted = encrypt(secret)
        if pem := form.cleaned_data.get("webhook_public_key_pem"):
            conn.webhook_public_key_pem = pem
        conn.save()
        messages.success(request, "Connection saved.")
        return redirect(reverse("connections:edit", args=[conn.pk]))
    return render(
        request,
        "connections/edit.html",
        {"form": form, "connection": conn, "creating": False},
    )


@_admin_required
@require_http_methods(["POST"])
def test_view(request: HttpRequest, pk: int) -> HttpResponse:
    conn = get_object_or_404(Connection, pk=pk)
    adapter = conn.adapter()
    try:
        ok = adapter.healthcheck()
        conn.status = Connection.STATUS_HEALTHY if ok else Connection.STATUS_DOWN
        conn.last_health_message = "OK" if ok else "Healthcheck returned False"
    except AdapterError as exc:
        conn.status = Connection.STATUS_DOWN
        conn.last_health_message = str(exc)[:255]
    except Exception as exc:  # noqa: BLE001
        conn.status = Connection.STATUS_DOWN
        conn.last_health_message = f"Unexpected error: {exc}"[:255]
    conn.last_health_check_at = timezone.now()
    conn.save(update_fields=["status", "last_health_check_at", "last_health_message"])

    if conn.status == Connection.STATUS_HEALTHY:
        messages.success(request, f"“{conn.name}” is healthy.")
    else:
        messages.error(request, f"“{conn.name}” failed: {conn.last_health_message}")
    return redirect(reverse("connections:edit", args=[conn.pk]))


@_admin_required
@require_http_methods(["POST"])
def delete_view(request: HttpRequest, pk: int) -> HttpResponse:
    conn = get_object_or_404(Connection, pk=pk)
    name = conn.name
    conn.delete()
    messages.success(request, f"Deleted connection “{name}”.")
    return redirect(reverse("connections:list"))
