"""Custom admin panel views for browsing Messages.

Read-only: API is the canonical write surface; humans navigate here to debug
sends, inspect headers/HTML, and watch event timelines.
"""
from __future__ import annotations

from django.contrib import messages as flash
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from apps.messages_api.models import Message

PAGE_SIZE = 25


@login_required(login_url="/login/")
def list_view(request: HttpRequest, sandbox: bool = False) -> HttpResponse:
    # Live and test (captured) mail live on separate pages: the main Messages list
    # shows real sends; the Test Inbox shows mail captured by sandbox pools.
    qs = Message.objects.select_related("connection", "pool", "api_key").filter(sandbox=sandbox)

    status = request.GET.get("status", "").strip()
    q = request.GET.get("q", "").strip()

    if status:
        qs = qs.filter(status=status)
    if q:
        qs = qs.filter(
            Q(subject__icontains=q)
            | Q(from_address__icontains=q)
            | Q(provider_message_id__icontains=q)
            | Q(id__icontains=q)
        )

    paginator = Paginator(qs, PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page", 1))

    list_url = reverse("messages_panel:test_inbox" if sandbox else "messages_panel:list")
    context = {
        "page": page,
        "status_choices": Message.STATUS_CHOICES,
        "active_status": status,
        "q": q,
        "sandbox": sandbox,
        "list_url": list_url,
    }
    if request.headers.get("HX-Request"):
        return render(request, "messages/_list_table.html", context)
    return render(request, "messages/list.html", context)


@login_required(login_url="/login/")
@require_http_methods(["POST"])
def simulate_event_view(request: HttpRequest, message_id: str) -> HttpResponse:
    """Simulate a delivery event for a sandbox (captured) message and fan it out
    to outbound webhooks. Sandbox-only — live messages get real provider events."""
    from apps.events.simulate import SimulateError, simulate_event

    message = get_object_or_404(Message, pk=message_id)
    event_type = request.POST.get("type", "").strip()
    try:
        simulate_event(message, event_type)
    except SimulateError as exc:
        flash.error(request, str(exc))
    else:
        flash.success(request, f"Simulated “{event_type}” event and dispatched it to webhooks.")
    return redirect(reverse("messages_panel:detail", args=[message.id]))


@login_required(login_url="/login/")
def detail_view(request: HttpRequest, message_id: str) -> HttpResponse:
    message = get_object_or_404(
        Message.objects.select_related("connection", "pool", "api_key").prefetch_related("events", "attachments"),
        pk=message_id,
    )
    body = message.body()
    from apps.events.simulate import SIMULATABLE_TYPES

    return render(
        request,
        "messages/detail.html",
        {
            "message": message,
            "body": body,
            "events": message.events.all(),
            "attachments": message.attachments.all(),
            "simulatable_types": SIMULATABLE_TYPES if message.sandbox else (),
        },
    )
