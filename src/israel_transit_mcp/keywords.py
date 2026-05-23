"""Hebrew traffic-disruption keyword classifier.

Three tiers, chosen from the RSS research:

- Tier 1 fires the event by itself ("חסימה", "תאונה", "הפגנה" ...).
- Tier 2 is a road/area boost — only matters when Tier 1 also matched,
  but lets us geofence later (a closure mentioning כביש 4 ranks higher
  for a commuter on כביש 4).
- Tier 3 is noise-prone weather/atmospheric terms that need a Tier 1
  co-occurrence to fire.

Returns a `Classification` with the matched tokens and the inferred
`DisruptionKind`, so the aggregator can both filter and label the event.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import DisruptionKind


# Compile once at import. All patterns are anchored on \b? — Hebrew has
# no ASCII word boundaries so we rely on non-letter neighbours instead.
def _alt(*tokens: str) -> re.Pattern[str]:
    return re.compile("(?:" + "|".join(tokens) + ")", re.UNICODE)


TIER1 = _alt(
    "תנועה",
    "עומס(?:י(?:ם)?)?",
    "פקק(?:ים)?",
    "כביש(?:ים)?",
    "חסימ(?:ה|ת|ות)",
    "נסגר(?:ה|ו)?",
    "נחסם(?:ה|ו)?",
    "תאונ(?:ה|ת|ות)",
    "פגיע(?:ה|ת)",
    "הרוג(?:ים)?",
    "פצוע(?:ים)?",
    "הפגנ(?:ה|ות|ת)",
    "חוסמים",
    "מחא(?:ה|ות)",
    "שביתת? ?(?:נהגים|תחבורה)?",
    "רכבת ישראל",
    "רכבת קלה",
    "מטרו",
    "איילון",
    'נתב"ג',
    'מע"צ',
    "נתיבי ישראל",
)

TIER2 = _alt(
    "כביש ?1",
    "כביש ?2",
    "כביש ?4",
    "כביש ?6",
    "כביש ?20",
    "כביש ?40",
    "כביש ?443",
    "נתיבי איילון",
    "מנהרות הכרמל",
    "גשר המיתרים",
    "מחלף",
    "צומת",
)

TIER3 = _alt(
    "מזג אוויר",
    "שלג",
    "שיטפון",
    "גשם כבד",
    "סופה",
)


# Rule order encodes precedence: when a headline matches multiple kinds
# (e.g. "closure caused by accident"), the impact-level kind wins over
# the cause-level one. For routing, road-closed is more actionable than
# accident-on-road.
_KIND_RULES: tuple[tuple[re.Pattern[str], DisruptionKind], ...] = (
    (_alt("הפגנ", "מחא", "חוסמים", "שביתת? ?תחבורה"), DisruptionKind.PROTEST),
    (_alt("חסימ", "נסגר", "נחסם", "כביש סגור"), DisruptionKind.CLOSURE),
    (_alt("תאונ", "פגיע", "התנגש", "הרוג", "פצוע"), DisruptionKind.ACCIDENT),
    (_alt("פקק", "עומס", "תנועה"), DisruptionKind.JAM),
    (_alt("שלג", "שיטפון", "גשם כבד", "סופה"), DisruptionKind.WEATHER),
    (_alt("עבודות", "שיפוצים", 'מע"צ'), DisruptionKind.ROADWORK),
    (_alt("רכבת", "קו ", "תחבורה ציבורית", "שביתה"), DisruptionKind.SERVICE_DISRUPTION),
)


@dataclass
class Classification:
    matched: bool
    kind: DisruptionKind = DisruptionKind.OTHER
    tier1_hits: list[str] = field(default_factory=list)
    tier2_hits: list[str] = field(default_factory=list)
    tier3_hits: list[str] = field(default_factory=list)

    @property
    def confidence(self) -> float:
        """Cheap heuristic: how strongly does this text look like a
        traffic-relevant event?"""
        score = 0.0
        score += 0.5 * min(len(self.tier1_hits), 3)
        score += 0.2 * min(len(self.tier2_hits), 3)
        score += 0.1 * min(len(self.tier3_hits), 2)
        return min(score, 1.0)


def classify(text: str) -> Classification:
    """Inspect title+description for traffic-disruption signals."""
    if not text:
        return Classification(matched=False)
    t1 = TIER1.findall(text)
    t2 = TIER2.findall(text)
    t3 = TIER3.findall(text)
    matched = bool(t1) or (bool(t3) and bool(t2))
    if not matched:
        return Classification(matched=False, tier1_hits=t1, tier2_hits=t2, tier3_hits=t3)
    kind = DisruptionKind.OTHER
    for pattern, k in _KIND_RULES:
        if pattern.search(text):
            kind = k
            break
    return Classification(
        matched=True,
        kind=kind,
        tier1_hits=t1,
        tier2_hits=t2,
        tier3_hits=t3,
    )
