from __future__ import annotations

import re

from pydantic import BaseModel, Field

from apps.accounts.models import WorkspaceSettings

# Cap how much body text we hand to the model — bounds tokens/cost on big emails.
_MAX_BODY_CHARS = 8000
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# Map our stored provider value -> SimplerLLM LLMProvider enum member name.
# SimplerLLM unified LLM interface.
# Docs: https://docs.simplerllm.com/simplerllm/docs/llm-interface/ (fetched 2026-06-05)
_PROVIDER_ENUM = {
    WorkspaceSettings.AI_PROVIDER_OPENAI: "OPENAI",
    WorkspaceSettings.AI_PROVIDER_ANTHROPIC: "ANTHROPIC",
    WorkspaceSettings.AI_PROVIDER_GEMINI: "GEMINI",
}

# Cheap, fast defaults used when the operator leaves the model field blank.
DEFAULT_MODELS = {
    WorkspaceSettings.AI_PROVIDER_OPENAI: "gpt-4o-mini",
    WorkspaceSettings.AI_PROVIDER_ANTHROPIC: "claude-haiku-4-5",
    WorkspaceSettings.AI_PROVIDER_GEMINI: "gemini-2.0-flash",
}


class AIProviderError(Exception):
    """The configured AI provider is misconfigured or unreachable."""


def build_llm(provider: str, model: str, api_key: str):
    """Construct a SimplerLLM LLM instance for the given provider. Key is passed
    directly (overrides any env var), so nothing leaks from the host environment."""
    from SimplerLLM.language.llm import LLM, LLMProvider

    enum_name = _PROVIDER_ENUM.get(provider)
    if not enum_name:
        raise AIProviderError(f"Unsupported AI provider: {provider!r}")
    model = (model or "").strip() or DEFAULT_MODELS.get(provider, "")
    if not model:
        raise AIProviderError("No model configured for this provider.")
    return LLM.create(
        provider=getattr(LLMProvider, enum_name),
        model_name=model,
        api_key=api_key,
    )


def test_connection(provider: str, model: str, api_key: str) -> str:
    """Cheap round-trip to validate the key + model. Returns the model's reply.
    Raises AIProviderError on any failure."""
    try:
        llm = build_llm(provider, model, api_key)
        reply = llm.generate_response(prompt="Reply with the single word: OK")
    except AIProviderError:
        raise
    except Exception as exc:  # SimplerLLM surfaces provider-specific errors
        raise AIProviderError(str(exc)) from exc
    if not reply:
        raise AIProviderError("Provider returned an empty response.")
    return reply.strip()


class SpamFinding(BaseModel):
    label: str = Field(description="Short name of a CONCRETE spam/phishing indicator actually present, e.g. 'Credential request' or 'Spoofed sender domain'")
    detail: str = Field(description="One sentence quoting or citing the specific thing in THIS email that triggered it")


class ContentAnalysis(BaseModel):
    score: int = Field(description="Spam/phishing likelihood 0-100. 0-39 legitimate, 40-69 suspicious, 70-100 spam/phishing. A short, plain, non-deceptive message is legitimate (low score).")
    summary: str = Field(description="One sentence that is CONSISTENT with the score: a low score must read as benign, a high score as a clear warning. Never contradict the score.")
    findings: list[SpamFinding] = Field(default_factory=list, description="Only genuine spam/phishing indicators that raised the score. Empty list when the email looks legitimate.")


def _plaintext(html: str, text: str) -> str:
    """Prefer the text part; fall back to a crude HTML-to-text strip."""
    body = text or _WS_RE.sub(" ", _TAG_RE.sub(" ", html or "")).strip()
    return body[:_MAX_BODY_CHARS]


def analyze_content(
    provider: str,
    model: str,
    api_key: str,
    *,
    subject: str,
    from_address: str,
    html: str = "",
    text: str = "",
) -> ContentAnalysis:
    """Score the email's content for spam/phishing via the configured provider.
    Raises AIProviderError on any failure."""
    from SimplerLLM.language.llm_addons import generate_pydantic_json_model

    body = _plaintext(html, text)
    prompt = (
        "You are an email spam and phishing detection engine. Rate how likely THIS email "
        "is to be spam or a phishing attempt, on a 0-100 scale.\n\n"
        "Real spam/phishing signals (raise the score): deceptive or misleading subject lines, "
        "urgency or pressure tactics, requests for credentials, payment, or sensitive data, "
        "sender domains spoofed to impersonate a known brand, links whose visible text hides "
        "a different destination, and classic spam offers (prizes, lotteries, miracle cures).\n\n"
        "Do NOT treat the following as spam by themselves — they are normal and should keep the "
        "score LOW: a short or minimal message (e.g. 'hi'), a brief or generic subject, an "
        "internal test or smoke-test message, the absence of a call to action or personalisation, "
        "or simply an unfamiliar but otherwise plausible sender domain. Judge intent and "
        "deception, not brevity or polish.\n\n"
        "Score honestly: most ordinary mail scores under 40. Only list findings that genuinely "
        "raised the score; if the email looks legitimate, return an empty findings list and a "
        "benign one-sentence summary that matches the low score.\n\n"
        f"From: {from_address}\n"
        f"Subject: {subject}\n\n"
        f"Body:\n{body}"
    )
    try:
        llm = build_llm(provider, model, api_key)
        result = generate_pydantic_json_model(
            model_class=ContentAnalysis,
            prompt=prompt,
            llm_instance=llm,
        )
    except AIProviderError:
        raise
    except Exception as exc:
        raise AIProviderError(str(exc)) from exc
    if isinstance(result, str):  # SimplerLLM returns an error string on failure
        raise AIProviderError(result)
    # Clamp defensively — models occasionally return out-of-range scores.
    result.score = max(0, min(100, int(result.score)))
    return result
