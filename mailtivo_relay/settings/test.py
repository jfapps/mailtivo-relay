from cryptography.fernet import Fernet

from .base import *  # noqa: F401, F403

DEBUG = False
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# Ephemeral per-run key. Tests only encrypt then decrypt fixture data within
# the same process, so a fresh key each run is sufficient — and it keeps any
# real-looking secret out of the (public) repository and CI logs.
RELAY_FERNET_KEY = Fernet.generate_key().decode()

# Tests don't run collectstatic, so the manifest doesn't exist — fall back to
# the plain FS finder so {% static %} resolves at render time.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
