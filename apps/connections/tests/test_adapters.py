from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone

import pytest
import responses
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives import hashes

from apps.connections.adapters import AdapterError, PostalAdapter, ResendAdapter
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
    assert ev.occurred_at == datetime.fromtimestamp(1747500000.5, tz=timezone.utc)


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
