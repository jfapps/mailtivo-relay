from .base import *  # noqa: F401, F403
from .base import INSTALLED_APPS

DEBUG = True
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [*INSTALLED_APPS, "django_extensions"]

# Tailwind theme app is initialized on Day 2; uncomment then.
# INSTALLED_APPS = [*INSTALLED_APPS, "tailwind", "theme"]
# TAILWIND_APP_NAME = "theme"
INTERNAL_IPS = ["127.0.0.1"]

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
