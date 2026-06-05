"""Panel "Tools" area.

Tools are small operator utilities that live outside the normal send pipeline —
things you reach for while debugging or validating a setup. The first one is
Test Send: compose an email in the panel and route it through the real pool /
adapter pipeline synchronously, so you get an immediate pass/fail with the
provider's response instead of watching a queued row flip.
"""
from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from apps.accounts.models import WorkspaceSettings
from apps.audit.models import AuditLog
from apps.messages_api.models import Message
from apps.pools.models import Pool
from apps.sending.tasks import send_message

from .forms import TestSendForm

# Default body for a fresh test-send form — friendly and obviously a test.
_DEFAULT_BODY = (
    "This is a test email sent from Mailtivo-Relay.\n\n"
    "If you're reading this, your provider connection, pool routing, and "
    "sending pipeline are all working.\n"
)


@login_required(login_url="/login/")
def index(request: HttpRequest) -> HttpResponse:
    return render(request, "tools/index.html", {})


@login_required(login_url="/login/")
@require_http_methods(["GET", "POST"])
def test_send(request: HttpRequest) -> HttpResponse:
    workspace = WorkspaceSettings.load()
    has_pool = Pool.objects.exists()

    if request.method == "POST":
        form = TestSendForm(request.POST)
        result = None
        if has_pool and form.is_valid():
            result = _run_test_send(request, form)
        context = {"form": form, "has_pool": has_pool, "result": result}
        if getattr(request, "htmx", False):
            return render(request, "tools/_test_send_form.html", context)
        return render(request, "tools/test_send.html", context)

    initial = {
        "from_address": workspace.from_email_default or "",
        "to_address": request.user.email,
        "subject": "Mailtivo-Relay test email",
        "body": _DEFAULT_BODY,
    }
    first_pool = Pool.objects.first()
    if first_pool is not None:
        initial["pool"] = first_pool.pk
    form = TestSendForm(initial=initial)
    return render(
        request,
        "tools/test_send.html",
        {"form": form, "has_pool": has_pool, "result": None},
    )


def _run_test_send(request: HttpRequest, form: TestSendForm) -> dict:
    """Build a Message from the form, route it synchronously, return a result dict."""
    workspace = WorkspaceSettings.load()
    data = form.cleaned_data
    body = data["body"]

    message = Message(
        pool=data["pool"],
        from_address=data["from_address"],
        to=[data["to_address"]],
        subject=data["subject"],
        tags=[{"name": "source", "value": "panel-test-send"}],
    )
    message.set_body(html=_text_to_html(body), text=body)
    message.set_retention(workspace.retention_days)
    message.save()

    # Route synchronously so the operator sees the outcome on this page. This is
    # the same entrypoint Django-Q2 calls for a real send.
    status = send_message(str(message.id))
    message.refresh_from_db()

    AuditLog.record(
        request.user,
        action="tools.test_send",
        target=data["to_address"],
        detail={"message_id": message.id, "status": status, "pool": data["pool"].name},
    )

    return {
        "message": message,
        "ok": status == Message.STATUS_SENT,
        "status": status,
    }


def _text_to_html(text: str) -> str:
    """Minimal text→HTML so the HTML part isn't empty. Not a full converter."""
    from django.utils.html import escape

    return "<br>".join(escape(line) for line in text.splitlines())
