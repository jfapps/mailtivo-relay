from __future__ import annotations

from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db.models import Count
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from allauth.socialaccount.models import SocialApp
from django.contrib.sites.models import Site

from apps.accounts.models import Invitation, User, WorkspaceSettings
from apps.audit.models import AuditLog
from apps.core.encryption import decrypt, encrypt
from apps.messages_api.models import Message

from .forms import GoogleOAuthForm, InvitationForm, WorkspaceSettingsForm


def _admin_required(view):
    return login_required(login_url="/login/")(
        user_passes_test(lambda u: u.is_authenticated and u.is_workspace_admin, login_url="/login/")(view)
    )


def _sync_google_social_app(ws: WorkspaceSettings) -> None:
    """Reflect WorkspaceSettings.google_oauth_* into an allauth SocialApp row."""
    if not ws.google_oauth_enabled:
        SocialApp.objects.filter(provider="google").delete()
        return
    site = Site.objects.get(pk=1)
    secret = ""
    if ws.google_oauth_client_secret_encrypted:
        try:
            secret = decrypt(bytes(ws.google_oauth_client_secret_encrypted))
        except Exception:
            secret = ""
    app, _ = SocialApp.objects.update_or_create(
        provider="google",
        defaults={
            "name": "Google",
            "client_id": ws.google_oauth_client_id,
            "secret": secret,
            "key": "",
        },
    )
    app.sites.add(site)


def _kpi_data(window_hours: int = 24) -> dict:
    since = timezone.now() - timedelta(hours=window_hours)
    qs = Message.objects.filter(created_at__gte=since)
    counts: dict[str, int] = {
        row["status"]: row["n"] for row in qs.values("status").annotate(n=Count("id"))
    }
    sent = (
        counts.get(Message.STATUS_SENT, 0)
        + counts.get(Message.STATUS_DELIVERED, 0)
        + counts.get(Message.STATUS_BOUNCED, 0)
        + counts.get(Message.STATUS_COMPLAINED, 0)
    )
    delivered = counts.get(Message.STATUS_DELIVERED, 0)
    bounced = counts.get(Message.STATUS_BOUNCED, 0)
    complained = counts.get(Message.STATUS_COMPLAINED, 0)
    failed = counts.get(Message.STATUS_FAILED, 0)
    queued = counts.get(Message.STATUS_QUEUED, 0) + counts.get(Message.STATUS_SENDING, 0)
    total = sum(counts.values())

    # Hourly sparkline of accepted-to-upstream messages (sent + downstream states).
    sparkline: list[int] = []
    for hour in range(window_hours):
        hour_start = since + timedelta(hours=hour)
        hour_end = hour_start + timedelta(hours=1)
        n = Message.objects.filter(
            created_at__gte=hour_start,
            created_at__lt=hour_end,
            status__in=[
                Message.STATUS_SENT, Message.STATUS_DELIVERED,
                Message.STATUS_BOUNCED, Message.STATUS_COMPLAINED,
            ],
        ).count()
        sparkline.append(n)
    peak = max(sparkline) if sparkline else 0

    return {
        "window_hours": window_hours,
        "total": total,
        "sent": sent,
        "delivered": delivered,
        "bounced": bounced,
        "complained": complained,
        "failed": failed,
        "queued": queued,
        "delivery_rate": (delivered / sent * 100) if sent else 0.0,
        "bounce_rate": (bounced / sent * 100) if sent else 0.0,
        "sparkline": sparkline,
        "sparkline_peak": peak,
    }


@login_required(login_url="/login/")
def dashboard(request: HttpRequest) -> HttpResponse:
    return render(request, "panel/dashboard.html", {"kpis": _kpi_data()})


@login_required(login_url="/login/")
def kpis_partial(request: HttpRequest) -> HttpResponse:
    return render(request, "panel/_kpis.html", {"kpis": _kpi_data()})


@_admin_required
@require_http_methods(["GET", "POST"])
def settings_view(request: HttpRequest) -> HttpResponse:
    ws = WorkspaceSettings.load()

    settings_form = WorkspaceSettingsForm(request.POST or None, instance=ws, prefix="ws")

    initial_google = {
        "enabled": ws.google_oauth_enabled,
        "client_id": ws.google_oauth_client_id,
    }
    google_form = GoogleOAuthForm(request.POST or None, initial=initial_google, prefix="google")

    if request.method == "POST":
        section = request.POST.get("section", "")
        if section == "workspace" and settings_form.is_valid():
            settings_form.save()
            AuditLog.record(request.user, action="settings.workspace_saved")
            messages.success(request, "Workspace settings saved.")
            return redirect(reverse("panel:settings"))
        if section == "google" and google_form.is_valid():
            ws.google_oauth_enabled = bool(google_form.cleaned_data["enabled"])
            ws.google_oauth_client_id = google_form.cleaned_data.get("client_id", "")
            secret_input = google_form.cleaned_data.get("client_secret") or ""
            if secret_input:
                ws.google_oauth_client_secret_encrypted = encrypt(secret_input)
            elif not ws.google_oauth_enabled:
                ws.google_oauth_client_secret_encrypted = b""
            ws.save()
            _sync_google_social_app(ws)
            AuditLog.record(
                request.user,
                action="settings.google_oauth_toggled",
                detail={"enabled": ws.google_oauth_enabled},
            )
            messages.success(
                request,
                "Google sign-in enabled." if ws.google_oauth_enabled else "Google sign-in disabled.",
            )
            return redirect(reverse("panel:settings"))

    has_google_secret = bool(ws.google_oauth_client_secret_encrypted)
    return render(
        request,
        "panel/settings.html",
        {
            "settings_form": settings_form,
            "google_form": google_form,
            "has_google_secret": has_google_secret,
        },
    )


@_admin_required
@require_http_methods(["GET", "POST"])
def team_view(request: HttpRequest) -> HttpResponse:
    form = InvitationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        invitation, raw_token = Invitation.issue(
            email=form.cleaned_data["email"],
            invited_by=request.user,
            is_admin=bool(form.cleaned_data.get("is_admin")),
        )
        invite_url = request.build_absolute_uri(
            reverse("accounts:invitation_accept", args=[raw_token])
        )
        # Day 1 used console email; this lands in the console / outbox.
        from django.core.mail import send_mail

        send_mail(
            subject=f"You're invited to {WorkspaceSettings.load().name} on Mailtivo-Relay",
            message=f"Accept your invitation here (valid 7 days): {invite_url}",
            from_email=None,
            recipient_list=[invitation.email],
        )
        AuditLog.record(
            request.user,
            action="team.invited",
            target=invitation.email,
            detail={"is_admin": invitation.is_admin},
        )
        messages.success(request, f"Invitation sent to {invitation.email}.")
        return redirect(reverse("panel:team"))

    members = User.objects.filter(is_active=True).order_by("email")
    pending = (
        Invitation.objects.filter(accepted_at__isnull=True, revoked_at__isnull=True, expires_at__gt=timezone.now())
        .order_by("-created_at")
    )
    return render(
        request,
        "panel/team.html",
        {"form": form, "members": members, "pending": pending},
    )


@_admin_required
@require_http_methods(["POST"])
def invitation_revoke(request: HttpRequest, invitation_id: int) -> HttpResponse:
    try:
        invitation = Invitation.objects.get(pk=invitation_id, accepted_at__isnull=True, revoked_at__isnull=True)
    except Invitation.DoesNotExist:
        messages.error(request, "Invitation not found or already used.")
        return redirect(reverse("panel:team"))
    invitation.revoked_at = timezone.now()
    invitation.save(update_fields=["revoked_at"])
    AuditLog.record(request.user, action="team.invite_revoked", target=invitation.email)
    messages.success(request, f"Revoked invitation for {invitation.email}.")
    return redirect(reverse("panel:team"))


@_admin_required
@require_http_methods(["POST"])
def member_remove(request: HttpRequest, user_id: int) -> HttpResponse:
    if user_id == request.user.pk:
        messages.error(request, "You can't remove yourself.")
        return redirect(reverse("panel:team"))
    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        messages.error(request, "Member not found.")
        return redirect(reverse("panel:team"))
    user.is_active = False
    user.save(update_fields=["is_active"])
    AuditLog.record(request.user, action="team.member_removed", target=user.email)
    messages.success(request, f"Removed {user.email}.")
    return redirect(reverse("panel:team"))
