"""Custom admin panel views for browsing Messages.

Read-only: API is the canonical write surface; humans navigate here to debug
sends, inspect headers/HTML, and watch event timelines.
"""
from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render

from apps.messages_api.models import Message

PAGE_SIZE = 25


@login_required(login_url="/login/")
def list_view(request: HttpRequest) -> HttpResponse:
    qs = Message.objects.select_related("connection", "pool", "api_key").all()

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

    context = {
        "page": page,
        "status_choices": Message.STATUS_CHOICES,
        "active_status": status,
        "q": q,
    }
    if request.headers.get("HX-Request"):
        return render(request, "messages/_list_table.html", context)
    return render(request, "messages/list.html", context)


@login_required(login_url="/login/")
def detail_view(request: HttpRequest, message_id: str) -> HttpResponse:
    message = get_object_or_404(
        Message.objects.select_related("connection", "pool", "api_key").prefetch_related("events", "attachments"),
        pk=message_id,
    )
    body = message.body()
    return render(
        request,
        "messages/detail.html",
        {
            "message": message,
            "body": body,
            "events": message.events.all(),
            "attachments": message.attachments.all(),
        },
    )
