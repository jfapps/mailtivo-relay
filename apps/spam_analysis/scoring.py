from __future__ import annotations

VERDICT_CLEAN = "clean"
VERDICT_SUSPICIOUS = "suspicious"
VERDICT_SPAM = "spam"

# Score band -> verdict: clean 0-39, suspicious 40-69, spam 70-100.
SUSPICIOUS_THRESHOLD = 40
SPAM_THRESHOLD = 70

# A live Safe-Browsing-flagged link is a strong signal on its own — floor the
# final score into spam territory regardless of what the content model thought.
MALICIOUS_LINK_FLOOR = 85


def verdict_for(score: int) -> str:
    if score >= SPAM_THRESHOLD:
        return VERDICT_SPAM
    if score >= SUSPICIOUS_THRESHOLD:
        return VERDICT_SUSPICIOUS
    return VERDICT_CLEAN


def combine(ai_score: int, flagged_link_count: int) -> tuple[int, str]:
    """Fold the AI content score (0-100) and the count of Safe-Browsing-flagged
    links into a final score + verdict."""
    score = max(0, min(100, int(ai_score)))
    if flagged_link_count > 0:
        score = max(score, MALICIOUS_LINK_FLOOR)
    return score, verdict_for(score)
