from __future__ import annotations

from unittest import mock

import pytest

from apps.accounts.models import WorkspaceSettings
from apps.core.encryption import encrypt
from apps.messages_api.models import Message
from apps.spam_analysis import analyzer, tasks


@pytest.fixture
def configured_ws(db):
    ws = WorkspaceSettings.load()
    ws.ai_provider = "openai"
    ws.ai_api_key_encrypted = encrypt("sk-test")
    ws.save()
    return ws


def _message():
    msg = Message.objects.create(from_address="a@b.test", subject="Hi", to=["x@y.test"])
    msg.set_body(text="hello world")
    msg.save()
    return msg


@pytest.mark.django_db
def test_task_persists_done_report(configured_ws):
    msg = _message()
    fake_report = {
        "score": 80,
        "verdict": "spam",
        "provider": "openai",
        "model": "gpt-4o-mini",
        "ai": {"score": 80, "summary": "spammy", "findings": []},
        "links": {"checked": False, "error": "", "flagged_count": 0, "results": []},
    }
    with mock.patch("apps.spam_analysis.analyzer.analyze", return_value=fake_report):
        result = tasks.analyze_message(msg.id)

    assert result == "done"
    msg.refresh_from_db()
    assert msg.spam_score == 80
    assert msg.spam_verdict == "spam"
    assert msg.spam_report["state"] == "done"
    assert msg.spam_analyzed_at is not None


@pytest.mark.django_db
def test_task_records_error_state(configured_ws):
    msg = _message()
    with mock.patch("apps.spam_analysis.analyzer.analyze", side_effect=analyzer.AnalysisError("no provider")):
        result = tasks.analyze_message(msg.id)

    assert result.startswith("error")
    msg.refresh_from_db()
    assert msg.spam_report == {"state": "error", "error": "no provider"}
    assert msg.spam_score is None
    assert msg.spam_verdict == ""


@pytest.mark.django_db
def test_task_handles_missing_message():
    assert tasks.analyze_message("01JABCDEFGHIJKLMNOPQRSTUVW") == "missing"
