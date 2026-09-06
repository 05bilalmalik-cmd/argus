from __future__ import annotations

import importlib
import json
import queue
import threading
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import cli
from app.automation.targets import TargetResolution
from app.automation.types import RunMode
from app.config import Settings
from app.db import Database
from app.domain.states import ApplicationState
from app.domain.targets import TargetKind
from app.models import Application, Opportunity
from app.services.navigator import HeadedSessionWorker, _SourceResolutionExecutor
from app.services.target_resolution import ResolutionContext


def _batch_module():
    try:
        return importlib.import_module("app.services.batch_target_resolution")
    except ModuleNotFoundError:
        pytest.fail("batch target-resolution service is not implemented")


def _database(tmp_path: Path) -> tuple[Settings, Database]:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    return settings, database


def _seed(database: Database, count: int) -> list[str]:
    identifiers: list[str] = []
    with database.session_scope() as session:
        for index in range(count):
            opportunity = Opportunity(
                employer=f"Employer {index}",
                role_title=f"Role {index}",
                cycle="2027",
                url=f"https://jobs{index}.example.test/jobs/role-{index}",
                source="trackr_live",
                ats_type="custom",
            )
            session.add(opportunity)
            session.flush()
            session.add(
                Application(
                    opportunity_id=opportunity.id,
                    state=ApplicationState.DISCOVERED.value,
                )
            )
            identifiers.append(opportunity.id)
    return identifiers


def _job_detail(context: ResolutionContext) -> TargetResolution:
    return TargetResolution(
        source_url=context.source_url,
        final_url=context.source_url,
        kind=TargetKind.JOB_DETAIL,
        reason_codes=("employer_job_detail",),
        evidence={"source_inspection": True},
    )


def _records(database: Database) -> dict[str, dict[str, object]]:
    with database.session_scope() as session:
        records = list(session.scalars(select(Opportunity).order_by(Opportunity.id)).all())
        return {
            item.id: {
                "target_status": item.target_status,
                "application_url": item.application_url,
                "resolved_ats_type": item.resolved_ats_type,
                "resolution_evidence_json": item.resolution_evidence_json,
                "resolved_at": item.resolved_at,
                "resolution_attempted_at": item.resolution_attempted_at,
            }
            for item in records
        }


def test_resolve_targets_parser_exposes_the_safe_batch_controls() -> None:
    try:
        args = cli.build_parser().parse_args(
            [
                "resolve-targets",
                "--limit",
                "7",
                "--employer",
                "Acme",
                "--ats",
                "greenhouse",
                "--programme",
                "spring_week",
                "--only-unattempted",
                "--dry-run",
                "--concurrency",
                "2",
                "--delay-seconds",
                "1.5",
            ]
        )
    except SystemExit:
        pytest.fail("resolve-targets CLI command is not implemented")

    assert args.limit == 7
    assert args.employer == "Acme"
    assert args.ats == "greenhouse"
    assert args.programme == "spring_week"
    assert args.only_unattempted is True
    assert args.retry_failed is False
    assert args.dry_run is True
    assert args.concurrency == 2
    assert args.delay_seconds == 1.5
    assert RunMode.SUBMIT.value not in vars(args).values()


def test_unattempted_selection_uses_programme_deadline_fifo_and_id_order(
    tmp_path: Path,
) -> None:
    batch = _batch_module()
    settings, database = _database(tmp_path)
    created = datetime(2026, 8, 27, 1, 0, tzinfo=timezone.utc)
    rows = (
        (
            "yii",
            "year_in_industry",
            date(2026, 11, 30),
            created + timedelta(hours=6),
            "01-yii",
        ),
        (
            "spring-early-a",
            "spring_week",
            date(2026, 9, 1),
            created + timedelta(hours=7),
            "02-spring-a",
        ),
        (
            "spring-early-b",
            "spring_week",
            date(2026, 9, 1),
            created + timedelta(hours=7),
            "03-spring-b",
        ),
        (
            "spring-late",
            "spring_week",
            date(2026, 10, 1),
            created + timedelta(hours=2),
            "04-spring-late",
        ),
        ("spring-null", "spring_week", None, created, "05-spring-null"),
        ("summer", "summer", date(2026, 8, 28), created, "06-summer"),
        (
            "other",
            "other",
            date(2026, 8, 27),
            created - timedelta(hours=1),
            "07-other",
        ),
    )
    with database.session_scope() as session:
        for employer, programme, deadline, created_at, identifier in rows:
            opportunity = Opportunity(
                id=identifier,
                employer=employer,
                role_title="Analyst",
                programme_group=programme,
                cycle="2027",
                url=f"https://{identifier}.example.test/jobs/analyst",
                source="test",
                ats_type="custom",
                deadline=deadline,
                application_window_status="OPEN",
                created_at=created_at,
            )
            session.add(opportunity)
            session.flush()
            session.add(
                Application(
                    opportunity_id=opportunity.id,
                    state=ApplicationState.DISCOVERED.value,
                )
            )

    selected = batch.BatchTargetResolutionDriver(database, settings)._select(
        batch.BatchResolveOptions(only_unattempted=True)
    )

    assert [item.opportunity_id for item in selected] == [
        "01-yii",
        "02-spring-a",
        "03-spring-b",
        "04-spring-late",
        "05-spring-null",
        "06-summer",
        "07-other",
    ]


def test_programme_filter_selects_only_requested_programme(tmp_path: Path) -> None:
    batch = _batch_module()
    settings, database = _database(tmp_path)
    with database.session_scope() as session:
        for programme in ("year_in_industry", "spring_week", "summer", "other"):
            opportunity = Opportunity(
                employer=programme,
                role_title="Analyst",
                programme_group=programme,
                cycle="2027",
                url=f"https://{programme}.example.test/jobs/analyst",
                source="test",
                ats_type="custom",
            )
            session.add(opportunity)
            session.flush()
            session.add(
                Application(
                    opportunity_id=opportunity.id,
                    state=ApplicationState.DISCOVERED.value,
                )
            )

    selected = batch.BatchTargetResolutionDriver(database, settings)._select(
        batch.BatchResolveOptions(programme="spring_week", only_unattempted=True)
    )

    assert [item.employer for item in selected] == ["spring_week"]


def test_resumability_after_interruption_skips_committed_rows(tmp_path: Path) -> None:
    batch = _batch_module()
    settings, database = _database(tmp_path)
    identifiers = _seed(database, 3)
    calls: list[str] = []

    def resolver(context: ResolutionContext) -> TargetResolution:
        calls.append(context.opportunity_id)
        return _job_detail(context)

    driver = batch.BatchTargetResolutionDriver(
        database,
        settings,
        resolver_factory=lambda: resolver,
    )

    def interrupt_after_first(_row) -> None:  # noqa: ANN001
        raise KeyboardInterrupt

    interrupted = driver.run(
        batch.BatchResolveOptions(concurrency=1, delay_seconds=0),
        progress=interrupt_after_first,
    )

    assert interrupted.interrupted is True
    first_pass = _records(database)
    completed = [
        identifier
        for identifier, record in first_pass.items()
        if record["resolution_attempted_at"] is not None
    ]
    assert len(completed) == 1
    assert first_pass[completed[0]]["target_status"] == TargetKind.JOB_DETAIL.value

    resumed = driver.run(batch.BatchResolveOptions(concurrency=1, delay_seconds=0))

    assert resumed.interrupted is False
    assert resumed.attempted == 2
    assert Counter(calls) == Counter(identifiers)
    assert all(
        record["target_status"] == TargetKind.JOB_DETAIL.value
        and record["resolution_attempted_at"] is not None
        for record in _records(database).values()
    )


def test_two_full_runs_are_idempotent(tmp_path: Path) -> None:
    batch = _batch_module()
    settings, database = _database(tmp_path)
    _seed(database, 2)
    calls = 0

    def resolver(context: ResolutionContext) -> TargetResolution:
        nonlocal calls
        calls += 1
        return _job_detail(context)

    driver = batch.BatchTargetResolutionDriver(
        database,
        settings,
        resolver_factory=lambda: resolver,
    )
    first = driver.run(batch.BatchResolveOptions(concurrency=1, delay_seconds=0))
    after_first = _records(database)
    second = driver.run(batch.BatchResolveOptions(concurrency=1, delay_seconds=0))

    assert first.attempted == 2
    assert second.selected == 0
    assert second.attempted == 0
    assert calls == 2
    assert _records(database) == after_first


def test_retry_failed_revisits_job_detail_rows_for_bounded_apply_follow(
    tmp_path: Path,
) -> None:
    batch = _batch_module()
    settings, database = _database(tmp_path)
    identifier = _seed(database, 1)[0]
    calls = 0

    def resolver(context: ResolutionContext) -> TargetResolution:
        nonlocal calls
        calls += 1
        return _job_detail(context)

    driver = batch.BatchTargetResolutionDriver(
        database,
        settings,
        resolver_factory=lambda: resolver,
    )
    first = driver.run(batch.BatchResolveOptions(concurrency=1, delay_seconds=0))
    assert first.rows[0].opportunity_id == identifier
    assert first.rows[0].target_status == TargetKind.JOB_DETAIL.value

    retry = driver.run(
        batch.BatchResolveOptions(
            retry_failed=True,
            concurrency=1,
            delay_seconds=0,
        )
    )

    assert retry.selected == 1
    assert retry.attempted == 1
    assert calls == 2


def test_retry_limit_replays_the_latest_attempted_cohort_not_new_rows(
    tmp_path: Path,
) -> None:
    batch = _batch_module()
    settings, database = _database(tmp_path)
    identifiers = _seed(database, 4)
    calls: list[str] = []

    def resolver(context: ResolutionContext) -> TargetResolution:
        calls.append(context.opportunity_id)
        if context.opportunity_id == identifiers[1]:
            return TargetResolution(
                source_url=context.source_url,
                final_url=context.source_url,
                kind=TargetKind.LISTING,
                reason_codes=("search_or_listing_page",),
            )
        if context.opportunity_id == identifiers[2]:
            return TargetResolution(
                source_url=context.source_url,
                final_url=context.source_url,
                kind=TargetKind.UNRESOLVED,
                reason_codes=("synthetic_unresolved",),
            )
        return _job_detail(context)

    driver = batch.BatchTargetResolutionDriver(
        database,
        settings,
        resolver_factory=lambda: resolver,
    )
    first = driver.run(
        batch.BatchResolveOptions(limit=3, concurrency=1, delay_seconds=0)
    )
    assert [row.opportunity_id for row in first.rows] == identifiers[:3]

    retry = driver.run(
        batch.BatchResolveOptions(
            retry_failed=True,
            limit=3,
            concurrency=1,
            delay_seconds=0,
        )
    )

    assert [row.opportunity_id for row in retry.rows] == identifiers[:3]
    assert identifiers[3] not in calls
    assert retry.selected == 3
    assert retry.attempted == 2
    assert retry.target_histogram == Counter(
        {
            TargetKind.JOB_DETAIL.value: 1,
            TargetKind.LISTING.value: 1,
            TargetKind.UNRESOLVED.value: 1,
        }
    )


def test_one_resolver_failure_is_recorded_and_does_not_abort_batch(tmp_path: Path) -> None:
    batch = _batch_module()
    settings, database = _database(tmp_path)
    identifiers = _seed(database, 3)
    failing_id = identifiers[1]

    def resolver(context: ResolutionContext) -> TargetResolution:
        if context.opportunity_id == failing_id:
            raise RuntimeError("synthetic network failure")
        return _job_detail(context)

    report = batch.BatchTargetResolutionDriver(
        database,
        settings,
        resolver_factory=lambda: resolver,
    ).run(batch.BatchResolveOptions(concurrency=1, delay_seconds=0))

    records = _records(database)
    assert report.attempted == 3
    assert report.failure_reasons["resolver_failed"] == 1
    assert records[failing_id]["target_status"] == TargetKind.UNRESOLVED.value
    assert records[failing_id]["resolution_attempted_at"] is not None
    failed_evidence = json.loads(str(records[failing_id]["resolution_evidence_json"]))
    assert failed_evidence["reason_codes"] == ["resolver_failed"]
    assert failed_evidence["evidence"]["resolver_error"] == "runtimeerror"
    assert sum(
        record["target_status"] == TargetKind.JOB_DETAIL.value
        for record in records.values()
    ) == 2


def test_dry_run_writes_nothing_and_never_constructs_a_resolver(tmp_path: Path) -> None:
    batch = _batch_module()
    settings, database = _database(tmp_path)
    _seed(database, 2)
    before = _records(database)

    def forbidden_factory():
        pytest.fail("dry-run must not construct or invoke a navigator resolver")

    report = batch.BatchTargetResolutionDriver(
        database,
        settings,
        resolver_factory=forbidden_factory,
    ).run(batch.BatchResolveOptions(dry_run=True, concurrency=1, delay_seconds=0))

    assert report.dry_run is True
    assert report.selected == 2
    assert report.attempted == 0
    assert _records(database) == before


def test_attempted_failure_and_never_attempted_are_distinct_and_retry_is_opt_in(
    tmp_path: Path,
) -> None:
    batch = _batch_module()
    settings, database = _database(tmp_path)
    identifiers = _seed(database, 2)

    def failing(_context: ResolutionContext) -> TargetResolution:
        raise TimeoutError("synthetic timeout")

    driver = batch.BatchTargetResolutionDriver(
        database,
        settings,
        resolver_factory=lambda: failing,
    )
    first = driver.run(
        batch.BatchResolveOptions(limit=1, concurrency=1, delay_seconds=0)
    )
    after_first = _records(database)

    attempted = [
        identifier
        for identifier in identifiers
        if after_first[identifier]["resolution_attempted_at"] is not None
    ]
    untouched = [identifier for identifier in identifiers if identifier not in attempted]
    assert first.attempted == 1
    assert len(attempted) == len(untouched) == 1
    assert after_first[attempted[0]]["target_status"] == TargetKind.UNRESOLVED.value
    assert json.loads(
        str(after_first[attempted[0]]["resolution_evidence_json"])
    )["reason_codes"] == ["resolver_failed"]
    assert after_first[untouched[0]]["resolution_attempted_at"] is None

    second = driver.run(
        batch.BatchResolveOptions(limit=1, concurrency=1, delay_seconds=0)
    )
    assert second.attempted == 1
    assert _records(database)[untouched[0]]["resolution_attempted_at"] is not None

    retried = driver.run(
        batch.BatchResolveOptions(
            limit=1,
            retry_failed=True,
            concurrency=1,
            delay_seconds=0,
        )
    )
    assert retried.attempted == 1


class _Request:
    def __init__(
        self,
        *,
        url: str,
        method: str = "GET",
        resource_type: str = "document",
        post_data: str = "",
        headers: dict[str, str] | None = None,
        redirected_from=None,  # noqa: ANN001
    ) -> None:
        self.url = url
        self.method = method
        self.resource_type = resource_type
        self.post_data = post_data
        self.headers = headers or {"accept": "text/html"}
        self.redirected_from = redirected_from


class _Route:
    def __init__(self, request: _Request) -> None:
        self.request = request
        self.continued = 0
        self.aborted: list[str] = []

    def continue_(self) -> None:
        self.continued += 1

    def abort(self, reason: str) -> None:
        self.aborted.append(reason)


def _source_worker(*, url: str, executor=None) -> HeadedSessionWorker:  # noqa: ANN001
    worker = HeadedSessionWorker(
        session_id="batch-source-egress",
        application_id="application-1",
        mode=RunMode.REVIEW.value,
        url=url,
        summary={"source_resolution": True, "provider": ""},
        command_queue=queue.Queue(),
        event_queue=queue.Queue(),
        ttl_seconds=30,
        headless=True,
        allowlist=frozenset({"jobs.example.com", "grnh.se"}),
        journey_executor=executor,
    )
    worker.owner_thread_id = threading.get_ident()
    return worker


@pytest.mark.parametrize(
    "outgoing_request",
    [
        _Request(
            url="https://jobs.example.com/application",
            method="POST",
            post_data="next=true",
        ),
        _Request(url="https://jobs.example.com/application?email=person@example.test"),
        _Request(
            url="https://jobs.example.com/application",
            headers={"authorization": "Bearer candidate-secret"},
        ),
    ],
)
def test_resolution_capability_still_refuses_post_data_and_pii(
    outgoing_request: _Request,
) -> None:
    route = _Route(outgoing_request)
    worker = _source_worker(url="https://jobs.example.com/job/123")

    worker._route(route)

    assert route.continued == 0
    assert route.aborted == ["blockedbyclient"]
    assert worker._egress_records[-1]["fatal"] is True


def test_single_row_capability_is_revoked_after_resolver_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = _batch_module()
    navigator_module = importlib.import_module("app.services.navigator")
    monkeypatch.setattr(navigator_module, "safe_public_navigation_url", lambda _url: True)
    settings, database = _database(tmp_path)
    events: list[str] = []
    issued: list[object] = []
    original_issue = navigator_module.SourceResolutionCapability.issue.__func__

    def tracked_issue(cls, source_url: str):  # noqa: ANN001
        capability = original_issue(cls, source_url)
        issued.append(capability)
        return capability

    monkeypatch.setattr(
        navigator_module.SourceResolutionCapability,
        "issue",
        classmethod(tracked_issue),
    )

    class FailingNavigator:
        def resolve_application_target(self, _application_id, **kwargs):  # noqa: ANN001
            assert kwargs["source_capability"].active is True
            events.append("resolve")
            raise RuntimeError("synthetic navigator failure")

        def shutdown(self) -> None:
            assert issued[-1].active is True
            events.append("shutdown")

    resolver = batch.SingleUseNavigatorTargetResolver(
        database,
        settings,
        navigator_factory=lambda *_args, **_kwargs: FailingNavigator(),
    )
    context = ResolutionContext(
        opportunity_id="opportunity-1",
        application_id="application-1",
        source_url="https://jobs.example.com/job/123",
        employer="Example Employer",
        role_title="Analyst",
        cycle="2027",
        provider_hint="custom",
        source="trackr_live",
    )

    with pytest.raises(RuntimeError, match="synthetic navigator failure"):
        resolver(context)

    assert events == ["resolve", "shutdown"]
    assert len(issued) == 1
    assert issued[0].active is False


@pytest.mark.parametrize(
    "url",
    [
        "http://jobs.example.com/job/123",
        "https://127.0.0.1/job/123",
        "https://10.0.0.8/job/123",
        "https://169.254.10.10/job/123",
    ],
)
def test_source_capability_refuses_non_public_or_non_https_urls(url: str) -> None:
    navigator_module = importlib.import_module("app.services.navigator")
    capability_type = getattr(navigator_module, "SourceResolutionCapability", None)
    assert capability_type is not None, "source capability is not implemented"

    with pytest.raises(ValueError, match="public HTTPS"):
        capability_type.issue(url)


class _CustomPage:
    url = "https://higher.gs.com/jobs/analyst"

    def content(self) -> str:
        return "<html><main><h1>Analyst opportunity</h1></main></html>"

    def evaluate(self, _script, *_args):  # noqa: ANN001
        return {}


@pytest.mark.parametrize("provider_hint", ["custom", "unknown", "generic", "other"])
def test_generic_source_hint_does_not_override_final_url_classification(
    provider_hint: str,
) -> None:
    executor = _SourceResolutionExecutor(
        source_url="https://higher.gs.com/jobs/analyst",
        provider_hint=provider_hint,
        employer="Goldman Sachs",
        role_title="Analyst",
    )

    executor.prepare(_CustomPage())

    assert executor.resolution is not None
    assert executor.resolution.kind == TargetKind.JOB_DETAIL
    assert executor.resolution.provider == ""


def test_shortlink_records_full_direct_redirect_chain_and_classifies_final_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    navigator_module = importlib.import_module("app.services.navigator")
    monkeypatch.setattr(navigator_module, "safe_public_navigation_url", lambda _url: True)
    source = "https://grnh.se/twyh9job1us"
    final = "https://higher.gs.com/jobs/analyst"
    executor = _SourceResolutionExecutor(
        source_url=source,
        provider_hint="greenhouse_shortlink",
        employer="Goldman Sachs",
        role_title="Analyst",
    )
    initial = _Request(url=source)
    redirected = _Request(url=final, redirected_from=initial)
    route = _Route(redirected)
    worker = _source_worker(url=source, executor=executor)

    worker._route(route)
    executor.prepare(_CustomPage())

    assert route.continued == 1
    assert executor.resolution is not None
    assert executor.resolution.final_url == final
    assert executor.resolution.kind == TargetKind.JOB_DETAIL
    assert list(executor.resolution.evidence["redirect_chain"]) == [source, final]
    assert executor.resolution.provider != "greenhouse"


def test_shortlink_query_cannot_launder_an_arbitrary_redirect_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    navigator_module = importlib.import_module("app.services.navigator")
    monkeypatch.setattr(navigator_module, "safe_public_navigation_url", lambda _url: True)
    source = "https://grnh.se/redirect?next=https://attacker.example"
    initial = _Request(url=source)
    redirected = _Request(
        url="https://attacker.example/application",
        redirected_from=initial,
    )
    route = _Route(redirected)

    _source_worker(url=source)._route(route)

    assert route.continued == 0
    assert route.aborted == ["blockedbyclient"]


def test_cli_reports_when_verified_identity_backfills_placeholder_employer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings, _database_instance = _database(tmp_path)

    class _Driver:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def run(self, _options, *, progress):
            progress(
                SimpleNamespace(
                    index=1,
                    opportunity_id="opportunity-1",
                    employer="Unknown",
                    role_title="Analyst",
                    target_status=TargetKind.APPLICATION_ENTRY.value,
                    reason_codes=("direct_ats_job",),
                    employer_backfilled=True,
                    established_employer="Acme Capital",
                )
            )
            return SimpleNamespace(
                target_histogram=Counter({TargetKind.APPLICATION_ENTRY.value: 1}),
                failure_reasons=Counter(),
                selected=1,
                attempted=1,
                interrupted=False,
                fatal_safety_violation=None,
            )

    monkeypatch.setattr(cli.Settings, "load", lambda: settings)
    monkeypatch.setattr(cli, "BatchTargetResolutionDriver", _Driver)
    args = SimpleNamespace(
        limit=1,
        employer="",
        ats="",
        only_unattempted=False,
        retry_failed=True,
        dry_run=False,
        concurrency=1,
        delay_seconds=0,
    )

    assert cli._command_resolve_targets(args) == 0

    output = capsys.readouterr().out
    assert "--backfill-employer: Acme Capital" in output
    assert RunMode.SUBMIT.value not in output


def test_cli_reports_fatal_apply_click_boundary_violation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings, _database_instance = _database(tmp_path)

    class _Driver:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def run(self, _options, *, progress):
            return SimpleNamespace(
                target_histogram=Counter(),
                failure_reasons=Counter(),
                selected=1,
                attempted=0,
                interrupted=True,
                fatal_safety_violation=(
                    "Apply click landed on a confirmation/submitted page"
                ),
            )

    monkeypatch.setattr(cli.Settings, "load", lambda: settings)
    monkeypatch.setattr(cli, "BatchTargetResolutionDriver", _Driver)
    args = SimpleNamespace(
        limit=1,
        employer="",
        ats="",
        only_unattempted=False,
        retry_failed=True,
        dry_run=False,
        concurrency=1,
        delay_seconds=0,
    )

    assert cli._command_resolve_targets(args) == 130

    output = capsys.readouterr().out
    assert "FATAL SAFETY VIOLATION:" in output
    assert "confirmation/submitted page" in output
