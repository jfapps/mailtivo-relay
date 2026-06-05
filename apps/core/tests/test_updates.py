from __future__ import annotations

from unittest import mock

import pytest
import requests

from apps.accounts.models import WorkspaceSettings
from apps.core import updates
from apps.core.version import get_version


def test_get_version_matches_dunder():
    from mailtivo_relay import __version__

    assert get_version() == __version__


@pytest.mark.django_db
@pytest.mark.parametrize(
    "current,latest,expected",
    [
        ("1.0.0", "1.1.0", True),
        ("1.0.0", "v1.1.0", True),   # tolerate leading 'v'
        ("1.0.0", "1.0.0", False),
        ("1.0.0", "0.9.0", False),
        ("1.0.0", "", False),        # nothing cached yet
        ("1.0.0", "not-a-version", False),
    ],
)
def test_update_available(current, latest, expected):
    ws = WorkspaceSettings.load()
    ws.latest_version = latest
    with mock.patch.object(updates, "get_version", return_value=current):
        assert updates.update_available(ws) is expected


@pytest.mark.django_db
def test_check_for_update_populates_singleton():
    payload = {"tag_name": "v2.3.4", "html_url": "https://github.com/o/r/releases/tag/v2.3.4"}
    resp = mock.Mock(status_code=200)
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None

    with mock.patch.object(updates.requests, "get", return_value=resp) as get:
        tag = updates.check_for_update()

    assert tag == "v2.3.4"
    get.assert_called_once()
    ws = WorkspaceSettings.load()
    assert ws.latest_version == "v2.3.4"
    assert ws.latest_release_url == payload["html_url"]
    assert ws.update_checked_at is not None


@pytest.mark.django_db
def test_check_for_update_respects_disabled_toggle():
    ws = WorkspaceSettings.load()
    ws.update_check_enabled = False
    ws.save()

    with mock.patch.object(updates.requests, "get") as get:
        assert updates.check_for_update() is None
    get.assert_not_called()


@pytest.mark.django_db
def test_check_for_update_swallows_network_error():
    with mock.patch.object(updates.requests, "get", side_effect=requests.RequestException("boom")):
        assert updates.check_for_update() is None
    assert WorkspaceSettings.load().latest_version == ""
