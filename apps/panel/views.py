from __future__ import annotations

from datetime import timedelta

from allauth.socialaccount.models import SocialApp
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.sites.models import Site
from django.db.models import Count
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from apps.accounts.models import Invitation, User, WorkspaceSettings
from apps.audit.models import AuditLog
from apps.core.encryption import decrypt, encrypt
from apps.messages_api.models import Attachment, IdempotencyRecord, Message, PurgeRun

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
    secret = ""  # nosec B105 - optional decrypted setting starts empty
    if ws.google_oauth_client_secret_encrypted:
        try:
            secret = decrypt(bytes(ws.google_oauth_client_secret_encrypted))
        except Exception:
            secret = ""  # nosec B105 - clear optional value after failed decrypt
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
    # Production deliverability only — sandbox (captured) mail has no real delivery
    # lifecycle, so including it would distort delivery/bounce rates and volume.
    # Capture-pool stats live on the Test Inbox instead (see messages_api.panel_views).
    since = timezone.now() - timedelta(hours=window_hours)
    qs = Message.objects.filter(created_at__gte=since, sandbox=False)
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
            sandbox=False,
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
    from apps.api_keys.models import APIKey
    from apps.connections.models import Connection
    from apps.pools.models import PoolMember

    has_connection = Connection.objects.exists()
    has_populated_pool = PoolMember.objects.exists()
    has_api_key = APIKey.objects.filter(revoked_at__isnull=True).exists()
    setup_complete = has_connection and has_populated_pool and has_api_key

    return render(
        request,
        "panel/dashboard.html",
        {
            "kpis": _kpi_data(),
            "setup": {
                "connection": has_connection,
                "pool": has_populated_pool,
                "api_key": has_api_key,
                "complete": setup_complete,
            },
        },
    )


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
        if section == "updates":
            from django_q.tasks import async_task

            ws.update_check_enabled = bool(request.POST.get("update_check_enabled"))
            ws.save(update_fields=["update_check_enabled", "updated_at"])
            AuditLog.record(
                request.user,
                action="settings.update_check_toggled",
                detail={"enabled": ws.update_check_enabled},
            )
            if "check_now" in request.POST and ws.update_check_enabled:
                async_task("apps.core.updates.check_for_update")
                messages.success(request, "Checking for updates — refresh in a moment.")
            else:
                messages.success(request, "Update settings saved.")
            return redirect(f"{reverse('panel:settings')}#updates")

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
@require_http_methods(["GET"])
def data_view(request: HttpRequest) -> HttpResponse:
    """Data & storage: what the relay is holding, retention state, purge history."""
    from django.db.models import Sum
    from django.db.models.functions import Length

    from apps.events.models import Event
    from apps.messages_api.tasks import _IN_FLIGHT_STATUSES

    now = timezone.now()
    ws = WorkspaceSettings.load()

    att = Attachment.objects.aggregate(n=Count("id"), b64_chars=Sum(Length("content_b64")))
    # content_b64 is base64 text; decoded payload is ~3/4 of its length.
    attachment_bytes = int((att["b64_chars"] or 0) * 3 / 4)

    stats = {
        "total_messages": Message.objects.count(),
        "bodies_stored": Message.objects.filter(body_purged_at__isnull=True).count(),
        "bodies_purged": Message.objects.filter(body_purged_at__isnull=False).count(),
        "pending_purge": (
            Message.objects.filter(retention_expires_at__lt=now, body_purged_at__isnull=True)
            .exclude(status__in=_IN_FLIGHT_STATUSES)
            .count()
        ),
        "captured": Message.objects.filter(sandbox=True).count(),
        "attachments": att["n"] or 0,
        "attachment_bytes": attachment_bytes,
        "events": Event.objects.count(),
        "idempotency_records": IdempotencyRecord.objects.count(),
        "oldest_message_at": (
            Message.objects.order_by("created_at").values_list("created_at", flat=True).first()
        ),
    }
    return render(
        request,
        "panel/data.html",
        {
            "stats": stats,
            "runs": PurgeRun.objects.select_related("created_by")[:10],
            "retention_enabled": ws.retention_enabled,
            "retention_days": ws.retention_days,
        },
    )


@_admin_required
@require_http_methods(["POST"])
def data_purge_now(request: HttpRequest) -> HttpResponse:
    from apps.messages_api.tasks import purge_expired

    result = purge_expired(trigger=PurgeRun.TRIGGER_MANUAL, user_id=request.user.pk)
    AuditLog.record(request.user, action="data.purge_run", detail=result)
    messages.success(
        request,
        f"Purged {result['messages_purged']} message bodies and "
        f"{result['attachments_purged']} attachments.",
    )
    return redirect(reverse("panel:data"))


@_admin_required
@require_http_methods(["POST"])
def data_purge_older(request: HttpRequest) -> HttpResponse:
    from apps.messages_api.tasks import purge_expired

    try:
        days = int(request.POST.get("days", ""))
    except ValueError:
        days = -1
    if days < 1 or days > 3650:
        messages.error(request, "Enter a number of days between 1 and 3650.")
        return redirect(reverse("panel:data"))

    result = purge_expired(
        trigger=PurgeRun.TRIGGER_MANUAL, older_than_days=days, user_id=request.user.pk,
    )
    AuditLog.record(request.user, action="data.purge_older_than", detail={"days": days, **result})
    messages.success(
        request,
        f"Purged {result['messages_purged']} message bodies older than {days} days.",
    )
    return redirect(reverse("panel:data"))


@_admin_required
@require_http_methods(["POST"])
def data_delete_captured(request: HttpRequest) -> HttpResponse:
    from apps.messages_api.tasks import delete_captured_messages

    n = delete_captured_messages()
    AuditLog.record(request.user, action="data.captured_deleted", detail={"count": n})
    messages.success(request, f"Deleted {n} captured test message{'s' if n != 1 else ''}.")
    return redirect(reverse("panel:data"))


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
