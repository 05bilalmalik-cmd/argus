from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.domain.targets import TargetKind
from app.models import Opportunity
from app.routers.pages import _target_ui


def _opportunity(
    *,
    target_status: TargetKind,
    application_url: str | None = None,
    resolved_at: datetime | None = None,
) -> Opportunity:
    return Opportunity(
        employer="ARGUS Test Capital",
        role_title="Summer Analyst",
        cycle="2027",
        url="https://example.test/source",
        application_url=application_url,
        target_status=target_status.value,
        resolved_at=resolved_at,
    )


@pytest.mark.parametrize("target_status", [TargetKind.APPLICATION_ENTRY, TargetKind.APPLICATION_FORM])
def test_unpromoted_application_target_never_uses_verified_label(target_status: TargetKind) -> None:
    record = _opportunity(target_status=target_status)

    target = _target_ui(record, SimpleNamespace(state="READY_TO_SUBMIT"))

    assert target["automation_eligible"] is False
    assert target["navigator_allowed"] is False
    assert not str(target["label"]).casefold().startswith("verified ")


def test_target_with_url_but_without_resolution_timestamp_is_not_verified() -> None:
    record = _opportunity(
        target_status=TargetKind.APPLICATION_ENTRY,
        application_url="https://boards.greenhouse.io/argus/jobs/1234",
        resolved_at=None,
    )

    target = _target_ui(record, SimpleNamespace(state="NEEDS_USER"))

    assert target["automation_eligible"] is False
    assert target["navigator_allowed"] is False
    assert not str(target["label"]).casefold().startswith("verified ")


def test_currently_promoted_application_target_keeps_verified_label() -> None:
    record = _opportunity(
        target_status=TargetKind.APPLICATION_ENTRY,
        application_url="https://boards.greenhouse.io/argus/jobs/1234",
        resolved_at=datetime.now(timezone.utc),
    )

    target = _target_ui(record, SimpleNamespace(state="NEEDS_USER"))

    assert target["automation_eligible"] is True
    assert target["navigator_allowed"] is True
    assert target["label"] == "Verified application entry"
