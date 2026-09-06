from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from app.config import Settings
from app.db import Database
from app.domain.states import ApplicationState
from app.models import Application, Opportunity
from app.services.dashboard import DashboardService


def test_dashboard_snapshot_counts_pipeline_and_urgent_actions(tmp_path: Path) -> None:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    db = Database(settings)
    db.create_schema()
    with db.session_scope() as session:
        states = [
            ApplicationState.QUEUED,
            ApplicationState.NEEDS_USER,
            ApplicationState.CONFIRMATION_VERIFIED,
            ApplicationState.NEEDS_OA,
            ApplicationState.BLOCKED,
            ApplicationState.FAILED_RETRYABLE,
            ApplicationState.OA_PENDING,
        ]
        for index, state in enumerate(states):
            role = Opportunity(
                employer=f"Firm {index}",
                role_title="Summer Analyst",
                cycle="2027",
                url=f"https://jobs.example.test/{index}",
                deadline=date.today() + timedelta(days=index + 1),
            )
            session.add(role)
            session.flush()
            session.add(
                Application(
                    opportunity_id=role.id,
                    state=state.value,
                    priority=90 - index,
                    next_action=(
                        "Complete OA"
                        if state in {ApplicationState.NEEDS_OA, ApplicationState.OA_PENDING}
                        else ""
                    ),
                    next_action_deadline=(
                        datetime.now(timezone.utc) + timedelta(hours=20)
                        if state == ApplicationState.OA_PENDING
                        else None
                    ),
                )
            )
        session.flush()

        snapshot = DashboardService(session).snapshot()

        assert snapshot.total_applications == 7
        assert snapshot.needs_user == 4
        assert snapshot.oa_pending == 2
        assert snapshot.submitted == 1
        assert snapshot.pipeline[ApplicationState.QUEUED.value] == 1
        assert snapshot.urgent_actions[0].employer == "Firm 6"
