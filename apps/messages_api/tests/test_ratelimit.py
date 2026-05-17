from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse


def _post(client, bearer, body):
    return client.post(
        reverse("messages_api:emails_create"),
        data=json.dumps(body),
        content_type="application/json",
        HTTP_AUTHORIZATION=bearer,
    )


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.mark.django_db
@override_settings(API_RATE_LIMIT_PER_SECOND=3, API_RATE_LIMIT_PER_MINUTE=1000)
def test_rate_limit_returns_429_with_retry_after(client, bearer):
    body = {"from": "a@x.test", "to": ["b@y.test"], "subject": "hi", "text": "."}
    with patch("apps.sending.tasks.enqueue_message"):
        for _ in range(3):
            r = _post(client, bearer, body)
            assert r.status_code == 200, r.content
        r = _post(client, bearer, body)
    assert r.status_code == 429
    payload = r.json()
    assert payload["name"] == "rate_limit_exceeded"
    assert int(r["Retry-After"]) >= 1
    assert r["RateLimit-Limit"] == "3"


@pytest.mark.django_db
@override_settings(API_RATE_LIMIT_PER_SECOND=1000, API_RATE_LIMIT_PER_MINUTE=2)
def test_per_minute_limit_kicks_in(client, bearer):
    body = {"from": "a@x.test", "to": ["b@y.test"], "subject": "hi", "text": "."}
    with patch("apps.sending.tasks.enqueue_message"):
        for _ in range(2):
            assert _post(client, bearer, body).status_code == 200
        r = _post(client, bearer, body)
    assert r.status_code == 429
