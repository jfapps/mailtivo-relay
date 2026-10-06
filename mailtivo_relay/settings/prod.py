from decouple import config
from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F401, F403

DEBUG = False

# Fail fast on a misconfigured install instead of running with guessable or
# broken secrets. Both are one-liners to mint — see .env.example.
if SECRET_KEY == "dev-insecure-change-me":  # noqa: F405  # nosec B105 - reject known development sentinel
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY is not set. Generate one: "
        "python -c \"import secrets; print(secrets.token_urlsafe(64))\""
    )
if not config("RELAY_FERNET_KEY", default=""):
    raise ImproperlyConfigured(
        "RELAY_FERNET_KEY is not set — message bodies and provider credentials "
        "cannot be encrypted without it. Generate one: python -c "
        "\"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
    )

SECURE_HSTS_SECONDS = config("SECURE_HSTS_SECONDS", default=31536000, cast=int)
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_SSL_REDIRECT = config("SECURE_SSL_REDIRECT", default=True, cast=bool)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_BROWSER_XSS_FILTER = True
