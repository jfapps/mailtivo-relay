from __future__ import annotations

from .base import (
    AdapterError,
    AdapterResult,
    BaseAdapter,
    NormalizedEvent,
    PERMANENT_FAILURE,
    TEMPORARY_FAILURE,
)
from .postal import PostalAdapter
from .resend import ResendAdapter

PROVIDERS: dict[str, type[BaseAdapter]] = {
    "postal": PostalAdapter,
    "resend": ResendAdapter,
}

PROVIDER_LABELS: dict[str, str] = {
    "postal": "Postal",
    "resend": "Resend",
}


def get_adapter_class(provider_code: str) -> type[BaseAdapter]:
    try:
        return PROVIDERS[provider_code]
    except KeyError:
        raise AdapterError(f"Unknown provider: {provider_code}")


__all__ = [
    "AdapterError",
    "AdapterResult",
    "BaseAdapter",
    "NormalizedEvent",
    "PERMANENT_FAILURE",
    "TEMPORARY_FAILURE",
    "PostalAdapter",
    "ResendAdapter",
    "PROVIDERS",
    "PROVIDER_LABELS",
    "get_adapter_class",
]
