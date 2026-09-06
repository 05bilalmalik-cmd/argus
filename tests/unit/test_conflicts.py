from app.domain.conflicts import (
    CandidateApplication,
    ConflictRuleSnapshot,
    ExistingApplication,
    evaluate_conflicts,
)
from app.domain.states import ApplicationState


def test_maximum_applications_rule_blocks_second_active_application() -> None:
    candidate = CandidateApplication("Ares Management", "2027", "Private Credit", "credit")
    existing = [
        ExistingApplication(
            "Ares Management", "2027", "Real Assets", "real_assets", ApplicationState.QUEUED
        )
    ]
    rules = [ConflictRuleSnapshot("Ares*", "2027", 1, ())]

    decision = evaluate_conflicts(candidate, existing, rules)

    assert decision.blocked is True
    assert "maximum_applications_reached" in decision.reason_codes


def test_mutually_exclusive_programmes_block_conflicting_group() -> None:
    candidate = CandidateApplication("Example Bank", "2027", "Markets", "markets")
    existing = [
        ExistingApplication(
            "Example Bank", "2027", "Investment Banking", "ibd", ApplicationState.SUBMITTED
        )
    ]
    rules = [
        ConflictRuleSnapshot(
            "Example Bank", "2027", 3, (frozenset({"ibd", "markets", "asset_management"}),)
        )
    ]

    decision = evaluate_conflicts(candidate, existing, rules)

    assert decision.blocked is True
    assert "mutually_exclusive_programme" in decision.reason_codes


def test_rejected_application_does_not_consume_active_limit() -> None:
    candidate = CandidateApplication("Example Bank", "2027", "Markets", "markets")
    existing = [
        ExistingApplication(
            "Example Bank", "2027", "Investment Banking", "ibd", ApplicationState.REJECTED
        )
    ]
    rules = [ConflictRuleSnapshot("Example Bank", "2027", 1, ())]

    decision = evaluate_conflicts(candidate, existing, rules)

    assert decision.blocked is False
    assert decision.reason_codes == ()


def test_unmatched_employer_has_no_conflict() -> None:
    candidate = CandidateApplication("Firm Two", "2027", "Credit", "credit")
    existing = [
        ExistingApplication("Firm One", "2027", "Credit", "credit", ApplicationState.SUBMITTED)
    ]
    rules = [ConflictRuleSnapshot("Firm One", "2027", 1, ())]

    assert evaluate_conflicts(candidate, existing, rules).blocked is False


def test_unqueued_and_blocked_records_do_not_consume_employer_limit() -> None:
    candidate = CandidateApplication("Example Bank", "2027", "Markets", "markets")
    rules = [ConflictRuleSnapshot("Example Bank", "2027", 1, ())]

    for state in (
        ApplicationState.DISCOVERED,
        ApplicationState.ELIGIBILITY_CHECKED,
        ApplicationState.BLOCKED,
        ApplicationState.REJECTED,
    ):
        decision = evaluate_conflicts(
            candidate,
            [ExistingApplication("Example Bank", "2027", "IBD", "ibd", state)],
            rules,
        )
        assert decision.blocked is False, state


def test_programme_groups_match_case_and_spacing_variants() -> None:
    candidate = CandidateApplication("Example Bank", "2027", "Markets", " Markets ")
    existing = [
        ExistingApplication(
            "Example Bank",
            "2027",
            "Investment Banking",
            "IBD",
            ApplicationState.SUBMITTED,
        )
    ]
    rules = [
        ConflictRuleSnapshot(
            "Example Bank",
            "2027",
            3,
            (frozenset({"ibd", "markets"}),),
        )
    ]

    decision = evaluate_conflicts(candidate, existing, rules)

    assert decision.blocked is True
    assert decision.reason_codes == ("mutually_exclusive_programme",)
