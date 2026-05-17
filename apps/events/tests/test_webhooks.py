from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from django.urls import reverse

from apps.connections.models import Connection
from apps.core.encryption import encrypt
from apps.events.models import Event
from apps.messages_api.models import Message

FIXTURES = Path(__file__).resolve().parents[3] / "docs" / "providers"


# ---------- helpers --------------------------------------------------------

def _gen_postal_keypair() -> tuple[str, rsa.RSAPrivateKey]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return pub_pem, key


def _postal_conn(pub_pem: str) -> Connection:
    return Connection.objects.create(
        name="postal",
        provider_code=Connection.PROVIDER_POSTAL,
        base_url="https://postal.example.test",
        credentials_encrypted=encrypt("srv-test"),
        webhook_public_key_pem=pub_pem,
    )


def _resend_conn(secret: str) -> Connection:
    return Connection.objects.create(
        name="resend",
        provider_code=Connection.PROVIDER_RESEND,
        credentials_encrypted=encrypt("re_test"),
        webhook_secret_encrypted=encrypt(secret),
    )


def _resend_sig_headers(secret: str, body: bytes, *, svix_id: str = "msg_evt_1", svix_ts: str = "1747500000") -> dict:
    key = base64.b64decode(secret.removeprefix("whsec_"))
    signed = f"{svix_id}.{svix_ts}.".encode() + body
    sig = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    return {
        "HTTP_SVIX_ID": svix_id,
        "HTTP_SVIX_TIMESTAMP": svix_ts,
        "HTTP_SVIX_SIGNATURE": f"v1,{sig}",
    }


# ---------- Postal ---------------------------------------------------------

@pytest.mark.django_db
def test_postal_webhook_rejects_bad_signature(client):
    pub_pem, _priv = _gen_postal_keypair()
    conn = _postal_conn(pub_pem)
    raw = (FIXTURES / "postal-payload-fixtures" / "message_sent.json").read_bytes()
    r = client.post(
        reverse("events:postal", args=[conn.id]),
        data=raw,
        content_type="application/json",
        HTTP_X_POSTAL_SIGNATURE_256="bm90LWEtcmVhbC1zaWc=",
    )
    assert r.status_code == 401
    assert Event.objects.count() == 0


@pytest.mark.django_db
def test_postal_webhook_accepts_and_updates_status(client):
    pub_pem, priv = _gen_postal_keypair()
    conn = _postal_conn(pub_pem)

    msg = Message(
        from_address="sender@example.test",
        to=["recipient@example.test"],
        subject="Hello",
        provider_message_id="<msg-100001@postal.example.test>",
        recipient_tokens={"recipient@example.test": "tok-msg-sent-01"},
        connection=conn,
        status=Message.STATUS_SENT,
    )
    msg.set_body(text="hi")
    msg.save()

    raw = (FIXTURES / "postal-payload-fixtures" / "message_sent.json").read_bytes()
    sig = base64.b64encode(priv.sign(raw, padding.PKCS1v15(), hashes.SHA256())).decode()

    r = client.post(
        reverse("events:postal", args=[conn.id]),
        data=raw,
        content_type="application/json",
        HTTP_X_POSTAL_SIGNATURE_256=sig,
    )
    assert r.status_code == 200, r.content
    ev = Event.objects.get()
    assert ev.type == "delivered"
    assert ev.message_id == msg.id
    msg.refresh_from_db()
    assert msg.status == Message.STATUS_DELIVERED


@pytest.mark.django_db
def test_postal_webhook_dedup_on_provider_event_id(client):
    pub_pem, priv = _gen_postal_keypair()
    conn = _postal_conn(pub_pem)
    raw = (FIXTURES / "postal-payload-fixtures" / "message_link_clicked.json").read_bytes()
    sig = base64.b64encode(priv.sign(raw, padding.PKCS1v15(), hashes.SHA256())).decode()

    def post():
        return client.post(
            reverse("events:postal", args=[conn.id]),
            data=raw,
            content_type="application/json",
            HTTP_X_POSTAL_SIGNATURE_256=sig,
        )

    r1 = post()
    r2 = post()
    assert r1.status_code == 200 and r2.status_code == 200
    assert r2.json().get("deduped") is True
    assert Event.objects.count() == 1


@pytest.mark.django_db
def test_postal_webhook_matches_by_recipient_token(client):
    pub_pem, priv = _gen_postal_keypair()
    conn = _postal_conn(pub_pem)
    # Message has only a token, not a provider_message_id matching the payload.
    msg = Message(
        from_address="sender@example.test",
        to=["recipient@example.test"],
        subject="Hello",
        recipient_tokens={"recipient@example.test": "tok-msg-sent-01"},
        connection=conn,
        status=Message.STATUS_SENT,
    )
    msg.set_body(text="hi")
    msg.save()

    raw = (FIXTURES / "postal-payload-fixtures" / "message_bounced.json").read_bytes()
    sig = base64.b64encode(priv.sign(raw, padding.PKCS1v15(), hashes.SHA256())).decode()
    r = client.post(
        reverse("events:postal", args=[conn.id]),
        data=raw,
        content_type="application/json",
        HTTP_X_POSTAL_SIGNATURE_256=sig,
    )
    assert r.status_code == 200, r.content
    msg.refresh_from_db()
    assert msg.status == Message.STATUS_BOUNCED


# ---------- Resend ---------------------------------------------------------

@pytest.mark.django_db
def test_resend_webhook_happy_path(client):
    secret = "whsec_" + base64.b64encode(b"a-very-secret-key").decode()
    conn = _resend_conn(secret)

    msg = Message(
        from_address="acme@a.test",
        to=["recipient@example.test"],
        subject="Hello",
        provider_message_id="4ef9a417-0000-0000-0000-resend-deliv01",
        connection=conn,
        status=Message.STATUS_SENT,
    )
    msg.set_body(text=".")
    msg.save()

    raw = (FIXTURES / "resend-payload-fixtures" / "email_delivered.json").read_bytes()
    headers = _resend_sig_headers(secret, raw, svix_id="msg_evt_delivered")
    r = client.post(
        reverse("events:resend", args=[conn.id]),
        data=raw,
        content_type="application/json",
        **headers,
    )
    assert r.status_code == 200, r.content
    ev = Event.objects.get()
    assert ev.type == "delivered"
    assert ev.provider_event_id == "msg_evt_delivered"  # svix-id takes precedence
    msg.refresh_from_db()
    assert msg.status == Message.STATUS_DELIVERED


@pytest.mark.django_db
def test_resend_webhook_rejects_tampered_body(client):
    secret = "whsec_" + base64.b64encode(b"a-very-secret-key").decode()
    conn = _resend_conn(secret)
    raw = (FIXTURES / "resend-payload-fixtures" / "email_delivered.json").read_bytes()
    headers = _resend_sig_headers(secret, raw)
    # Tamper with body but keep signature.
    r = client.post(
        reverse("events:resend", args=[conn.id]),
        data=raw + b" ",
        content_type="application/json",
        **headers,
    )
    assert r.status_code == 401
    assert Event.objects.count() == 0


@pytest.mark.django_db
def test_resend_webhook_bounce_parses_reason(client):
    secret = "whsec_" + base64.b64encode(b"a-very-secret-key").decode()
    conn = _resend_conn(secret)
    raw = (FIXTURES / "resend-payload-fixtures" / "email_bounced.json").read_bytes()
    headers = _resend_sig_headers(secret, raw, svix_id="msg_evt_bnc")
    r = client.post(
        reverse("events:resend", args=[conn.id]),
        data=raw,
        content_type="application/json",
        **headers,
    )
    assert r.status_code == 200, r.content
    ev = Event.objects.get()
    assert ev.type == "bounced"
    assert "does not exist" in ev.bounce_reason


@pytest.mark.django_db
def test_resend_webhook_dedupes_on_svix_id(client):
    secret = "whsec_" + base64.b64encode(b"a-very-secret-key").decode()
    conn = _resend_conn(secret)
    raw = (FIXTURES / "resend-payload-fixtures" / "email_clicked.json").read_bytes()
    headers = _resend_sig_headers(secret, raw, svix_id="msg_evt_clk")
    r1 = client.post(reverse("events:resend", args=[conn.id]), data=raw, content_type="application/json", **headers)
    r2 = client.post(reverse("events:resend", args=[conn.id]), data=raw, content_type="application/json", **headers)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r2.json().get("deduped") is True
    assert Event.objects.count() == 1


@pytest.mark.django_db
def test_terminal_status_precedence(client):
    """A delivered message that later opens/clicks stays delivered."""
    secret = "whsec_" + base64.b64encode(b"a-very-secret-key").decode()
    conn = _resend_conn(secret)
    msg = Message(
        from_address="a@x.test",
        to=["recipient@example.test"],
        subject="Hello",
        provider_message_id="4ef9a417-0000-0000-0000-resend-clk-01",
        connection=conn,
        status=Message.STATUS_DELIVERED,
    )
    msg.set_body(text=".")
    msg.save()

    raw = (FIXTURES / "resend-payload-fixtures" / "email_clicked.json").read_bytes()
    headers = _resend_sig_headers(secret, raw, svix_id="msg_evt_clk2")
    r = client.post(reverse("events:resend", args=[conn.id]), data=raw, content_type="application/json", **headers)
    assert r.status_code == 200
    msg.refresh_from_db()
    # Clicked is non-terminal — must not regress status.
    assert msg.status == Message.STATUS_DELIVERED


@pytest.mark.django_db
def test_webhook_404_for_wrong_provider(client):
    """A connection registered as Resend can't receive Postal webhooks."""
    secret = "whsec_" + base64.b64encode(b"a-very-secret-key").decode()
    conn = _resend_conn(secret)
    raw = json.dumps({"event": "MessageSent"}).encode()
    r = client.post(reverse("events:postal", args=[conn.id]), data=raw, content_type="application/json")
    assert r.status_code == 404
