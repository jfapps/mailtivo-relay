from __future__ import annotations

import re
from email import policy
from email.parser import Parser
from typing import Any


_RESULT_RE = re.compile(r"\b(spf|dkim|dmarc)=([a-zA-Z0-9_-]+)", re.I)
_HEADER_FROM_RE = re.compile(r"\bheader\.from=([^;\s]+)", re.I)
_SMTP_FROM_RE = re.compile(r"\bsmtp\.mailfrom=([^;\s]+)", re.I)
_DKIM_DOMAIN_RE = re.compile(r"\bheader\.(?:d|i)=@?([^;\s]+)", re.I)
_DKIM_SELECTOR_RE = re.compile(r"\bheader\.s=([^;\s]+)", re.I)


def _domain_from_address(value: str) -> str:
    value = (value or "").strip().strip("<>")
    if "@" not in value:
        return value.lower().rstrip(".")
    return value.rsplit("@", 1)[1].lower().rstrip(".")


def _aligned(candidate: str, domain: str) -> bool:
    candidate = _domain_from_address(candidate)
    domain = domain.lower().rstrip(".")
    return bool(candidate) and (candidate == domain or candidate.endswith("." + domain))


def inspect_delivery_headers(domain: str, raw_headers: str) -> dict[str, Any]:
    raw_headers = (raw_headers or "").strip()
    if not raw_headers:
        raise ValueError("headers_required")
    if len(raw_headers) > 200_000:
        raise ValueError("headers_too_large")

    # Parse headers only. Appending a blank body prevents accidental body parsing/storage.
    message = Parser(policy=policy.default).parsestr(raw_headers + "\n\n")
    auth_headers = list(message.get_all("Authentication-Results", []))
    arc_headers = list(message.get_all("ARC-Authentication-Results", []))
    combined = "\n".join(str(x) for x in (auth_headers + arc_headers))

    results = {k.lower(): v.lower() for k, v in _RESULT_RE.findall(combined)}
    spf_result = results.get("spf", "unknown")
    dkim_result = results.get("dkim", "unknown")
    dmarc_result = results.get("dmarc", "unknown")

    from_domain = ""
    match = _HEADER_FROM_RE.search(combined)
    if match:
        from_domain = _domain_from_address(match.group(1))
    if not from_domain:
        from_domain = _domain_from_address(str(message.get("From", "")))

    smtp_domain = ""
    match = _SMTP_FROM_RE.search(combined)
    if match:
        smtp_domain = _domain_from_address(match.group(1))
    if not smtp_domain:
        smtp_domain = _domain_from_address(str(message.get("Return-Path", "")))

    dkim_domain = ""
    match = _DKIM_DOMAIN_RE.search(combined)
    if match:
        dkim_domain = _domain_from_address(match.group(1))
    if not dkim_domain:
        dkim_signature = str(message.get("DKIM-Signature", ""))
        d_match = re.search(r"(?:^|;)\s*d=([^;\s]+)", dkim_signature, re.I)
        if d_match:
            dkim_domain = _domain_from_address(d_match.group(1))

    dkim_selector = ""
    match = _DKIM_SELECTOR_RE.search(combined)
    if match:
        dkim_selector = match.group(1).lower().rstrip(".")
    if not dkim_selector:
        dkim_signature = str(message.get("DKIM-Signature", ""))
        s_match = re.search(r"(?:^|;)\s*s=([^;\s]+)", dkim_signature, re.I)
        if s_match:
            dkim_selector = s_match.group(1).lower().rstrip(".")

    spf_aligned = spf_result == "pass" and _aligned(smtp_domain, domain)
    dkim_aligned = dkim_result == "pass" and _aligned(dkim_domain, domain)
    from_aligned = _aligned(from_domain, domain)
    dmarc_pass = dmarc_result == "pass" and from_aligned

    delivery_auth_ready = bool(spf_result == "pass" and dkim_result == "pass" and dmarc_pass and dkim_aligned)

    if delivery_auth_ready:
        status = "verified"
        detail = "SPF, DKIM and DMARC passed on a real delivered message."
    elif dkim_result in {"unknown", "none", "neutral"}:
        status = "dkim_missing"
        detail = "The delivered message did not prove a passing DKIM signature."
    elif dkim_result != "pass":
        status = "dkim_failed"
        detail = f"DKIM result was {dkim_result}."
    elif not dkim_aligned:
        status = "dkim_unaligned"
        detail = "DKIM passed, but the signing domain is not aligned with the From domain."
    elif not dmarc_pass:
        status = "dmarc_failed"
        detail = "The delivered message did not pass aligned DMARC."
    else:
        status = "incomplete"
        detail = "Authentication is incomplete for BIMI readiness."

    return {
        "status": status,
        "detail": detail,
        "spf": spf_result,
        "dkim": dkim_result,
        "dmarc": dmarc_result,
        "from_domain": from_domain or None,
        "smtp_mailfrom_domain": smtp_domain or None,
        "dkim_domain": dkim_domain or None,
        "dkim_selector": dkim_selector or None,
        "spf_aligned": spf_aligned,
        "dkim_aligned": dkim_aligned,
        "dmarc_pass": dmarc_pass,
        "delivery_auth_ready": delivery_auth_ready,
    }
