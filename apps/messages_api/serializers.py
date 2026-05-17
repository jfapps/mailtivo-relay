"""Resend-compat request validation + response shaping for /api/v1/emails.

Resend's request body (https://resend.com/docs/api-reference/emails/send-email):
  {
    "from": "Acme <onboarding@resend.dev>",
    "to": ["delivered@resend.dev"],          # string or string[]
    "subject": "hello",
    "html": "...", "text": "...",            # at least one required
    "cc": [...], "bcc": [...], "reply_to": [...],   # string or string[]
    "scheduled_at": "2024-08-05T11:52:01.858Z",
    "headers": {...},
    "tags": [{"name": "...", "value": "..."}],
    "attachments": [
      {"filename": "...", "content": "<b64>", "content_type": "...", "content_id": "..."}
      or {"filename": "...", "path": "<url>"}
    ]
  }
Response: 200 with {"id": "uuid"} on success.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any

EMAIL_RE = re.compile(r"^[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+$")
ANGLE_EMAIL_RE = re.compile(r"<([^<>]+)>")

MAX_RECIPIENTS = 50


class ValidationError(Exception):
    def __init__(self, message: str, *, field: str = "", code: str = "validation_error") -> None:
        super().__init__(message)
        self.field = field
        self.code = code
        self.message = message


def _coerce_list(v: Any, *, field: str) -> list[str]:
    if v is None or v == "":
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        return [str(x) for x in v if x]
    raise ValidationError(f"{field} must be a string or list of strings.", field=field)


def _strip_angle(addr: str) -> str:
    m = ANGLE_EMAIL_RE.search(addr)
    return m.group(1) if m else addr.strip()


def _validate_recipients(addrs: list[str], *, field: str) -> list[str]:
    if len(addrs) > MAX_RECIPIENTS:
        raise ValidationError(f"{field}: too many recipients (max {MAX_RECIPIENTS}).", field=field)
    out: list[str] = []
    for a in addrs:
        cleaned = a.strip()
        bare = _strip_angle(cleaned)
        if not EMAIL_RE.match(bare):
            raise ValidationError(f"{field}: invalid email address '{a}'.", field=field)
        out.append(cleaned)
    return out


def _validate_iso8601(value: str, *, field: str) -> str:
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception as exc:
        raise ValidationError(f"{field}: must be ISO-8601 ('{value}').", field=field) from exc
    return value


def parse_email_request(body: dict) -> dict:
    """Validate + normalize an incoming /emails request body.

    Returns a dict ready for Message creation. Raises ValidationError on bad input.
    """
    if not isinstance(body, dict):
        raise ValidationError("Request body must be a JSON object.")

    from_addr = (body.get("from") or "").strip()
    if not from_addr:
        raise ValidationError("'from' is required.", field="from")
    bare_from = _strip_angle(from_addr)
    if not EMAIL_RE.match(bare_from):
        raise ValidationError("'from' must be a valid email address.", field="from")

    to = _validate_recipients(_coerce_list(body.get("to"), field="to"), field="to")
    if not to:
        raise ValidationError("'to' must have at least one recipient.", field="to")
    cc = _validate_recipients(_coerce_list(body.get("cc"), field="cc"), field="cc")
    bcc = _validate_recipients(_coerce_list(body.get("bcc"), field="bcc"), field="bcc")
    reply_to = _validate_recipients(_coerce_list(body.get("reply_to"), field="reply_to"), field="reply_to")

    subject = body.get("subject") or ""
    if not isinstance(subject, str):
        raise ValidationError("'subject' must be a string.", field="subject")

    html = body.get("html") or ""
    text = body.get("text") or ""
    if not isinstance(html, str) or not isinstance(text, str):
        raise ValidationError("'html'/'text' must be strings.", field="html")
    if not html and not text:
        raise ValidationError("At least one of 'html' or 'text' is required.", field="html")

    headers = body.get("headers") or {}
    if not isinstance(headers, dict):
        raise ValidationError("'headers' must be an object.", field="headers")

    tags = body.get("tags") or []
    if tags and not isinstance(tags, list):
        raise ValidationError("'tags' must be a list of {name, value}.", field="tags")
    for t in tags:
        if not isinstance(t, dict) or "name" not in t or "value" not in t:
            raise ValidationError("Each tag must be a {name, value} object.", field="tags")

    scheduled_at = body.get("scheduled_at")
    if scheduled_at:
        if not isinstance(scheduled_at, str):
            raise ValidationError("'scheduled_at' must be a string.", field="scheduled_at")
        _validate_iso8601(scheduled_at, field="scheduled_at")

    attachments_in = body.get("attachments") or []
    if attachments_in and not isinstance(attachments_in, list):
        raise ValidationError("'attachments' must be a list.", field="attachments")
    attachments: list[dict] = []
    for i, a in enumerate(attachments_in):
        if not isinstance(a, dict) or not a.get("filename"):
            raise ValidationError(f"attachment[{i}].filename is required.", field="attachments")
        content = a.get("content")
        if not content:
            # Remote-URL attachments (Resend supports `path`) aren't fetched server-side in v1.
            raise ValidationError(
                f"attachment[{i}].content (base64) is required. URL-based attachments aren't supported yet.",
                field="attachments",
            )
        attachments.append(
            {
                "filename": a["filename"],
                "content_b64": content,
                "content_type": a.get("content_type") or "",
                "content_id": a.get("content_id") or "",
            }
        )

    return {
        "from_address": from_addr,
        "to": to,
        "cc": cc,
        "bcc": bcc,
        "reply_to": reply_to,
        "subject": subject,
        "html": html,
        "text": text,
        "headers": headers,
        "tags": tags,
        "scheduled_at": scheduled_at,
        "attachments": attachments,
    }


def fingerprint_request(body: dict) -> str:
    """Stable hash of the request body for idempotency-key replay checks."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def serialize_message(message) -> dict:
    """Resend-compatible representation. Resend returns just {id} on POST, and
    a fuller object on GET. We serve both shapes from a single function."""
    body = message.body()
    return {
        "object": "email",
        "id": message.id,
        "from": message.from_address,
        "to": list(message.to or []),
        "cc": list(message.cc or []),
        "bcc": list(message.bcc or []),
        "reply_to": list(message.reply_to or []),
        "subject": message.subject,
        "html": body["html"],
        "text": body["text"],
        "headers": dict(message.headers or {}),
        "tags": list(message.tags or []),
        "last_event": message.status,
        "created_at": message.created_at.isoformat() if message.created_at else None,
        "scheduled_at": message.scheduled_at.isoformat() if message.scheduled_at else None,
    }
