from __future__ import annotations

import os

from mailtivo_relay import __version__


def get_version() -> str:
    """The running app version. Canonical source: mailtivo_relay.__version__."""
    return __version__


def get_build_sha() -> str:
    """Optional build commit SHA, injected at image build time via RELAY_GIT_SHA.

    Purely informational (shown for traceability); empty when not provided.
    """
    return os.environ.get("RELAY_GIT_SHA", "")
