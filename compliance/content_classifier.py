"""
Content classifier — restricted-category checks for outbound message content.
Blocked categories: gambling, lending/financial products, cannabis, adult content.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class ContentCategory(str, Enum):
    CLEAN = "clean"
    GAMBLING = "gambling"
    LENDING = "lending"
    CANNABIS = "cannabis"
    ADULT = "adult"
    SPAM = "spam"


@dataclass(frozen=True)
class ClassificationResult:
    category: ContentCategory
    blocked: bool
    confidence: float
    matched_signals: list[str]
    reason: str


# Keyword signal banks (extend with ML classifier in production)
_SIGNALS: dict[ContentCategory, list[str]] = {
    ContentCategory.GAMBLING: [
        r"\bcasino\b", r"\bbet\b", r"\bbetting\b", r"\bpoker\b",
        r"\bslots?\b", r"\bsportsbook\b", r"\blottery\b", r"\bgambl\w+\b",
    ],
    ContentCategory.LENDING: [
        r"\bpayday loan\b", r"\bcash advance\b", r"\bpre-?approved loan\b",
        r"\bcredit offer\b", r"\bdebt relief\b", r"\bloan approval\b",
    ],
    ContentCategory.CANNABIS: [
        r"\bcannabis\b", r"\bmarijuana\b", r"\bweed\b", r"\bdispensary\b",
        r"\bthc\b", r"\bcbd oil\b", r"\bganja\b",
    ],
    ContentCategory.ADULT: [
        r"\bxxx\b", r"\bporn\b", r"\berotic\b", r"\bsexual content\b",
        r"\badult content\b",
    ],
    ContentCategory.SPAM: [
        r"\b(free|win|winner|prize|congrat\w+)\b.*\b(click|claim|now)\b",
        r"\b100%\s+free\b",
        r"\bno credit check\b",
        r"\bact now\b",
        r"\blimited time offer\b",
    ],
}

_COMPILED: dict[ContentCategory, list[re.Pattern]] = {
    cat: [re.compile(pat, re.IGNORECASE) for pat in pats]
    for cat, pats in _SIGNALS.items()
}


def classify_content(text: str) -> ClassificationResult:
    """
    Classify message content for restricted categories.
    Returns ClassificationResult; blocked=True means the message must not be sent.

    Priority order: specific harm categories (GAMBLING, LENDING, CANNABIS, ADULT)
    take precedence over the general SPAM category.
    """
    matched_signals: list[str] = []
    detected_category = ContentCategory.CLEAN

    # Check specific harm categories first (highest priority)
    priority_order = [
        ContentCategory.GAMBLING,
        ContentCategory.CANNABIS,
        ContentCategory.ADULT,
        ContentCategory.LENDING,
        ContentCategory.SPAM,   # lowest priority — only if nothing more specific matched
    ]

    for category in priority_order:
        patterns = _COMPILED.get(category, [])
        for pattern in patterns:
            if pattern.search(text):
                matched_signals.append(f"{category.value}:{pattern.pattern}")
                detected_category = category
                break
        if detected_category != ContentCategory.CLEAN:
            break   # stop at first matching category (highest priority wins)

    if detected_category != ContentCategory.CLEAN:
        return ClassificationResult(
            category=detected_category,
            blocked=True,
            confidence=0.9,
            matched_signals=matched_signals,
            reason=f"Content contains signals associated with restricted category: {detected_category.value}",
        )

    return ClassificationResult(
        category=ContentCategory.CLEAN,
        blocked=False,
        confidence=0.95,
        matched_signals=[],
        reason="Content passed restricted-category checks",
    )


def is_blocked(text: str) -> bool:
    return classify_content(text).blocked
