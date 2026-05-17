from __future__ import annotations

import pytest
from django.utils import timezone

from apps.messages_api.models import Message


@pytest.mark.django_db
def test_message_body_roundtrips_through_fernet():
    msg = Message(from_address="a@x.test", to=["b@y.test"], subject="hi")
    msg.set_body(html="<p>hi</p>", text="hi")
    msg.save()
    msg.refresh_from_db()
    assert msg.body() == {"html": "<p>hi</p>", "text": "hi"}
    # Ciphertext is not the plaintext.
    assert b"<p>hi</p>" not in bytes(msg.body_encrypted)


@pytest.mark.django_db
def test_message_retention_set():
    msg = Message(from_address="a@x.test", to=["b@y.test"], subject="hi")
    msg.set_body(text=".")
    msg.set_retention(30)
    msg.save()
    assert msg.retention_expires_at is not None
    assert msg.retention_expires_at > timezone.now()


@pytest.mark.django_db
def test_message_ulid_id_assigned():
    msg = Message(from_address="a@x.test", to=["b@y.test"], subject="hi")
    msg.set_body(text=".")
    msg.save()
    assert isinstance(msg.id, str)
    assert len(msg.id) == 26  # Crockford-base32 ULID length
