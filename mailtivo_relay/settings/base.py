from pathlib import Path

import dj_database_url
from decouple import Csv, config

BASE_DIR = Path(__file__).resolve().parent.parent.parent

SECRET_KEY = config("DJANGO_SECRET_KEY", default="dev-insecure-change-me")
DEBUG = config("DJANGO_DEBUG", default=False, cast=bool)
ALLOWED_HOSTS = config("DJANGO_ALLOWED_HOSTS", default="localhost,127.0.0.1", cast=Csv())
CSRF_TRUSTED_ORIGINS = config(
    "DJANGO_CSRF_TRUSTED_ORIGINS",
    default="http://localhost:8000,http://127.0.0.1:8000",
    cast=Csv(),
)

# Custom user lives at the email address.
AUTH_USER_MODEL = "accounts.User"

INSTALLED_APPS = [
    # No django.contrib.admin — Mailtivo-Relay ships a custom panel.
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",  # required by allauth's SocialApp linkage
    # Third-party
    "django_htmx",
    "django_q",
    "rest_framework",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
    # First-party
    "apps.core",
    "apps.accounts",
    "apps.panel",
    "apps.connections",
    "apps.pools",
    "apps.api_keys",
    "apps.messages_api",
    "apps.events",
    "apps.suppressions",
    "apps.sending",
    "apps.audit",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
    "allauth.account.middleware.AccountMiddleware",
]

ROOT_URLCONF = "mailtivo_relay.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "mailtivo_relay" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.accounts.context_processors.workspace",
            ],
        },
    },
]

WSGI_APPLICATION = "mailtivo_relay.wsgi.application"
ASGI_APPLICATION = "mailtivo_relay.asgi.application"

DATABASES = {
    "default": dj_database_url.config(
        default=config(
            "DATABASE_URL",
            default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        ),
        conn_max_age=600,
    )
}

# Argon2id first — falls back to PBKDF2 for legacy hashes.
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    "django.contrib.auth.hashers.BCryptSHA256PasswordHasher",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "mailtivo_relay" / "static"]

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
SITE_ID = 1

# Auth redirects
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/app/"
LOGOUT_REDIRECT_URL = "/login/"

# Allauth — kept conservative; Google provider is toggled at runtime from WorkspaceSettings.
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*", "password2*"]
ACCOUNT_EMAIL_VERIFICATION = "optional"
SOCIALACCOUNT_PROVIDERS: dict[str, dict] = {}  # populated dynamically when Google OAuth is enabled

# Body encryption — Fernet key (URL-safe base64-encoded 32-byte key).
# Use `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` to mint.
RELAY_FERNET_KEY = config("RELAY_FERNET_KEY", default="")
RELAY_FERNET_KEY_OLD = config("RELAY_FERNET_KEY_OLD", default="")  # for rotation via MultiFernet

# Magic link expiry (minutes).
MAGIC_LINK_TTL_MINUTES = config("MAGIC_LINK_TTL_MINUTES", default=15, cast=int)

# Django-Q2 — ORM broker by default so solo self-hosters need no Redis.
Q_CLUSTER = {
    "name": "mailtivo_relay",
    "workers": config("Q_WORKERS", default=4, cast=int),
    "recycle": 500,
    "timeout": 90,
    "retry": 120,
    "compress": True,
    "save_limit": 1000,
    "queue_limit": 50,
    "cpu_affinity": 1,
    "label": "Mailtivo Q",
    "orm": "default",
}

# DRF — Bearer auth handled by our own middleware on /api/v1/.
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": [],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
}

# Email backend in non-prod: console. Prod will swap to actual SMTP/Postal/etc.
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
DEFAULT_FROM_EMAIL = config("DEFAULT_FROM_EMAIL", default="no-reply@mailtivo.local")

# Security defaults (tightened in prod.py).
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_HTTPONLY = False  # HTMX needs to read CSRF token
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {"format": "{asctime} {levelname} {name} {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django.db.backends": {"level": "WARNING"},
        "mailtivo_relay": {"level": "DEBUG", "propagate": True},
    },
}
