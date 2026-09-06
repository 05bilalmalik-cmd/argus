from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


def _outer_probe(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    probe = textwrap.dedent(
        """
        import json
        import os
        import subprocess
        import sys
        import time

        from app.services.process_containment import OwnedProcessTree

        descendant_script = "import time; time.sleep(300)"
        root_script = (
            "import json,subprocess,sys,time; "
            "child=subprocess.Popen([sys.executable,'-c',%r]); "
            "print(json.dumps({'descendant_pid':child.pid}), flush=True); "
            "time.sleep(300)"
        ) % descendant_script
        tree = OwnedProcessTree.spawn(
            [sys.executable, "-c", root_script],
            cwd=os.getcwd(),
            env=dict(os.environ),
        )
        line = tree.receive_line(timeout_seconds=5.0, max_bytes=4096)
        descendant_pid = int(json.loads(line)["descendant_pid"])
        result = tree.terminate_and_reap(timeout_seconds=8.0)
        print(json.dumps({
            "forced": result.forced,
            "verified_empty": result.verified_empty,
            "descendant_pid": descendant_pid,
        }), flush=True)
        """
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.getcwd()
    return subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )


def test_owned_process_tree_forcibly_reaps_root_and_descendant(tmp_path: Path) -> None:
    result = _outer_probe(tmp_path)

    assert result.returncode == 0, result.stderr
    payload = __import__("json").loads(result.stdout.strip().splitlines()[-1])
    assert payload["forced"] is True
    assert payload["verified_empty"] is True


def test_row_timeout_must_be_finite_and_positive() -> None:
    from app.services.batch_target_resolution import BatchResolveOptions

    with pytest.raises(ValueError, match="row-timeout"):
        BatchResolveOptions(row_timeout_seconds=0)
    with pytest.raises(ValueError, match="row-timeout"):
        BatchResolveOptions(row_timeout_seconds=float("inf"))


def test_default_row_timeout_is_bounded() -> None:
    from app.services.batch_target_resolution import BatchResolveOptions

    options = BatchResolveOptions()

    assert 10 <= options.row_timeout_seconds <= 120


def test_timed_out_owner_tree_is_reaped_and_later_row_commits(
    tmp_path: Path,
) -> None:
    import json

    from sqlalchemy import select

    from app.automation.targets import TargetResolution
    from app.config import Settings
    from app.db import Database
    from app.domain.targets import TargetKind
    from app.models import Application, Opportunity
    from app.services.batch_target_resolution import (
        BatchResolveOptions,
        BatchTargetResolutionDriver,
        NavigationRowTimeout,
    )
    from app.services.process_containment import OwnedProcessTree

    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    with database.session_scope() as session:
        opportunities = [
            Opportunity(
                employer=f"Employer {index}",
                role_title=f"Role {index}",
                cycle="2027",
                url=f"https://example.test/jobs/{index}",
                source="test",
                ats_type="unknown",
                application_window_status="OPEN",
            )
            for index in (1, 2)
        ]
        session.add_all(opportunities)
        session.flush()
        session.add_all(
            Application(opportunity_id=item.id, state="DISCOVERED")
            for item in opportunities
        )
        identifiers = [item.id for item in opportunities]

    calls = 0
    tree_results = []

    def resolver_factory():  # noqa: ANN202 - driver test seam
        def resolve(context):  # noqa: ANN001, ANN202 - driver test seam
            nonlocal calls
            calls += 1
            if calls == 1:
                descendant_script = "import time; time.sleep(300)"
                root_script = (
                    "import json,subprocess,sys,time; "
                    "child=subprocess.Popen([sys.executable,'-c',%r]); "
                    "print(json.dumps({'descendant_pid':child.pid}), flush=True); "
                    "time.sleep(300)"
                ) % descendant_script
                tree = OwnedProcessTree.spawn(
                    [sys.executable, "-c", root_script],
                    cwd=tmp_path,
                    env=dict(os.environ),
                )
                tree.receive_line(timeout_seconds=5.0, max_bytes=4096)
                with pytest.raises(TimeoutError):
                    tree.receive_line(timeout_seconds=0.1, max_bytes=4096)
                tree_result = tree.terminate_and_reap(timeout_seconds=8.0)
                tree_results.append(tree_result)
                raise NavigationRowTimeout("simulated owner never returned")
            return TargetResolution(
                source_url=context.source_url,
                final_url=context.source_url,
                kind=TargetKind.JOB_DETAIL,
                reason_codes=("unverified_job_detail",),
            )

        return resolve

    report = BatchTargetResolutionDriver(
        database,
        settings,
        resolver_factory=resolver_factory,
    ).run(
        BatchResolveOptions(concurrency=1, delay_seconds=0, row_timeout_seconds=1)
    )

    assert report.selected == 2
    assert report.attempted == 2
    assert report.interrupted is False
    assert len(tree_results) == 1
    assert tree_results[0].verified_empty is True
    with database.session_scope() as session:
        rows = list(
            session.scalars(
                select(Opportunity)
                .where(Opportunity.id.in_(identifiers))
                .order_by(Opportunity.url)
            ).all()
        )
        first_evidence = json.loads(rows[0].resolution_evidence_json)
        assert rows[0].resolution_attempted_at is not None
        assert first_evidence["reason_codes"] == ["resolver_failed"]
        assert first_evidence["evidence"]["resolver_error"] == (
            "navigationrowtimeout"
        )
        assert rows[1].resolution_attempted_at is not None
        assert rows[1].target_status == TargetKind.JOB_DETAIL.value
