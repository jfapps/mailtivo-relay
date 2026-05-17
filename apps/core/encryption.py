from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


@lru_cache(maxsize=1)
def _cipher() -> MultiFernet:
    primary = getattr(settings, "RELAY_FERNET_KEY", "") or ""
    if not primary:
        raise ImproperlyConfigured(
            "RELAY_FERNET_KEY is not set. Mint one with "
            "`python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"`."
        )
    keys = [Fernet(primary.encode())]
    old = getattr(settings, "RELAY_FERNET_KEY_OLD", "") or ""
    if old:
        keys.append(Fernet(old.encode()))
    return MultiFernet(keys)


def encrypt(value: str) -> bytes:
    if value is None:
        return b""
    return _cipher().encrypt(value.encode("utf-8"))


def decrypt(token: bytes | str) -> str:
    if not token:
        return ""
    if isinstance(token, str):
        token = token.encode("utf-8")
    return _cipher().decrypt(token).decode("utf-8")
