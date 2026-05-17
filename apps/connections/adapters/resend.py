"""Resend HTTP adapter.

Reference (retrieved 2026-05-17 — re-verify before significant changes):
  - Send:        POST https://api.resend.com/emails
                 https://resend.com/docs/api-reference/emails/send-email
  - Retrieve:    GET  https://api.resend.com/emails/{id}
                 https://resend.com/docs/api-reference/emails/retrieve-email
  - Errors:      https://resend.com/docs/api-reference/errors
  - Rate limit:  5 req/s/team. Headers ratelimit-limit / remaining / reset, retry-after.
                 https://resend.com/docs/api-reference/rate-limit
  - Webhooks:    Svix-signed. Headers: svix-id, svix-timestamp, svix-signature.
                 Signed content = f"{id}.{timestamp}.{raw_body}", HMAC-SHA256,
                 secret = base64-decode of the value after the `whsec_` prefix.
                 https://resend.com/docs/dashboard/webhooks/verify-webhooks-requests
  - Events:      email.sent / delivered / delivery_delayed / complained / bounced /
                 opened / clicked / failed / scheduled / received / suppressed.
                 https://resend.com/docs/webhooks/event-types

  Auth: `Authorization: Bearer re_...`
  Idempotency: `Idempotency-Key` header, 24h dedup; same key + different body -> 409.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone

import requests

from apps.core.encryption import decrypt

from .base import (
    AdapterError,
    AdapterResult,
    BaseAdapter,
    NormalizedEvent,
    PERMANENT_FAILURE,
    TEMPORARY_FAILURE,
)

_DEFAULT_BASE_URL = "https://api.resend.com"
_HTTP_TIMEOUT = 15

# email.* -> internal type
_EVENT_MAP = {
    "email.sent": "sent",
    "email.delivered": "delivered",
    "email.delivery_delayed": "deferred",
    "email.bounced": "bounced",
    "email.complained": "complained",
    "email.opened": "opened",
    "email.clicked": "clicked",
    "email.failed": "failed",
    "email.scheduled": "queued",
    "email.received": "delivered",  # treat inbound mirror as delivered for status
    "email.suppressed": "failed",
}


class ResendAdapter(BaseAdapter):
    provider_code = "resend"

    # --- helpers ----------------------------------------------------------
    def _api_key(self) -> str:
        if not self.connection.credentials_encrypted:
            raise AdapterError("Resend connection has no API key configured.")
        return decrypt(bytes(self.connection.credentials_encrypted))

    def _webhook_secret(self) -> str:
        if not self.connection.webhook_secret_encrypted:
            raise AdapterError("Resend connection has no webhook signing secret configured.")
        return decrypt(bytes(self.connection.webhook_secret_encrypted))

    def _base_url(self) -> str:
        return (self.connection.base_url or _DEFAULT_BASE_URL).rstrip("/")

    def _headers(self, *, idempotency_key: str | None = None) -> dict[str, str]:
        h = {
            "Authorization": f"Bearer {self._api_key()}",
            "Content-Type": "application/json",
            "User-Agent": "Mailtivo-Relay/1.0",
        }
        if idempotency_key:
            h["Idempotency-Key"] = idempotency_key
        return h

    @staticmethod
    def _build_payload(message: dict) -> dict:
        payload: dict = {
            "from": message["from"],
            "to": message["to"] if isinstance(message["to"], list) else [message["to"]],
            "subject": message["subject"],
        }
        if html := message.get("html"):
            payload["html"] = html
        if text := message.get("text"):
            payload["text"] = text
        for k in ("cc", "bcc", "reply_to"):
            if v := message.get(k):
                payload[k] = v if isinstance(v, list) else [v]
        if headers := message.get("headers"):
            payload["headers"] = headers
        if tags := message.get("tags"):
            payload["tags"] = tags
        if scheduled_at := message.get("scheduled_at"):
            payload["scheduled_at"] = scheduled_at
        if atts := message.get("attachments"):
            payload["attachments"] = [
                {
                    "filename": a["filename"],
                    "content": a["content_b64"],
                    **({"content_type": a["content_type"]} if a.get("content_type") else {}),
                    **({"content_id": a["content_id"]} if a.get("content_id") else {}),
                }
                for a in atts
            ]
        return payload

    @staticmethod
    def _raise_for_status(resp: requests.Response) -> None:
        if 200 <= resp.status_code < 300:
            return
        try:
            body = resp.json()
            msg = body.get("message") or body.get("error") or resp.text
        except Exception:
            msg = resp.text
        kind = TEMPORARY_FAILURE if resp.status_code >= 500 or resp.status_code in {408, 425, 429} else PERMANENT_FAILURE
        raise AdapterError(f"Resend HTTP {resp.status_code}: {msg}", kind=kind, status_code=resp.status_code)

    # --- BaseAdapter ------------------------------------------------------
    def send(self, *, message: dict) -> AdapterResult:
        url = f"{self._base_url()}/emails"
        try:
            resp = requests.post(
                url,
                headers=self._headers(idempotency_key=message.get("idempotency_key")),
                data=json.dumps(self._build_payload(message)),
                timeout=_HTTP_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise AdapterError(f"Resend network error: {exc}", kind=TEMPORARY_FAILURE) from exc

        self._raise_for_status(resp)
        body = resp.json()
        msg_id = body.get("id")
        if not msg_id:
            raise AdapterError(f"Resend response missing id: {body}", kind=TEMPORARY_FAILURE)
        return AdapterResult(provider_message_id=msg_id, raw_response=body)

    def healthcheck(self) -> bool:
        # Resend has no documented liveness endpoint. Hit the rate-limit / 404
        # path to confirm auth + reachability without sending mail. A revoked
        # key returns 401; a working key returns 404 with a JSON body.
        try:
            resp = requests.get(
                f"{self._base_url()}/emails/healthcheck-noop",
                headers=self._headers(),
                timeout=_HTTP_TIMEOUT,
            )
        except requests.RequestException:
            return False
        if resp.status_code == 401:
            return False
        return resp.status_code in (200, 404, 422)

    def verify_webhook(self, *, headers, body: bytes) -> bool:
        svix_id = headers.get("svix-id") or headers.get("Svix-Id") or ""
        svix_ts = headers.get("svix-timestamp") or headers.get("Svix-Timestamp") or ""
        svix_sig = headers.get("svix-signature") or headers.get("Svix-Signature") or ""
        if not (svix_id and svix_ts and svix_sig):
            return False

        secret = self._webhook_secret()
        if not secret.startswith("whsec_"):
            return False
        try:
            key = base64.b64decode(secret.removeprefix("whsec_"))
        except (ValueError, TypeError):
            return False

        signed_content = f"{svix_id}.{svix_ts}.".encode("utf-8") + body
        expected = base64.b64encode(hmac.new(key, signed_content, hashlib.sha256).digest()).decode("ascii")

        for token in svix_sig.split(" "):
            if "," not in token:
                continue
            _, presented = token.split(",", 1)
            if hmac.compare_digest(presented, expected):
                return True
        return False

    def parse_event(self, *, body: bytes) -> NormalizedEvent:
        payload = json.loads(body.decode("utf-8"))
        ev_type_raw = payload.get("type", "")
        ev_type = _EVENT_MAP.get(ev_type_raw, "unknown")
        ts_str = payload.get("created_at") or ""
        try:
            occurred_at = datetime.fromisoformat(ts_str.replace("Z", "+00:00")) if ts_str else datetime.now(timezone.utc)
        except ValueError:
            occurred_at = datetime.now(timezone.utc)

        data = payload.get("data") or {}
        email_id = data.get("email_id") or data.get("id") or ""
        # Resend payloads don't ship a globally unique webhook id in the body — Svix headers do,
        # but we want it accessible to the parser. Compose from email_id + type + timestamp.
        provider_event_id = f"{email_id}:{ev_type_raw}:{ts_str}"

        ev = NormalizedEvent(
            type=ev_type,
            occurred_at=occurred_at,
            provider_event_id=provider_event_id,
            provider_message_id=email_id,
            raw=payload,
        )
        recipients = data.get("to") or []
        if recipients:
            ev.recipient = recipients[0] if isinstance(recipients, list) else recipients
        if ev_type == "bounced":
            bounce = data.get("bounce") or {}
            ev.bounce_reason = bounce.get("message") or bounce.get("subType") or bounce.get("type") or ""
        elif ev_type == "clicked":
            click = data.get("click") or {}
            ev.link_url = click.get("link", "")
            ev.user_agent = click.get("userAgent", "")
            ev.ip = click.get("ipAddress", "")
        return ev
