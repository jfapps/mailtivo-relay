"""Adapter interface that every upstream provider implements.

A "Connection" stores credentials + base URL for one upstream account at one
provider. An adapter instance wraps a Connection and knows how to:
  - send a Message (returning provider_message_id + recipient mapping),
  - verify an incoming webhook's signature,
  - normalize a raw webhook payload into a NormalizedEvent,
  - run a health-check.

Adapters MUST be stateless beyond the Connection they wrap, because the
send pipeline (Day 4) may instantiate one per task.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Mapping

if TYPE_CHECKING:
    from apps.connections.models import Connection


PERMANENT_FAILURE = "permanent"  # don't retry on this connection (4xx body errors)
TEMPORARY_FAILURE = "temporary"  # retry / try next member (5xx, timeouts, 429)


class AdapterError(Exception):
    """Raised when an adapter cannot complete an operation.

    `kind` is one of PERMANENT_FAILURE / TEMPORARY_FAILURE — the router uses this
    to decide whether to fall through to another pool member.
    """

    def __init__(self, message: str, *, kind: str = PERMANENT_FAILURE, status_code: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code


@dataclass(slots=True)
class AdapterResult:
    """Outcome of a successful `send()`."""

    provider_message_id: str
    # Per-recipient identifier returned by the provider, when distinct from
    # provider_message_id (Postal returns a `token` per recipient).
    recipient_tokens: Mapping[str, str] = field(default_factory=dict)
    # Whatever raw response the provider returned, for audit / debugging.
    raw_response: dict = field(default_factory=dict)


@dataclass(slots=True)
class NormalizedEvent:
    """A provider webhook event after normalization to Mailtivo-Relay's schema."""

    EVENT_TYPES = {
        "queued",
        "sent",
        "delivered",
        "deferred",
        "bounced",
        "complained",
        "opened",
        "clicked",
        "failed",
        "unknown",
    }

    type: str
    occurred_at: datetime
    provider_event_id: str
    recipient: str = ""
    provider_message_id: str = ""
    recipient_token: str = ""  # Postal per-recipient identifier
    raw: dict = field(default_factory=dict)
    link_url: str = ""
    user_agent: str = ""
    ip: str = ""
    bounce_reason: str = ""

    def __post_init__(self) -> None:
        if self.type not in self.EVENT_TYPES:
            self.type = "unknown"


class BaseAdapter:
    """Interface every provider adapter implements."""

    provider_code: str = ""

    def __init__(self, connection: "Connection") -> None:
        self.connection = connection

    # --- outbound ---------------------------------------------------------
    def send(self, *, message: dict) -> AdapterResult:
        """Send `message` upstream and return an AdapterResult.

        `message` is a provider-agnostic dict shaped as:
            {
              "from": "Name <addr@dom>",
              "to": ["a@x"], "cc": [...], "bcc": [...],
              "subject": "...",
              "html": "...", "text": "...",
              "reply_to": ["..."],
              "headers": {"X-Foo": "bar"},
              "tags": [{"name": "...", "value": "..."}],  # provider normalizes
              "attachments": [{"filename":"...", "content_type":"...", "content_b64":"..."}],
              "scheduled_at": "ISO 8601" | None,
            }
        Raises AdapterError on failure.
        """
        raise NotImplementedError

    def healthcheck(self) -> bool:
        """Return True if the connection looks usable. Should not raise."""
        raise NotImplementedError

    # --- inbound ----------------------------------------------------------
    def verify_webhook(self, *, headers: Mapping[str, str], body: bytes) -> bool:
        """Return True if the signature on this raw webhook payload is valid."""
        raise NotImplementedError

    def parse_event(self, *, body: bytes) -> NormalizedEvent:
        """Parse a verified raw payload into a NormalizedEvent."""
        raise NotImplementedError
