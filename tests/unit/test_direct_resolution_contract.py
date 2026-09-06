from __future__ import annotations

from dataclasses import replace

import pytest

from app.automation.host_policy import origin_for_url
from app.automation.targets import TargetResolution
from app.automation.types import RunMode
from app.domain.targets import TargetKind
from app.services.navigator import ApplicationNavigator


def _resolution() -> TargetResolution:
    url = "http://127.0.0.1:8787/application"
    return TargetResolution(
        source_url=url,
        final_url=url,
        kind=TargetKind.APPLICATION_ENTRY,
        provider="loopback",
        identity_verified=True,
        evidence={
            "synthetic_lab": True,
            "provider": "loopback",
            "application_origin": origin_for_url(url),
            "employer": "Demo Employer",
            "role": "Demo Role",
            "requisition": "/application",
            "form_identity": "application",
        },
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider", "lever"),
        ("employer", "Other Employer"),
        ("role", "Other Role"),
        ("requisition", "/other-application"),
        ("form_identity", "other-form"),
        ("application_origin", "http://127.0.0.1:8788"),
    ],
)
def test_direct_resolution_contradiction_is_rejected_before_worker_creation(
    field: str, value: str
) -> None:
    base = _resolution()
    evidence = dict(base.evidence)
    if field in {"employer", "role", "form_identity"}:
        evidence["nested"] = {field: value}
    elif field == "requisition":
        evidence["nested"] = {"target_path": value}
    else:
        evidence[field] = value
    candidate = replace(base, evidence=evidence)
    created: list[object] = []

    navigator = ApplicationNavigator(
        worker_factory=lambda **kwargs: created.append(kwargs),
        headless=True,
    )
    try:
        with pytest.raises(ValueError, match="resolution|provider|evidence|contract|origin"):
            navigator.start("demo-application", RunMode.REVIEW, resolution=candidate)
        assert created == []
        assert navigator._sessions == {}
    finally:
        navigator.shutdown()


def test_direct_resolution_nested_form_aliases_are_cross_bound() -> None:
    base = _resolution()
    evidence = {
        **base.evidence,
        "form": {
            "binding_verified": True,
            "bound_provider": "loopback",
            "bound_target_url": "http://127.0.0.1:8787/application/",
            "root_token": "application",
            "bound_form_identity": "other-form",
        },
    }
    candidate = replace(base, evidence=evidence)
    created: list[object] = []
    navigator = ApplicationNavigator(
        worker_factory=lambda **kwargs: created.append(kwargs),
        headless=True,
    )
    try:
        with pytest.raises(ValueError, match="resolution|form|identity|evidence"):
            navigator.start("demo-application-form", RunMode.REVIEW, resolution=candidate)
        assert created == []
    finally:
        navigator.shutdown()
