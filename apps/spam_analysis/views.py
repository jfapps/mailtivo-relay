from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from apps.accounts.models import WorkspaceSettings
from apps.audit.models import AuditLog
from apps.core.encryption import decrypt, encrypt

from . import ai_provider, safe_browsing
from .forms import AIProviderForm, SafeBrowsingForm


def _admin_required(view):
    return login_required(login_url="/login/")(
        user_passes_test(lambda u: u.is_authenticated and u.is_workspace_admin, login_url="/login/")(view)
    )


@_admin_required
@require_http_methods(["GET", "POST"])
def integrations_view(request: HttpRequest) -> HttpResponse:
    ws = WorkspaceSettings.load()
    has_ai_key = bool(ws.ai_api_key_encrypted)
    has_sb_key = bool(ws.safe_browsing_api_key_encrypted)

    ai_form = AIProviderForm(
        request.POST or None,
        prefix="ai",
        initial={"provider": ws.ai_provider, "model": ws.ai_model, "has_key": has_ai_key},
    )
    sb_form = SafeBrowsingForm(
        request.POST or None,
        prefix="sb",
        initial={"enabled": ws.safe_browsing_enabled, "has_key": has_sb_key},
    )

    if request.method == "POST":
        section = request.POST.get("section", "")
        if section == "ai" and ai_form.is_valid():
            ws.ai_provider = ai_form.cleaned_data.get("provider", "")
            ws.ai_model = (ai_form.cleaned_data.get("model") or "").strip()
            key_input = ai_form.cleaned_data.get("api_key") or ""
            if key_input:
                ws.ai_api_key_encrypted = encrypt(key_input)
            elif not ws.ai_provider:
                ws.ai_api_key_encrypted = b""
            ws.save()
            AuditLog.record(request.user, action="integrations.ai_saved", detail={"provider": ws.ai_provider})
            messages.success(request, "AI provider settings saved.")
            return redirect(reverse("integrations:index"))
        if section == "safe_browsing" and sb_form.is_valid():
            ws.safe_browsing_enabled = bool(sb_form.cleaned_data["enabled"])
            key_input = sb_form.cleaned_data.get("api_key") or ""
            if key_input:
                ws.safe_browsing_api_key_encrypted = encrypt(key_input)
            elif not ws.safe_browsing_enabled:
                ws.safe_browsing_api_key_encrypted = b""
            ws.save()
            AuditLog.record(
                request.user,
                action="integrations.safe_browsing_saved",
                detail={"enabled": ws.safe_browsing_enabled},
            )
            messages.success(request, "Google Safe Browsing settings saved.")
            return redirect(reverse("integrations:index"))

    return render(
        request,
        "spam_analysis/integrations.html",
        {
            "ai_form": ai_form,
            "sb_form": sb_form,
            "ai_provider": ws.ai_provider,
            "ai_model": ws.ai_model,
            "ai_default_models": ai_provider.DEFAULT_MODELS,
            "ai_provider_choices": WorkspaceSettings.AI_PROVIDER_CHOICES,
            "has_ai_key": has_ai_key,
            "sb_enabled": ws.safe_browsing_enabled,
            "has_sb_key": has_sb_key,
        },
    )


def _result(request: HttpRequest, ok: bool, message: str) -> HttpResponse:
    return render(request, "spam_analysis/_test_result.html", {"ok": ok, "message": message})


@_admin_required
@require_http_methods(["POST"])
def ai_test(request: HttpRequest) -> HttpResponse:
    ws = WorkspaceSettings.load()
    if not ws.ai_provider or not ws.ai_api_key_encrypted:
        return _result(request, False, "Select a provider and save an API key first.")
    try:
        key = decrypt(bytes(ws.ai_api_key_encrypted))
        reply = ai_provider.test_connection(ws.ai_provider, ws.ai_model, key)
    except Exception as exc:
        return _result(request, False, f"Failed: {exc}")
    return _result(request, True, f"Connected — model replied “{reply[:60]}”.")


@_admin_required
@require_http_methods(["POST"])
def safe_browsing_test(request: HttpRequest) -> HttpResponse:
    ws = WorkspaceSettings.load()
    if not ws.safe_browsing_api_key_encrypted:
        return _result(request, False, "Save a Safe Browsing API key first.")
    try:
        key = decrypt(bytes(ws.safe_browsing_api_key_encrypted))
        flagged = safe_browsing.test_connection(key)
    except Exception as exc:
        return _result(request, False, f"Failed: {exc}")
    if flagged:
        return _result(request, True, "Connected — Google's test threat URL was correctly flagged.")
    return _result(request, False, "Key accepted but the test URL was not flagged (unexpected).")
