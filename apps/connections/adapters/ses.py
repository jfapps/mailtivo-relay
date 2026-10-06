"""Amazon SES (v2) HTTP adapter.

Reference (retrieved 2026-06-05 — re-verify before significant changes):
  - Send:        POST https://email.<region>.amazonaws.com/v2/email/outbound-emails
                 https://docs.aws.amazon.com/ses/latest/APIReference-V2/API_SendEmail.html
                 Body: Content.Simple (Subject/Body/Headers/Attachments), Destination,
                 FromEmailAddress, ReplyToAddresses, ConfigurationSetName, EmailTags.
                 Response: {"MessageId": "..."}.
  - Health:      GET https://email.<region>.amazonaws.com/v2/email/account (GetAccount)
                 https://docs.aws.amazon.com/ses/latest/APIReference-V2/API_GetAccount.html
  - Auth:        AWS Signature Version 4, signing service "ses".
                 https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv4-signing.html
  - Events:      SES has no native webhooks. Events are published to an SNS topic via a
                 configuration set event destination, delivered to us as SNS HTTPS POSTs.
                 SNS message types: SubscriptionConfirmation / Notification / UnsubscribeConfirmation.
                 https://docs.aws.amazon.com/ses/latest/dg/notification-contents.html
                 https://docs.aws.amazon.com/sns/latest/dg/sns-verify-signature-of-message.html

  Credentials: credentials_encrypted holds json.dumps({"access_key_id", "secret_access_key"}).
  Region + configuration set are non-secret Connection fields.

  Unsupported in v1 (silently ignored): message scheduling and idempotency keys — SES
  SendEmail has neither. Custom headers go through Content.Simple.Headers (best effort);
  byte-exact header fidelity would need Content.Raw (MIME), which is out of scope.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from datetime import UTC, datetime
from urllib.parse import urlparse

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.x509 import load_pem_x509_certificate

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
_SIGV4_SERVICE = "ses"
_SIGV4_ALGORITHM = "AWS4-HMAC-SHA256"

# SES notificationType / eventType -> internal type.
_EVENT_MAP = {
    "Send": "sent",
    "Delivery": "delivered",
    "Bounce": "bounced",
    "Complaint": "complained",
    "Reject": "failed",
    "Open": "opened",
    "Click": "clicked",
    "DeliveryDelay": "deferred",
    "Rendering Failure": "failed",
    "RenderingFailure": "failed",
}

# Allowed AWS SNS host pattern — both the SigningCertURL and SubscribeURL must match
# before we fetch/GET them, to prevent SSRF via a forged notification.
_SNS_HOST_RE = re.compile(r"^sns\.[a-z0-9-]+\.amazonaws\.com(\.cn)?$")

# SES EmailTags Name/Value must match this; non-conforming tags are dropped.
_TAG_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Small in-process cache of SNS signing certs, keyed by URL. Certs rotate rarely.
_CERT_CACHE: dict[str, object] = {}


def is_sns_url(url: str) -> bool:
    """True if `url` is an https URL on a real AWS SNS host (SSRF allowlist)."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    return bool(_SNS_HOST_RE.match(parsed.hostname))


def _as_list(v) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


class SesAdapter(BaseAdapter):
    provider_code = "ses"

    # --- helpers ----------------------------------------------------------
    def _creds(self) -> tuple[str, str]:
        if not self.connection.credentials_encrypted:
            raise AdapterError("SES connection has no credentials configured.")
        try:
            data = json.loads(decrypt(bytes(self.connection.credentials_encrypted)))
            return data["access_key_id"], data["secret_access_key"]
        except (ValueError, KeyError, TypeError) as exc:
            raise AdapterError(f"SES credentials are malformed: {exc}") from exc

    def _region(self) -> str:
        if not self.connection.aws_region:
            raise AdapterError("SES connection has no AWS region configured.")
        return self.connection.aws_region

    def _config_set(self) -> str:
        return self.connection.ses_configuration_set or ""

    def _host(self) -> str:
        return f"email.{self._region()}.amazonaws.com"

    def _endpoint(self, path: str) -> str:
        return f"https://{self._host()}{path}"

    # --- SigV4 ------------------------------------------------------------
    @staticmethod
    def _sign(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    def _sigv4_headers(self, *, method: str, path: str, query: str, payload: bytes) -> dict[str, str]:
        access_key_id, secret_access_key = self._creds()
        region = self._region()
        host = self._host()

        now = datetime.now(UTC)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = now.strftime("%Y%m%d")

        payload_hash = hashlib.sha256(payload).hexdigest()

        canonical_headers = (
            f"host:{host}\n"
            f"x-amz-content-sha256:{payload_hash}\n"
            f"x-amz-date:{amz_date}\n"
        )
        signed_headers = "host;x-amz-content-sha256;x-amz-date"
        canonical_request = "\n".join(
            [method, path, query, canonical_headers, signed_headers, payload_hash]
        )

        credential_scope = f"{datestamp}/{region}/{_SIGV4_SERVICE}/aws4_request"
        string_to_sign = "\n".join(
            [
                _SIGV4_ALGORITHM,
                amz_date,
                credential_scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )

        k_date = self._sign(f"AWS4{secret_access_key}".encode(), datestamp)
        k_region = self._sign(k_date, region)
        k_service = self._sign(k_region, _SIGV4_SERVICE)
        k_signing = self._sign(k_service, "aws4_request")
        signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

        authorization = (
            f"{_SIGV4_ALGORITHM} Credential={access_key_id}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        headers = {
            "Authorization": authorization,
            "X-Amz-Date": amz_date,
            "X-Amz-Content-Sha256": payload_hash,
            "Host": host,
            "User-Agent": "Mailtivo-Relay/1.0",
        }
        if method == "POST":
            headers["Content-Type"] = "application/json"
        return headers

    # --- payload ----------------------------------------------------------
    @staticmethod
    def _build_payload(message: dict) -> dict:
        body: dict = {}
        if html := message.get("html"):
            body["Html"] = {"Data": html}
        if text := message.get("text"):
            body["Text"] = {"Data": text}

        simple: dict = {
            "Subject": {"Data": message["subject"]},
            "Body": body,
        }
        if headers := message.get("headers"):
            simple["Headers"] = [{"Name": str(k), "Value": str(v)} for k, v in headers.items()]
        if atts := message.get("attachments"):
            simple["Attachments"] = [
                {
                    "FileName": a["filename"],
                    "RawContent": a["content_b64"],
                    "ContentType": a.get("content_type") or "application/octet-stream",
                    "ContentDisposition": "ATTACHMENT",
                    **({"ContentId": a["content_id"]} if a.get("content_id") else {}),
                }
                for a in atts
            ]

        destination: dict = {"ToAddresses": _as_list(message.get("to"))}
        if cc := message.get("cc"):
            destination["CcAddresses"] = _as_list(cc)
        if bcc := message.get("bcc"):
            destination["BccAddresses"] = _as_list(bcc)

        payload: dict = {
            "FromEmailAddress": message["from"],
            "Destination": destination,
            "Content": {"Simple": simple},
        }
        if reply_to := message.get("reply_to"):
            payload["ReplyToAddresses"] = _as_list(reply_to)
        if tags := message.get("tags"):
            email_tags = []
            for t in (tags if isinstance(tags, list) else [tags]):
                if isinstance(t, dict):
                    name = str(t.get("name") or t.get("value") or "")
                    value = str(t.get("value") or "1")
                else:
                    name, value = str(t), "1"
                if _TAG_RE.match(name) and _TAG_RE.match(value):
                    email_tags.append({"Name": name, "Value": value})
            if email_tags:
                payload["EmailTags"] = email_tags
        return payload

    @staticmethod
    def _raise_for_status(resp: requests.Response) -> None:
        if 200 <= resp.status_code < 300:
            return
        try:
            body = resp.json()
            msg = body.get("message") or body.get("Message") or resp.text
            err_type = body.get("__type") or body.get("type") or ""
        except Exception:
            msg, err_type = resp.text, ""
        # Throttling can arrive as a 400 with a Throttling __type — treat as temporary.
        throttled = "Throttl" in err_type or "TooManyRequests" in err_type
        if resp.status_code >= 500 or resp.status_code == 429 or throttled:
            kind = TEMPORARY_FAILURE
        else:
            kind = PERMANENT_FAILURE
        raise AdapterError(f"SES HTTP {resp.status_code}: {msg}", kind=kind, status_code=resp.status_code)

    # --- BaseAdapter ------------------------------------------------------
    def send(self, *, message: dict) -> AdapterResult:
        payload = self._build_payload(message)
        if config_set := self._config_set():
            payload["ConfigurationSetName"] = config_set
        body = json.dumps(payload).encode("utf-8")
        path = "/v2/email/outbound-emails"
        headers = self._sigv4_headers(method="POST", path=path, query="", payload=body)
        try:
            resp = requests.post(self._endpoint(path), headers=headers, data=body, timeout=_HTTP_TIMEOUT)
        except requests.RequestException as exc:
            raise AdapterError(f"SES network error: {exc}", kind=TEMPORARY_FAILURE) from exc

        self._raise_for_status(resp)
        data = resp.json()
        msg_id = data.get("MessageId")
        if not msg_id:
            raise AdapterError(f"SES response missing MessageId: {data}", kind=TEMPORARY_FAILURE)
        return AdapterResult(provider_message_id=msg_id, raw_response=data)

    def healthcheck(self) -> bool:
        # GetAccount confirms reachability + valid SigV4 creds without sending mail.
        path = "/v2/email/account"
        try:
            headers = self._sigv4_headers(method="GET", path=path, query="", payload=b"")
            resp = requests.get(self._endpoint(path), headers=headers, timeout=_HTTP_TIMEOUT)
        except AdapterError:
            return False
        except requests.RequestException:
            return False
        return resp.status_code == 200

    def verify_webhook(self, *, headers, body: bytes) -> bool:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return False

        cert_url = payload.get("SigningCertURL") or payload.get("SigningCertUrl") or ""
        if not is_sns_url(cert_url):
            return False

        public_key = self._sns_public_key(cert_url)
        if public_key is None:
            return False

        msg_type = payload.get("Type", "")
        canonical = self._sns_canonical_string(payload, msg_type)
        if canonical is None:
            return False

        try:
            signature = base64.b64decode(payload.get("Signature", ""))
        except (binascii.Error, ValueError):
            return False

        algo = hashes.SHA256() if str(payload.get("SignatureVersion")) == "2" else hashes.SHA1()
        try:
            public_key.verify(signature, canonical.encode("utf-8"), padding.PKCS1v15(), algo)
            return True
        except InvalidSignature:
            return False

    @staticmethod
    def _sns_public_key(cert_url: str):
        cached = _CERT_CACHE.get(cert_url)
        if cached is not None:
            return cached
        try:
            resp = requests.get(cert_url, timeout=_HTTP_TIMEOUT)
            if resp.status_code != 200:
                return None
            cert = load_pem_x509_certificate(resp.content)
        except (requests.RequestException, ValueError):
            return None
        key = cert.public_key()
        _CERT_CACHE[cert_url] = key
        return key

    @staticmethod
    def _sns_canonical_string(payload: dict, msg_type: str) -> str | None:
        if msg_type == "Notification":
            keys = ["Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type"]
        elif msg_type in ("SubscriptionConfirmation", "UnsubscribeConfirmation"):
            keys = ["Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type"]
        else:
            return None
        parts = []
        for key in keys:
            # Subject is optional; only include it when present.
            if key == "Subject" and "Subject" not in payload:
                continue
            parts.append(f"{key}\n{payload.get(key, '')}\n")
        return "".join(parts)

    def parse_event(self, *, body: bytes) -> NormalizedEvent:
        outer = json.loads(body.decode("utf-8"))
        if outer.get("Type") != "Notification":
            raise ValueError("SES webhook: not an SNS Notification")
        inner = json.loads(outer["Message"])

        notif_type = inner.get("notificationType") or inner.get("eventType") or ""
        ev_type = _EVENT_MAP.get(notif_type, "unknown")

        mail = inner.get("mail") or {}
        provider_message_id = str(mail.get("messageId") or "")
        provider_event_id = str(outer.get("MessageId") or f"{provider_message_id}:{notif_type}")

        occurred_at = self._parse_ts(inner, notif_type, mail, outer)

        ev = NormalizedEvent(
            type=ev_type,
            occurred_at=occurred_at,
            provider_event_id=provider_event_id,
            provider_message_id=provider_message_id,
            raw=outer,
        )

        ev.recipient = self._first_recipient(inner, notif_type, mail)
        if ev_type == "bounced":
            bounce = inner.get("bounce") or {}
            recips = bounce.get("bouncedRecipients") or []
            diag = recips[0].get("diagnosticCode", "") if recips else ""
            ev.bounce_reason = (
                f"{bounce.get('bounceType', '')}/{bounce.get('bounceSubType', '')} {diag}".strip()
            )
            # bounceType: Permanent (hard) / Transient (soft) / Undetermined.
            # https://docs.aws.amazon.com/ses/latest/dg/notification-contents.html (2026-07-11)
            btype = str(bounce.get("bounceType") or "").lower()
            ev.bounce_class = {"permanent": "hard", "transient": "soft"}.get(btype, "unknown")
        elif ev_type == "clicked":
            click = inner.get("click") or {}
            ev.link_url = click.get("link", "")
            ev.user_agent = click.get("userAgent", "")
            ev.ip = click.get("ipAddress", "")
        elif ev_type == "opened":
            opened = inner.get("open") or {}
            ev.user_agent = opened.get("userAgent", "")
            ev.ip = opened.get("ipAddress", "")
        return ev

    @staticmethod
    def _parse_ts(inner: dict, notif_type: str, mail: dict, outer: dict) -> datetime:
        section = {
            "Bounce": "bounce",
            "Complaint": "complaint",
            "Delivery": "delivery",
            "DeliveryDelay": "deliveryDelay",
        }.get(notif_type)
        ts = ""
        if section:
            ts = (inner.get(section) or {}).get("timestamp", "")
        ts = ts or mail.get("timestamp", "") or outer.get("Timestamp", "")
        if ts:
            try:
                return datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                pass
        return datetime.now(UTC)

    @staticmethod
    def _first_recipient(inner: dict, notif_type: str, mail: dict) -> str:
        if notif_type == "Bounce":
            recips = (inner.get("bounce") or {}).get("bouncedRecipients") or []
            if recips:
                return recips[0].get("emailAddress", "")
        elif notif_type == "Complaint":
            recips = (inner.get("complaint") or {}).get("complainedRecipients") or []
            if recips:
                return recips[0].get("emailAddress", "")
        elif notif_type == "Delivery":
            recips = (inner.get("delivery") or {}).get("recipients") or []
            if recips:
                return recips[0]
        dest = mail.get("destination") or []
        return dest[0] if dest else ""
