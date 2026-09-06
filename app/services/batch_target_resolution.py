from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from sqlalchemy import case, func, or_, select

from app.config import Settings
from app.db import Database
from app.domain.states import (
    UserApplicationStatus,
    user_status_is_automation_eligible,
)
from app.domain.targets import TargetKind
from app.models import Application, Opportunity
from app.scouting.application_window import ApplicationWindowStatus
from app.scouting.programmes import ProgrammeType
from app.services.target_resolution import (
    NavigatorTargetResolver,
    ResolutionContext,
    TargetResolutionService,
    UserStatusAutomationExcludedError,
    _resolution_context,
)
from app.services.resolution_apply_click import (
    ApplyClickBudget,
    ApplyClickSafetyViolation,
)
from app.services.process_containment import OwnedProcessTree


_FAILURE_KINDS = frozenset(
    {
        TargetKind.UNRESOLVED.value,
        TargetKind.MISMATCH.value,
        TargetKind.BLOCKED.value,
        TargetKind.MULTIPLE_CANDIDATE_ROLES.value,
        TargetKind.AUTH_WALL.value,
        TargetKind.HUMAN_CHALLENGE.value,
        TargetKind.NON_HTML.value,
    }
)

_PROGRAMME_ORDER = tuple(ProgrammeType)
_SELECTABLE_PROGRAMMES = frozenset(
    programme.value
    for programme in _PROGRAMME_ORDER
    if programme is not ProgrammeType.OTHER
)


@dataclass(frozen=True, slots=True)
class BatchResolveOptions:
    limit: int | None = None
    employer: str = ""
    ats: str = ""
    programme: str = "all"
    only_unattempted: bool = False
    retry_failed: bool = False
    dry_run: bool = False
    concurrency: int = 2
    delay_seconds: float = 2.0
    row_timeout_seconds: float = 45.0

    def __post_init__(self) -> None:
        if self.limit is not None and self.limit < 1:
            raise ValueError("--limit must be a positive integer")
        if not 1 <= int(self.concurrency) <= 8:
            raise ValueError("--concurrency must be between 1 and 8")
        if not math.isfinite(float(self.delay_seconds)) or self.delay_seconds < 0:
            raise ValueError("--delay-seconds must be a finite non-negative number")
        if (
            not math.isfinite(float(self.row_timeout_seconds))
            or self.row_timeout_seconds <= 0
        ):
            raise ValueError("--row-timeout-seconds must be finite and positive")
        if self.only_unattempted and self.retry_failed:
            raise ValueError("--only-unattempted and --retry-failed are mutually exclusive")
        object.__setattr__(self, "employer", str(self.employer or "").strip())
        object.__setattr__(self, "ats", str(self.ats or "").strip().casefold())
        programme = str(self.programme or "all").strip().casefold()
        if programme != "all" and programme not in _SELECTABLE_PROGRAMMES:
            allowed = ", ".join((*sorted(_SELECTABLE_PROGRAMMES), "all"))
            raise ValueError(f"--programme must be one of: {allowed}")
        object.__setattr__(self, "programme", programme)


@dataclass(frozen=True, slots=True)
class BatchWorkItem:
    index: int
    opportunity_id: str
    application_id: str | None
    employer: str
    role_title: str
    source_url: str


@dataclass(frozen=True, slots=True)
class BatchRowResult:
    index: int
    opportunity_id: str
    employer: str
    role_title: str
    target_status: str
    reason_codes: tuple[str, ...]
    attempted: bool
    application_url: str | None = None
    error_type: str = ""
    employer_backfilled: bool = False
    established_employer: str = ""


@dataclass(frozen=True, slots=True)
class BatchResolveReport:
    selected: int
    attempted: int
    dry_run: bool
    interrupted: bool
    rows: tuple[BatchRowResult, ...]
    target_histogram: Counter[str]
    failure_reasons: Counter[str]
    fatal_safety_violation: str = ""


class SingleUseNavigatorTargetResolver:
    """Resolve one row with one Navigator and one revocable source grant."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        navigator_factory: Callable[..., Any] | None = None,
        apply_click_budget: ApplyClickBudget | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        self.navigator_factory = navigator_factory
        self.apply_click_budget = apply_click_budget

    def __call__(self, context: ResolutionContext) -> Any:
        if not context.application_id:
            return None
        from app.services.navigator import (
            ApplicationNavigator,
            SourceResolutionCapability,
        )

        capability = SourceResolutionCapability.issue(
            context.inspection_url or context.source_url
        )
        factory = self.navigator_factory or ApplicationNavigator
        navigator = factory(
            self.database,
            self.settings,
            headless=True,
            apply_click_budget=self.apply_click_budget,
        )
        try:
            return NavigatorTargetResolver(
                navigator,
                source_capability=capability,
                headed=False,
            )(context)
        finally:
            try:
                navigator.shutdown()
            finally:
                capability.revoke()


class NavigationRowTimeout(TimeoutError):
    """A contained row exceeded its wall-clock navigation bound."""


class ProcessContainmentError(RuntimeError):
    """The exact row process tree could not be proven empty."""


class IsolatedNavigatorTargetResolver:
    """Run one Navigator in a one-shot, killable child process tree."""

    def __init__(
        self,
        settings: Settings,
        *,
        timeout_seconds: float,
        apply_click_budget: ApplyClickBudget | None,
    ) -> None:
        self.settings = settings
        self.timeout_seconds = float(timeout_seconds)
        self.apply_click_budget = apply_click_budget

    @staticmethod
    def _resolution(payload: Mapping[str, object] | None) -> Any:
        if payload is None:
            return None
        from app.automation.targets import TargetResolution

        return TargetResolution(
            source_url=str(payload.get("source_url") or ""),
            final_url=str(payload.get("final_url") or ""),
            kind=TargetKind(str(payload.get("kind") or TargetKind.UNRESOLVED.value)),
            provider=str(payload.get("provider") or ""),
            identity_verified=bool(payload.get("identity_verified")),
            form_verified=bool(payload.get("form_verified")),
            reason_codes=tuple(
                str(item) for item in (payload.get("reason_codes") or ())
            ),
            evidence=(
                payload.get("evidence")
                if isinstance(payload.get("evidence"), Mapping)
                else {}
            ),
        )

    def __call__(self, context: ResolutionContext) -> Any:
        # Reserving before spawn makes the process-wide cap conservative: a
        # timed-out row consumes its lease even when no click can be proven.
        click_authorized = bool(
            self.apply_click_budget is not None
            and self.apply_click_budget.try_consume()
        )
        request = {
            "context": asdict(context),
            "data_dir": str(self.settings.data_dir),
            "apply_click_authorized": click_authorized,
        }
        env = dict(os.environ)
        repository_root = Path(__file__).resolve().parents[2]
        env["PYTHONPATH"] = os.pathsep.join(
            value
            for value in (str(repository_root), env.get("PYTHONPATH", ""))
            if value
        )
        tree = OwnedProcessTree.spawn(
            [sys.executable, "-m", "app.services.target_resolution_child"],
            cwd=repository_root,
            env=env,
        )
        try:
            tree.send_line(json.dumps(request, separators=(",", ":")))
            line = tree.receive_line(
                timeout_seconds=self.timeout_seconds,
                max_bytes=1_048_576,
            )
            payload = json.loads(line)
            if not isinstance(payload, Mapping):
                raise ValueError("Contained resolver response is not an object")
            tree.send_line("ACK", max_bytes=32)
            reaped = tree.wait_and_reap(timeout_seconds=8.0)
            if not reaped.verified_empty:
                raise ProcessContainmentError(
                    "Contained resolver exited without a clean process-tree proof"
                )
            if not bool(payload.get("ok")):
                error_type = str(payload.get("error_type") or "child_error")
                if bool(payload.get("fatal_safety_violation")) or error_type == (
                    "applyclicksafetyviolation"
                ):
                    raise ApplyClickSafetyViolation(
                        "Apply click crossed the confirmation/submission boundary; "
                        "priority run halted",
                        evidence=(
                            payload.get("apply_click")
                            if isinstance(payload.get("apply_click"), Mapping)
                            else None
                        ),
                    )
                raise RuntimeError(f"Contained resolver failed: {error_type}")
            resolution_payload = payload.get("resolution")
            resolution = self._resolution(
                resolution_payload if isinstance(resolution_payload, Mapping) else None
            )
            handoff = payload.get("handoff")
            return resolution, handoff if isinstance(handoff, Mapping) else {}
        except TimeoutError as exc:
            reaped = tree.terminate_and_reap(timeout_seconds=8.0)
            if not reaped.verified_empty:
                raise ProcessContainmentError(
                    "Timed-out resolver process tree could not be proven empty"
                ) from exc
            raise NavigationRowTimeout(
                f"Navigation row exceeded {self.timeout_seconds:g} seconds"
            ) from exc
        except BaseException:
            if tree.process.poll() is None:
                reaped = tree.terminate_and_reap(timeout_seconds=8.0)
                if not reaped.verified_empty:
                    raise ProcessContainmentError(
                        "Failed resolver process tree could not be proven empty"
                    )
            raise


class _NavigationRateLimiter:
    def __init__(
        self,
        delay_seconds: float,
        *,
        sleep: Callable[[float], None],
        monotonic: Callable[[], float],
    ) -> None:
        self.delay_seconds = float(delay_seconds)
        self.sleep = sleep
        self.monotonic = monotonic
        self._lock = threading.Lock()
        self._next_start = 0.0

    def wait(self) -> None:
        if self.delay_seconds <= 0:
            return
        with self._lock:
            now = self.monotonic()
            remaining = self._next_start - now
            if remaining > 0:
                self.sleep(remaining)
                now = self.monotonic()
            self._next_start = max(now, self._next_start) + self.delay_seconds


class BatchTargetResolutionDriver:
    """Select, resolve, and commit each target as an independent row."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        resolver_factory: Callable[[], Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.database = database
        self.settings = settings
        self._custom_resolver_factory = resolver_factory
        self._use_process_isolation = resolver_factory is None
        self._row_timeout_seconds = 45.0
        self.apply_click_budget = (
            ApplyClickBudget(int(getattr(settings, "apply_click_run_cap", 25)))
            if bool(getattr(settings, "apply_click_enabled", False))
            else None
        )
        self.resolver_factory = resolver_factory or (
            lambda: SingleUseNavigatorTargetResolver(
                database,
                settings,
                apply_click_budget=self.apply_click_budget,
            )
        )
        self.sleep = sleep
        self.monotonic = monotonic
        self._fatal_stop = threading.Event()

    def _select(self, options: BatchResolveOptions) -> list[BatchWorkItem]:
        from app.models import OpportunityArchive

        with self.database.session_scope() as session:
            statement = (
                select(Opportunity, Application.id)
                .outerjoin(Application, Application.opportunity_id == Opportunity.id)
                .where(
                    Opportunity.application_window_status
                    == ApplicationWindowStatus.OPEN.value,
                    Opportunity.user_status.in_(
                        (
                            UserApplicationStatus.INTERESTED.value,
                            UserApplicationStatus.NOT_APPLIED.value,
                        )
                    ),
                    ~Opportunity.archive_record.has(
                        OpportunityArchive.archived_at.is_not(None)
                    )
                )
            )
            if options.retry_failed:
                statement = statement.where(
                    Opportunity.resolution_attempted_at.is_not(None)
                )
                if options.limit is None:
                    statement = statement.where(
                        Opportunity.target_status.in_(
                            (
                                TargetKind.UNRESOLVED.value,
                                TargetKind.JOB_DETAIL.value,
                                TargetKind.MULTIPLE_CANDIDATE_ROLES.value,
                            )
                        )
                    )
            else:
                statement = statement.where(
                    Opportunity.target_status == TargetKind.UNRESOLVED.value,
                    Opportunity.resolution_attempted_at.is_(None),
                )
            if options.employer:
                statement = statement.where(
                    func.lower(Opportunity.employer).contains(
                        options.employer.casefold()
                    )
                )
            if options.ats:
                ats_values = (
                    ("greenhouse", "greenhouse_shortlink")
                    if options.ats == "greenhouse"
                    else (options.ats,)
                )
                statement = statement.where(
                    or_(
                        func.lower(Opportunity.ats_type).in_(ats_values),
                        func.lower(Opportunity.resolved_ats_type).in_(ats_values),
                    )
                )
            if options.programme != "all":
                statement = statement.where(
                    Opportunity.programme_group == options.programme
                )
            status_priority = case(
                {
                    UserApplicationStatus.INTERESTED.value: 0,
                    UserApplicationStatus.NOT_APPLIED.value: 1,
                },
                value=Opportunity.user_status,
                else_=2,
            )
            if options.retry_failed:
                statement = statement.order_by(
                    status_priority,
                    Opportunity.resolution_attempted_at.desc(),
                    Opportunity.created_at.desc(),
                    Opportunity.id.desc(),
                )
            else:
                programme_priority = case(
                    {
                        programme.value: priority
                        for priority, programme in enumerate(_PROGRAMME_ORDER)
                    },
                    value=Opportunity.programme_group,
                    else_=len(_PROGRAMME_ORDER),
                )
                statement = statement.order_by(
                    status_priority,
                    programme_priority,
                    Opportunity.deadline.is_(None),
                    Opportunity.deadline,
                    Opportunity.created_at,
                    Opportunity.id,
                )
            if options.limit is not None:
                statement = statement.limit(options.limit)
            rows = list(session.execute(statement).all())
            if options.retry_failed:
                # Execute the latest cohort selection descending, then present
                # and process it in its original chronological order.
                rows.reverse()
            return [
                BatchWorkItem(
                    index=index,
                    opportunity_id=opportunity.id,
                    application_id=application_id,
                    employer=str(opportunity.employer or ""),
                    role_title=str(opportunity.role_title or ""),
                    source_url=str(opportunity.url or ""),
                )
                for index, (opportunity, application_id) in enumerate(rows, start=1)
            ]

    @staticmethod
    def _reason_codes(record: Opportunity) -> tuple[str, ...]:
        try:
            evidence = json.loads(record.resolution_evidence_json or "{}")
        except (TypeError, ValueError):
            return ("resolution_evidence_invalid",)
        if not isinstance(evidence, Mapping):
            return ("resolution_evidence_invalid",)
        values = evidence.get("reason_codes") or ()
        if not isinstance(values, (list, tuple)):
            return ("resolution_evidence_invalid",)
        return tuple(str(value) for value in values if str(value))

    def _result_from_record(
        self,
        item: BatchWorkItem,
        record: Opportunity,
        *,
        attempted: bool,
        error_type: str = "",
    ) -> BatchRowResult:
        employer_backfilled = False
        established_employer = ""
        try:
            evidence = json.loads(record.resolution_evidence_json or "{}")
        except (TypeError, ValueError):
            evidence = {}
        if isinstance(evidence, Mapping):
            backfill = evidence.get("employer_backfill")
            if isinstance(backfill, Mapping):
                established_employer = str(
                    backfill.get("established_employer") or ""
                ).strip()
                employer_backfilled = bool(established_employer)
        return BatchRowResult(
            index=item.index,
            opportunity_id=item.opportunity_id,
            employer=item.employer,
            role_title=item.role_title,
            target_status=str(record.target_status or TargetKind.UNRESOLVED.value),
            reason_codes=self._reason_codes(record),
            attempted=attempted,
            application_url=record.application_url,
            error_type=error_type,
            employer_backfilled=employer_backfilled,
            established_employer=established_employer,
        )

    def _record_outer_failure(
        self,
        item: BatchWorkItem,
        exc: BaseException,
    ) -> BatchRowResult:
        error_type = type(exc).__name__.casefold()
        reason = (
            "batch_interrupted"
            if isinstance(exc, (KeyboardInterrupt, SystemExit))
            else "batch_exception"
        )
        with self.database.session_scope() as session:
            if isinstance(exc, UserStatusAutomationExcludedError):
                record = session.get(Opportunity, item.opportunity_id)
                if record is None:
                    raise KeyError(item.opportunity_id)
                return self._result_from_record(item, record, attempted=False)
            record = TargetResolutionService(session).record_failure(
                item.opportunity_id,
                reason_code=reason,
                error_type=error_type,
            )
            return self._result_from_record(
                item,
                record,
                attempted=True,
                error_type=error_type,
            )

    def _record_fatal_apply_click(
        self,
        item: BatchWorkItem,
        error: ApplyClickSafetyViolation,
    ) -> None:
        evidence = getattr(error, "audit_evidence", {})
        try:
            with self.database.session_scope() as session:
                TargetResolutionService(session).record_apply_click_safety_violation(
                    item.opportunity_id,
                    evidence=evidence if isinstance(evidence, Mapping) else None,
                )
        except Exception as audit_error:  # noqa: BLE001 - preserve fatal stop
            raise ApplyClickSafetyViolation(
                "Apply click crossed the confirmation/submission boundary and its "
                "audit could not be persisted; priority run halted",
                evidence=evidence if isinstance(evidence, Mapping) else None,
            ) from audit_error

    def _process(
        self,
        item: BatchWorkItem,
        limiter: _NavigationRateLimiter,
    ) -> BatchRowResult:
        try:
            # Read and validate the row in a short transaction.  Browser work
            # happens after it closes; the result is revalidated against a
            # fresh context immediately before the parent-only write.
            with self.database.session_scope() as session:
                opportunity = session.get(Opportunity, item.opportunity_id)
                if opportunity is None:
                    raise KeyError(item.opportunity_id)
                if not user_status_is_automation_eligible(opportunity.user_status):
                    return self._result_from_record(
                        item,
                        opportunity,
                        attempted=False,
                    )
                if not opportunity.is_open_for_applications or opportunity.is_archived:
                    return self._result_from_record(
                        item,
                        opportunity,
                        attempted=False,
                    )
                if opportunity.target_status not in {
                    TargetKind.UNRESOLVED.value,
                    TargetKind.JOB_DETAIL.value,
                    TargetKind.MULTIPLE_CANDIDATE_ROLES.value,
                }:
                    return self._result_from_record(
                        item,
                        opportunity,
                        attempted=False,
                    )
                context = _resolution_context(
                    opportunity,
                    application_id=item.application_id,
                )
            if self._fatal_stop.is_set():
                with self.database.session_scope() as session:
                    record = session.get(Opportunity, item.opportunity_id)
                    if record is None:
                        raise KeyError(item.opportunity_id)
                    return self._result_from_record(item, record, attempted=False)
            limiter.wait()
            if self._fatal_stop.is_set():
                with self.database.session_scope() as session:
                    record = session.get(Opportunity, item.opportunity_id)
                    if record is None:
                        raise KeyError(item.opportunity_id)
                    return self._result_from_record(item, record, attempted=False)
            resolver = (
                IsolatedNavigatorTargetResolver(
                    self.settings,
                    timeout_seconds=self._row_timeout_seconds,
                    apply_click_budget=self.apply_click_budget,
                )
                if self._use_process_isolation
                else self.resolver_factory()
            )
            resolver_result: Any = None
            resolver_exception: BaseException | None = None
            try:
                resolver_result = (
                    resolver(context) if callable(resolver) else resolver.resolve(context)
                )
            except ApplyClickSafetyViolation:
                raise
            except ProcessContainmentError:
                raise
            except BaseException as exc:  # replayed inside the service boundary
                resolver_exception = exc

            def deliver(fresh_context: ResolutionContext) -> Any:
                if fresh_context != context:
                    raise RuntimeError("resolution_context_changed_during_navigation")
                if resolver_exception is not None:
                    raise resolver_exception
                return resolver_result

            with self.database.session_scope() as session:
                opportunity = session.get(Opportunity, item.opportunity_id)
                if opportunity is None:
                    raise KeyError(item.opportunity_id)
                if not user_status_is_automation_eligible(opportunity.user_status):
                    return self._result_from_record(
                        item,
                        opportunity,
                        attempted=False,
                    )
                outcome = TargetResolutionService(session).resolve(
                    opportunity.id,
                    application_id=item.application_id,
                    resolver=deliver,
                )
                return self._result_from_record(
                    item,
                    outcome.opportunity,
                    attempted=True,
                )
        except BaseException as exc:
            if isinstance(exc, ApplyClickSafetyViolation):
                self._record_fatal_apply_click(item, exc)
                raise
            result = self._record_outer_failure(item, exc)
            if isinstance(
                exc,
                (KeyboardInterrupt, SystemExit, ProcessContainmentError),
            ):
                raise
            return result

    def _summaries(
        self,
        items: list[BatchWorkItem],
    ) -> tuple[Counter[str], Counter[str]]:
        if not items:
            return Counter(), Counter()
        identifiers = [item.opportunity_id for item in items]
        target_histogram: Counter[str] = Counter()
        failure_reasons: Counter[str] = Counter()
        with self.database.session_scope() as session:
            records = list(
                session.scalars(
                    select(Opportunity).where(Opportunity.id.in_(identifiers))
                ).all()
            )
            for record in records:
                status = str(record.target_status or TargetKind.UNRESOLVED.value)
                target_histogram[status] += 1
                if status in _FAILURE_KINDS and record.resolution_attempted_at is not None:
                    reasons = self._reason_codes(record) or ("unknown_failure",)
                    failure_reasons.update(reasons)
        return target_histogram, failure_reasons

    def run(
        self,
        options: BatchResolveOptions,
        *,
        progress: Callable[[BatchRowResult], None] | None = None,
    ) -> BatchResolveReport:
        # The allowance is scoped to this invocation, not to the lifetime of
        # a reusable driver object.  Every row-local resolver created below
        # reads this same object through the factory closure.
        if bool(getattr(self.settings, "apply_click_enabled", False)):
            self.apply_click_budget = ApplyClickBudget(
                int(getattr(self.settings, "apply_click_run_cap", 25))
            )
        self._fatal_stop.clear()
        self._row_timeout_seconds = float(options.row_timeout_seconds)
        items = self._select(options)
        if options.dry_run:
            rows: list[BatchRowResult] = []
            with self.database.session_scope() as session:
                for item in items:
                    record = session.get(Opportunity, item.opportunity_id)
                    if record is None:
                        continue
                    row = self._result_from_record(item, record, attempted=False)
                    rows.append(row)
                    if progress is not None:
                        progress(row)
            histogram, failures = self._summaries(items)
            return BatchResolveReport(
                selected=len(items),
                attempted=0,
                dry_run=True,
                interrupted=False,
                rows=tuple(rows),
                target_histogram=histogram,
                failure_reasons=failures,
            )

        limiter = _NavigationRateLimiter(
            options.delay_seconds,
            sleep=self.sleep,
            monotonic=self.monotonic,
        )
        completed_rows: list[BatchRowResult] = []
        interrupted = False
        fatal_safety_violation = ""
        executor = ThreadPoolExecutor(
            max_workers=options.concurrency,
            thread_name_prefix="argus-target-resolution",
        )
        pending: dict[Future[BatchRowResult], BatchWorkItem] = {}
        next_index = 0

        def submit_one() -> bool:
            nonlocal next_index
            if next_index >= len(items):
                return False
            item = items[next_index]
            next_index += 1
            pending[executor.submit(self._process, item, limiter)] = item
            return True

        try:
            for _ in range(min(options.concurrency, len(items))):
                submit_one()
            while pending:
                done, _ = wait(tuple(pending), return_when=FIRST_COMPLETED)
                for future in sorted(done, key=lambda value: pending[value].index):
                    pending.pop(future)
                    row = future.result()
                    completed_rows.append(row)
                    if progress is not None:
                        progress(row)
                    submit_one()
        except KeyboardInterrupt:
            interrupted = True
            for future in pending:
                future.cancel()
        except ApplyClickSafetyViolation as exc:
            interrupted = True
            fatal_safety_violation = str(exc)
            self._fatal_stop.set()
            for future in pending:
                future.cancel()
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

        histogram, failures = self._summaries(items)
        completed_rows.sort(key=lambda row: row.index)
        return BatchResolveReport(
            selected=len(items),
            attempted=sum(row.attempted for row in completed_rows),
            dry_run=False,
            interrupted=interrupted,
            rows=tuple(completed_rows),
            target_histogram=histogram,
            failure_reasons=failures,
            fatal_safety_violation=fatal_safety_violation,
        )
