"""Pure, deterministic identity planning for live Trackr programme rows.

Trackr's API ``id`` is the only authoritative provider identity.  The helper
in this module deliberately has no SQLAlchemy or network dependency so a full
batch can be planned before any opportunity is mutated.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Mapping


TRACKR_ID_MAX_LENGTH = 128


class TrackrIdentityConflictError(ValueError):
    """A single Trackr ID was accompanied by conflicting material evidence."""


def normalize_trackr_id(raw: object) -> str:
    """Return a storage-safe Trackr ID, or ``""`` for explicitly ID-less data."""

    if not isinstance(raw, str):
        return ""
    value = raw.strip()
    if not value or len(value) > TRACKR_ID_MAX_LENGTH:
        return ""
    return value


def _normalise_text(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().split())


@dataclass(frozen=True, slots=True)
class IdentityCandidate:
    raw_id: str
    employer: str
    role_title: str
    cycle: str
    source: str
    tracker_url: str
    programme_group: str
    location: str
    division: str
    opportunity_id: str | None = None

    @property
    def claim_key(self) -> tuple[str, str, str, str, str, str, str, str]:
        return (
            _normalise_text(self.employer),
            _normalise_text(self.role_title),
            _normalise_text(self.cycle),
            self.source.strip(),
            self.tracker_url.strip(),
            _normalise_text(self.programme_group),
            _normalise_text(self.location),
            _normalise_text(self.division),
        )


class TrackrIdentityDisposition(str, Enum):
    EXISTING_BOUND = "existing_bound"
    CLAIM_LEGACY = "claim_legacy"
    INSERT_NEW = "insert_new"


@dataclass(frozen=True, slots=True)
class PlannedIdentity:
    raw_id: str
    disposition: TrackrIdentityDisposition
    opportunity_id: str | None = None


@dataclass(frozen=True, slots=True)
class TrackrIdentityPlan:
    items: tuple[PlannedIdentity, ...]
    ambiguous_legacy_opportunity_ids: tuple[str, ...]
    ambiguous_raw_ids: tuple[str, ...]

    @property
    def by_raw_id(self) -> dict[str, PlannedIdentity]:
        return {item.raw_id: item for item in self.items}


def plan_trackr_identities(
    incoming: Iterable[IdentityCandidate],
    bound_opportunity_ids: Mapping[str, str],
    legacy_candidates: Iterable[IdentityCandidate],
) -> TrackrIdentityPlan:
    """Build the complete ID disposition map without mutating any record."""

    incoming_rows = tuple(incoming)
    by_id: dict[str, IdentityCandidate] = {}
    inbound_by_key: dict[
        tuple[str, str, str, str, str, str, str, str], list[IdentityCandidate]
    ] = defaultdict(list)
    for row in incoming_rows:
        raw_id = normalize_trackr_id(row.raw_id)
        if not raw_id:
            raise ValueError("identity planner accepts only valid Trackr IDs")
        if raw_id in by_id:
            raise TrackrIdentityConflictError(
                f"duplicate Trackr programme ID reached planner: {raw_id}"
            )
        by_id[raw_id] = row
        inbound_by_key[row.claim_key].append(row)

    legacy_by_key: dict[
        tuple[str, str, str, str, str, str, str, str], list[IdentityCandidate]
    ] = defaultdict(list)
    for candidate in legacy_candidates:
        if candidate.opportunity_id is None:
            raise ValueError("legacy identity candidate requires an opportunity ID")
        if normalize_trackr_id(candidate.raw_id):
            raise ValueError("legacy identity candidate must be unbound")
        legacy_by_key[candidate.claim_key].append(candidate)

    planned: list[PlannedIdentity] = []
    for raw_id in sorted(by_id):
        row = by_id[raw_id]
        bound_id = bound_opportunity_ids.get(raw_id)
        if bound_id is not None:
            planned.append(
                PlannedIdentity(
                    raw_id,
                    TrackrIdentityDisposition.EXISTING_BOUND,
                    bound_id,
                )
            )
            continue
        legacy = legacy_by_key.get(row.claim_key, [])
        if len(inbound_by_key[row.claim_key]) == 1 and len(legacy) == 1:
            planned.append(
                PlannedIdentity(
                    raw_id,
                    TrackrIdentityDisposition.CLAIM_LEGACY,
                    legacy[0].opportunity_id,
                )
            )
        else:
            planned.append(
                PlannedIdentity(raw_id, TrackrIdentityDisposition.INSERT_NEW)
            )

    ambiguous_ids: set[str] = set()
    ambiguous_raw_ids: set[str] = set()
    for key, rows in inbound_by_key.items():
        if len(rows) < 2:
            continue
        if not any(row.raw_id not in bound_opportunity_ids for row in rows):
            continue
        matching_legacy = legacy_by_key.get(key, [])
        if matching_legacy:
            ambiguous_raw_ids.update(row.raw_id for row in rows)
            ambiguous_ids.update(
                candidate.opportunity_id
                for candidate in matching_legacy
                if candidate.opportunity_id is not None
            )

    return TrackrIdentityPlan(
        items=tuple(planned),
        ambiguous_legacy_opportunity_ids=tuple(sorted(ambiguous_ids)),
        ambiguous_raw_ids=tuple(sorted(ambiguous_raw_ids)),
    )


def material_identity_signature(item: object) -> tuple[object, ...]:
    """Stable material evidence used to reject conflicting duplicate IDs."""

    return (
        _normalise_text(getattr(item, "employer", "")),
        _normalise_text(getattr(item, "role_title", "")),
        str(
            getattr(item, "tracker_url", getattr(item, "source_url", "")) or ""
        ).strip(),
        str(
            getattr(
                item,
                "employer_application_url",
                getattr(item, "application_url", ""),
            )
            or ""
        ).strip(),
        str(getattr(item, "opening_date", "") or ""),
        str(getattr(item, "closing_date", getattr(item, "deadline", "")) or ""),
        _normalise_text(getattr(item, "programme_type", "")),
        _normalise_text(getattr(item, "location", "")),
        str(getattr(item, "source", "") or "").strip(),
        _normalise_text(getattr(item, "ats_type", "")),
        repr(getattr(item, "explicit_status", None)),
        getattr(item, "rolling", None),
        str(getattr(item, "season", "") or "").strip(),
        bool(getattr(item, "invalid_application_url", False)),
    )
