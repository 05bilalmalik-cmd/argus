from app.domain.risk import RiskFinding, calculate_risk


def test_no_findings_produces_risk_zero_and_allows_submission() -> None:
    decision = calculate_risk([])

    assert decision.level == 0
    assert decision.can_submit is True


def test_highest_finding_controls_risk_and_blocks_submission() -> None:
    decision = calculate_risk(
        [
            RiskFinding("optional_omission", 1, "Optional field omitted"),
            RiskFinding("unapproved_sponsorship", 3, "Sensitive answer not approved"),
            RiskFinding("unknown_required", 2, "Required field is unknown"),
        ]
    )

    assert decision.level == 3
    assert decision.can_submit is False
    assert decision.blocking_codes == (
        "optional_omission",
        "unapproved_sponsorship",
        "unknown_required",
    )


def test_destination_mismatch_is_risk_four() -> None:
    decision = calculate_risk(
        [RiskFinding("destination_mismatch", 4, "Role identity differs from queue")]
    )

    assert decision.level == 4
    assert decision.can_submit is False
