from __future__ import annotations

import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.events.models import Event
from apps.messages_api.models import Message
from django.utils import timezone


@pytest.fixture
def owner(db):
    return User.objects.create_user(email="o@a.test", password="ChangeMe-12345!", is_workspace_admin=True)


@pytest.fixture
def auth_client(client, owner):
    client.force_login(owner)
    return client


def _make_message(**overrides) -> Message:
    defaults = dict(from_address="a@x.test", to=["b@y.test"], subject="hello")
    defaults.update(overrides)
    msg = Message(**defaults)
    msg.set_body(html="<p>body</p>", text="body")
    msg.save()
    return msg


@pytest.mark.django_db
def test_messages_list_requires_login(client):
    r = client.get(reverse("messages_panel:list"))
    assert r.status_code in (302, 301)
    assert "/login" in r["Location"]


@pytest.mark.django_db
def test_messages_list_renders(auth_client):
    _make_message(subject="hello world")
    _make_message(subject="another", status=Message.STATUS_DELIVERED)
    r = auth_client.get(reverse("messages_panel:list"))
    assert r.status_code == 200
    assert b"hello world" in r.content
    assert b"another" in r.content


@pytest.mark.django_db
def test_messages_list_filters_by_status(auth_client):
    _make_message(subject="bouncer", status=Message.STATUS_BOUNCED)
    _make_message(subject="deliveree", status=Message.STATUS_DELIVERED)
    r = auth_client.get(reverse("messages_panel:list") + "?status=bounced")
    assert r.status_code == 200
    assert b"bouncer" in r.content
    assert b"deliveree" not in r.content


@pytest.mark.django_db
def test_messages_list_search(auth_client):
    _make_message(subject="needle")
    _make_message(subject="haystack")
    r = auth_client.get(reverse("messages_panel:list") + "?q=needle")
    assert r.status_code == 200
    assert b"needle" in r.content
    assert b"haystack" not in r.content


@pytest.mark.django_db
def test_messages_list_htmx_returns_partial(auth_client):
    _make_message(subject="hello")
    r = auth_client.get(
        reverse("messages_panel:list"),
        HTTP_HX_REQUEST="true",
    )
    assert r.status_code == 200
    # Partial does not include the panel chrome.
    assert b"<aside" not in r.content
    assert b"hello" in r.content


@pytest.mark.django_db
def test_message_detail_renders_envelope_and_events(auth_client):
    msg = _make_message(subject="t", status=Message.STATUS_DELIVERED)
    Event.objects.create(
        message=msg,
        type="delivered",
        provider_event_id="evt-1",
        occurred_at=timezone.now(),
    )
    r = auth_client.get(reverse("messages_panel:detail", args=[msg.id]))
    assert r.status_code == 200
    assert b"a@x.test" in r.content
    assert b"Delivered" in r.content


@pytest.mark.django_db
def test_message_detail_404(auth_client):
    r = auth_client.get(reverse("messages_panel:detail", args=["01000000000000000000000000"]))
    assert r.status_code == 404


@pytest.mark.django_db
def test_dashboard_kpis_render(auth_client):
    _make_message(status=Message.STATUS_DELIVERED)
    _make_message(status=Message.STATUS_BOUNCED)
    r = auth_client.get(reverse("panel:dashboard"))
    assert r.status_code == 200
    assert b"Delivery rate" in r.content
    assert b"Bounce rate" in r.content


@pytest.mark.django_db
def test_dashboard_kpi_partial(auth_client):
    _make_message(status=Message.STATUS_DELIVERED)
    r = auth_client.get(reverse("panel:kpis"))
    assert r.status_code == 200
    assert b"Delivery rate" in r.content
    # Partial has no <aside> (sidebar) — it's the polled fragment.
    assert b"<aside" not in r.content
