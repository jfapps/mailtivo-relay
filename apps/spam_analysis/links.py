from __future__ import annotations

import re

# Matches http/https URLs in plain text and inside HTML attribute values. The
# character class stops at quotes, angle brackets, whitespace and closing
# punctuation so href="..." values are captured cleanly.
_URL_RE = re.compile(r"""https?://[^\s"'<>)\]}]+""", re.IGNORECASE)
_TRAILING = ".,;:!?\"')]}>"
_MAX_LINKS = 500  # Mirrors the Safe Browsing per-request cap.


def extract_urls(html: str = "", text: str = "") -> list[str]:
    """Return de-duplicated http(s) URLs found across the HTML and text bodies,
    in first-seen order, with trailing punctuation trimmed."""
    found: list[str] = []
    seen: set[str] = set()
    for blob in (html or "", text or ""):
        for raw in _URL_RE.findall(blob):
            url = raw.rstrip(_TRAILING)
            if url and url not in seen:
                seen.add(url)
                found.append(url)
                if len(found) >= _MAX_LINKS:
                    return found
    return found
