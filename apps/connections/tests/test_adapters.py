from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
from datetime import datetime

import pytest
import responses
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from apps.connections.adapters import AdapterError, PostalAdapter, ResendAdapter, SesAdapter
from apps.connections.adapters import ses as ses_module
from apps.connections.models import Connection
from apps.core.encryption import encrypt

# ---------- Resend ---------------------------------------------------------

def _resend_conn(*, api_key="re_test123", webhook_secret="whsec_dGVzdHNlY3JldA==") -> Connection:
    return Connection(
        name="r",
        provider_code="resend",
        credentials_encrypted=encrypt(api_key),
        webhook_secret_encrypted=encrypt(webhook_secret),
    )


@pytest.mark.django_db
@responses.activate
def test_resend_send_success():
    conn = _resend_conn()
    responses.add(
        responses.POST,
        "https://api.resend.com/emails",
        json={"id": "abc-123"},
        status=200,
    )
    result = ResendAdapter(conn).send(
        message={"from": "a@x", "to": ["b@y"], "subject": "hi", "html": "<p>hi</p>"}
    )
    assert result.provider_message_id == "abc-123"
    # Verify Idempotency-Key header is forwarded.
    responses.replace(
        responses.POST, "https://api.resend.com/emails", json={"id": "abc-123"}, status=200
    )
    ResendAdapter(conn).send(
        message={"from": "a@x", "to": ["b@y"], "subject": "hi", "html": "<p>hi</p>", "idempotency_key": "k-1"}
    )
    assert "Idempotency-Key" in responses.calls[-1].request.headers


@pytest.mark.django_db
@responses.activate
def test_resend_send_5xx_is_temporary():
    conn = _resend_conn()
    responses.add(responses.POST, "https://api.resend.com/emails", json={"message": "boom"}, status=503)
    with pytest.raises(AdapterError) as exc:
        ResendAdapter(conn).send(message={"from": "a@x", "to": ["b@y"], "subject": "hi", "text": "."})
    assert exc.value.kind == "temporary"


@pytest.mark.django_db
@responses.activate
def test_resend_send_400_is_permanent():
    conn = _resend_conn()
    responses.add(
        responses.POST,
        "https://api.resend.com/emails",
        json={"name": "validation_error", "message": "bad"},
        status=422,
    )
    with pytest.raises(AdapterError) as exc:
        ResendAdapter(conn).send(message={"from": "a@x", "to": ["b@y"], "subject": "hi"})
    assert exc.value.kind == "permanent"


@pytest.mark.django_db
def test_resend_webhook_signature_roundtrip():
    secret = "whsec_" + base64.b64encode(b"verysecret-key-bytes").decode()
    conn = _resend_conn(webhook_secret=secret)
    raw = b'{"type":"email.delivered","created_at":"2026-05-17T12:00:00.000Z","data":{"email_id":"abc","to":["x@y"]}}'
    svix_id = "msg_1"
    svix_ts = "1747500000"
    key_bytes = base64.b64decode(secret.removeprefix("whsec_"))
    signed = f"{svix_id}.{svix_ts}.".encode() + raw
    sig = base64.b64encode(hmac.new(key_bytes, signed, hashlib.sha256).digest()).decode()

    headers = {"svix-id": svix_id, "svix-timestamp": svix_ts, "svix-signature": f"v1,{sig}"}
    assert ResendAdapter(conn).verify_webhook(headers=headers, body=raw) is True

    # Tampered body fails.
    assert ResendAdapter(conn).verify_webhook(headers=headers, body=raw + b" ") is False


@pytest.mark.django_db
def test_resend_parse_event_normalizes_bounce():
    conn = _resend_conn()
    raw = json.dumps(
        {
            "type": "email.bounced",
            "created_at": "2026-05-17T12:00:00.000Z",
            "data": {
                "email_id": "abc",
                "to": ["bouncer@x.test"],
                "bounce": {"type": "hard", "subType": "general", "message": "mailbox unavailable"},
            },
        }
    ).encode()
    ev = ResendAdapter(conn).parse_event(body=raw)
    assert ev.type == "bounced"
    assert ev.provider_message_id == "abc"
    assert ev.recipient == "bouncer@x.test"
    assert ev.bounce_reason == "mailbox unavailable"


# ---------- Postal ---------------------------------------------------------

def _gen_postal_keypair() -> tuple[str, rsa.RSAPrivateKey]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return pub_pem, key


def _postal_conn(*, public_pem: str = "", api_key: str = "srv-test") -> Connection:
    return Connection(
        name="p",
        provider_code="postal",
        base_url="https://postal.example.com",
        credentials_encrypted=encrypt(api_key),
        webhook_public_key_pem=public_pem,
    )


@pytest.mark.django_db
@responses.activate
def test_postal_send_success():
    conn = _postal_conn()
    responses.add(
        responses.POST,
        "https://postal.example.com/api/v1/send/message",
        json={
            "status": "success",
            "time": 0.02,
            "data": {
                "message_id": "msg-uuid",
                "messages": {"a@x": {"id": 1, "token": "tok1"}, "b@x": {"id": 2, "token": "tok2"}},
            },
        },
        status=200,
    )
    result = PostalAdapter(conn).send(
        message={"from": "a@x", "to": ["a@x", "b@x"], "subject": "hi", "text": "."}
    )
    assert result.provider_message_id == "msg-uuid"
    assert result.recipient_tokens == {"a@x": "tok1", "b@x": "tok2"}


@pytest.mark.django_db
@responses.activate
def test_postal_send_error_envelope_is_permanent():
    conn = _postal_conn()
    responses.add(
        responses.POST,
        "https://postal.example.com/api/v1/send/message",
        json={"status": "error", "data": {"code": "ValidationError", "message": "Bad to"}},
        status=200,
    )
    with pytest.raises(AdapterError) as exc:
        PostalAdapter(conn).send(message={"from": "a@x", "to": ["b@x"], "subject": "hi", "text": "."})
    assert exc.value.kind == "permanent"


@pytest.mark.django_db
def test_postal_webhook_sha256_signature():
    pub_pem, priv = _gen_postal_keypair()
    conn = _postal_conn(public_pem=pub_pem)
    raw = b'{"event":"MessageSent","timestamp":1747500000.0,"uuid":"u-1","payload":{"message":{"id":1,"token":"tok1","to":"a@x"}}}'
    sig = base64.b64encode(priv.sign(raw, padding.PKCS1v15(), hashes.SHA256())).decode()
    assert PostalAdapter(conn).verify_webhook(headers={"x-postal-signature-256": sig}, body=raw)


@pytest.mark.django_db
def test_postal_webhook_sha1_legacy_signature():
    pub_pem, priv = _gen_postal_keypair()
    conn = _postal_conn(public_pem=pub_pem)
    raw = b'{"event":"MessageSent","timestamp":1747500000.0,"uuid":"u-1","payload":{"message":{"id":1,"token":"tok1","to":"a@x"}}}'
    sig = base64.b64encode(priv.sign(raw, padding.PKCS1v15(), hashes.SHA1())).decode()  # noqa: S303
    assert PostalAdapter(conn).verify_webhook(headers={"x-postal-signature": sig}, body=raw)


@pytest.mark.django_db
def test_postal_webhook_invalid_signature_rejected():
    pub_pem, priv = _gen_postal_keypair()
    conn = _postal_conn(public_pem=pub_pem)
    raw = b'{"event":"MessageSent"}'
    bad_sig = base64.b64encode(priv.sign(b"different body", padding.PKCS1v15(), hashes.SHA256())).decode()
    assert PostalAdapter(conn).verify_webhook(headers={"x-postal-signature-256": bad_sig}, body=raw) is False


@pytest.mark.django_db
def test_postal_parse_event_message_sent():
    conn = _postal_conn()
    raw = json.dumps(
        {
            "event": "MessageSent",
            "timestamp": 1747500000.5,
            "uuid": "u-1",
            "payload": {"message": {"id": 1, "token": "tok1", "to": "rcpt@x.test", "message_id": "mid-1"}},
        }
    ).encode()
    ev = PostalAdapter(conn).parse_event(body=raw)
    assert ev.type == "delivered"
    assert ev.recipient_token == "tok1"
    assert ev.recipient == "rcpt@x.test"
    assert ev.occurred_at == datetime.fromtimestamp(1747500000.5, tz=dt.UTC)


@pytest.mark.django_db
def test_postal_parse_event_bounced():
    conn = _postal_conn()
    raw = json.dumps(
        {
            "event": "MessageBounced",
            "timestamp": 1747500000.0,
            "uuid": "u-2",
            "payload": {
                "message": {"id": 9, "token": "tok9", "to": "bounced@x.test"},
                "details": "554 5.1.1 user unknown",
            },
        }
    ).encode()
    ev = PostalAdapter(conn).parse_event(body=raw)
    assert ev.type == "bounced"
    assert "user unknown" in ev.bounce_reason


# ---------- Amazon SES -----------------------------------------------------

_SES_SEND_URL = "https://email.us-east-1.amazonaws.com/v2/email/outbound-emails"


def _ses_conn(*, region="us-east-1", akid="AKIA_TEST", secret="secret123", config_set="") -> Connection:
    return Connection(
        name="s",
        provider_code="ses",
        aws_region=region,
        ses_configuration_set=config_set,
        credentials_encrypted=encrypt(json.dumps({"access_key_id": akid, "secret_access_key": secret})),
    )


def _self_signed_cert_pem(key: rsa.RSAPrivateKey) -> bytes:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sns.amazonaws.com")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(dt.datetime(2020, 1, 1))
        .not_valid_after(dt.datetime(2035, 1, 1))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


def _sns_canonical(payload: dict) -> bytes:
    keys = ["Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type"]
    parts = []
    for k in keys:
        if k == "Subject" and "Subject" not in payload:
            continue
        parts.append(f"{k}\n{payload.get(k, '')}\n")
    return "".join(parts).encode()


def _sign_sns(payload: dict, key: rsa.RSAPrivateKey, *, version: str = "1") -> dict:
    algo = hashes.SHA1() if version == "1" else hashes.SHA256()  # noqa: S303 - SNS v1 uses SHA1
    sig = base64.b64encode(key.sign(_sns_canonical(payload), padding.PKCS1v15(), algo)).decode()
    payload = {**payload, "Signature": sig, "SignatureVersion": version}
    return payload


@pytest.fixture(autouse=True)
def _clear_cert_cache():
    ses_module._CERT_CACHE.clear()
    yield
    ses_module._CERT_CACHE.clear()


@pytest.mark.django_db
@responses.activate
def test_ses_send_success():
    conn = _ses_conn(config_set="cs-1")
    responses.add(responses.POST, _SES_SEND_URL, json={"MessageId": "ses-msg-1"}, status=200)
    result = SesAdapter(conn).send(
        message={"from": "a@x", "to": ["b@y"], "subject": "hi", "html": "<p>hi</p>"}
    )
    assert result.provider_message_id == "ses-msg-1"
    sent = json.loads(responses.calls[-1].request.body)
    assert sent["FromEmailAddress"] == "a@x"
    assert sent["Destination"]["ToAddresses"] == ["b@y"]
    assert sent["Content"]["Simple"]["Subject"]["Data"] == "hi"
    assert sent["ConfigurationSetName"] == "cs-1"


@pytest.mark.django_db
@responses.activate
def test_ses_sigv4_headers_present():
    conn = _ses_conn()
    responses.add(responses.POST, _SES_SEND_URL, json={"MessageId": "m"}, status=200)
    SesAdapter(conn).send(message={"from": "a@x", "to": ["b@y"], "subject": "hi", "text": "."})
    headers = responses.calls[-1].request.headers
    auth = headers["Authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKIA_TEST/")
    assert "/us-east-1/ses/aws4_request" in auth
    assert "SignedHeaders=host;x-amz-content-sha256;x-amz-date" in auth
    assert "Signature=" in auth
    assert "X-Amz-Date" in headers
    assert "X-Amz-Content-Sha256" in headers


@pytest.mark.django_db
@responses.activate
def test_ses_send_400_is_permanent():
    conn = _ses_conn()
    responses.add(
        responses.POST,
        _SES_SEND_URL,
        json={"__type": "MessageRejected", "message": "Email address is not verified"},
        status=400,
    )
    with pytest.raises(AdapterError) as exc:
        SesAdapter(conn).send(message={"from": "a@x", "to": ["b@y"], "subject": "hi", "text": "."})
    assert exc.value.kind == "permanent"


@pytest.mark.django_db
@responses.activate
def test_ses_send_throttling_400_is_temporary():
    conn = _ses_conn()
    responses.add(
        responses.POST,
        _SES_SEND_URL,
        json={"__type": "ThrottlingException", "message": "Rate exceeded"},
        status=400,
    )
    with pytest.raises(AdapterError) as exc:
        SesAdapter(conn).send(message={"from": "a@x", "to": ["b@y"], "subject": "hi", "text": "."})
    assert exc.value.kind == "temporary"


@pytest.mark.django_db
@responses.activate
def test_ses_send_429_is_temporary():
    conn = _ses_conn()
    responses.add(responses.POST, _SES_SEND_URL, json={"message": "slow down"}, status=429)
    with pytest.raises(AdapterError) as exc:
        SesAdapter(conn).send(message={"from": "a@x", "to": ["b@y"], "subject": "hi", "text": "."})
    assert exc.value.kind == "temporary"


@pytest.mark.django_db
@responses.activate
def test_ses_send_5xx_is_temporary():
    conn = _ses_conn()
    responses.add(responses.POST, _SES_SEND_URL, json={"message": "boom"}, status=503)
    with pytest.raises(AdapterError) as exc:
        SesAdapter(conn).send(message={"from": "a@x", "to": ["b@y"], "subject": "hi", "text": "."})
    assert exc.value.kind == "temporary"


def _ses_notification(inner: dict, *, msg_id="sns-1", topic="arn:aws:sns:us-east-1:1:t") -> dict:
    return {
        "Type": "Notification",
        "MessageId": msg_id,
        "TopicArn": topic,
        "Message": json.dumps(inner),
        "Timestamp": "2026-06-05T00:00:00.000Z",
        "SigningCertURL": "https://sns.us-east-1.amazonaws.com/cert.pem",
    }


@pytest.mark.django_db
@responses.activate
@pytest.mark.parametrize("version", ["1", "2"])
def test_ses_sns_signature_roundtrip(version):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    responses.add(responses.GET, "https://sns.us-east-1.amazonaws.com/cert.pem", body=_self_signed_cert_pem(key))
    conn = _ses_conn()
    payload = _sign_sns(_ses_notification({"notificationType": "Delivery", "mail": {"messageId": "m1"}}), key, version=version)
    raw = json.dumps(payload).encode()
    assert SesAdapter(conn).verify_webhook(headers={}, body=raw) is True

    # Tampered Message fails.
    tampered = json.loads(raw)
    tampered["Message"] = tampered["Message"] + " "
    assert SesAdapter(conn).verify_webhook(headers={}, body=json.dumps(tampered).encode()) is False


@pytest.mark.django_db
def test_ses_sns_rejects_untrusted_cert_host():
    conn = _ses_conn()
    payload = _ses_notification({"notificationType": "Delivery", "mail": {"messageId": "m1"}})
    payload["SigningCertURL"] = "https://evil.example.com/cert.pem"
    payload["Signature"] = base64.b64encode(b"x").decode()
    payload["SignatureVersion"] = "1"
    assert SesAdapter(conn).verify_webhook(headers={}, body=json.dumps(payload).encode()) is False


@pytest.mark.django_db
def test_ses_parse_bounce():
    conn = _ses_conn()
    inner = {
        "notificationType": "Bounce",
        "mail": {"messageId": "ses-msg-1", "destination": ["b@x.test"], "timestamp": "2026-06-05T00:00:00.000Z"},
        "bounce": {
            "bounceType": "Permanent",
            "bounceSubType": "General",
            "timestamp": "2026-06-05T00:00:01.000Z",
            "bouncedRecipients": [{"emailAddress": "b@x.test", "diagnosticCode": "smtp; 550 user unknown"}],
        },
    }
    raw = json.dumps(_ses_notification(inner, msg_id="sns-bounce-1")).encode()
    ev = SesAdapter(conn).parse_event(body=raw)
    assert ev.type == "bounced"
    assert ev.provider_message_id == "ses-msg-1"
    assert ev.provider_event_id == "sns-bounce-1"
    assert ev.recipient == "b@x.test"
    assert "550 user unknown" in ev.bounce_reason


@pytest.mark.django_db
def test_ses_parse_complaint():
    conn = _ses_conn()
    inner = {
        "notificationType": "Complaint",
        "mail": {"messageId": "ses-msg-2", "destination": ["c@x.test"]},
        "complaint": {"complainedRecipients": [{"emailAddress": "c@x.test"}], "timestamp": "2026-06-05T00:00:00.000Z"},
    }
    ev = SesAdapter(conn).parse_event(body=json.dumps(_ses_notification(inner)).encode())
    assert ev.type == "complained"
    assert ev.recipient == "c@x.test"
    assert ev.provider_message_id == "ses-msg-2"


@pytest.mark.django_db
def test_ses_parse_delivery():
    conn = _ses_conn()
    inner = {
        "notificationType": "Delivery",
        "mail": {"messageId": "ses-msg-3", "destination": ["d@x.test"]},
        "delivery": {"recipients": ["d@x.test"], "timestamp": "2026-06-05T00:00:00.000Z"},
    }
    ev = SesAdapter(conn).parse_event(body=json.dumps(_ses_notification(inner)).encode())
    assert ev.type == "delivered"
    assert ev.recipient == "d@x.test"
    assert ev.provider_message_id == "ses-msg-3"


@pytest.mark.django_db
def test_ses_parse_eventtype_key():
    # Configuration-set event destinations use `eventType` instead of `notificationType`.
    conn = _ses_conn()
    inner = {
        "eventType": "Open",
        "mail": {"messageId": "ses-msg-4", "destination": ["o@x.test"]},
        "open": {"userAgent": "Mozilla", "ipAddress": "1.2.3.4", "timestamp": "2026-06-05T00:00:00.000Z"},
    }
    ev = SesAdapter(conn).parse_event(body=json.dumps(_ses_notification(inner)).encode())
    assert ev.type == "opened"
    assert ev.user_agent == "Mozilla"
    assert ev.ip == "1.2.3.4"
