from __future__ import annotations

from django.http import HttpRequest

from apps.core.updates import update_available
from apps.core.version import get_version

from .models import WorkspaceSettings


def workspace(request: HttpRequest) -> dict[str, object]:
    """Make the singleton workspace settings + app version available to every template."""
    ws = WorkspaceSettings.load()
    return {
        "workspace": ws,
        "app_version": get_version(),
        "update_available": update_available(ws),
        "latest_version": ws.latest_version,
        "latest_release_url": ws.latest_release_url,
    }
