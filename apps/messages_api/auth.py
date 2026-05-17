"""Bearer-token authentication for /api/v1/* endpoints.

We don't use DRF's authentication classes — the auth here is intentionally
minimal so the API behaves exactly like Resend's (Bearer mr_live_..., JSON
errors in the Resend error shape).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from django.http import HttpRequest, JsonResponse

from apps.api_keys.models import APIKey


@dataclass(slots=True)
class AuthContext:
    api_key: APIKey


def _unauthorized(message: str) -> JsonResponse:
    return JsonResponse(
        {"statusCode": 401, "name": "missing_api_key", "message": message},
        status=401,
    )


def _forbidden(message: str) -> JsonResponse:
    return JsonResponse(
        {"statusCode": 403, "name": "insufficient_scope", "message": message},
        status=403,
    )


def require_api_key(*, scope: str = APIKey.SCOPE_SEND) -> Callable:
    """View decorator. Resolves an APIKey from `Authorization: Bearer ...`."""

    def deco(view):
        def wrapper(request: HttpRequest, *args, **kwargs):
            auth_header = request.META.get("HTTP_AUTHORIZATION", "")
            if not auth_header.lower().startswith("bearer "):
                return _unauthorized("Missing or invalid Authorization header.")
            raw = auth_header.split(" ", 1)[1].strip()
            key = APIKey.authenticate(raw)
            if key is None:
                return _unauthorized("API key is invalid or revoked.")
            if not key.has_scope(scope):
                return _forbidden(f"API key is missing required scope '{scope}'.")
            request.api_auth = AuthContext(api_key=key)  # type: ignore[attr-defined]
            return view(request, *args, **kwargs)

        wrapper.csrf_exempt = True  # never CSRF on /api/v1/*
        return wrapper

    return deco
