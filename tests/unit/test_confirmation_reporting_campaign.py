"""Scope existence reporting is separate from automation eligibility/budget."""
import pytest

from app.domain.targets import TargetKind
from app.scouting.service import ScoutService
from tests.unit.test_confirmation_scope_campaign import (
    _application, _opportunity, _ReviewRunner, _setup,
)


@pytest.mark.parametrize("state,target,result_key", [
    ("NEEDS_USER", TargetKind.APPLICATION_FORM.value, "stopped:NEEDS_USER"),
    ("NEEDS_OA", TargetKind.APPLICATION_FORM.value, "stopped:NEEDS_OA"),
    ("NEEDS_USER", TargetKind.AUTH_WALL.value, "skipped:login_required"),
])
def test_existing_human_only_scope_is_not_unknown(tmp_path, state, target, result_key):
    settings, db, crypto = _setup(tmp_path)
    try:
        with db.session_scope() as session:
            opportunity = _opportunity(session, employer="Synthetic Bank", slug="synthetic", target_status=target)
            app = _application(session, opportunity, state=state, priority=0)
            runner = _ReviewRunner()
            result = ScoutService(session, settings, crypto).run_autopilot(
                runner, application_scope=[app.id], confirmed_application_ids=[app.id])
            assert runner.application_ids == []
            assert result["submitted"] == 0
            assert len(result["details"]) == 1
            assert result["details"][0]["application_id"] == app.id
            assert result["details"][0]["result"] == result_key
            assert result["details"][0]["state"] == state
            assert app.state == state
    finally:
        db.engine.dispose()


@pytest.mark.parametrize("budget", [0, 1, 2])
def test_scoped_budget_deferred_identity_is_not_unknown(tmp_path, budget):
    settings, db, crypto = _setup(tmp_path)
    try:
        with db.session_scope() as session:
            ids = []
            for i in range(2):
                opportunity = _opportunity(session, employer=f"Synthetic {i}", slug=f"synthetic-{i}", target_status=TargetKind.APPLICATION_FORM.value)
                ids.append(_application(session, opportunity, state="PACKAGE_PREPARED", priority=i, app_id=f"selected-{i}").id)
            runner = _ReviewRunner()
            result = ScoutService(session, settings, crypto).run_autopilot(
                runner, max_runs=budget, application_scope=ids, confirmed_application_ids=ids)
            assert len(runner.application_ids) == budget
            assert result["attempted_runs"] == budget
            assert result["submitted"] == 0
            details = {d["application_id"]:d["result"] for d in result["details"]}
            assert set(details) == set(ids)
            assert sum(v == "deferred:max_runs" for v in details.values()) == 2 - budget
            assert "unknown_application_id" not in details.values()
    finally:
        db.engine.dispose()
