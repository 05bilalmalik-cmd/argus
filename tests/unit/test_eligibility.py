from datetime import date, timedelta

from app.domain.eligibility import CandidateSnapshot, OpportunitySnapshot, evaluate_eligibility


def candidate(**overrides):
    values = {
        "expected_graduation_year": 2029,
        "requires_sponsorship": None,
        "work_authorisation_approved": False,
        "preferred_locations": ("London",),
    }
    values.update(overrides)
    return CandidateSnapshot(**values)


def opportunity(**overrides):
    values = {
        "opportunity_id": "opp-1",
        "url": "https://jobs.example.test/1",
        "deadline": date.today() + timedelta(days=10),
        "min_graduation_year": 2028,
        "max_graduation_year": 2029,
        "sponsorship_supported": None,
        "location": "London",
        "already_applied": False,
    }
    values.update(overrides)
    return OpportunitySnapshot(**values)


def test_expired_opportunity_is_ineligible() -> None:
    decision = evaluate_eligibility(
        candidate(), opportunity(deadline=date.today() - timedelta(days=1)), date.today()
    )

    assert decision.eligible is False
    assert "deadline_expired" in decision.reason_codes


def test_graduation_year_outside_bounds_is_ineligible() -> None:
    decision = evaluate_eligibility(
        candidate(expected_graduation_year=2030), opportunity(), date.today()
    )

    assert decision.eligible is False
    assert "graduation_year_too_late" in decision.reason_codes


def test_unknown_work_authorisation_requires_review_not_guessing() -> None:
    decision = evaluate_eligibility(
        candidate(requires_sponsorship=None, work_authorisation_approved=False),
        opportunity(sponsorship_supported=False),
        date.today(),
    )

    assert decision.eligible is True
    assert decision.requires_review is True
    assert "work_authorisation_unverified" in decision.reason_codes


def test_verified_sponsorship_mismatch_is_ineligible() -> None:
    decision = evaluate_eligibility(
        candidate(requires_sponsorship=True, work_authorisation_approved=True),
        opportunity(sponsorship_supported=False),
        date.today(),
    )

    assert decision.eligible is False
    assert "sponsorship_not_supported" in decision.reason_codes


def test_duplicate_application_is_ineligible() -> None:
    decision = evaluate_eligibility(candidate(), opportunity(already_applied=True), date.today())

    assert decision.eligible is False
    assert "duplicate_application" in decision.reason_codes


def test_matching_candidate_is_eligible_without_review() -> None:
    decision = evaluate_eligibility(
        candidate(requires_sponsorship=False, work_authorisation_approved=True),
        opportunity(sponsorship_supported=False),
        date.today(),
    )

    assert decision.eligible is True
    assert decision.requires_review is False
    assert decision.reason_codes == ()
