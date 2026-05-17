from __future__ import annotations

import pytest

from apps.messages_api.serializers import ValidationError, parse_email_request


def test_parse_minimal_payload():
    out = parse_email_request(
        {"from": "a@x.test", "to": "b@y.test", "subject": "hi", "html": "<p>hi</p>"}
    )
    assert out["to"] == ["b@y.test"]
    assert out["html"] == "<p>hi</p>"


def test_parse_coerces_string_to_list():
    out = parse_email_request(
        {"from": "a@x.test", "to": "b@y.test", "cc": "c@y.test", "subject": "x", "text": "."}
    )
    assert out["cc"] == ["c@y.test"]


def test_parse_accepts_angle_addressed_from():
    out = parse_email_request(
        {"from": "Acme <onboarding@acme.test>", "to": ["b@y.test"], "subject": "x", "text": "."}
    )
    assert out["from_address"] == "Acme <onboarding@acme.test>"


def test_parse_requires_body():
    with pytest.raises(ValidationError):
        parse_email_request({"from": "a@x.test", "to": ["b@y.test"], "subject": "hi"})


def test_parse_rejects_bad_email():
    with pytest.raises(ValidationError) as exc:
        parse_email_request(
            {"from": "a@x.test", "to": ["not-an-email"], "subject": "x", "text": "."}
        )
    assert "to" in str(exc.value)


def test_parse_requires_to():
    with pytest.raises(ValidationError):
        parse_email_request({"from": "a@x.test", "to": [], "subject": "x", "text": "."})


def test_parse_attachment_requires_content():
    with pytest.raises(ValidationError):
        parse_email_request(
            {
                "from": "a@x.test",
                "to": ["b@y.test"],
                "subject": "x",
                "text": ".",
                "attachments": [{"filename": "a.pdf", "path": "https://example/a.pdf"}],
            }
        )


def test_parse_rejects_bad_scheduled_at():
    with pytest.raises(ValidationError):
        parse_email_request(
            {
                "from": "a@x.test",
                "to": ["b@y.test"],
                "subject": "x",
                "text": ".",
                "scheduled_at": "tomorrow",
            }
        )


def test_parse_rejects_invalid_tag():
    with pytest.raises(ValidationError):
        parse_email_request(
            {
                "from": "a@x.test",
                "to": ["b@y.test"],
                "subject": "x",
                "text": ".",
                "tags": [{"only_name": "x"}],
            }
        )
