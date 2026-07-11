from __future__ import annotations

from typing import TYPE_CHECKING

from apps.accounts.models import WorkspaceSettings
from apps.core.encryption import decrypt

from . import ai_provider, links, safe_browsing, scoring

if TYPE_CHECKING:
    from apps.messages_api.models import Message


class AnalysisError(Exception):
    """Analysis could not be performed (e.g. no provider configured)."""


def analyze(message: Message) -> dict:
    """Run spam analysis for a single message and return a report dict.

    Pure with respect to the database — the caller persists the result and stamps
    the timestamp. Raises AnalysisError if no AI provider is configured, or
    AIProviderError if the provider call fails.
    """
    ws = WorkspaceSettings.load()
    if not ws.ai_provider or not ws.ai_api_key_encrypted:
        raise AnalysisError("No AI provider is configured. Set one up under Integrations.")

    body = message.body()
    urls = links.extract_urls(html=body["html"], text=body["text"])

    # --- AI content analysis ---
    ai_key = decrypt(bytes(ws.ai_api_key_encrypted))
    content = ai_provider.analyze_content(
        ws.ai_provider,
        ws.ai_model,
        ai_key,
        subject=message.subject,
        from_address=message.from_address,
        html=body["html"],
        text=body["text"],
    )

    # --- Safe Browsing link checks ---
    sb_enabled = bool(ws.safe_browsing_enabled and ws.safe_browsing_api_key_encrypted)
    sb_error = ""
    matches: dict[str, list[dict]] = {}
    if sb_enabled and urls:
        sb_key = decrypt(bytes(ws.safe_browsing_api_key_encrypted))
        try:
            matches = safe_browsing.check_urls(sb_key, urls)
        except safe_browsing.SafeBrowsingError as exc:
            sb_error = str(exc)

    link_results = []
    for url in urls:
        threats = matches.get(url, [])
        link_results.append(
            {
                "url": url,
                "flagged": bool(threats),
                "threats": sorted({t.get("threatType", "") for t in threats if t.get("threatType")}),
            }
        )
    flagged_count = sum(1 for r in link_results if r["flagged"])

    score, verdict = scoring.combine(content.score, flagged_count)

    return {
        "score": score,
        "verdict": verdict,
        "provider": ws.ai_provider,
        "model": ws.ai_model or ai_provider.DEFAULT_MODELS.get(ws.ai_provider, ""),
        "ai": {
            "score": content.score,
            "summary": content.summary,
            "findings": [f.model_dump() for f in content.findings],
        },
        "links": {
            "checked": sb_enabled,
            "error": sb_error,
            "flagged_count": flagged_count,
            "results": link_results,
        },
    }
