"""Security + login-lockout middleware.

Kept in apps.core because both apply across every request, panel + API.

The CSP is deliberately permissive of inline scripts because Alpine.js relies
on them (x-show, x-cloak, etc.). HTMX runs the same way. We still cut off
remote-iframe / external-asset surface area.
"""
from __future__ import annotations

from typing import Callable

from django.core.cache import cache
from django.http import HttpRequest, HttpResponse, HttpResponseForbidden


_DEFAULT_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://unpkg.com; "
    "style-src 'self' 'unsafe-inline' https://rsms.me; "
    "font-src 'self' https://rsms.me; "
    "img-src 'self' data: https:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "form-action 'self'; "
    "base-uri 'self'"
)

_DEFAULT_PERMISSIONS_POLICY = (
    "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
    "magnetometer=(), microphone=(), payment=(), usb=()"
)


class SecurityHeadersMiddleware:
    """Add CSP, Referrer, Permissions-Policy, X-Content-Type-Options.

    HSTS, X-Frame-Options, secure cookies, and SSL redirect are handled by
    Django's built-in SecurityMiddleware + settings — we don't duplicate.
    Skips /api/v1/* since those are JSON for machines, not browser-rendered.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        if request.path.startswith("/api/v1/"):
            return response
        response.setdefault("Content-Security-Policy", _DEFAULT_CSP)
        response.setdefault("Permissions-Policy", _DEFAULT_PERMISSIONS_POLICY)
        response.setdefault("Referrer-Policy", "same-origin")
        response.setdefault("X-Content-Type-Options", "nosniff")
        return response


# ---- login lockout --------------------------------------------------------

LOCKOUT_FAILURES = 10
LOCKOUT_WINDOW_SECONDS = 15 * 60
LOCKOUT_PATHS = ("/login/", "/login/magic/")


def _client_ip(request: HttpRequest) -> str:
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "0.0.0.0")


def _bucket_key(ip: str) -> str:
    return f"mr:loginlock:{ip}"


def login_lockout_count(ip: str) -> int:
    return int(cache.get(_bucket_key(ip)) or 0)


def login_lockout_reset(ip: str) -> None:
    cache.delete(_bucket_key(ip))


def login_lockout_register_failure(ip: str) -> int:
    key = _bucket_key(ip)
    cache.add(key, 0, timeout=LOCKOUT_WINDOW_SECONDS)
    try:
        return int(cache.incr(key))
    except ValueError:
        cache.set(key, 1, timeout=LOCKOUT_WINDOW_SECONDS)
        return 1


class LoginLockoutMiddleware:
    """Refuse POSTs to login endpoints once an IP has failed >= LOCKOUT_FAILURES
    times in LOCKOUT_WINDOW_SECONDS. The view is responsible for *recording*
    failures via login_lockout_register_failure() and *clearing* the counter on
    success via login_lockout_reset().
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.method == "POST" and request.path in LOCKOUT_PATHS:
            ip = _client_ip(request)
            if login_lockout_count(ip) >= LOCKOUT_FAILURES:
                retry_after = LOCKOUT_WINDOW_SECONDS
                resp = HttpResponseForbidden(
                    "Too many failed login attempts. Try again later."
                )
                resp["Retry-After"] = str(retry_after)
                return resp
            # Attach the IP so the view can record failure / clear on success.
            request.login_ip = ip  # type: ignore[attr-defined]
        return self.get_response(request)


# Re-export so callers can `from apps.core.middleware import register_failure, reset_lockout`.
register_failure = login_lockout_register_failure
reset_lockout = login_lockout_reset
