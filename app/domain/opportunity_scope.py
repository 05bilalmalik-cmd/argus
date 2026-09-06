"""Fail-safe scope policy for opportunity programmes and locations."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum


def _fold(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    return " ".join(
        "".join(character for character in text if not unicodedata.combining(character))
        .casefold()
        .split()
    )


_PLACEMENT_TITLE = re.compile(
    r"\byear\s*-?\s*in\s*-?\s*industry\b|"
    r"\bplacements?\b",
    re.I,
)

_IN_SCOPE_TITLE = re.compile(
    _PLACEMENT_TITLE.pattern
    + r"|\binternships?\b|\binterns?\b|\bundergraduates?\b",
    re.I,
)

_SCHOOL_LEAVER_TITLE = re.compile(r"\bschool\s+leaver\b", re.I)
_APPRENTICESHIP_TITLE = re.compile(r"\bapprenticeship\b", re.I)

_OUT_OF_SCOPE_TITLE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("graduate_scheme", re.compile(r"\bgraduate\s+scheme\b", re.I)),
    (
        "graduate_programme",
        re.compile(r"\bgraduate\s+program(?:me)?\b", re.I),
    ),
    ("graduate_analyst", re.compile(r"\bgraduate\s+analyst\b", re.I)),
    (
        "full_time_analyst",
        re.compile(r"\bfull\s*-?\s*time\s+analyst\b", re.I),
    ),
)

_OUT_OF_SCOPE_PROGRAMME_EVIDENCE = {
    "graduate": "graduate",
    "graduate_scheme": "graduate_scheme",
    "graduate_program": "graduate_programme",
    "graduate_programme": "graduate_programme",
    "graduate_analyst": "graduate_analyst",
    "full_time": "full_time",
    "full_time_analyst": "full_time_analyst",
    "school_leaver": "school_leaver",
    "apprentice": "apprenticeship",
    "apprenticeship": "apprenticeship",
}


def out_of_scope_programme_reason(
    role_title: str,
    programme_evidence: str = "",
) -> str | None:
    """Return explicit out-of-scope evidence, preserving named internships.

    School-leaver evidence is unconditional. Apprenticeships are excluded
    unless the title is explicitly a placement. Strong in-scope title evidence
    then outranks only ambiguous graduate/full-time wording.
    """

    title = _fold(role_title)
    evidence = re.sub(r"[^a-z0-9]+", "_", _fold(programme_evidence)).strip("_")
    evidence_reason = _OUT_OF_SCOPE_PROGRAMME_EVIDENCE.get(evidence)
    if _SCHOOL_LEAVER_TITLE.search(title) or evidence_reason == "school_leaver":
        return "school_leaver"
    if _APPRENTICESHIP_TITLE.search(title) or evidence_reason == "apprenticeship":
        if _PLACEMENT_TITLE.search(title):
            return None
        return "apprenticeship"
    if _IN_SCOPE_TITLE.search(title):
        return None
    for reason, pattern in _OUT_OF_SCOPE_TITLE_PATTERNS:
        if pattern.search(title):
            return reason
    return evidence_reason


class LocationDisposition(StrEnum):
    KEEP_UK = "keep_uk"
    KEEP_UNKNOWN = "keep_unknown"
    ARCHIVE_NON_UK = "archive_non_uk"


@dataclass(frozen=True, slots=True)
class LocationScopeDecision:
    disposition: LocationDisposition
    reason: str

    @property
    def should_archive(self) -> bool:
        return self.disposition is LocationDisposition.ARCHIVE_NON_UK


_UK_LOCATION = re.compile(
    r"\b(?:"
    r"u\.?k\.?|gb|gbr|united\s+kingdom|great\s+britain|britain|british|"
    r"england|scotland|wales|northern\s+ireland|n\.?\s*ireland|"
    r"london|bristol|manchester|birmingham|leeds|liverpool|edinburgh|"
    r"glasgow|cardiff|belfast|cambridge|oxford|reading|aberdeen|dundee|"
    r"sheffield|nottingham|newcastle|southampton|portsmouth|exeter|bath|"
    r"coventry|leicester|norwich|swansea|milton\s+keynes|bournemouth|"
    r"brighton|guildford|watford|chelmsford|ipswich|slough|canary\s+wharf"
    r")\b",
    re.I,
)

# Archiving needs positive non-UK evidence. An unrecognised place is retained
# instead of being guessed non-UK.
_NON_UK_LOCATION = re.compile(
    r"\b(?:"
    r"chicago|new\s+york|seattle|fort\s+lauderdale|florida|"
    r"united\s+states|u\.?s\.?a\.?|"
    r"singapore|amsterdam|netherlands|hong\s+kong|japan|tokyo|"
    r"warsaw|poland|madrid|spain|sao\s+paulo|brazil|paris|france|"
    r"berlin|frankfurt|germany|zurich|geneva|switzerland|"
    r"dublin|republic\s+of\s+ireland|australia|canada|china|india|"
    r"united\s+arab\s+emirates|u\.?a\.?e\.?|dubai"
    r")\b",
    re.I,
)


def classify_location_scope(location: str) -> LocationScopeDecision:
    """Classify location conservatively: uncertainty always remains visible."""

    folded = _fold(location)
    if not folded:
        return LocationScopeDecision(
            LocationDisposition.KEEP_UNKNOWN,
            "location_blank",
        )
    if _UK_LOCATION.search(folded):
        return LocationScopeDecision(LocationDisposition.KEEP_UK, "uk_location")
    if _NON_UK_LOCATION.search(folded):
        return LocationScopeDecision(
            LocationDisposition.ARCHIVE_NON_UK,
            "non_uk_location",
        )
    return LocationScopeDecision(
        LocationDisposition.KEEP_UNKNOWN,
        "location_unknown",
    )
