from __future__ import annotations

from unittest import mock

import pytest

from apps.accounts.models import WorkspaceSettings
from apps.core.encryption import encrypt
from apps.messages_api.models import Message
from apps.spam_analysis import analyzer, links, scoring
from apps.spam_analysis.ai_provider import ContentAnalysis, SpamFinding

# ---- links -------------------------------------------------------------------

def test_extract_urls_dedupes_and_trims():
    html = '<a href="https://good.example/path">click</a> and https://good.example/path again'
    text = "Visit http://bad.test/landing. Thanks!"
    urls = links.extract_urls(html=html, text=text)
    assert urls == ["https://good.example/path", "http://bad.test/landing"]


def test_extract_urls_empty():
    assert links.extract_urls(html="", text="no links here") == []


# ---- scoring -----------------------------------------------------------------

@pytest.mark.parametrize(
    "score,expected",
    [(0, "clean"), (39, "clean"), (40, "suspicious"), (69, "suspicious"), (70, "spam"), (100, "spam")],
)
def test_verdict_bands(score, expected):
    assert scoring.verdict_for(score) == expected


def test_flagged_link_floors_into_spam():
    score, verdict = scoring.combine(ai_score=10, flagged_link_count=1)
    assert score >= scoring.SPAM_THRESHOLD
    assert verdict == "spam"


def test_no_flag_keeps_ai_score():
    assert scoring.combine(ai_score=25, flagged_link_count=0) == (25, "clean")


# ---- analyzer orchestration --------------------------------------------------

@pytest.fixture
def configured_ws(db):
    ws = WorkspaceSettings.load()
    ws.ai_provider = "openai"
    ws.ai_model = "gpt-4o-mini"
    ws.ai_api_key_encrypted = encrypt("sk-test")
    ws.save()
    return ws


def _message(html="", text=""):
    msg = Message(from_address="sender@evil.test", subject="You won!", to=["a@b.test"])
    msg.set_body(html=html, text=text)
    return msg


@pytest.mark.django_db
def test_analyze_requires_provider():
    msg = _message(text="hello")
    with pytest.raises(analyzer.AnalysisError):
        analyzer.analyze(msg)


@pytest.mark.django_db
def test_analyze_combines_ai_and_links(configured_ws):
    configured_ws.safe_browsing_enabled = True
    configured_ws.safe_browsing_api_key_encrypted = encrypt("AIza-test")
    configured_ws.save()

    msg = _message(text="Claim your prize at http://malware.test/go now")
    fake_ai = ContentAnalysis(score=30, summary="Looks promotional", findings=[SpamFinding(label="Urgency", detail="'now'")])
    fake_matches = {"http://malware.test/go": [{"threatType": "MALWARE"}]}

    with mock.patch("apps.spam_analysis.ai_provider.analyze_content", return_value=fake_ai), \
         mock.patch("apps.spam_analysis.safe_browsing.check_urls", return_value=fake_matches):
        report = analyzer.analyze(msg)

    assert report["verdict"] == "spam"  # flagged link floors the score
    assert report["score"] >= scoring.SPAM_THRESHOLD
    assert report["links"]["checked"] is True
    assert report["links"]["flagged_count"] == 1
    assert report["links"]["results"][0]["threats"] == ["MALWARE"]
    assert report["ai"]["score"] == 30
    assert report["ai"]["findings"][0]["label"] == "Urgency"


@pytest.mark.django_db
def test_analyze_skips_safe_browsing_when_disabled(configured_ws):
    msg = _message(text="Plain message with http://example.test/x")
    fake_ai = ContentAnalysis(score=10, summary="Looks fine", findings=[])

    with mock.patch("apps.spam_analysis.ai_provider.analyze_content", return_value=fake_ai) as ai, \
         mock.patch("apps.spam_analysis.safe_browsing.check_urls") as sb:
        report = analyzer.analyze(msg)

    ai.assert_called_once()
    sb.assert_not_called()
    assert report["links"]["checked"] is False
    assert report["links"]["results"][0]["url"] == "http://example.test/x"
    assert report["links"]["results"][0]["flagged"] is False
    assert report["verdict"] == "clean"


@pytest.mark.django_db
def test_analyze_tolerates_safe_browsing_error(configured_ws):
    from apps.spam_analysis.safe_browsing import SafeBrowsingError

    configured_ws.safe_browsing_enabled = True
    configured_ws.safe_browsing_api_key_encrypted = encrypt("AIza-test")
    configured_ws.save()

    msg = _message(text="Link http://example.test/x")
    fake_ai = ContentAnalysis(score=55, summary="Maybe", findings=[])

    with mock.patch("apps.spam_analysis.ai_provider.analyze_content", return_value=fake_ai), \
         mock.patch("apps.spam_analysis.safe_browsing.check_urls", side_effect=SafeBrowsingError("rate limited")):
        report = analyzer.analyze(msg)

    assert report["links"]["error"] == "rate limited"
    assert report["links"]["flagged_count"] == 0
    assert report["verdict"] == "suspicious"  # AI score 55 stands
