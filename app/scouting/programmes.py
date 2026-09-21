"""Programme-type classification for internship opportunities.

Classifies a role title / programme name into one of four programme groups and
encodes the user's application priority:

    1. YEAR_IN_INDUSTRY  (highest priority - always preferred)
    2. SPRING_WEEK       (auto-apply)
    3. SUMMER            (fallback when a firm has no Year in Industry)
    4. OTHER             (tracked, never auto-applied)

The autopilot uses `should_auto_apply` and the fallback rule:
a firm's Summer is only auto-applied if that firm has no open
Year in Industry opportunity in the database.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping


class ProgrammeType(StrEnum):
    YEAR_IN_INDUSTRY = "year_in_industry"
    SPRING_WEEK = "spring_week"
    SUMMER = "summer"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class ProgrammeFraming:
    degree_length_years: int
    graduation_year: int
    cv_variant_tag: str


PROGRAMME_FRAMING: Mapping[str, ProgrammeFraming] = MappingProxyType(
    {
        ProgrammeType.YEAR_IN_INDUSTRY.value: ProgrammeFraming(4, 2029, "yii-cv"),
        ProgrammeType.SPRING_WEEK.value: ProgrammeFraming(4, 2029, "yii-cv"),
        ProgrammeType.SUMMER.value: ProgrammeFraming(3, 2028, "summer-cv"),
    }
)


def resolve_programme_framing(value: str | ProgrammeType | None) -> ProgrammeFraming | None:
    """Return the coupled application framing for a supported programme group."""

    key = str(value or "").strip().casefold()
    return PROGRAMME_FRAMING.get(key)


# Ordered: most specific patterns first.
_PATTERNS: tuple[tuple[ProgrammeType, tuple[re.Pattern[str], ...]], ...] = (
    (
        ProgrammeType.YEAR_IN_INDUSTRY,
        (
            re.compile(r"year\s*-?\s*in\s*-?\s*industry", re.I),
            re.compile(r"\bYii\b"),
            re.compile(r"industrial\s+placement", re.I),
            re.compile(r"placement\s+year", re.I),
            re.compile(r"sandwich\s+(?:year|placement)", re.I),
            re.compile(r"industry\s+placement", re.I),
        ),
    ),
    (
        ProgrammeType.SPRING_WEEK,
        (
            re.compile(r"spring\s+week", re.I),
            re.compile(r"spring\s+insight", re.I),
            re.compile(r"spring\s+programme", re.I),
            re.compile(r"spring\s+program", re.I),
            re.compile(r"first\s+year\s+(?:insight|programme|program)", re.I),
            re.compile(r"insight\s+week", re.I),
        ),
    ),
    (
        ProgrammeType.SUMMER,
        (
            re.compile(r"summer\s+(?:internship|analyst|analyst|associate|placement|programme|program|intern)", re.I),
            re.compile(r"summer\s+\d{4}", re.I),
            re.compile(r"\bsummer\b", re.I),
            re.compile(r"off[- ]?cycle\s+(?:internship|analyst)", re.I),
            re.compile(r"winter\s+internship", re.I),
        ),
    ),
)

# A title that is ONLY "internship"/"internship programme" with no seasonal or
# placement marker is treated as Summer by UK finance convention.
_GENERIC_INTERNSHIP = re.compile(r"\binternship\b|\bintern\b", re.I)


def classify_programme(*texts: str) -> ProgrammeType:
    """Classify from any combination of role title / programme / division text."""
    blob = " ".join(t for t in texts if t)
    for programme_type, patterns in _PATTERNS:
        if any(p.search(blob) for p in patterns):
            return programme_type
    # Generic placement is a fallback: seasonal placements are summer, while
    # explicit industrial/year-long patterns above still take precedence.
    if re.search(r"\bplacement\b(?!.*consult)", blob, re.I):
        return ProgrammeType.YEAR_IN_INDUSTRY
    if _GENERIC_INTERNSHIP.search(blob):
        return ProgrammeType.SUMMER
    return ProgrammeType.OTHER


# Auto-apply priority: lower value = applied first.
AUTO_APPLY_PRIORITY = {
    "year_in_industry": 0,
    "spring_week": 1,
    "summer": 2,
    "other": 9,
}


def should_auto_apply(programme_type: str) -> bool:
    return AUTO_APPLY_PRIORITY.get(programme_type, 9) <= 2
