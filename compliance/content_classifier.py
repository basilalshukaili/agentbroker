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

# Gambling detection uses a two-tier system to avoid false positives:
#  - DEFINITIVE signals: unambiguously gambling (casino, sportsbook, jackpot, ...)
#    → one match is enough to block.
#  - AMBIGUOUS signals: common words that appear in booking/scheduling contexts
#    ("your 3pm slot", "bet you'll love it") → require 2+ matches to block.
# This prevents lone words like "slot" or "bet" inside transactional confirmation
# messages from triggering a gambling violation.
_GAMBLING_DEFINITIVE: list[str] = [
    r"\bcasino\b",
    r"\bbetting\b",
    r"\bpoker\b",
    r"\bsportsbook\b",
    r"\blottery\b",
    r"\bgambl\w+\b",
    r"\bjackpot\b",
    r"\bwager\b",
    r"\bodds\b",
    r"\bscratch\s*card\b",
]
_GAMBLING_AMBIGUOUS: list[str] = [
    r"\bslots?\b",  # "appointment slot" is not gambling
    r"\bbet\b",     # "I bet" / "bet you" is not gambling
]

_SIGNALS: dict[ContentCategory, list[str]] = {
    # Gambling signals are handled separately via the two-tier system above.
    # Leave an empty list here so _COMPILED builds cleanly; classify_content
    # has dedicated logic for gambling.
    ContentCategory.GAMBLING: [],
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

# Pre-compiled gambling-specific pattern lists
_GAMBLING_DEFINITIVE_COMPILED: list[re.Pattern] = [
    re.compile(pat, re.IGNORECASE) for pat in _GAMBLING_DEFINITIVE
]
_GAMBLING_AMBIGUOUS_COMPILED: list[re.Pattern] = [
    re.compile(pat, re.IGNORECASE) for pat in _GAMBLING_AMBIGUOUS
]


def _check_gambling(text: str) -> list[str]:
    """
    Context-aware gambling check.  Returns a non-empty list of matched signals
    if the text should be blocked as gambling content, empty list otherwise.

    Rules:
     - Any DEFINITIVE signal (casino, sportsbook, jackpot, ...) → block.
     - AMBIGUOUS signals (slot, bet) only block when 2+ of them appear,
       preventing lone generic words inside booking confirmations from blocking.
    """
    matched: list[str] = []
    # Definitive signals: one hit is enough.
    for pat in _GAMBLING_DEFINITIVE_COMPILED:
        if pat.search(text):
            matched.append(f"gambling:{pat.pattern}")
            return matched  # short-circuit — definitive match found

    # Ambiguous signals: require at least two independent matches.
    ambig_hits: list[str] = []
    for pat in _GAMBLING_AMBIGUOUS_COMPILED:
        if pat.search(text):
            ambig_hits.append(f"gambling:{pat.pattern}")
    if len(ambig_hits) >= 2:
        return ambig_hits

    return []  # no gambling detected


def classify_content(text: str) -> ClassificationResult:
    """
    Classify message content for restricted categories.
    Returns ClassificationResult; blocked=True means the message must not be sent.

    Priority order: specific harm categories (GAMBLING, LENDING, CANNABIS, ADULT)
    take precedence over the general SPAM category.

    Gambling uses a two-tier signal system (see _check_gambling) so that lone
    generic words like "slot" or "bet" inside transactional booking messages
    do not trigger a false positive.
    """
    matched_signals: list[str] = []
    detected_category = ContentCategory.CLEAN

    # --- Gambling: two-tier context-aware check ---
    gambling_signals = _check_gambling(text)
    if gambling_signals:
        matched_signals.extend(gambling_signals)
        detected_category = ContentCategory.GAMBLING

    if detected_category == ContentCategory.CLEAN:
        # Check remaining specific harm categories first (highest priority)
        priority_order = [
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
