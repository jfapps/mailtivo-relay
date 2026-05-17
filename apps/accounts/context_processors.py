from __future__ import annotations

from django.http import HttpRequest

from .models import WorkspaceSettings


def workspace(request: HttpRequest) -> dict[str, object]:
    """Make the singleton workspace settings available to every template."""
    return {"workspace": WorkspaceSettings.load()}
