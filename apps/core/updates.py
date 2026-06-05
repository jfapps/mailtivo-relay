from __future__ import annotations

import logging

import requests
from django.conf import settings
from django.utils import timezone
from packaging.version import InvalidVersion, Version

from apps.accounts.models import WorkspaceSettings

from .version import get_version

log = logging.getLogger(__name__)

# GitHub REST: "Get the latest release". Public repos need no auth.
# Docs: https://docs.github.com/en/rest/releases/releases#get-the-latest-release
# (verified 2026-05-31; API version pinned via the header below)
_GITHUB_API_VERSION = "2022-11-28"


def _parse(tag: str) -> Version | None:
    """Parse a release tag into a Version, tolerating a leading 'v'."""
    try:
        return Version(tag.lstrip("vV"))
    except (InvalidVersion, AttributeError):
        return None


def update_available(ws: WorkspaceSettings | None = None) -> bool:
    """True when the cached latest release is newer than the running version."""
    ws = ws or WorkspaceSettings.load()
    if not ws.latest_version:
        return False
    latest = _parse(ws.latest_version)
    current = _parse(get_version())
    if latest is None or current is None:
        return False
    return latest > current


def check_for_update() -> str | None:
    """Fetch the latest GitHub release tag and cache it on WorkspaceSettings.

    Best-effort: network/parse errors are swallowed (logged). Returns the
    discovered tag, or None when skipped or on failure.
    """
    ws = WorkspaceSettings.load()
    if not ws.update_check_enabled:
        return None

    url = f"https://api.github.com/repos/{settings.GITHUB_REPO}/releases/latest"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": _GITHUB_API_VERSION,
        "User-Agent": f"mailtivo-relay/{get_version()}",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=(5, 10))
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:  # ValueError = bad JSON
        log.warning("update check failed: %s", exc)
        return None

    tag = (data.get("tag_name") or "").strip()
    if not tag:
        log.warning("update check: response had no tag_name")
        return None

    ws.latest_version = tag[:40]
    ws.latest_release_url = (data.get("html_url") or "")[:200]
    ws.update_checked_at = timezone.now()
    ws.save(update_fields=["latest_version", "latest_release_url", "update_checked_at", "updated_at"])
    return tag
