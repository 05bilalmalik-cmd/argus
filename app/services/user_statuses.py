"""Candidate-owned opportunity status mutation and conservative import.

This module deliberately has no dependency on application preparation,
navigation, automation runners, or submission services.  A status change is a
data-and-audit operation only.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.states import UserApplicationStatus, UserStatusActor
from app.models import Opportunity
from app.security.audit import AuditInput, append_audit


_NON_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")
_INTERNAL_INITIAL_DOT = re.compile(r"(?<=[a-z0-9])\.(?=[a-z0-9])")


# An attestation must remain stable across the status mutation it authorizes.
# Every other mapped Opportunity column, including UUID and destination
# evidence, is bound into the reviewed cohort digest.
STATUS_IMPORT_ATTESTATION_EXCLUDED_COLUMNS = frozenset(
    {
        "user_status",
        "user_status_actor",
        "user_status_updated_at",
        "updated_at",
    }
)

# Automatic equivalence is narrower than reviewed attestation. Distinct row
# IDs and ingestion-observation timestamps are expected for literal duplicate
# observations; all role, Trackr, window, URL, ATS, and requirement fields
# must be byte-for-byte equal outside employer case/whitespace.
_AUTOMATIC_DUPLICATE_EXCLUDED_COLUMNS = (
    STATUS_IMPORT_ATTESTATION_EXCLUDED_COLUMNS
    | {
        "id",
        "employer",
        "created_at",
        "resolved_at",
        "resolution_attempted_at",
    }
)


def normalize_status_match_text(value: object) -> str:
    """Normalize only case, punctuation, ampersands, accents, and whitespace.

    Deliberately absent are fuzzy matching, aliases, word stemming, and legal
    suffix stripping: an import key must remain an exact evidence match after
    the transformations required by the Phase 25 brief.
    """

    normalized = unicodedata.normalize("NFKD", str(value or "").strip().casefold())
    normalized = "".join(
        character for character in normalized if not unicodedata.combining(character)
    )
    normalized = normalized.replace("&", " and ")
    normalized = _INTERNAL_INITIAL_DOT.sub("", normalized)
    return " ".join(_NON_ALPHANUMERIC.sub(" ", normalized).split())


def parse_user_status(value: UserApplicationStatus | str) -> UserApplicationStatus:
    if isinstance(value, UserApplicationStatus):
        return value
    normalized = normalize_status_match_text(value)
    for status in UserApplicationStatus:
        if normalized == normalize_status_match_text(status.value):
            return status
    raise ValueError(f"unsupported user status: {value!r}")


def parse_user_status_actor(value: UserStatusActor | str) -> UserStatusActor:
    if isinstance(value, UserStatusActor):
        actor = value
    else:
        try:
            actor = UserStatusActor(str(value).strip().casefold())
        except ValueError as error:
            raise ValueError(f"unsupported user status actor: {value!r}") from error
    if actor is UserStatusActor.DEFAULT:
        raise ValueError("default is creation provenance, not an explicit mutation actor")
    return actor


@dataclass(frozen=True, slots=True)
class UserStatusChange:
    opportunity_id: str
    old_status: str
    new_status: str
    old_actor: str
    new_actor: str
    changed: bool
    reason: str = ""


def set_user_status(
    session: Session,
    opportunity: Opportunity | str,
    status: UserApplicationStatus | str,
    *,
    actor: UserStatusActor | str,
    changed_at: datetime | None = None,
) -> UserStatusChange:
    """Set an explicit status and append exactly one audit event when changed.

    User ownership is authoritative over later imports.  Repeating the same
    status from the same actor is a no-op, making UI retries and imports
    idempotent.
    """

    if isinstance(opportunity, str):
        record = session.get(Opportunity, opportunity)
        if record is None:
            raise LookupError(f"opportunity not found: {opportunity}")
    else:
        record = opportunity

    parsed_status = parse_user_status(status)
    parsed_actor = parse_user_status_actor(actor)
    old_status = str(record.user_status or UserApplicationStatus.NOT_APPLIED.value)
    old_actor = str(record.user_status_actor or UserStatusActor.DEFAULT.value)

    if parsed_actor is UserStatusActor.IMPORT and old_actor == UserStatusActor.USER.value:
        return UserStatusChange(
            record.id,
            old_status,
            old_status,
            old_actor,
            old_actor,
            False,
            "user_owned_status_preserved",
        )

    if old_status == parsed_status.value and old_actor == parsed_actor.value:
        return UserStatusChange(
            record.id,
            old_status,
            old_status,
            old_actor,
            old_actor,
            False,
            "unchanged",
        )

    timestamp = changed_at or datetime.now(timezone.utc)
    record.user_status = parsed_status.value
    record.user_status_actor = parsed_actor.value
    record.user_status_updated_at = timestamp
    append_audit(
        session,
        AuditInput(
            parsed_actor.value,
            "opportunity.user_status_changed",
            "opportunity",
            record.id,
            {
                "old_status": old_status,
                "new_status": parsed_status.value,
                "old_actor": old_actor,
                "new_actor": parsed_actor.value,
                "automation_started": False,
                "submission_started": False,
                "application_state_changed": False,
            },
        ),
    )
    return UserStatusChange(
        record.id,
        old_status,
        parsed_status.value,
        old_actor,
        parsed_actor.value,
        True,
    )


@dataclass(frozen=True, slots=True)
class UserStatusImportRow:
    source_line: int
    status: UserApplicationStatus | str
    employer: str
    programme_name: str
    programme_group: str

    @property
    def match_key(self) -> tuple[str, str, str]:
        return (
            normalize_status_match_text(self.employer),
            normalize_status_match_text(self.programme_name),
            normalize_status_match_text(self.programme_group),
        )


@dataclass(frozen=True, slots=True)
class UserStatusImportCandidate:
    """Privacy-safe opportunity evidence captured before an import mutation."""

    opportunity_id: str
    employer: str
    role_title: str
    programme_group: str
    cycle: str
    location: str
    division: str
    source: str
    target_status: str
    user_status: str
    user_status_actor: str
    identity_sha256: str
    destination_sha256: str


@dataclass(frozen=True, slots=True)
class UserStatusImportOutcome:
    row: UserStatusImportRow
    disposition: str
    reason: str
    candidates: tuple[UserStatusImportCandidate, ...]


@dataclass(frozen=True, slots=True)
class UserStatusImportReport:
    total_count: int
    matched_count: int
    matched_candidate_count: int
    changed_count: int
    unchanged_count: int
    user_owned_count: int
    unmatched_rows: tuple[UserStatusImportRow, ...]
    ambiguous_rows: tuple[UserStatusImportRow, ...]
    user_owned_rows: tuple[UserStatusImportRow, ...]
    outcomes: tuple[UserStatusImportOutcome, ...]
    dry_run: bool

    @property
    def unmatched_count(self) -> int:
        return len(self.unmatched_rows)

    @property
    def ambiguous_count(self) -> int:
        return len(self.ambiguous_rows)

    @property
    def unmatched_lines(self) -> tuple[int, ...]:
        return tuple(row.source_line for row in self.unmatched_rows)

    @property
    def ambiguous_lines(self) -> tuple[int, ...]:
        return tuple(row.source_line for row in self.ambiguous_rows)

    @property
    def user_owned_lines(self) -> tuple[int, ...]:
        return tuple(row.source_line for row in self.user_owned_rows)


def _opportunity_match_key(record: Opportunity) -> tuple[str, str, str]:
    return (
        normalize_status_match_text(record.employer),
        normalize_status_match_text(record.role_title),
        normalize_status_match_text(record.programme_group),
    )


def _case_whitespace_text(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _attestation_json_value(value: object) -> object:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def status_import_attestation_columns() -> tuple[str, ...]:
    return tuple(
        column.name
        for column in Opportunity.__table__.columns
        if column.name not in STATUS_IMPORT_ATTESTATION_EXCLUDED_COLUMNS
    )


def _opportunity_payload(
    record: Opportunity,
    *,
    excluded_columns: frozenset[str] | set[str],
) -> dict[str, object]:
    return {
        column.name: _attestation_json_value(getattr(record, column.name))
        for column in Opportunity.__table__.columns
        if column.name not in excluded_columns
    }


def _sha256_json(value: object) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def status_import_candidate_sha256(record: Opportunity) -> str:
    return _sha256_json(
        _opportunity_payload(
            record,
            excluded_columns=STATUS_IMPORT_ATTESTATION_EXCLUDED_COLUMNS,
        )
    )


def status_import_row_sha256(row: UserStatusImportRow) -> str:
    return _sha256_json(
        {
            "source_line": row.source_line,
            "status": parse_user_status(row.status).value,
            "employer": row.employer,
            "programme_name": row.programme_name,
            "programme_group": row.programme_group,
        }
    )


def status_import_cohort_sha256(
    row: UserStatusImportRow,
    candidates: Iterable[Opportunity],
) -> str:
    return _sha256_json(
        {
            "row_sha256": status_import_row_sha256(row),
            "candidates": [
                {
                    "opportunity_id": candidate.id,
                    "identity_sha256": status_import_candidate_sha256(candidate),
                }
                for candidate in sorted(candidates, key=lambda item: item.id)
            ],
        }
    )


def _strict_duplicate_signature(record: Opportunity) -> str:
    """Identity evidence for rows that differ only by employer case/spacing.

    IDs and observation timestamps are deliberately excluded. Destination,
    source, role, cycle, geography, and resolution identity must otherwise be
    identical; broader ingestion duplicates require an explicit reviewed set.
    """

    return _sha256_json(
        _opportunity_payload(
            record,
            excluded_columns=_AUTOMATIC_DUPLICATE_EXCLUDED_COLUMNS,
        )
    )


def _equivalent_case_whitespace_duplicates(candidates: list[Opportunity]) -> bool:
    if len(candidates) < 2:
        return False
    employers = {_case_whitespace_text(record.employer) for record in candidates}
    signatures = {_strict_duplicate_signature(record) for record in candidates}
    return len(employers) == 1 and len(signatures) == 1


def _candidate_evidence(record: Opportunity) -> UserStatusImportCandidate:
    return UserStatusImportCandidate(
        opportunity_id=record.id,
        employer=record.employer,
        role_title=record.role_title,
        programme_group=record.programme_group,
        cycle=record.cycle,
        location=record.location,
        division=record.division,
        source=record.source,
        target_status=record.target_status,
        user_status=str(record.user_status or UserApplicationStatus.NOT_APPLIED.value),
        user_status_actor=str(record.user_status_actor or UserStatusActor.DEFAULT.value),
        identity_sha256=status_import_candidate_sha256(record),
        destination_sha256=_sha256_json(
            {
                "url": record.url,
                "application_url": record.application_url,
                "target_status": record.target_status,
                "resolved_ats_type": record.resolved_ats_type,
                "resolution_evidence_json": record.resolution_evidence_json,
            }
        ),
    )


def import_user_statuses(
    session: Session,
    rows: Iterable[UserStatusImportRow],
    *,
    dry_run: bool,
    reviewed_duplicate_candidates: Mapping[int, Iterable[str]] | None = None,
    reviewed_duplicate_attestations: Mapping[int, str] | None = None,
) -> UserStatusImportReport:
    """Plan or apply an exact normalized-triple status import.

    Zero matches are unmatched. Multiple database matches are applied only
    when they are strict case/whitespace copies or their exact candidate-ID
    set was reviewed explicitly. No fuzzy comparison or best-effort choice is
    attempted, and contradictory duplicate source rows fail closed.
    """

    import_rows = tuple(rows)
    for row in import_rows:
        parse_user_status(row.status)
        if not all(row.match_key):
            raise ValueError(f"empty import match component on source line {row.source_line}")

    opportunity_index: dict[tuple[str, str, str], list[Opportunity]] = {}
    for record in session.scalars(select(Opportunity)).all():
        opportunity_index.setdefault(_opportunity_match_key(record), []).append(record)
    for candidates in opportunity_index.values():
        candidates.sort(key=lambda candidate: candidate.id)

    conflicting_keys: set[tuple[str, str, str]] = set()
    statuses_by_key: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for row in import_rows:
        statuses_by_key[row.match_key].add(parse_user_status(row.status).value)
    for key, statuses in statuses_by_key.items():
        if len(statuses) > 1:
            conflicting_keys.add(key)

    reviewed = {
        int(source_line): frozenset(str(identifier) for identifier in identifiers)
        for source_line, identifiers in (reviewed_duplicate_candidates or {}).items()
    }
    if any(not identifiers for identifiers in reviewed.values()):
        raise ValueError("reviewed duplicate candidate sets cannot be empty")
    reviewed_attestations = {
        int(source_line): str(digest).strip().casefold()
        for source_line, digest in (reviewed_duplicate_attestations or {}).items()
    }
    if any(
        len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest)
        for digest in reviewed_attestations.values()
    ):
        raise ValueError("reviewed duplicate attestations must be SHA-256 digests")

    matched_count = 0
    matched_candidate_count = 0
    changed_count = 0
    unchanged_count = 0
    user_owned_count = 0
    user_owned: list[UserStatusImportRow] = []
    unmatched: list[UserStatusImportRow] = []
    ambiguous: list[UserStatusImportRow] = []
    outcomes: list[UserStatusImportOutcome] = []

    for row in import_rows:
        candidates = opportunity_index.get(row.match_key, [])
        evidence = tuple(_candidate_evidence(candidate) for candidate in candidates)
        if row.match_key in conflicting_keys:
            ambiguous.append(row)
            outcomes.append(
                UserStatusImportOutcome(
                    row,
                    "ambiguous",
                    "conflicting_import_statuses",
                    evidence,
                )
            )
            continue
        if not candidates:
            unmatched.append(row)
            outcomes.append(
                UserStatusImportOutcome(row, "unmatched", "no_candidates", ())
            )
            continue

        candidate_ids = frozenset(candidate.id for candidate in candidates)
        has_review = (
            row.source_line in reviewed or row.source_line in reviewed_attestations
        )
        reason = "unique_match"
        if has_review:
            # A reviewed cohort is fail-closed even if its cardinality later
            # drops to one. Otherwise deletion of a reviewed duplicate could
            # silently turn an attested decision into an ordinary unique match.
            if reviewed.get(row.source_line) != candidate_ids:
                ambiguous.append(row)
                outcomes.append(
                    UserStatusImportOutcome(
                        row,
                        "ambiguous",
                        "reviewed_candidate_set_mismatch",
                        evidence,
                    )
                )
                continue
            if row.source_line not in reviewed_attestations:
                ambiguous.append(row)
                outcomes.append(
                    UserStatusImportOutcome(
                        row,
                        "ambiguous",
                        "reviewed_candidate_attestation_missing",
                        evidence,
                    )
                )
                continue
            actual_attestation = status_import_cohort_sha256(row, candidates)
            if reviewed_attestations[row.source_line] != actual_attestation:
                ambiguous.append(row)
                outcomes.append(
                    UserStatusImportOutcome(
                        row,
                        "ambiguous",
                        "reviewed_candidate_attribute_drift",
                        evidence,
                    )
                )
                continue
            reason = "reviewed_duplicate_candidate_set"
        elif len(candidates) > 1:
            if _equivalent_case_whitespace_duplicates(candidates):
                reason = "equivalent_duplicate_role"
            else:
                ambiguous.append(row)
                outcomes.append(
                    UserStatusImportOutcome(
                        row,
                        "ambiguous",
                        "different_role_candidates",
                        evidence,
                    )
                )
                continue

        matched_count += 1
        matched_candidate_count += len(candidates)
        parsed_status = parse_user_status(row.status)
        row_has_user_owned = False
        for record in candidates:
            if record.user_status_actor == UserStatusActor.USER.value:
                user_owned_count += 1
                row_has_user_owned = True
                continue
            if (
                record.user_status == parsed_status.value
                and record.user_status_actor == UserStatusActor.IMPORT.value
            ):
                unchanged_count += 1
                continue
            changed_count += 1
            if not dry_run:
                set_user_status(session, record, parsed_status, actor=UserStatusActor.IMPORT)
        if row_has_user_owned:
            user_owned.append(row)
        outcomes.append(UserStatusImportOutcome(row, "matched", reason, evidence))

    return UserStatusImportReport(
        total_count=len(import_rows),
        matched_count=matched_count,
        matched_candidate_count=matched_candidate_count,
        changed_count=changed_count,
        unchanged_count=unchanged_count,
        user_owned_count=user_owned_count,
        unmatched_rows=tuple(unmatched),
        ambiguous_rows=tuple(ambiguous),
        user_owned_rows=tuple(user_owned),
        outcomes=tuple(outcomes),
        dry_run=dry_run,
    )
