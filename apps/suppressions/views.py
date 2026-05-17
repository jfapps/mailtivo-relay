from __future__ import annotations

import csv
import io

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpRequest, HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from .forms import AddSuppressionForm, ImportSuppressionsForm
from .models import Suppression

PAGE_SIZE = 50


def _admin_required(view):
    return login_required(login_url="/login/")(
        user_passes_test(lambda u: u.is_authenticated and u.is_workspace_admin, login_url="/login/")(view)
    )


@_admin_required
@require_http_methods(["GET", "POST"])
def list_view(request: HttpRequest) -> HttpResponse:
    add_form = AddSuppressionForm(request.POST or None if request.POST.get("section") == "add" else None)
    import_form = ImportSuppressionsForm(
        request.POST or None if request.POST.get("section") == "import" else None,
        request.FILES or None if request.POST.get("section") == "import" else None,
    )

    if request.method == "POST":
        section = request.POST.get("section", "")
        if section == "add" and add_form.is_valid():
            row = Suppression.add(
                add_form.cleaned_data["email"],
                reason=add_form.cleaned_data["reason"],
                note=add_form.cleaned_data.get("note", ""),
                created_by=request.user,
            )
            from apps.audit.models import AuditLog

            AuditLog.record(
                request.user,
                action="suppression.added",
                target=row.email if row else add_form.cleaned_data["email"],
                detail={"reason": add_form.cleaned_data["reason"]},
            )
            messages.success(request, f"Added {add_form.cleaned_data['email']} to suppression list.")
            return redirect(reverse("suppressions:list"))

        if section == "import" and import_form.is_valid():
            f = import_form.cleaned_data["csv_file"]
            reason = import_form.cleaned_data["reason"]
            added = _import_csv(f, reason=reason, created_by=request.user)
            messages.success(request, f"Imported {added} new suppressions.")
            return redirect(reverse("suppressions:list"))

    qs = Suppression.objects.all()
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(email__icontains=q) | Q(note__icontains=q))
    page = Paginator(qs, PAGE_SIZE).get_page(request.GET.get("page", 1))

    return render(
        request,
        "suppressions/list.html",
        {
            "page": page,
            "add_form": add_form,
            "import_form": import_form,
            "q": q,
        },
    )


@_admin_required
@require_http_methods(["POST"])
def remove_view(request: HttpRequest, pk: int) -> HttpResponse:
    row = get_object_or_404(Suppression, pk=pk)
    email = row.email
    row.delete()
    from apps.audit.models import AuditLog

    AuditLog.record(request.user, action="suppression.removed", target=email)
    messages.success(request, f"Removed {email}.")
    return redirect(reverse("suppressions:list"))


@_admin_required
def export_view(request: HttpRequest) -> StreamingHttpResponse:
    """Stream all suppressions as CSV (email, reason, note, created_at)."""

    def rows():
        yield "email,reason,note,created_at\n"
        for s in Suppression.objects.all().iterator(chunk_size=500):
            note = (s.note or "").replace('"', '""')
            yield f'{s.email},{s.reason},"{note}",{s.created_at.isoformat()}\n'

    response = StreamingHttpResponse(rows(), content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="suppressions.csv"'
    return response


def _import_csv(upload, *, reason: str, created_by) -> int:
    """Best-effort import. Accepts either a one-email-per-line file or a CSV
    with an `email` column. Returns the count of newly-added rows."""
    raw = upload.read()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="ignore")
    text = raw.lstrip("﻿")

    if "," in text.splitlines()[0] if text.splitlines() else False:
        reader = csv.DictReader(io.StringIO(text))
        emails = [(row.get("email") or "").strip() for row in reader if row.get("email")]
    else:
        emails = [line.strip() for line in text.splitlines() if line.strip()]

    added = 0
    for email in emails:
        if not email or "@" not in email:
            continue
        # Skip rows already present.
        if Suppression.objects.filter(email=email.lower()).exists():
            continue
        if Suppression.add(email, reason=reason, created_by=created_by):
            added += 1
    return added
