from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.api_keys.models import APIKey
from apps.messages_api.models import IdempotencyRecord, Message


def _post(client, url, body, *, bearer="", idem=""):
    headers = {"HTTP_AUTHORIZATION": bearer} if bearer else {}
    if idem:
        headers["HTTP_IDEMPOTENCY_KEY"] = idem
    return client.post(
        url, data=json.dumps(body), content_type="application/json", **headers
    )


@pytest.mark.django_db
def test_emails_create_requires_bearer(client):
    r = _post(client, reverse("messages_api:emails_create"), {"from": "a@x", "to": ["b@y"]})
    assert r.status_code == 401
    assert r.json()["name"] == "missing_api_key"


@pytest.mark.django_db
def test_emails_create_rejects_revoked_key(client, api_key, bearer):
    from django.utils import timezone

    api_key.revoked_at = timezone.now()
    api_key.save(update_fields=["revoked_at"])
    r = _post(client, reverse("messages_api:emails_create"),
              {"from": "a@x.test", "to": ["b@y.test"], "subject": "x", "text": "."},
              bearer=bearer)
    assert r.status_code == 401


@pytest.mark.django_db
def test_emails_create_requires_scope(client, pool):
    key, secret = APIKey.issue(name="read-only", scopes=["read"], default_pool=pool)
    r = _post(client, reverse("messages_api:emails_create"),
              {"from": "a@x.test", "to": ["b@y.test"], "subject": "x", "text": "."},
              bearer=f"Bearer {secret}")
    assert r.status_code == 403
    assert r.json()["name"] == "insufficient_scope"


@pytest.mark.django_db
def test_emails_create_persists_message_and_enqueues(client, bearer, api_key):
    with patch("apps.sending.tasks.enqueue_message") as enqueue:
        r = _post(client, reverse("messages_api:emails_create"),
                  {"from": "Acme <onb@acme.test>", "to": ["dest@y.test"],
                   "subject": "hello", "html": "<p>hi</p>"},
                  bearer=bearer)
    assert r.status_code == 200, r.content
    body = r.json()
    assert "id" in body
    msg = Message.objects.get(pk=body["id"])
    assert msg.status == Message.STATUS_QUEUED
    assert msg.from_address == "Acme <onb@acme.test>"
    assert msg.to == ["dest@y.test"]
    assert msg.body() == {"html": "<p>hi</p>", "text": ""}
    assert msg.api_key_id == api_key.pk
    assert msg.pool_id == api_key.default_pool_id
    enqueue.assert_called_once()


@pytest.mark.django_db
def test_emails_create_returns_422_on_validation_error(client, bearer):
    r = _post(client, reverse("messages_api:emails_create"),
              {"from": "a@x.test", "to": ["b@y.test"], "subject": "x"},  # no body
              bearer=bearer)
    assert r.status_code == 422
    payload = r.json()
    assert payload["name"] == "validation_error"
    assert "html" in payload["message"] or "text" in payload["message"]


@pytest.mark.django_db
def test_emails_create_400_when_key_has_no_pool(client):
    key, secret = APIKey.issue(name="orphan", scopes=["*"])  # no default_pool
    r = _post(client, reverse("messages_api:emails_create"),
              {"from": "a@x.test", "to": ["b@y.test"], "subject": "x", "text": "."},
              bearer=f"Bearer {secret}")
    assert r.status_code == 400
    assert r.json()["name"] == "no_pool_assigned"


@pytest.mark.django_db
def test_idempotency_replay_returns_same_id(client, bearer):
    body = {"from": "a@x.test", "to": ["b@y.test"], "subject": "hi", "text": "."}
    with patch("apps.sending.tasks.enqueue_message"):
        r1 = _post(client, reverse("messages_api:emails_create"), body, bearer=bearer, idem="key-1")
        r2 = _post(client, reverse("messages_api:emails_create"), body, bearer=bearer, idem="key-1")
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["id"] == r2.json()["id"]
    assert Message.objects.count() == 1
    assert IdempotencyRecord.objects.count() == 1


@pytest.mark.django_db
def test_idempotency_conflict_on_different_body(client, bearer):
    body = {"from": "a@x.test", "to": ["b@y.test"], "subject": "hi", "text": "."}
    with patch("apps.sending.tasks.enqueue_message"):
        r1 = _post(client, reverse("messages_api:emails_create"), body, bearer=bearer, idem="key-2")
        assert r1.status_code == 200
        # Change the subject — same idempotency key.
        body2 = {**body, "subject": "different"}
        r2 = _post(client, reverse("messages_api:emails_create"), body2, bearer=bearer, idem="key-2")
    assert r2.status_code == 409
    assert r2.json()["name"] == "idempotency_conflict"
    assert Message.objects.count() == 1


@pytest.mark.django_db
def test_emails_retrieve_returns_resend_shape(client, bearer):
    with patch("apps.sending.tasks.enqueue_message"):
        r = _post(client, reverse("messages_api:emails_create"),
                  {"from": "a@x.test", "to": ["b@y.test"], "subject": "hi", "html": "<p>hi</p>"},
                  bearer=bearer)
    msg_id = r.json()["id"]
    g = client.get(
        reverse("messages_api:emails_retrieve", args=[msg_id]),
        HTTP_AUTHORIZATION=bearer,
    )
    assert g.status_code == 200
    body = g.json()
    assert body["id"] == msg_id
    assert body["object"] == "email"
    assert body["to"] == ["b@y.test"]
    assert body["html"] == "<p>hi</p>"


@pytest.mark.django_db
def test_emails_retrieve_not_found(client, bearer):
    g = client.get(
        reverse("messages_api:emails_retrieve", args=["01000000000000000000000000"]),
        HTTP_AUTHORIZATION=bearer,
    )
    assert g.status_code == 404


@pytest.mark.django_db
def test_api_endpoints_are_csrf_exempt(client, bearer):
    """The whole point of an API: never enforce session CSRF on Bearer requests."""
    enforce_client = client.__class__(enforce_csrf_checks=True)
    with patch("apps.sending.tasks.enqueue_message"):
        r = _post(enforce_client, reverse("messages_api:emails_create"),
                  {"from": "a@x.test", "to": ["b@y.test"], "subject": "x", "text": "."},
                  bearer=bearer)
    assert r.status_code == 200
