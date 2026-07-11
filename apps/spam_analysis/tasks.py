from __future__ import annotations

from django.utils import timezone

from apps.messages_api.models import Message

from . import analyzer
from .ai_provider import AIProviderError

_FIELDS = ["spam_report", "spam_score", "spam_verdict", "spam_analyzed_at"]


def analyze_message(message_id: str) -> str:
    """Django-Q2 entrypoint: analyse one message and persist the result.

    The detail-page panel reads spam_report["state"] to render running / done /
    error. Enqueue with async_task("apps.spam_analysis.tasks.analyze_message", id).
    """
    try:
        message = Message.objects.get(pk=message_id)
    except Message.DoesNotExist:
        return "missing"

    try:
        report = analyzer.analyze(message)
    except (analyzer.AnalysisError, AIProviderError) as exc:
        message.spam_report = {"state": "error", "error": str(exc)}
        message.spam_score = None
        message.spam_verdict = ""
        message.spam_analyzed_at = timezone.now()
        message.save(update_fields=_FIELDS)
        return f"error: {exc}"

    report["state"] = "done"
    message.spam_report = report
    message.spam_score = report["score"]
    message.spam_verdict = report["verdict"]
    message.spam_analyzed_at = timezone.now()
    message.save(update_fields=_FIELDS)
    return "done"
