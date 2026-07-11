"""Custom admin panel views for browsing Messages.

Read-only: API is the canonical write surface; humans navigate here to debug
sends, inspect headers/HTML, and watch event timelines.
"""
from __future__ import annotations

from datetime import timedelta

from django.contrib import messages as flash
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from apps.messages_api.models import Message

PAGE_SIZE = 25


def _capture_stats(window_hours: int = 24) -> dict:
    """Lightweight volume stats for the Test Inbox header. Captured mail only ever
    has one real status, so volume (not delivery/bounce rates) is the useful signal."""
    since = timezone.now() - timedelta(hours=window_hours)
    base = Message.objects.filter(sandbox=True)
    sparkline = [
        base.filter(
            created_at__gte=since + timedelta(hours=h),
            created_at__lt=since + timedelta(hours=h + 1),
        ).count()
        for h in range(window_hours)
    ]
    return {
        "total": base.count(),
        "last_window": base.filter(created_at__gte=since).count(),
        "window_hours": window_hours,
        "sparkline": sparkline,
        "sparkline_peak": max(sparkline) if sparkline else 0,
    }


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
    if sandbox:
        context["capture_stats"] = _capture_stats()
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


def _spam_context(message: Message) -> dict:
    """Whether an AI provider is configured — the Analyze button needs it."""
    from apps.accounts.models import WorkspaceSettings

    ws = WorkspaceSettings.load()
    return {
        "message": message,
        "ai_configured": bool(ws.ai_provider and ws.ai_api_key_encrypted),
    }


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
            **_spam_context(message),
        },
    )


@login_required(login_url="/login/")
@require_http_methods(["POST"])
def analyze_view(request: HttpRequest, message_id: str) -> HttpResponse:
    """Enqueue spam analysis and return the panel in its 'running' state. The
    panel then polls spam_panel_view until the worker finishes."""
    from django_q.tasks import async_task

    message = get_object_or_404(Message, pk=message_id)
    message.spam_report = {"state": "running"}
    message.save(update_fields=["spam_report"])
    async_task("apps.spam_analysis.tasks.analyze_message", message.id)
    return render(request, "messages/_spam_panel.html", _spam_context(message))


@login_required(login_url="/login/")
def spam_panel_view(request: HttpRequest, message_id: str) -> HttpResponse:
    """Return the current spam panel — polled by HTMX while analysis runs."""
    message = get_object_or_404(Message, pk=message_id)
    return render(request, "messages/_spam_panel.html", _spam_context(message))
