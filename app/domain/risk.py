from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True, slots=True)
class RiskFinding:
    code: str
    level: int
    reason: str

    def __post_init__(self) -> None:
        if not 0 <= self.level <= 4:
            raise ValueError("Risk level must be between 0 and 4")


@dataclass(frozen=True, slots=True)
class RiskDecision:
    level: int
    can_submit: bool
    findings: tuple[RiskFinding, ...]
    blocking_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FormReadiness:
    """Evidence contract for one application-form step.

    An adapter asserts readiness only when it can name WHERE it inspected
    (application root) and WHY the control inventory is trustworthy.
    ``step_requires_no_fields`` may be True only with explicit provider
    evidence (e.g. a Workday review/consent step that legitimately carries
    no inputs).  Zero fields without such proof is never a ready form.
    """

    application_root_found: bool = False
    controls_enumerated: bool = False
    submission_candidate_present: bool = False
    step_requires_no_fields: bool = False
    step_name: str = ""

    def zero_field_step_is_legitimate(self) -> bool:
        return self.step_requires_no_fields and bool(self.step_name)


def calculate_risk(findings: Sequence[RiskFinding]) -> RiskDecision:
    frozen = tuple(findings)
    level = max((finding.level for finding in frozen), default=0)
    return RiskDecision(
        level=level,
        can_submit=level == 0,
        findings=frozen,
        blocking_codes=tuple(finding.code for finding in frozen if finding.level > 0),
    )


def application_form_not_found_finding(
    adapter: str,
    readiness: FormReadiness | None = None,
) -> RiskFinding:
    """Fail-closed finding when no verified application form exists."""

    detail = (
        f"No application root or form controls were found (adapter={adapter})"
        if readiness is None
        else (
            f"Application form not verified on this page (adapter={adapter}, "
            f"root={readiness.application_root_found}, "
            f"controls={readiness.controls_enumerated})"
        )
    )
    return RiskFinding("application_form_not_found", 4, detail)
