from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
from pathlib import Path

import pytest
import responses
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from django.urls import reverse

from apps.connections.adapters import ses as ses_module
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


# ---------- Amazon SES (SNS) -----------------------------------------------

_SNS_CERT_URL = "https://sns.us-east-1.amazonaws.com/cert.pem"


def _ses_conn() -> Connection:
    return Connection.objects.create(
        name="ses",
        provider_code=Connection.PROVIDER_SES,
        aws_region="us-east-1",
        credentials_encrypted=encrypt(json.dumps({"access_key_id": "AKIA", "secret_access_key": "s"})),
    )


def _sns_cert_pem(key: rsa.RSAPrivateKey) -> bytes:
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


def _sign_sns(payload: dict, key: rsa.RSAPrivateKey) -> bytes:
    if payload["Type"] == "Notification":
        keys = ["Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type"]
    else:
        keys = ["Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type"]
    canonical = "".join(f"{k}\n{payload[k]}\n" for k in keys if k in payload).encode()
    sig = base64.b64encode(key.sign(canonical, padding.PKCS1v15(), hashes.SHA1())).decode()  # noqa: S303
    signed = {**payload, "Signature": sig, "SignatureVersion": "1", "SigningCertURL": _SNS_CERT_URL}
    return json.dumps(signed).encode()


@pytest.fixture(autouse=True)
def _clear_ses_cert_cache():
    ses_module._CERT_CACHE.clear()
    yield
    ses_module._CERT_CACHE.clear()


@pytest.mark.django_db
@responses.activate
def test_ses_webhook_notification_updates_status(client):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    responses.add(responses.GET, _SNS_CERT_URL, body=_sns_cert_pem(key))
    conn = _ses_conn()

    msg = Message(
        from_address="a@x.test",
        to=["recipient@example.test"],
        subject="Hello",
        provider_message_id="ses-deliv-1",
        connection=conn,
        status=Message.STATUS_SENT,
    )
    msg.set_body(text=".")
    msg.save()

    inner = {"notificationType": "Delivery", "mail": {"messageId": "ses-deliv-1", "destination": ["recipient@example.test"]}, "delivery": {"recipients": ["recipient@example.test"]}}
    body = _sign_sns(
        {"Type": "Notification", "MessageId": "sns-deliv-1", "TopicArn": "arn:aws:sns:us-east-1:1:t", "Message": json.dumps(inner), "Timestamp": "2026-06-05T00:00:00.000Z"},
        key,
    )
    r = client.post(reverse("events:ses", args=[conn.id]), data=body, content_type="application/json", HTTP_X_AMZ_SNS_MESSAGE_TYPE="Notification")
    assert r.status_code == 200, r.content
    ev = Event.objects.get()
    assert ev.type == "delivered"
    msg.refresh_from_db()
    assert msg.status == Message.STATUS_DELIVERED


@pytest.mark.django_db
@responses.activate
def test_ses_webhook_confirms_subscription(client):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    responses.add(responses.GET, _SNS_CERT_URL, body=_sns_cert_pem(key))
    subscribe_url = "https://sns.us-east-1.amazonaws.com/?Action=ConfirmSubscription&Token=abc"
    responses.add(responses.GET, subscribe_url, body="<ok/>")
    conn = _ses_conn()

    body = _sign_sns(
        {
            "Type": "SubscriptionConfirmation",
            "MessageId": "sns-confirm-1",
            "Token": "abc",
            "TopicArn": "arn:aws:sns:us-east-1:1:t",
            "Message": "You have chosen to subscribe...",
            "SubscribeURL": subscribe_url,
            "Timestamp": "2026-06-05T00:00:00.000Z",
        },
        key,
    )
    r = client.post(reverse("events:ses", args=[conn.id]), data=body, content_type="application/json", HTTP_X_AMZ_SNS_MESSAGE_TYPE="SubscriptionConfirmation")
    assert r.status_code == 200, r.content
    assert r.json().get("confirmed") is True
    assert any(call.request.url == subscribe_url for call in responses.calls)
    assert Event.objects.count() == 0


@pytest.mark.django_db
@responses.activate
def test_ses_webhook_rejects_untrusted_subscribe_url(client):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    responses.add(responses.GET, _SNS_CERT_URL, body=_sns_cert_pem(key))
    conn = _ses_conn()

    evil = "https://evil.example.com/confirm"
    body = _sign_sns(
        {
            "Type": "SubscriptionConfirmation",
            "MessageId": "sns-confirm-2",
            "Token": "abc",
            "TopicArn": "arn:aws:sns:us-east-1:1:t",
            "Message": "You have chosen to subscribe...",
            "SubscribeURL": evil,
            "Timestamp": "2026-06-05T00:00:00.000Z",
        },
        key,
    )
    r = client.post(reverse("events:ses", args=[conn.id]), data=body, content_type="application/json", HTTP_X_AMZ_SNS_MESSAGE_TYPE="SubscriptionConfirmation")
    assert r.status_code == 400


# ---------- bounce classification + suppression ----------------------------

def _ses_bounce_body(key: rsa.RSAPrivateKey, *, bounce_type: str, sns_id: str, topic="arn:aws:sns:us-east-1:1:t") -> bytes:
    inner = {
        "notificationType": "Bounce",
        "mail": {"messageId": "ses-b-1", "destination": ["victim@example.test"]},
        "bounce": {
            "bounceType": bounce_type,
            "bounceSubType": "General",
            "bouncedRecipients": [{"emailAddress": "victim@example.test", "diagnosticCode": "x"}],
            "timestamp": "2026-07-11T00:00:00.000Z",
        },
    }
    return _sign_sns(
        {"Type": "Notification", "MessageId": sns_id, "TopicArn": topic, "Message": json.dumps(inner), "Timestamp": "2026-07-11T00:00:00.000Z"},
        key,
    )


@pytest.mark.django_db
@responses.activate
def test_ses_transient_bounce_does_not_suppress(client):
    from apps.suppressions.models import Suppression

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    responses.add(responses.GET, _SNS_CERT_URL, body=_sns_cert_pem(key))
    conn = _ses_conn()

    body = _ses_bounce_body(key, bounce_type="Transient", sns_id="sns-soft-1")
    r = client.post(reverse("events:ses", args=[conn.id]), data=body, content_type="application/json", HTTP_X_AMZ_SNS_MESSAGE_TYPE="Notification")
    assert r.status_code == 200, r.content
    assert Event.objects.get().type == "bounced"
    # The whole point: a mailbox-full style bounce must NOT block the address.
    assert not Suppression.objects.filter(email="victim@example.test").exists()


@pytest.mark.django_db
@responses.activate
def test_ses_permanent_bounce_suppresses(client):
    from apps.suppressions.models import Suppression

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    responses.add(responses.GET, _SNS_CERT_URL, body=_sns_cert_pem(key))
    conn = _ses_conn()

    body = _ses_bounce_body(key, bounce_type="Permanent", sns_id="sns-hard-1")
    r = client.post(reverse("events:ses", args=[conn.id]), data=body, content_type="application/json", HTTP_X_AMZ_SNS_MESSAGE_TYPE="Notification")
    assert r.status_code == 200, r.content
    s = Suppression.objects.get(email="victim@example.test")
    assert s.reason == Suppression.REASON_HARD_BOUNCE


@pytest.mark.django_db
@responses.activate
def test_ses_undetermined_bounce_suppresses(client):
    """Unknown-class bounces keep the protective default: suppress."""
    from apps.suppressions.models import Suppression

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    responses.add(responses.GET, _SNS_CERT_URL, body=_sns_cert_pem(key))
    conn = _ses_conn()

    body = _ses_bounce_body(key, bounce_type="Undetermined", sns_id="sns-und-1")
    r = client.post(reverse("events:ses", args=[conn.id]), data=body, content_type="application/json", HTTP_X_AMZ_SNS_MESSAGE_TYPE="Notification")
    assert r.status_code == 200, r.content
    assert Suppression.objects.filter(email="victim@example.test").exists()


# ---------- SNS topic pinning -----------------------------------------------

@pytest.mark.django_db
@responses.activate
def test_ses_notification_from_wrong_topic_rejected(client):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    responses.add(responses.GET, _SNS_CERT_URL, body=_sns_cert_pem(key))
    conn = _ses_conn()
    Connection.objects.filter(pk=conn.pk).update(sns_topic_arn="arn:aws:sns:us-east-1:1:mine")
    conn.refresh_from_db()

    body = _ses_bounce_body(key, bounce_type="Permanent", sns_id="sns-evil-1", topic="arn:aws:sns:us-east-1:666:attacker")
    r = client.post(reverse("events:ses", args=[conn.id]), data=body, content_type="application/json", HTTP_X_AMZ_SNS_MESSAGE_TYPE="Notification")
    assert r.status_code == 403
    assert Event.objects.count() == 0


@pytest.mark.django_db
@responses.activate
def test_ses_first_confirmed_subscription_pins_topic(client):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    responses.add(responses.GET, _SNS_CERT_URL, body=_sns_cert_pem(key))
    subscribe_url = "https://sns.us-east-1.amazonaws.com/?Action=ConfirmSubscription&Token=abc"
    responses.add(responses.GET, subscribe_url, body="<ok/>")
    conn = _ses_conn()
    assert conn.sns_topic_arn == ""

    body = _sign_sns(
        {
            "Type": "SubscriptionConfirmation",
            "MessageId": "sns-pin-1",
            "Token": "abc",
            "TopicArn": "arn:aws:sns:us-east-1:1:mine",
            "Message": "You have chosen to subscribe...",
            "SubscribeURL": subscribe_url,
            "Timestamp": "2026-07-11T00:00:00.000Z",
        },
        key,
    )
    r = client.post(reverse("events:ses", args=[conn.id]), data=body, content_type="application/json", HTTP_X_AMZ_SNS_MESSAGE_TYPE="SubscriptionConfirmation")
    assert r.status_code == 200, r.content
    conn.refresh_from_db()
    assert conn.sns_topic_arn == "arn:aws:sns:us-east-1:1:mine"

    # A second subscription attempt from a different topic is now rejected.
    evil = _sign_sns(
        {
            "Type": "SubscriptionConfirmation",
            "MessageId": "sns-pin-2",
            "Token": "abc",
            "TopicArn": "arn:aws:sns:us-east-1:666:attacker",
            "Message": "You have chosen to subscribe...",
            "SubscribeURL": subscribe_url,
            "Timestamp": "2026-07-11T00:00:00.000Z",
        },
        key,
    )
    r2 = client.post(reverse("events:ses", args=[conn.id]), data=evil, content_type="application/json", HTTP_X_AMZ_SNS_MESSAGE_TYPE="SubscriptionConfirmation")
    assert r2.status_code == 403
