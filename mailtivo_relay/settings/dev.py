from importlib.util import find_spec

from .base import *  # noqa: F401, F403
from .base import INSTALLED_APPS

DEBUG = True
ALLOWED_HOSTS = ["*"]

# django-extensions is a dev convenience (shell_plus, runserver_plus). It lives
# in requirements-dev.txt, so it's present in a local venv but NOT in the Docker
# image (which installs requirements.txt only). Enable it only when installed so
# the containerized dev overlay — which reuses the prod image — still boots.
if find_spec("django_extensions") is not None:
    INSTALLED_APPS = [*INSTALLED_APPS, "django_extensions"]

# Tailwind theme app is initialized on Day 2; uncomment then.
# INSTALLED_APPS = [*INSTALLED_APPS, "tailwind", "theme"]
# TAILWIND_APP_NAME = "theme"
INTERNAL_IPS = ["127.0.0.1"]

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
