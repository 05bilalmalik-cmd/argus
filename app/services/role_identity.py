from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable


MATCH_COVERAGE_THRESHOLD = 1.0
MATCH_JACCARD_THRESHOLD = 0.80
MISMATCH_COVERAGE_THRESHOLD = 0.50
MISMATCH_JACCARD_THRESHOLD = 0.35
MIN_EXPECTED_TOKENS = 3
_MAX_CANDIDATES = 100
_MAX_TEXT = 500
_TOKEN = re.compile(r"[a-z0-9]+")


def _identity(value: object) -> str:
    ascii_text = (
        unicodedata.normalize("NFKD", str(value or ""))
        .encode("ascii", "ignore")
        .decode("ascii")
        .casefold()
    )
    return " ".join(_TOKEN.findall(ascii_text))


def _unique_tokens(value: object) -> frozenset[str]:
    return frozenset(_identity(value).split())


def _role_tokens_without_employer_suffix_or_prefix(
    value: object,
    employer_identity: str,
) -> frozenset[str]:
    """Remove one explicit employer prefix/suffix, never interior role words."""

    words = _identity(value).split()
    employer_words = employer_identity.split()
    if employer_words and words[: len(employer_words)] == employer_words:
        words = words[len(employer_words) :]
    elif employer_words and words[-len(employer_words) :] == employer_words:
        words = words[: -len(employer_words)]
    return frozenset(words)


@dataclass(frozen=True, slots=True)
class RoleTitleCandidate:
    root_marker: str
    root_text: str
    source: str
    text: str


@dataclass(frozen=True, slots=True)
class RoleIdentityMatch:
    matched: bool
    decision: str
    method: str
    reason: str
    expected_employer: str
    expected_role_title: str
    compared_page_title: str
    title_source: str
    selected_root_marker: str
    employer_matched: bool
    overlap_count: int
    expected_token_count: int
    page_token_count: int
    coverage: float
    jaccard: float
    unexpected_page_token_count: int
    qualifying_candidate_count: int

    def audit_evidence(self) -> dict[str, object]:
        return {
            "decision": self.decision,
            "method": self.method,
            "reason": self.reason,
            "expected_employer": self.expected_employer[:_MAX_TEXT],
            "expected_role_title": self.expected_role_title[:_MAX_TEXT],
            "compared_page_title": self.compared_page_title[:_MAX_TEXT],
            "title_source": self.title_source[:80],
            "employer_matched": self.employer_matched,
            "overlap_count": self.overlap_count,
            "expected_word_count": self.expected_token_count,
            "page_word_count": self.page_token_count,
            "coverage": self.coverage,
            "jaccard": self.jaccard,
            "extra_page_word_count": self.unexpected_page_token_count,
            "match_max_extra_page_words": 0,
            "match_coverage_threshold": MATCH_COVERAGE_THRESHOLD,
            "match_jaccard_threshold": MATCH_JACCARD_THRESHOLD,
            "mismatch_coverage_threshold": MISMATCH_COVERAGE_THRESHOLD,
            "mismatch_jaccard_threshold": MISMATCH_JACCARD_THRESHOLD,
            "qualifying_title_count": self.qualifying_candidate_count,
        }


@dataclass(frozen=True, slots=True)
class _ScoredCandidate:
    candidate: RoleTitleCandidate
    employer_matched: bool
    overlap_count: int
    expected_token_count: int
    page_token_count: int
    coverage: float
    jaccard: float
    unexpected_page_token_count: int


def _contains_identity(container: str, expected: str) -> bool:
    """Match one normalized token phrase, never a substring of another token."""

    return bool(expected) and f" {expected} " in f" {container} "


def _score(
    candidate: RoleTitleCandidate,
    *,
    expected_employer_identity: str,
    expected_tokens: frozenset[str],
) -> _ScoredCandidate:
    page_tokens = _role_tokens_without_employer_suffix_or_prefix(
        candidate.text,
        expected_employer_identity,
    )
    overlap_count = len(expected_tokens & page_tokens)
    coverage = overlap_count / len(expected_tokens) if expected_tokens else 0.0
    union_count = len(expected_tokens | page_tokens)
    jaccard = overlap_count / union_count if union_count else 0.0
    root_identity = _identity(candidate.root_text)
    return _ScoredCandidate(
        candidate=candidate,
        employer_matched=_contains_identity(
            root_identity,
            expected_employer_identity,
        ),
        overlap_count=overlap_count,
        expected_token_count=len(expected_tokens),
        page_token_count=len(page_tokens),
        coverage=round(coverage, 6),
        jaccard=round(jaccard, 6),
        unexpected_page_token_count=len(
            page_tokens - expected_tokens
        ),
    )


def _result(
    scored: _ScoredCandidate | None,
    *,
    expected_role: str,
    expected_employer: str,
    decision: str,
    reason: str,
    qualifying_candidate_count: int,
) -> RoleIdentityMatch:
    return RoleIdentityMatch(
        matched=decision == "match",
        decision=decision,
        method="role_title_set_v2",
        reason=reason,
        expected_employer=str(expected_employer or "")[:_MAX_TEXT],
        expected_role_title=str(expected_role or "")[:_MAX_TEXT],
        compared_page_title=(scored.candidate.text[:_MAX_TEXT] if scored else ""),
        title_source=(scored.candidate.source[:80] if scored else ""),
        selected_root_marker=(scored.candidate.root_marker[:128] if scored else ""),
        employer_matched=bool(scored and scored.employer_matched),
        overlap_count=scored.overlap_count if scored else 0,
        expected_token_count=(
            scored.expected_token_count
            if scored
            else len(_unique_tokens(expected_role))
        ),
        page_token_count=scored.page_token_count if scored else 0,
        coverage=scored.coverage if scored else 0.0,
        jaccard=scored.jaccard if scored else 0.0,
        unexpected_page_token_count=(
            scored.unexpected_page_token_count if scored else 0
        ),
        qualifying_candidate_count=qualifying_candidate_count,
    )


def match_role_identity_v2(
    *,
    expected_role: str,
    expected_employer: str,
    candidates: Iterable[RoleTitleCandidate],
) -> RoleIdentityMatch:
    """Return a deterministic, conservative role-title fallback decision."""

    expected_tokens = _unique_tokens(expected_role)
    expected_employer_identity = _identity(expected_employer)
    unique: dict[tuple[str, str], RoleTitleCandidate] = {}
    for item in tuple(candidates)[:_MAX_CANDIDATES]:
        if not isinstance(item, RoleTitleCandidate):
            continue
        normalized_title = _identity(item.text)
        marker = str(item.root_marker or "")[:128]
        if not normalized_title or not marker:
            continue
        unique.setdefault((marker, normalized_title), item)
    scored = [
        _score(
            item,
            expected_employer_identity=expected_employer_identity,
            expected_tokens=expected_tokens,
        )
        for item in unique.values()
    ]
    ranked = sorted(
        scored,
        key=lambda item: (
            -int(item.employer_matched),
            -item.coverage,
            -item.jaccard,
            -item.overlap_count,
            item.page_token_count,
            _identity(item.candidate.text),
            item.candidate.root_marker,
        ),
    )
    best = ranked[0] if ranked else None
    if best is None:
        return _result(
            None,
            expected_role=expected_role,
            expected_employer=expected_employer,
            decision="abstain",
            reason="page_title_missing",
            qualifying_candidate_count=0,
        )
    employer_candidates = [item for item in ranked if item.employer_matched]
    if not employer_candidates:
        return _result(
            best,
            expected_role=expected_role,
            expected_employer=expected_employer,
            decision="abstain",
            reason="employer_identity_missing",
            qualifying_candidate_count=0,
        )
    best = employer_candidates[0]
    if best.expected_token_count < MIN_EXPECTED_TOKENS:
        return _result(
            best,
            expected_role=expected_role,
            expected_employer=expected_employer,
            decision="abstain",
            reason="role_title_too_short",
            qualifying_candidate_count=0,
        )
    qualifying = [
        item
        for item in employer_candidates
        if item.coverage >= MATCH_COVERAGE_THRESHOLD
        and item.jaccard >= MATCH_JACCARD_THRESHOLD
        and item.unexpected_page_token_count == 0
    ]
    if len(qualifying) == 1:
        return _result(
            qualifying[0],
            expected_role=expected_role,
            expected_employer=expected_employer,
            decision="match",
            reason="unique_high_confidence_title",
            qualifying_candidate_count=1,
        )
    if len(qualifying) > 1:
        return _result(
            qualifying[0],
            expected_role=expected_role,
            expected_employer=expected_employer,
            decision="abstain",
            reason="ambiguous_high_confidence_titles",
            qualifying_candidate_count=len(qualifying),
        )
    decision = (
        "abstain"
        if best.coverage >= MISMATCH_COVERAGE_THRESHOLD
        and best.jaccard >= MISMATCH_JACCARD_THRESHOLD
        else "mismatch"
    )
    return _result(
        best,
        expected_role=expected_role,
        expected_employer=expected_employer,
        decision=decision,
        reason=(
            "score_in_abstain_band"
            if decision == "abstain"
            else "role_title_mismatch"
        ),
        qualifying_candidate_count=0,
    )
