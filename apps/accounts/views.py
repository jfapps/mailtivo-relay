from __future__ import annotations

from django.contrib import messages
from django.contrib.auth import login, logout
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from apps.core.middleware import register_failure, reset_lockout

from .forms import LoginForm, MagicLinkRequestForm, OnboardingForm
from .models import Invitation, MagicLinkToken, User, WorkspaceSettings


def _client_ip(request: HttpRequest) -> str | None:
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def _owners_exist() -> bool:
    return User.objects.filter(is_workspace_admin=True).exists()


@require_http_methods(["GET", "POST"])
def onboarding(request: HttpRequest) -> HttpResponse:
    if _owners_exist():
        return redirect(reverse("accounts:login"))

    if request.method == "POST":
        form = OnboardingForm(request.POST)
        if form.is_valid():
            ws = WorkspaceSettings.load()
            ws.name = form.cleaned_data["workspace_name"]
            ws.save(update_fields=["name", "updated_at"])

            owner = User.objects.create_user(
                email=form.cleaned_data["email"],
                password=form.cleaned_data["password"],
                display_name=form.cleaned_data.get("display_name") or "",
                is_workspace_admin=True,
                is_staff=True,
            )
            login(request, owner)
            owner.last_login_ip = _client_ip(request)
            owner.save(update_fields=["last_login_ip"])
            return redirect("/app/")
    else:
        form = OnboardingForm()
    return render(request, "accounts/onboarding.html", {"form": form})


@require_http_methods(["GET", "POST"])
def login_view(request: HttpRequest) -> HttpResponse:
    if not _owners_exist():
        return redirect(reverse("accounts:onboarding"))

    ws = WorkspaceSettings.load()
    ip = _client_ip(request) or "0.0.0.0"
    if request.method == "POST":
        form = LoginForm(request.POST, request=request)
        if form.is_valid() and form.user is not None:
            reset_lockout(ip)
            login(request, form.user)
            form.user.last_login_ip = ip
            form.user.save(update_fields=["last_login_ip"])
            return redirect("/app/")
        register_failure(ip)
    else:
        form = LoginForm()
    return render(request, "accounts/login.html", {"form": form, "workspace": ws})


@require_http_methods(["POST"])
def logout_view(request: HttpRequest) -> HttpResponse:
    logout(request)
    return redirect(reverse("accounts:login"))


@require_http_methods(["GET", "POST"])
def magic_link_request(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        form = MagicLinkRequestForm(request.POST)
        if form.is_valid():
            email = form.cleaned_data["email"]
            try:
                user = User.objects.get(email__iexact=email, is_active=True)
            except User.DoesNotExist:
                user = None
            if user is not None:
                token, raw = MagicLinkToken.issue(user, ip=_client_ip(request))
                magic_url = request.build_absolute_uri(
                    reverse("accounts:magic_link_consume", args=[raw])
                )
                # Day 1: print to the console email backend. Real outbound email comes later.
                from django.core.mail import send_mail

                send_mail(
                    subject="Sign in to Mailtivo-Relay",
                    message=f"Sign in here (valid 15 minutes): {magic_url}",
                    from_email=None,
                    recipient_list=[user.email],
                )
            messages.success(
                request,
                "If that email matches an account, we just sent a sign-in link. Check your inbox.",
            )
            return redirect(reverse("accounts:magic_link_request"))
    else:
        form = MagicLinkRequestForm()
    return render(request, "accounts/magic_link_request.html", {"form": form})


@require_http_methods(["GET"])
def magic_link_consume(request: HttpRequest, token: str) -> HttpResponse:
    user = MagicLinkToken.consume(token)
    if user is None:
        messages.error(request, "That sign-in link is invalid or expired.")
        return redirect(reverse("accounts:login"))
    login(request, user)
    user.last_login_ip = _client_ip(request)
    user.save(update_fields=["last_login_ip"])
    return redirect("/app/")


@require_http_methods(["GET", "POST"])
def invitation_accept(request: HttpRequest, token: str) -> HttpResponse:
    invitation = Invitation.lookup(token)
    if invitation is None:
        messages.error(request, "That invitation is invalid or expired.")
        return redirect(reverse("accounts:login"))

    if request.method == "POST":
        password = request.POST.get("password") or ""
        confirm = request.POST.get("password_confirm") or ""
        display_name = request.POST.get("display_name") or ""
        if not password or password != confirm:
            messages.error(request, "Passwords don't match.")
        elif len(password) < 12:
            messages.error(request, "Password must be at least 12 characters.")
        else:
            user, _ = User.objects.get_or_create(
                email=invitation.email,
                defaults={"is_workspace_admin": invitation.is_admin, "display_name": display_name},
            )
            user.set_password(password)
            user.display_name = display_name or user.display_name
            user.is_workspace_admin = user.is_workspace_admin or invitation.is_admin
            user.is_active = True
            user.save()
            invitation.accepted_by = user
            invitation.accepted_at = timezone.now()
            invitation.save(update_fields=["accepted_by", "accepted_at"])
            login(request, user)
            return redirect("/app/")

    return render(request, "accounts/invitation_accept.html", {"invitation": invitation})
