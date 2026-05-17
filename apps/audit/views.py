from __future__ import annotations

from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from .models import AuditLog

PAGE_SIZE = 50


def _admin_required(view):
    return login_required(login_url="/login/")(
        user_passes_test(lambda u: u.is_authenticated and u.is_workspace_admin, login_url="/login/")(view)
    )


@_admin_required
def list_view(request: HttpRequest) -> HttpResponse:
    qs = AuditLog.objects.all()
    action = request.GET.get("action", "").strip()
    if action:
        qs = qs.filter(action__icontains=action)
    page = Paginator(qs, PAGE_SIZE).get_page(request.GET.get("page", 1))
    return render(request, "audit/list.html", {"page": page, "action": action})
