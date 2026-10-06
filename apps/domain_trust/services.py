from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

import requests

DNS_ENDPOINT = "https://dns.google/resolve"
_TAG_RE = re.compile(r"(?:^|;)\s*([a-zA-Z]+)=([^;]*)")


@dataclass
class CheckResult:
    status: str
    records: list[str]
    detail: str = ""


def _txt_records(name: str) -> list[str]:
    response = requests.get(
        DNS_ENDPOINT,
        params={"name": name, "type": "TXT"},
        headers={"Accept": "application/dns-json"},
        timeout=8,
    )
    response.raise_for_status()
    payload: dict[str, Any] = response.json()
    records: list[str] = []
    for answer in payload.get("Answer", []) or []:
        if int(answer.get("type", 0)) != 16:
            continue
        data = str(answer.get("data", "")).strip()
        # Google DoH returns TXT chunks quoted. Remove only wrapping quote syntax.
        if data.startswith('"') and data.endswith('"'):
            data = data[1:-1]
        data = data.replace('" "', "")
        records.append(data)
    return records


def _tags(record: str) -> dict[str, str]:
    return {m.group(1).lower(): m.group(2).strip() for m in _TAG_RE.finditer(record)}


def inspect_domain(domain: str, dkim_selector: str = "", certificate_type: str = "none") -> dict[str, Any]:
    root = _txt_records(domain)
    spf_records = [r for r in root if r.lower().startswith("v=spf1")]
    spf = CheckResult(
        status="published" if spf_records else "missing",
        records=spf_records,
        detail="SPF record published." if spf_records else "No SPF record found.",
    )

    dmarc_records = _txt_records(f"_dmarc.{domain}")
    dmarc_record = next((r for r in dmarc_records if r.lower().startswith("v=dmarc1")), "")
    dmarc_tags = _tags(dmarc_record) if dmarc_record else {}
    policy = dmarc_tags.get("p", "").lower()
    try:
        pct = int(dmarc_tags.get("pct", "100") or "100")
    except ValueError:
        pct = 0
    enforced = policy in {"quarantine", "reject"} and pct == 100
    if not dmarc_record:
        dmarc_status, dmarc_detail = "missing", "No DMARC record found."
    elif enforced:
        dmarc_status, dmarc_detail = "protected", f"DMARC enforcement active ({policy}, pct=100)."
    elif policy == "none":
        dmarc_status, dmarc_detail = "monitoring", "DMARC is monitoring only (p=none)."
    else:
        dmarc_status, dmarc_detail = "partial", f"DMARC is not fully enforced (p={policy or 'unset'}, pct={pct})."
    dmarc = CheckResult(dmarc_status, [dmarc_record] if dmarc_record else [], dmarc_detail)

    dkim_records: list[str] = []
    if dkim_selector:
        dkim_records = _txt_records(f"{dkim_selector}._domainkey.{domain}")
    dkim_ok = any(("p=" in r.lower()) or r.lower().startswith("v=dkim1") for r in dkim_records)
    if not dkim_selector:
        dkim = CheckResult("unknown", [], "Add the selector used by your sending provider.")
    elif dkim_ok:
        dkim = CheckResult("published", dkim_records, f"DKIM key found for selector {dkim_selector}.")
    else:
        dkim = CheckResult("missing", dkim_records, f"No DKIM key found for selector {dkim_selector}.")

    bimi_records = _txt_records(f"default._bimi.{domain}")
    bimi_record = next((r for r in bimi_records if r.lower().startswith("v=bimi1")), "")
    bimi_tags = _tags(bimi_record) if bimi_record else {}
    authority = bimi_tags.get("a", "")
    bimi = CheckResult(
        "published" if bimi_record else "missing",
        [bimi_record] if bimi_record else [],
        "BIMI record published." if bimi_record else "No BIMI record found.",
    )

    certificate_type = (certificate_type or "none").lower()
    gmail_logo_candidate = bool(spf_records and dkim_ok and enforced and bimi_record and authority)
    gmail_verified_check_candidate = bool(gmail_logo_candidate and certificate_type == "vmc")

    return {
        "domain": domain,
        "spf": asdict(spf),
        "dkim": asdict(dkim),
        "dmarc": asdict(dmarc),
        "bimi": asdict(bimi),
        "dmarc_policy": policy or None,
        "dmarc_pct": pct if dmarc_record else None,
        "bimi_authority": authority or None,
        "bimi_infrastructure_ready": bool(spf_records and dkim_ok and enforced),
        "certificate_type": certificate_type,
        "gmail_logo_candidate": gmail_logo_candidate,
        "gmail_verified_check_candidate": gmail_verified_check_candidate,
        "gmail_bimi_candidate": gmail_logo_candidate,
    }
