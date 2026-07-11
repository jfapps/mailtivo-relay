from __future__ import annotations

import requests

# Google Safe Browsing Lookup API v4 (threatMatches:find).
# Docs: https://developers.google.com/safe-browsing/v4/lookup-api (fetched 2026-06-05)
_ENDPOINT = "https://safebrowsing.googleapis.com/v4/threatMatches:find"
_CLIENT_ID = "mailtivo-relay"
_CLIENT_VERSION = "1.0"
_THREAT_TYPES = [
    "MALWARE",
    "SOCIAL_ENGINEERING",
    "UNWANTED_SOFTWARE",
    "POTENTIALLY_HARMFUL_APPLICATION",
]
_PLATFORM_TYPES = ["ANY_PLATFORM"]
_TIMEOUT = 15
_MAX_URLS = 500  # API hard limit per request.

# Google's canonical always-flagged test URL, used to verify an API key works.
TEST_URL = "http://malware.testing.google.test/testing/malware/"


class SafeBrowsingError(Exception):
    """The Safe Browsing API key is invalid or the service is unreachable."""


def check_urls(api_key: str, urls: list[str]) -> dict[str, list[dict]]:
    """Look the URLs up against the threat lists.

    Returns {url: [match, ...]} containing only the URLs that matched a threat.
    Clean URLs are omitted. Raises SafeBrowsingError on an API failure.
    """
    deduped = [u for u in dict.fromkeys(urls) if u][:_MAX_URLS]
    if not deduped:
        return {}
    body = {
        "client": {"clientId": _CLIENT_ID, "clientVersion": _CLIENT_VERSION},
        "threatInfo": {
            "threatTypes": _THREAT_TYPES,
            "platformTypes": _PLATFORM_TYPES,
            "threatEntryTypes": ["URL"],
            "threatEntries": [{"url": u} for u in deduped],
        },
    }
    try:
        resp = requests.post(_ENDPOINT, params={"key": api_key}, json=body, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        raise SafeBrowsingError(str(exc)) from exc
    if resp.status_code != 200:
        raise SafeBrowsingError(
            f"Safe Browsing API returned HTTP {resp.status_code}: {resp.text[:200]}"
        )
    matches = resp.json().get("matches", []) or []
    out: dict[str, list[dict]] = {}
    for match in matches:
        url = match.get("threat", {}).get("url", "")
        out.setdefault(url, []).append(match)
    return out


def test_connection(api_key: str) -> bool:
    """Verify the key by looking up Google's test malware URL, which always matches.
    Returns True if the test URL was flagged. Raises SafeBrowsingError on failure."""
    return TEST_URL in check_urls(api_key, [TEST_URL])
