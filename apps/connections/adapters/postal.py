"""Postal HTTP adapter.

Reference (retrieved 2026-05-17 — re-verify before significant changes):
  - Send:        POST https://<your-postal>/api/v1/send/message
                 https://docs.postalserver.io/developer/api/
                 https://apiv1.postalserver.io/controllers/send/message
  - Send raw:    POST .../api/v1/send/raw  (not used in v1)
  - Webhooks:    Documented event types — MessageSent, MessageDelayed,
                 MessageDeliveryFailed, MessageHeld, MessageBounced,
                 MessageLinkClicked, MessageLoaded, DomainDNSError.
                 Envelope: {event, timestamp, uuid, payload}.
                 https://docs.postalserver.io/developer/webhooks/

  Auth:       X-Server-API-Key: <token>
  Signatures: Three headers (Postal v3+ ships all three; v2 only the SHA-1 one):
                X-Postal-Signature      — RSA-SHA1 over raw body, Base64
                X-Postal-Signature-256  — RSA-SHA256 over raw body, Base64 (preferred)
                X-Postal-Signature-KID  — JWK key id
              Public key is the RSA signing key of the Postal instance
              (config/signing.key). We store it per-Connection.
              Source-verified at:
                https://github.com/postalserver/postal/blob/main/lib/postal/http.rb
                https://github.com/postalserver/postal/blob/main/lib/postal/signer.rb

  Health: No documented endpoint. We probe `GET /api/v1/...` and check for a
  Postal-shaped JSON error envelope to prove the server is up.
"""
from __future__ import annotations

import base64
import binascii
import json
from datetime import UTC, datetime

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from apps.core.encryption import decrypt

from .base import (
    PERMANENT_FAILURE,
    TEMPORARY_FAILURE,
    AdapterError,
    AdapterResult,
    BaseAdapter,
    NormalizedEvent,
)

_HTTP_TIMEOUT = 15

# Postal event name -> internal type
_EVENT_MAP = {
    "MessageSent": "delivered",  # Postal's "Sent" == handed to destination MX successfully
    "MessageDelayed": "deferred",
    "MessageDeliveryFailed": "failed",
    "MessageBounced": "bounced",
    "MessageHeld": "deferred",
    "MessageLinkClicked": "clicked",
    "MessageLoaded": "opened",
}


class PostalAdapter(BaseAdapter):
    provider_code = "postal"

    # --- helpers ----------------------------------------------------------
    def _server_api_key(self) -> str:
        if not self.connection.credentials_encrypted:
            raise AdapterError("Postal connection has no API key configured.")
        return decrypt(bytes(self.connection.credentials_encrypted))

    def _public_key(self) -> rsa.RSAPublicKey | None:
        if not self.connection.webhook_public_key_pem:
            return None
        try:
            key = serialization.load_pem_public_key(self.connection.webhook_public_key_pem.encode("utf-8"))
        except (ValueError, TypeError):
            return None
        if isinstance(key, rsa.RSAPublicKey):
            return key
        return None

    def _base_url(self) -> str:
        if not self.connection.base_url:
            raise AdapterError("Postal connection has no base URL configured.")
        return self.connection.base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {
            "X-Server-API-Key": self._server_api_key(),
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mailtivo-Relay/1.0",
        }

    @staticmethod
    def _build_payload(message: dict) -> dict:
        payload: dict = {
            "from": message["from"],
            "subject": message["subject"],
        }
        if to := message.get("to"):
            payload["to"] = to if isinstance(to, list) else [to]
        if cc := message.get("cc"):
            payload["cc"] = cc if isinstance(cc, list) else [cc]
        if bcc := message.get("bcc"):
            payload["bcc"] = bcc if isinstance(bcc, list) else [bcc]
        if reply_to := message.get("reply_to"):
            payload["reply_to"] = reply_to[0] if isinstance(reply_to, list) and reply_to else reply_to
        if html := message.get("html"):
            payload["html_body"] = html
        if text := message.get("text"):
            payload["plain_body"] = text
        if headers := message.get("headers"):
            payload["headers"] = headers
        # Postal supports a single `tag` string per message.
        if tags := message.get("tags"):
            first = tags[0] if isinstance(tags, list) else tags
            if isinstance(first, dict):
                payload["tag"] = first.get("name") or first.get("value") or ""
            else:
                payload["tag"] = str(first)
        if atts := message.get("attachments"):
            payload["attachments"] = [
                {
                    "name": a["filename"],
                    "content_type": a.get("content_type") or "application/octet-stream",
                    "data": a["content_b64"],
                }
                for a in atts
            ]
        return payload

    # --- BaseAdapter ------------------------------------------------------
    def send(self, *, message: dict) -> AdapterResult:
        url = f"{self._base_url()}/api/v1/send/message"
        try:
            resp = requests.post(
                url,
                headers=self._headers(),
                data=json.dumps(self._build_payload(message)),
                timeout=_HTTP_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise AdapterError(f"Postal network error: {exc}", kind=TEMPORARY_FAILURE) from exc

        if resp.status_code >= 500:
            raise AdapterError(f"Postal HTTP {resp.status_code}: {resp.text[:300]}", kind=TEMPORARY_FAILURE, status_code=resp.status_code)
        try:
            envelope = resp.json()
        except ValueError as exc:
            raise AdapterError(f"Postal returned non-JSON: {resp.text[:300]}", kind=TEMPORARY_FAILURE) from exc

        status = envelope.get("status")
        data = envelope.get("data") or {}
        if status != "success":
            code = (data.get("code") or "").lower()
            kind = TEMPORARY_FAILURE if code in {"unauthorized", "rate_limit_exceeded"} else PERMANENT_FAILURE
            raise AdapterError(
                f"Postal {status}: {data.get('message') or data}", kind=kind, status_code=resp.status_code
            )

        recipients = data.get("messages") or {}
        first_token = ""  # nosec B105 - empty parser sentinel, not a credential
        recipient_tokens: dict[str, str] = {}
        for addr, info in recipients.items():
            tok = info.get("token", "") if isinstance(info, dict) else ""
            recipient_tokens[addr] = tok
            if not first_token:
                first_token = tok
        provider_id = data.get("message_id") or first_token
        if not provider_id:
            raise AdapterError(f"Postal response missing message_id: {envelope}", kind=TEMPORARY_FAILURE)
        return AdapterResult(
            provider_message_id=provider_id,
            recipient_tokens=recipient_tokens,
            raw_response=envelope,
        )

    def healthcheck(self) -> bool:
        # Probe send endpoint with an empty body; we expect 400/422 (Postal
        # error envelope) on a reachable server with valid auth. 401 = bad key.
        try:
            resp = requests.post(
                f"{self._base_url()}/api/v1/send/message",
                headers=self._headers(),
                data="{}",
                timeout=_HTTP_TIMEOUT,
            )
        except requests.RequestException:
            return False
        if resp.status_code == 401:
            return False
        if resp.status_code >= 500:
            return False
        try:
            envelope = resp.json()
        except ValueError:
            return False
        # A "validation error" / "no recipients" status means we authenticated and got a structured response.
        if envelope.get("status") in {"error", "parameter-error"}:
            return True
        # Some Postal versions return `success` with structured zero-recipient data — that's fine too.
        return envelope.get("status") == "success"

    def verify_webhook(self, *, headers, body: bytes) -> bool:
        public_key = self._public_key()
        if public_key is None:
            return False
        sig_256 = (
            headers.get("x-postal-signature-256")
            or headers.get("X-Postal-Signature-256")
            or ""
        )
        sig_legacy = headers.get("x-postal-signature") or headers.get("X-Postal-Signature") or ""

        # Prefer SHA-256 when present.
        if sig_256:
            try:
                signature = base64.b64decode(sig_256)
            except (binascii.Error, ValueError):
                return False
            try:
                public_key.verify(signature, body, padding.PKCS1v15(), hashes.SHA256())
                return True
            except InvalidSignature:
                return False
        if sig_legacy:
            try:
                signature = base64.b64decode(sig_legacy)
            except (binascii.Error, ValueError):
                return False
            try:
                public_key.verify(signature, body, padding.PKCS1v15(), hashes.SHA1())  # noqa: S303  # nosec B303 - Postal legacy RSA-SHA1 verification
                return True
            except InvalidSignature:
                return False
        return False

    def parse_event(self, *, body: bytes) -> NormalizedEvent:
        envelope = json.loads(body.decode("utf-8"))
        event_name = envelope.get("event", "")
        ev_type = _EVENT_MAP.get(event_name, "unknown")

        ts_raw = envelope.get("timestamp")
        if isinstance(ts_raw, (int, float)):
            occurred_at = datetime.fromtimestamp(float(ts_raw), tz=UTC)
        else:
            occurred_at = datetime.now(UTC)

        uuid = envelope.get("uuid") or ""
        payload = envelope.get("payload") or {}
        msg = payload.get("message") or payload  # some events nest, some don't
        recipient_token = str(msg.get("token") or "")
        provider_message_id = str(msg.get("id") or msg.get("message_id") or "")
        provider_event_id = uuid or f"{provider_message_id}:{recipient_token}:{event_name}:{ts_raw}"

        ev = NormalizedEvent(
            type=ev_type,
            occurred_at=occurred_at,
            provider_event_id=provider_event_id,
            provider_message_id=provider_message_id,
            recipient_token=recipient_token,
            raw=envelope,
        )
        if to := (msg.get("to") or payload.get("to")):
            ev.recipient = to if isinstance(to, str) else (to[0] if to else "")
        if ev.type == "bounced":
            ev.bounce_reason = payload.get("details") or payload.get("output") or ""
            # Postal's MessageBounced payload carries no hard/soft indicator
            # (verified 2026-07-11: {original_message, bounce} only), so the
            # class stays "unknown" and the ingest path suppresses — Postal only
            # emits MessageBounced for genuine bounce messages it received.
        elif ev.type == "clicked":
            ev.link_url = payload.get("url", "")
            ev.user_agent = payload.get("user_agent", "")
            ev.ip = payload.get("ip_address", "")
        elif ev.type == "opened":
            ev.user_agent = payload.get("user_agent", "")
            ev.ip = payload.get("ip_address", "")
        return ev
