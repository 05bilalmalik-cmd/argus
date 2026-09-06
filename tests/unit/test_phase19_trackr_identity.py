from __future__ import annotations

from app.scouting.trackr_identity import (
    IdentityCandidate,
    TrackrIdentityDisposition,
    normalize_trackr_id,
    plan_trackr_identities,
)


def _candidate(
    raw_id: str,
    *,
    opportunity_id: str | None = None,
    source: str = "trackr_live:industrial-placements",
) -> IdentityCandidate:
    return IdentityCandidate(
        raw_id=raw_id,
        employer="Evidence Capital",
        role_title="Industrial Placement 2027",
        cycle="2027",
        source=source,
        tracker_url=(
            "https://app.the-trackr.com/uk-finance/industrial-placements"
            if source.endswith("industrial-placements")
            else "https://app.the-trackr.com/uk-finance/summer-internships"
        ),
        programme_group="year_in_industry",
        location="London",
        division="Investment Banking",
        opportunity_id=opportunity_id,
    )


def test_normalize_trackr_id_accepts_only_bounded_trimmed_strings() -> None:
    assert normalize_trackr_id("  abc-123  ") == "abc-123"
    assert normalize_trackr_id(None) == ""
    assert normalize_trackr_id(123) == ""
    assert normalize_trackr_id(" ") == ""
    assert normalize_trackr_id("x" * 129) == ""


def test_planner_claims_exact_one_to_one_legacy_match() -> None:
    incoming = _candidate("raw-1")
    legacy = _candidate("", opportunity_id="legacy-1")

    plan = plan_trackr_identities([incoming], {}, [legacy])

    assert plan.by_raw_id["raw-1"].disposition is TrackrIdentityDisposition.CLAIM_LEGACY
    assert plan.by_raw_id["raw-1"].opportunity_id == "legacy-1"
    assert plan.ambiguous_legacy_opportunity_ids == ()


def test_planner_separates_same_name_rows_from_different_slugs() -> None:
    yii = _candidate("raw-yii")
    summer = _candidate("raw-summer", source="trackr_live:summer-internships")

    plan = plan_trackr_identities([yii, summer], {}, [])

    assert set(plan.by_raw_id) == {"raw-yii", "raw-summer"}
    assert all(
        item.disposition is TrackrIdentityDisposition.INSERT_NEW
        for item in plan.by_raw_id.values()
    )


def test_planner_never_claims_one_legacy_row_for_two_same_slug_ids() -> None:
    first = _candidate("raw-1")
    second = _candidate("raw-2")
    legacy = _candidate("", opportunity_id="legacy-1")

    plan = plan_trackr_identities([first, second], {}, [legacy])

    assert all(
        item.disposition is TrackrIdentityDisposition.INSERT_NEW
        for item in plan.by_raw_id.values()
    )
    assert plan.ambiguous_legacy_opportunity_ids == ("legacy-1",)


def test_planner_does_not_guess_between_multiple_legacy_candidates() -> None:
    incoming = _candidate("raw-1")
    legacy = [
        _candidate("", opportunity_id="legacy-a"),
        _candidate("", opportunity_id="legacy-b"),
    ]

    plan = plan_trackr_identities([incoming], {}, legacy)

    assert plan.by_raw_id["raw-1"].disposition is TrackrIdentityDisposition.INSERT_NEW
    assert plan.ambiguous_legacy_opportunity_ids == ()


def test_bound_id_is_authoritative_and_excluded_from_claiming() -> None:
    incoming = _candidate("raw-1")
    legacy = _candidate("", opportunity_id="legacy-1")

    plan = plan_trackr_identities([incoming], {"raw-1": "bound-1"}, [legacy])

    assert plan.by_raw_id["raw-1"].disposition is TrackrIdentityDisposition.EXISTING_BOUND
    assert plan.by_raw_id["raw-1"].opportunity_id == "bound-1"
    assert plan.ambiguous_legacy_opportunity_ids == ()
