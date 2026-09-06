"""One-process, one-application Phase 21 PREFILL measurement harness.

Without ``--execute`` this module performs no ARGUS import and opens no
database.  Execution is deliberately limited to the previously inventoried
cohort and to one literal visible PREFILL request.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import multiprocessing
import os
import re
import secrets
import sqlite3
import sys
import time
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, MutableMapping


APPROVED_APPLICATION_IDS = (
    "75db5b22-02c7-4cbf-967f-ec63a4b8655a",
    "94a2189c-75df-43b7-9dff-d02f28f51eb5",
    "e6c45a6d-63f3-4833-84e5-ab516e5ee8e6",
    "d6d6c790-1802-4a5d-8fec-88112cf12e9d",
    "620d6240-a306-4be1-8cf5-dc17eac2df45",
    "06b89385-194d-41d8-b348-eaa38b572878",
    "bd4df3df-814b-4c53-b873-bef085cd35ef",
    "776ce2ea-3808-4cd3-8aae-d919d6f67029",
    "845a8fdb-e643-4f8f-933a-4ddbd1de790d",
    "b72894eb-f21c-4a4e-b6d8-4ee84beb0ab2",
)
_APPROVED_SET = frozenset(APPROVED_APPLICATION_IDS)
_RUN_PATH = "/api/applications/{application_id}/run?mode=prefill&headed=true"
_SESSIONS_PATH = "/api/handoff/sessions"
PHASE21_TOTAL_DEADLINE_SECONDS = 300.0
_WORKER_TOKEN_ENV = "ARGUS_PHASE21_PARENT_TOKEN"
_BACKUP_TOKEN_ENV = "ARGUS_PHASE21_BACKUP_PARENT_TOKEN"
_SAFE_ENVIRONMENT = {
    "ARGUS_AUTOMATION_MODE": "OFF",
    "ARGUS_ENABLE_LIVE_SUBMIT": "false",
    "ARGUS_ENABLE_APPLY_CLICK": "false",
    "ARGUS_SWEEP_INTERVAL_HOURS": "0",
    "ARGUS_ENABLE_TRACKR_LIVE": "false",
    "ARGUS_ENABLE_NOTIFICATIONS": "false",
}
_ELIGIBLE_USER_STATUSES = frozenset({"NOT_APPLIED", "INTERESTED"})
_NONTERMINAL_STATES = frozenset(
    {
        "NEEDS_USER",
        "NEEDS_OA",
        "READY_TO_SUBMIT",
        "BLOCKED",
        "FAILED_RETRYABLE",
        "FILLING",
    }
)
_SUBMISSION_TABLES = (
    "submission_intents",
    "submission_authorities",
    "submission_review_bindings",
    "lab_submissions",
)
_MEASURED_FIRST_PARTY = {
    "my.greenhouse.io": ("/users" + "/self", "fetch", "", False),
    "email-address-validator.us.greenhouse.io": (
        "/address/validate",
        "fetch",
        "email",
        True,
    ),
    "api-geocode-earth-proxy.greenhouse.io": (
        "/v1/autocomplete",
        "fetch",
        "location",
        True,
    ),
    "job-boards.cdn.greenhouse.io": (
        "/assets/flags-a2kmUSbF.webp",
        "image",
        "",
        False,
    ),
}
_TOKEN = re.compile(r"^[A-Za-z0-9_.:\-]{1,160}$")
_HOST = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)"
    r"(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))*$"
)
_CANONICAL_KEYS = frozenset(
    {
        "identity.first_name",
        "identity.last_name",
        "identity.full_name",
        "contact.email",
        "contact.phone",
        "contact.address_line_1",
        "contact.city",
        "contact.postcode",
        "contact.country",
        "contact.linkedin",
        "education.university",
        "education.degree",
        "education.graduation_year",
        "legal.work_authorisation",
        "legal.sponsorship",
        "document.cv",
        "document.cover_letter",
        "answer.motivation",
        "account.password",
        "sensitive.demographic",
        "legal.attestation",
        "handoff.assessment",
        "handoff.captcha",
        "unknown",
    }
)
_CONTROL_CATEGORIES = frozenset(
    {
        "text",
        "email",
        "tel",
        "phone",
        "select",
        "combobox",
        "checkbox",
        "radio",
        "file",
        "textarea",
        "date",
        "number",
        "password",
        "hidden",
    }
)
_ACTION_STATUSES = frozenset({"resolved", "blocked", "omitted"})
_ACTION_SOURCES = frozenset(
    {
        "approved_profile",
        "approved_answer",
        "approved_document",
        "profile",
        "answer",
        "document",
        "handoff",
        "human",
        "unmapped",
        "missing",
        "plausibility_guard",
    }
)
_FORBIDDEN_AUDIT_SEMANTICS = (
    "submission",
    "click",
    "authority",
    "intent",
    "confirm",
    "receipt",
)


class BackupBundle:
    def __init__(self, path: Path, evidence: dict[str, object]) -> None:
        self.path = path
        self.evidence = evidence


class DatabaseSnapshot:
    def __init__(
        self,
        *,
        integrity_check: str,
        foreign_key_violations: int,
        row_counts: dict[str, int],
        identities: dict[str, frozenset[tuple[object, ...]]],
        tables: frozenset[str],
    ) -> None:
        self.integrity_check = integrity_check
        self.foreign_key_violations = foreign_key_violations
        self.row_counts = row_counts
        self.identities = identities
        self.tables = tables


class HarnessFailure(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class RuntimeResources:
    def __init__(
        self,
        *,
        client: Any,
        navigator: Any,
        journey_class: type,
        database_path: Path,
        traces_dir: Path,
        screenshots_dir: Path,
        audit_check: Callable[[], Mapping[str, object]],
    ) -> None:
        self.client = client
        self.navigator = navigator
        self.journey_class = journey_class
        self.database_path = database_path
        self.traces_dir = traces_dir
        self.screenshots_dir = screenshots_dir
        self.audit_check = audit_check


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_integrity(connection: sqlite3.Connection) -> str:
    rows = connection.execute("PRAGMA integrity_check").fetchall()
    if rows != [("ok",)]:
        raise sqlite3.DatabaseError("database integrity check failed")
    return "ok"


def _read_foreign_keys(connection: sqlite3.Connection) -> int:
    count = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    if count:
        raise sqlite3.IntegrityError("foreign key verification failed")
    return count


def backup_database(
    database_path: Path,
    backup_directory: Path,
    application_id: str,
) -> BackupBundle:
    """Create and verify a WAL-consistent online backup from a read-only source."""

    backup_directory = Path(backup_directory).expanduser().resolve()
    backup_directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination_path = backup_directory / (
        f"argus-phase21-pre-prefill-{stamp}-{application_id[:8]}.db"
    )
    return _backup_database_to_path(database_path, destination_path, application_id)


def _backup_database_to_path(
    database_path: Path,
    destination_path: Path,
    application_id: str,
    *,
    partial_path: Path | None = None,
) -> BackupBundle:
    database_path = Path(database_path).expanduser().resolve()
    destination_path = Path(destination_path).expanduser().resolve()
    working_path = (
        destination_path
        if partial_path is None
        else Path(partial_path).expanduser().resolve()
    )
    if application_id not in _APPROVED_SET:
        raise ValueError("application is outside the approved cohort")
    if not database_path.is_file():
        raise FileNotFoundError(database_path)
    if working_path == destination_path and partial_path is not None:
        raise HarnessFailure("backup_location_invalid")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(working_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    try:
        with (
            closing(
                sqlite3.connect(database_path.as_uri() + "?mode=ro", uri=True)
            ) as source,
            closing(sqlite3.connect(working_path)) as destination,
        ):
            source.execute("PRAGMA query_only=ON")
            source_integrity = _read_integrity(source)
            source_foreign_keys = _read_foreign_keys(source)
            source.backup(destination)
            destination.commit()
            destination_integrity = _read_integrity(destination)
            destination_foreign_keys = _read_foreign_keys(destination)
    except BaseException:
        working_path.unlink(missing_ok=True)
        if working_path != destination_path:
            destination_path.unlink(missing_ok=True)
        raise
    if working_path != destination_path:
        try:
            os.link(working_path, destination_path)
            working_path.unlink()
        except BaseException:
            working_path.unlink(missing_ok=True)
            destination_path.unlink(missing_ok=True)
            raise
    size = destination_path.stat().st_size
    if size <= 0:
        destination_path.unlink(missing_ok=True)
        raise sqlite3.DatabaseError("backup is empty")
    return BackupBundle(
        destination_path,
        {
            "filename": destination_path.name,
            "bytes": size,
            "sha256": _sha256(destination_path),
            "integrity_check": destination_integrity,
            "foreign_key_violations": destination_foreign_keys,
            "source_integrity_check": source_integrity,
            "source_foreign_key_violations": source_foreign_keys,
            "method": "sqlite_online_backup",
        },
    )


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _snapshot_database(database_path: Path) -> DatabaseSnapshot:
    uri = Path(database_path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        integrity = _read_integrity(connection)
        foreign_keys = _read_foreign_keys(connection)
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        row_counts: dict[str, int] = {}
        identities: dict[str, frozenset[tuple[object, ...]]] = {}
        for table in tables:
            quoted = _quote_identifier(table)
            row_counts[table] = int(
                connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
            )
            columns = connection.execute(f"PRAGMA table_info({quoted})").fetchall()
            primary_keys = [
                str(item[1])
                for item in sorted(columns, key=lambda item: int(item[5] or 0))
                if int(item[5] or 0) > 0
            ]
            if primary_keys:
                projection = ", ".join(_quote_identifier(item) for item in primary_keys)
                rows = connection.execute(f"SELECT {projection} FROM {quoted}").fetchall()
                identities[table] = frozenset(tuple(row) for row in rows)
            else:
                identities[table] = frozenset()
    return DatabaseSnapshot(
        integrity_check=integrity,
        foreign_key_violations=foreign_keys,
        row_counts=row_counts,
        identities=identities,
        tables=frozenset(tables),
    )


def _data_directory(env: Mapping[str, str]) -> Path:
    configured = str(env.get("ARGUS_DATA_DIR") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    root = str(env.get("LOCALAPPDATA") or env.get("APPDATA") or "").strip()
    if not root:
        raise HarnessFailure("data_directory_unavailable")
    return (Path(root).expanduser() / "ARGUS").resolve()


def _canonical_live_data_directory(env: Mapping[str, str]) -> Path:
    """Resolve the only data directory accepted by the public execute path."""

    local = str(env.get("LOCALAPPDATA") or "").strip()
    if not local:
        raise HarnessFailure("data_directory_unavailable")
    canonical = (Path(local).expanduser() / "ARGUS").resolve()
    configured = str(env.get("ARGUS_DATA_DIR") or "").strip()
    if configured and Path(configured).expanduser().resolve() != canonical:
        raise HarnessFailure("noncanonical_data_directory")
    return canonical


def _apply_safety_environment(env: MutableMapping[str, str]) -> None:
    for key, value in _SAFE_ENVIRONMENT.items():
        env[key] = value


def _response_payload(response: Any, *, code: str) -> object:
    if int(getattr(response, "status_code", 0)) != 200:
        raise HarnessFailure(code)
    try:
        return response.json()
    except Exception as exc:
        raise HarnessFailure(code) from exc


def _application_gate(database_path: Path, application_id: str) -> dict[str, object]:
    uri = Path(database_path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute(
            "SELECT a.state, o.target_status, o.user_status, "
            "a.applied_at, a.submission_reference "
            "FROM applications AS a "
            "JOIN opportunities AS o ON o.id = a.opportunity_id "
            "WHERE a.id = ?",
            (application_id,),
        ).fetchall()
    if len(rows) != 1:
        raise HarnessFailure("application_drift")
    state, target_status, user_status, applied_at, submission_reference = rows[0]
    state = str(state or "")
    target_status = str(target_status or "")
    user_status = str(user_status or "")
    if (
        application_id not in _APPROVED_SET
        or state != "NEEDS_USER"
        or target_status != "APPLICATION_FORM"
        or user_status not in _ELIGIBLE_USER_STATUSES
        or applied_at not in (None, "")
        or str(submission_reference or "") != ""
    ):
        raise HarnessFailure("application_drift")
    return {
        "cohort_match": True,
        "state_needs_user": True,
        "target_application_form": True,
        "user_status_automation_eligible": True,
        "application_submission_markers_empty": True,
    }


def _submission_baseline_gate(snapshot: DatabaseSnapshot) -> dict[str, object]:
    missing = [table for table in _SUBMISSION_TABLES if table not in snapshot.tables]
    counts = {table: snapshot.row_counts.get(table, -1) for table in _SUBMISSION_TABLES}
    if missing or any(count != 0 for count in counts.values()):
        raise HarnessFailure("submission_baseline_not_empty")
    return {"submission_tables_zero": True}


def _list_application_sessions(client: Any, application_id: str) -> list[Mapping[str, object]]:
    payload = _response_payload(client.get(_SESSIONS_PATH), code="session_inventory_failed")
    if not isinstance(payload, list):
        raise HarnessFailure("session_inventory_failed")
    sessions = [
        item
        for item in payload
        if isinstance(item, Mapping) and item.get("application_id") == application_id
    ]
    if len(sessions) > 1:
        raise HarnessFailure("multiple_application_sessions")
    return sessions


def _safe_token(value: object, *, maximum: int = 160) -> str:
    text = str(value or "")[:maximum]
    return text if _TOKEN.fullmatch(text) else ""


def _field_projection(action: object) -> dict[str, object]:
    field = getattr(action, "field", None)
    question = getattr(field, "question", None)
    mapping = getattr(action, "mapping", None)
    canonical = getattr(mapping, "canonical_key", None)
    canonical_value = str(getattr(canonical, "value", canonical) or "")
    field_type = str(getattr(question, "field_type", "") or "").casefold()
    status = str(getattr(action, "status", "") or "").casefold()
    source = str(getattr(action, "source", "") or "").casefold()
    return {
        "required": getattr(question, "required", None) is True,
        "canonical_key": canonical_value if canonical_value in _CANONICAL_KEYS else "unknown",
        "control_category": field_type if field_type in _CONTROL_CATEGORIES else "unknown",
        "status": status if status in _ACTION_STATUSES else "unknown",
        "source": source if source in _ACTION_SOURCES else "unknown",
    }


def _control_fingerprint(action: object) -> bytes:
    """Return a process-local control identity that is never serialized."""

    field = getattr(action, "field", None)
    question = getattr(field, "question", None)
    selector = str(getattr(field, "selector", "") or "").strip()
    name = str(getattr(question, "name", "") or "").strip()
    if selector or name:
        material = json.dumps(
            ["selector", selector, "name", name],
            ensure_ascii=False,
            separators=(",", ":"),
        )
    else:
        label = str(getattr(question, "label", "") or "").strip()
        material = json.dumps(
            ["label", label],
            ensure_ascii=False,
            separators=(",", ":"),
        )
    return hashlib.sha256(material.encode("utf-8")).digest()


class _FieldCapture:
    def __init__(self) -> None:
        self._fields: dict[str, dict[str, object]] = {}
        self._successful: set[str] = set()
        self._failed: set[str] = set()
        self._action_ordinals: dict[int, str] = {}
        self._controls: dict[tuple[int, bytes, str, str, bool, int], str] = {}
        self._step_field_counts: dict[int, int] = {}
        self._action_refs: list[object] = []

    def _ordinal_for_action(self, action: object) -> str | None:
        return self._action_ordinals.get(id(action))

    def successful(self, action: object) -> None:
        ordinal = self._ordinal_for_action(action)
        if ordinal:
            self._successful.add(ordinal)
            self._failed.discard(ordinal)

    def failed(self, action: object) -> None:
        ordinal = self._ordinal_for_action(action)
        if ordinal and ordinal not in self._successful:
            self._failed.add(ordinal)

    def plan(self, owner: object, plan: object) -> None:
        actions = getattr(plan, "actions", ()) if plan is not None else ()
        if not isinstance(actions, Iterable):
            return
        try:
            step_number = max(1, int(getattr(owner, "step_index", 0)) + 1)
        except (TypeError, ValueError):
            step_number = 1
        occurrences: dict[tuple[bytes, str, str, bool], int] = {}
        for action in actions:
            projection = _field_projection(action)
            identity = (
                _control_fingerprint(action),
                str(projection["canonical_key"]),
                str(projection["control_category"]),
                bool(projection["required"]),
            )
            occurrence = occurrences.get(identity, 0) + 1
            occurrences[identity] = occurrence
            control_key = (step_number, *identity, occurrence)
            ordinal = self._controls.get(control_key)
            if ordinal is None:
                next_field = self._step_field_counts.get(step_number, 0) + 1
                self._step_field_counts[step_number] = next_field
                ordinal = f"step-{step_number}-field-{next_field}"
                self._controls[control_key] = ordinal
            self._fields[ordinal] = {"ordinal": ordinal, **projection}
            self._action_ordinals[id(action)] = ordinal
            self._action_refs.append(action)

    def payload(self) -> dict[str, object]:
        resolved_count = sum(
            1
            for field in self._fields.values()
            if field.get("status") == "resolved"
        )
        unfilled: list[dict[str, str]] = []
        for ordinal, field in self._fields.items():
            status = str(field.get("status") or "")
            if ordinal in self._failed:
                reason = "fill_failed"
            elif ordinal in self._successful:
                continue
            elif status in {"blocked", "omitted"}:
                reason = f"plan_{status}"
            else:
                reason = "not_attempted"
            unfilled.append({"ordinal": ordinal, "reason": reason})
        return {
            "before_filled_count": resolved_count,
            "after_filled_count": len(self._successful),
            "successful_fill_count": len(self._successful),
            "resolved_action_count": resolved_count,
            "fields": list(self._fields.values()),
            "failed_fields": sorted(self._failed),
            "unfilled": unfilled,
        }


@contextmanager
def _instrument_journey(journey_class: type, capture: _FieldCapture):
    original_fill = journey_class._fill_resolved_action
    original_build = journey_class._build_plan

    def measured_build(owner: object, fields: object, evidence: object):
        plan = original_build(owner, fields, evidence)
        capture.plan(owner, plan)
        return plan

    def measured_fill(owner: object, scope: object, action: object):
        try:
            result = original_fill(owner, scope, action)
        except BaseException:
            capture.failed(action)
            raise
        capture.successful(action)
        return result

    journey_class._build_plan = measured_build
    journey_class._fill_resolved_action = measured_fill
    try:
        yield
    finally:
        journey_class._fill_resolved_action = original_fill
        journey_class._build_plan = original_build


def _reason_category(value: object) -> str:
    text = str(value or "").casefold()
    for marker, category in (
        ("captcha", "captcha"),
        ("human", "human_boundary"),
        ("egress", "egress_guard"),
        ("field", "field_review"),
        ("root", "application_root"),
        ("timeout", "timeout"),
        ("fail", "failure"),
        ("cancel", "cancelled"),
    ):
        if marker in text:
            return category
    return "other" if text else "none"


def _project_first_party(value: object) -> list[dict[str, object]]:
    if not isinstance(value, (list, tuple)):
        return []
    output: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        host = str(item.get("host") or "").casefold().rstrip(".")[:253]
        raw_path = str(item.get("path") or "")
        path = raw_path.split("?", 1)[0].split("#", 1)[0][:2048]
        method = str(item.get("method") or "").upper()
        resource_type = _safe_token(item.get("resource_type"))
        vendor = _safe_token(item.get("manifest_vendor"))
        delivery = _safe_token(item.get("delivery"))
        data_kind = _safe_token(item.get("candidate_data_kind"))
        measured = _MEASURED_FIRST_PARTY.get(host)
        if (
            not _HOST.fullmatch(host)
            or measured is None
            or not path.startswith("/")
            or any(character in path for character in ("\r", "\n", "?", "#", "@"))
            or method not in {"GET", "HEAD"}
            or delivery != "blocked"
            or item.get("manifest_permission") is not True
            or type(item.get("carries_candidate_data")) is not bool
            or data_kind not in {"", "email", "location"}
            or item.get("manifest_vendor") != "greenhouse"
            or (path, resource_type, data_kind, item.get("carries_candidate_data"))
            != measured
        ):
            continue
        output.append(
            {
                "host": host,
                "path": path,
                "method": method,
                "resource_type": resource_type,
                "manifest_vendor": vendor,
                "manifest_permission": True,
                "delivery": "blocked",
                "carries_candidate_data": item["carries_candidate_data"],
                "candidate_data_kind": data_kind,
            }
        )
    return output


def _project_session(snapshot: Mapping[str, object]) -> dict[str, object]:
    manifest_value = snapshot.get("manifest")
    manifest = manifest_value if isinstance(manifest_value, Mapping) else {}
    boundary_value = snapshot.get("human_boundary")
    boundary = boundary_value if isinstance(boundary_value, Mapping) else {}
    boundary_candidate = str(boundary.get("kind") or boundary.get("type") or "").casefold()
    boundary_kind = (
        boundary_candidate
        if boundary_candidate in {"captcha", "assessment", "human_review", "authentication"}
        else "unknown"
    )
    if manifest.get("submission") != "not_clicked":
        raise HarnessFailure("session_invariant_failed")
    return {
        "session_id": _safe_token(snapshot.get("session_id")),
        "mode": _safe_token(snapshot.get("mode")),
        "state": _safe_token(snapshot.get("state")),
        "headed": snapshot.get("headed") is True,
        "reason_category": _reason_category(snapshot.get("reason")),
        "human_boundary_kind": boundary_kind,
        "captcha_or_handoff": boundary_kind == "captcha"
        or _safe_token(snapshot.get("state")) == "HUMAN_REQUIRED",
        "first_party_requests": _project_first_party(manifest.get("first_party_requests")),
    }


def _poll_cleanup(
    client: Any,
    session_id: str,
    timeout: float,
    interval: float,
) -> Mapping[str, object]:
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        payload = _response_payload(
            client.get(f"{_SESSIONS_PATH}/{session_id}"),
            code="cleanup_status_failed",
        )
        if not isinstance(payload, Mapping):
            raise HarnessFailure("cleanup_status_failed")
        if payload.get("worker_alive") is False and payload.get("cleanup_complete") is True:
            return payload
        if time.monotonic() >= deadline:
            return payload
        time.sleep(max(0.0, interval))


def _cleanup_owned_session(
    runtime: RuntimeResources,
    application_id: str,
    session_id: str,
    *,
    timeout: float,
    interval: float,
) -> dict[str, object]:
    cleanup = {
        "attempted": True,
        "cleanup_complete": False,
        "retry_cleanup_used": False,
        "worker_alive": True,
    }
    cancel_path = f"{_SESSIONS_PATH}/{session_id}/cancel"
    snapshot: Mapping[str, object] | None = None
    needs_retry = False
    try:
        response = runtime.client.post(cancel_path, json={"application_id": application_id})
        _response_payload(response, code="cleanup_cancel_failed")
        snapshot = _poll_cleanup(runtime.client, session_id, timeout, interval)
        needs_retry = (
            snapshot.get("worker_alive") is not False
            or snapshot.get("cleanup_complete") is not True
        )
    except Exception:
        needs_retry = True
    if needs_retry:
        cleanup["retry_cleanup_used"] = True
        try:
            runtime.navigator.retry_cleanup(session_id)
            snapshot = _poll_cleanup(runtime.client, session_id, timeout, interval)
        except Exception:
            snapshot = None
    if snapshot is not None:
        cleanup["worker_alive"] = snapshot.get("worker_alive") is True
        cleanup["cleanup_complete"] = snapshot.get("cleanup_complete") is True
    return cleanup


def _safe_artifact(
    path_value: object,
    expected_directory: Path,
    run_id: str,
    suffix: str,
) -> dict[str, object] | None:
    if not path_value:
        return None
    try:
        path = Path(str(path_value)).expanduser().resolve()
        expected = Path(expected_directory).expanduser().resolve()
    except (OSError, ValueError):
        return {"status": "rejected"}
    if path.parent != expected or path.name != f"{run_id}{suffix}" or not path.is_file():
        return {"status": "rejected"}
    return {
        "filename": path.name,
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _read_application_truth(
    connection: sqlite3.Connection,
    application_id: str,
) -> dict[str, object]:
    row = connection.execute(
        "SELECT a.state, a.applied_at, a.submission_reference, "
        "o.target_status, o.user_status "
        "FROM applications AS a "
        "JOIN opportunities AS o ON o.id = a.opportunity_id "
        "WHERE a.id = ?",
        (application_id,),
    ).fetchone()
    if row is None:
        return {
            "exists": False,
            "state_nonterminal": False,
            "applied_at_empty": False,
            "submission_reference_empty": False,
            "target_application_form": False,
            "user_status_automation_eligible": False,
        }
    state = str(row[0] or "")
    return {
        "exists": True,
        "state": state if state in _NONTERMINAL_STATES else "postsubmission_or_invalid",
        "state_nonterminal": state in _NONTERMINAL_STATES,
        "applied_at_empty": row[1] in (None, ""),
        "submission_reference_empty": str(row[2] or "") == "",
        "target_application_form": str(row[3] or "") == "APPLICATION_FORM",
        "user_status_automation_eligible": str(row[4] or "") in _ELIGIBLE_USER_STATUSES,
    }


def _semantic_run_and_audit_invariants(
    database_path: Path,
    before: DatabaseSnapshot,
    after: DatabaseSnapshot,
    application_id: str,
    response_run_id: str,
    response_state: str,
) -> tuple[dict[str, object], dict[str, object], list[dict[str, object]]]:
    uri = Path(database_path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        new_run_keys = (
            after.identities.get("automation_runs", frozenset())
            - before.identities.get("automation_runs", frozenset())
        )
        new_run_ids = [str(item[0]) for item in new_run_keys if len(item) == 1]
        run_rows = []
        if new_run_ids:
            placeholders = ",".join("?" for _ in new_run_ids)
            run_rows = connection.execute(
                "SELECT id, application_id, mode, state, receipt_json "
                f"FROM automation_runs WHERE id IN ({placeholders})",
                tuple(new_run_ids),
            ).fetchall()
        exact_rows = [row for row in run_rows if str(row[0]) == response_run_id]
        exact = exact_rows[0] if len(exact_rows) == 1 else None
        receipt_empty = False
        if exact is not None:
            try:
                receipt_empty = json.loads(str(exact[4] or "{}")) == {}
            except (TypeError, ValueError):
                receipt_empty = False
        current_epoch_row = connection.execute(
            "SELECT epoch FROM audit_chain_state WHERE id = 1"
        ).fetchone()
        current_epoch = int(current_epoch_row[0]) if current_epoch_row else -1
        new_audit_keys = (
            after.identities.get("audit_events", frozenset())
            - before.identities.get("audit_events", frozenset())
        )
        new_audit_ids = [int(item[0]) for item in new_audit_keys if len(item) == 1]
        audit_rows = []
        if new_audit_ids:
            placeholders = ",".join("?" for _ in new_audit_ids)
            audit_rows = connection.execute(
                "SELECT id, epoch, event_type, entity_type, entity_id, details_json "
                f"FROM audit_events WHERE id IN ({placeholders})",
                tuple(new_audit_ids),
            ).fetchall()
        parsed_events: list[tuple[tuple[object, ...], Mapping[str, object]]] = []
        for row in audit_rows:
            try:
                details = json.loads(str(row[5] or "{}"))
            except (TypeError, ValueError):
                details = {}
            parsed_events.append((row, details if isinstance(details, Mapping) else {}))
        finish_events = [
            (row, details)
            for row, details in parsed_events
            if str(row[2]) == "automation.run_finished"
        ]
        bound_finish = [
            (row, details)
            for row, details in finish_events
            if int(row[1]) == current_epoch
            and str(row[3]) == "automation_run"
            and str(row[4]) == response_run_id
            and str(details.get("application_id") or "") == application_id
            and str(details.get("mode") or "") == "prefill"
            and not str(details.get("receipt_reference") or "")
        ]
        forbidden_events = [
            str(row[2])
            for row, _details in parsed_events
            if any(
                token in str(row[2] or "").casefold()
                for token in _FORBIDDEN_AUDIT_SEMANTICS
            )
        ]
        application_truth = _read_application_truth(connection, application_id)
        fallback_requests = (
            _project_first_party(bound_finish[0][1].get("first_party_requests"))
            if len(bound_finish) == 1
            else []
        )
    run = {
        "exactly_one_new_run": len(run_rows) == 1,
        "response_run_is_new": exact is not None,
        "application_id_matches": exact is not None and str(exact[1]) == application_id,
        "mode": str(exact[2] or "") if exact is not None else "missing",
        "receipt_empty": receipt_empty,
        "state_nonterminal": exact is not None and str(exact[3] or "") in _NONTERMINAL_STATES,
        "response_state_matches_run": exact is not None
        and response_state == str(exact[3] or ""),
        "run_state_matches_application": exact is not None
        and str(exact[3] or "") == application_truth.get("state"),
        "no_submit_mode_run_added": all(str(row[2] or "") != "submit" for row in run_rows),
    }
    semantic = {
        **application_truth,
        "current_audit_epoch": current_epoch,
        "exact_new_run_finished_count": len(finish_events),
        "exact_bound_current_epoch_run_finished": len(bound_finish) == 1,
        "forbidden_semantic_audit_events_added": len(forbidden_events),
    }
    return run, semantic, fallback_requests


def _database_invariants(
    before: DatabaseSnapshot,
    after: DatabaseSnapshot,
) -> dict[str, object]:
    deleted_rows = sum(
        len(identities - after.identities.get(table, frozenset()))
        for table, identities in before.identities.items()
    )
    row_count_decreases = sum(
        max(0, count - after.row_counts.get(table, 0))
        for table, count in before.row_counts.items()
    )

    def delta(table: str) -> int:
        return after.row_counts.get(table, 0) - before.row_counts.get(table, 0)

    return {
        "integrity_check": after.integrity_check,
        "foreign_key_violations": after.foreign_key_violations,
        "deleted_rows": deleted_rows,
        "row_count_decreases": row_count_decreases,
        "automation_runs_added": delta("automation_runs"),
        "audit_events_added": delta("audit_events"),
        "submission_intents_added": delta("submission_intents"),
        "submission_authorities_added": delta("submission_authorities"),
        "submission_review_bindings_added": delta("submission_review_bindings"),
        "lab_submissions_added": delta("lab_submissions"),
        "submission_tables_baseline_zero": all(
            before.row_counts.get(table, -1) == 0 for table in _SUBMISSION_TABLES
        ),
        "submission_tables_after_zero": all(
            after.row_counts.get(table, -1) == 0 for table in _SUBMISSION_TABLES
        ),
        "row_counts": dict(sorted(after.row_counts.items())),
    }


def _invariants_safe(invariants: Mapping[str, object]) -> bool:
    run = invariants.get("run")
    database = invariants.get("database")
    audit = invariants.get("audit")
    semantic = invariants.get("semantic")
    if (
        not isinstance(run, Mapping)
        or not isinstance(database, Mapping)
        or not isinstance(audit, Mapping)
        or not isinstance(semantic, Mapping)
    ):
        return False
    return (
        run.get("exactly_one_new_run") is True
        and run.get("response_run_is_new") is True
        and run.get("application_id_matches") is True
        and run.get("mode") == "prefill"
        and run.get("receipt_empty") is True
        and run.get("state_nonterminal") is True
        and run.get("response_state_matches_run") is True
        and run.get("run_state_matches_application") is True
        and run.get("no_submit_mode_run_added") is True
        and database.get("integrity_check") == "ok"
        and database.get("foreign_key_violations") == 0
        and database.get("deleted_rows") == 0
        and database.get("row_count_decreases") == 0
        and database.get("automation_runs_added") == 1
        and all(database.get(f"{table}_added") == 0 for table in _SUBMISSION_TABLES)
        and database.get("submission_tables_baseline_zero") is True
        and database.get("submission_tables_after_zero") is True
        and semantic.get("exists") is True
        and semantic.get("state_nonterminal") is True
        and semantic.get("applied_at_empty") is True
        and semantic.get("submission_reference_empty") is True
        and semantic.get("target_application_form") is True
        and semantic.get("user_status_automation_eligible") is True
        and semantic.get("exact_new_run_finished_count") == 1
        and semantic.get("exact_bound_current_epoch_run_finished") is True
        and semantic.get("forbidden_semantic_audit_events_added") == 0
        and audit.get("epoch") == semantic.get("current_audit_epoch")
        and audit.get("valid") is True
        and audit.get("complete") is True
        and audit.get("state_consistent") is True
        and audit.get("pending_events") == 0
    )


def _project_audit(value: Mapping[str, object]) -> dict[str, object]:
    return {
        "valid": value.get("valid") is True,
        "complete": value.get("complete") is True,
        "state_consistent": value.get("state_consistent") is True,
        "pending_events": int(value.get("pending_events") or 0),
        "epoch": int(value.get("epoch") or 0),
        "checked_events": int(value.get("checked_events") or 0),
    }


def _failure_result(
    application_id: str,
    code: str,
    exc: BaseException | None = None,
) -> dict[str, object]:
    error: dict[str, object] = {"code": code}
    if exc is not None:
        error["exception_type"] = type(exc).__name__
    return {
        "ok": False,
        "application_id": application_id,
        "operation": "phase21_prefill_measurement",
        "error": error,
    }


@contextmanager
def _open_runtime(data_dir: Path):
    """Import ARGUS only after backup, own its singleton, and close its lifespan."""

    from app.config import AutomationMode, Settings
    from app.runtime_lock import RuntimeLock, runtime_lock_path
    from app.version import __version__

    settings = Settings.load()
    if settings.data_dir.resolve() != Path(data_dir).resolve():
        raise HarnessFailure("runtime_data_directory_mismatch")
    if (
        settings.automation_mode is not AutomationMode.OFF
        or settings.live_submit_enabled
        or settings.apply_click_enabled
        or settings.sweep_interval_hours != 0
        or settings.trackr_live_enabled
        or settings.notifications_enabled
    ):
        raise HarnessFailure("runtime_safety_configuration_failed")
    lock = RuntimeLock(
        runtime_lock_path(settings.data_dir),
        host=settings.host,
        port=settings.port,
        version=__version__,
        metadata={"command": "phase21_prefill_measure"},
    )
    with lock:
        from fastapi.testclient import TestClient
        from sqlalchemy import select

        from app.automation.runner import _OwnerThreadJourney
        from app.main import app
        from app.models import AuditChainState
        from app.security.audit import verify_audit_chain

        with TestClient(app) as client:
            def audit_check() -> Mapping[str, object]:
                with app.state.db.SessionLocal() as session:
                    state = session.scalar(
                        select(AuditChainState).where(AuditChainState.id == 1)
                    )
                    if state is None:
                        raise HarnessFailure("audit_state_missing")
                    checked = verify_audit_chain(session, epoch=int(state.epoch))
                    return {
                        "valid": checked.valid,
                        "complete": checked.complete,
                        "state_consistent": checked.state_consistent,
                        "pending_events": checked.pending_events,
                        "epoch": checked.epoch,
                        "checked_events": checked.checked_events,
                    }

            yield RuntimeResources(
                client=client,
                navigator=app.state.navigator,
                journey_class=_OwnerThreadJourney,
                database_path=settings.data_dir / "argus.db",
                traces_dir=settings.traces_dir,
                screenshots_dir=settings.screenshots_dir,
                audit_check=audit_check,
            )


def _execute_verified_backup(
    application_id: str,
    *,
    data_dir: Path,
    backup: BackupBundle,
    runtime_factory: Callable[[Path], Any] | None = None,
    cleanup_timeout: float = 5.0,
    poll_interval: float = 0.05,
) -> dict[str, object]:
    """Run only after the caller has created and verified the online backup."""

    result: dict[str, object] = {
        "ok": False,
        "application_id": application_id,
        "operation": "phase21_prefill_measurement",
        "backup": backup.evidence,
        "submission": "unknown",
        "cleanup": {
            "attempted": False,
            "cleanup_complete": True,
            "retry_cleanup_used": False,
            "worker_alive": False,
        },
    }
    factory = runtime_factory or _open_runtime
    inner_completed = False
    try:
        with factory(data_dir) as runtime:
            capture = _FieldCapture()
            post_attempted = False
            owned_session_id = ""
            response_payload: Mapping[str, object] | None = None
            response_run_id = ""
            response_state = ""
            before: DatabaseSnapshot | None = None
            try:
                health = _response_payload(runtime.client.get("/healthz"), code="health_failed")
                if (
                    not isinstance(health, Mapping)
                    or health.get("automation_mode") != "OFF"
                    or health.get("live_submit") is not False
                ):
                    raise HarnessFailure("unsafe_health")
                before = _snapshot_database(runtime.database_path)
                baseline = _submission_baseline_gate(before)
                gate = _application_gate(runtime.database_path, application_id)
                if _list_application_sessions(runtime.client, application_id):
                    raise HarnessFailure("live_session_exists")
                result["gate"] = {**gate, **baseline}
                post_attempted = True
                with _instrument_journey(runtime.journey_class, capture):
                    response = runtime.client.post(
                        _RUN_PATH.format(application_id=application_id)
                    )
                payload = _response_payload(response, code="prefill_request_failed")
                if not isinstance(payload, Mapping):
                    raise HarnessFailure("prefill_response_invalid")
                response_payload = payload
                response_session_id = _safe_token(
                    payload.get("session_id") or payload.get("handoff_session_id")
                )
                sessions = _list_application_sessions(runtime.client, application_id)
                if sessions:
                    listed_id = _safe_token(sessions[0].get("session_id"))
                    if response_session_id and listed_id != response_session_id:
                        raise HarnessFailure("session_binding_mismatch")
                    owned_session_id = response_session_id or listed_id
                elif response_session_id:
                    owned_session_id = response_session_id
                    raise HarnessFailure("session_binding_mismatch")
                if owned_session_id:
                    snapshot_value = _response_payload(
                        runtime.client.get(f"{_SESSIONS_PATH}/{owned_session_id}"),
                        code="session_snapshot_failed",
                    )
                    if (
                        not isinstance(snapshot_value, Mapping)
                        or snapshot_value.get("application_id") != application_id
                        or snapshot_value.get("mode") != "prefill"
                        or snapshot_value.get("headed") is not True
                    ):
                        raise HarnessFailure("session_invariant_failed")
                    result["session"] = _project_session(snapshot_value)
                run_id = _safe_token(payload.get("run_id"))
                response_run_id = run_id
                state = _safe_token(payload.get("state"))
                response_state = state
                blocked_reasons = [
                    _reason_category(item)
                    for item in payload.get("blocked_reasons", ())
                    if isinstance(payload.get("blocked_reasons", ()), (list, tuple))
                ]
                if (
                    not run_id
                    or state not in _NONTERMINAL_STATES
                    or payload.get("receipt") is not None
                ):
                    raise HarnessFailure("result_invariant_failed")
                result["run"] = {
                    "run_id": run_id,
                    "state": state,
                    "blocked_reason_categories": sorted(set(blocked_reasons)),
                    "abort_reason": str(
                        (result.get("session") or {}).get("reason_category") or "none"
                    )
                    if isinstance(result.get("session"), Mapping)
                    else "none",
                }
                result["measurement"] = capture.payload()
            except HarnessFailure as exc:
                result["error"] = {"code": exc.code}
            except Exception as exc:
                result["error"] = {
                    "code": "runtime_error",
                    "exception_type": type(exc).__name__,
                }
            finally:
                if post_attempted:
                    try:
                        if not owned_session_id:
                            sessions = _list_application_sessions(
                                runtime.client,
                                application_id,
                            )
                            if sessions:
                                owned_session_id = _safe_token(sessions[0].get("session_id"))
                        if owned_session_id:
                            result["cleanup"] = _cleanup_owned_session(
                                runtime,
                                application_id,
                                owned_session_id,
                                timeout=cleanup_timeout,
                                interval=poll_interval,
                            )
                    except Exception:
                        result["cleanup"] = {
                            "attempted": True,
                            "cleanup_complete": False,
                            "retry_cleanup_used": False,
                            "worker_alive": True,
                        }
            cleanup = result["cleanup"]
            if isinstance(cleanup, Mapping) and cleanup.get("cleanup_complete") is not True:
                result["ok"] = False
                result["error"] = {"code": "cleanup_uncertain"}
            if post_attempted and before is not None:
                try:
                    after = _snapshot_database(runtime.database_path)
                    inferred_new_runs = (
                        after.identities.get("automation_runs", frozenset())
                        - before.identities.get("automation_runs", frozenset())
                    )
                    invariant_run_id = response_run_id
                    if not invariant_run_id and len(inferred_new_runs) == 1:
                        invariant_run_id = str(next(iter(inferred_new_runs))[0])
                    run_invariants, semantic, event_first_party = (
                        _semantic_run_and_audit_invariants(
                            runtime.database_path,
                            before,
                            after,
                            application_id,
                            invariant_run_id,
                            response_state,
                        )
                    )
                    invariants = {
                        "run": run_invariants,
                        "database": _database_invariants(before, after),
                        "audit": _project_audit(runtime.audit_check()),
                        "semantic": semantic,
                    }
                    result["invariants"] = invariants
                    if response_payload is not None:
                        result["artifacts"] = {
                            "trace": _safe_artifact(
                                response_payload.get("trace_path"),
                                runtime.traces_dir,
                                invariant_run_id,
                                ".zip",
                            ),
                            "screenshot": _safe_artifact(
                                response_payload.get("screenshot_path"),
                                runtime.screenshots_dir,
                                invariant_run_id,
                                ".png",
                            ),
                        }
                    if "session" not in result and event_first_party:
                        result["first_party_requests"] = event_first_party
                    if not _invariants_safe(invariants) and "error" not in result:
                        result["error"] = {"code": "invariant_failed"}
                    elif (
                        response_payload is not None
                        and "error" not in result
                        and isinstance(cleanup, Mapping)
                        and cleanup.get("cleanup_complete") is True
                    ):
                        result["ok"] = True
                        result["submission"] = "not_clicked"
                except Exception as exc:
                    if "error" not in result:
                        result["error"] = {
                            "code": "invariant_check_failed",
                            "exception_type": type(exc).__name__,
                        }
            inner_completed = True
    except Exception as exc:
        result["ok"] = False
        cleanup = result.get("cleanup")
        code = (
            "cleanup_uncertain"
            if isinstance(cleanup, Mapping) and cleanup.get("cleanup_complete") is not True
            else "runtime_start_or_shutdown_failed"
        )
        result["error"] = {"code": code, "exception_type": type(exc).__name__}
    if not inner_completed and "error" not in result:
        result["error"] = {"code": "runtime_start_or_shutdown_failed"}
    return result


def execute_measurement(
    application_id: str,
    *,
    env: MutableMapping[str, str] | None = None,
    backup_creator: Callable[[Path, Path, str], BackupBundle] = backup_database,
    runtime_factory: Callable[[Path], Any] | None = None,
    cleanup_timeout: float = 5.0,
    poll_interval: float = 0.05,
) -> dict[str, object]:
    """Injected/local entry used by tests; the POST itself is never retried."""

    if application_id not in _APPROVED_SET:
        result = _failure_result(application_id, "application_not_approved")
        result["submission"] = "unknown"
        return result
    environment = os.environ if env is None else env
    _apply_safety_environment(environment)
    try:
        data_dir = _data_directory(environment)
        backup = backup_creator(data_dir / "argus.db", data_dir / "backups", application_id)
        _snapshot_database(backup.path)
    except Exception as exc:
        result = _failure_result(application_id, "backup_failed", exc)
        result["submission"] = "unknown"
        return result
    return _execute_verified_backup(
        application_id,
        data_dir=data_dir,
        backup=backup,
        runtime_factory=runtime_factory,
        cleanup_timeout=cleanup_timeout,
        poll_interval=poll_interval,
    )


def _verified_backup_evidence(
    bundle: BackupBundle,
    *,
    data_dir: Path,
    application_id: str,
) -> dict[str, object]:
    path = Path(bundle.path).resolve()
    expected_parent = (Path(data_dir).resolve() / "backups").resolve()
    filename_pattern = re.compile(
        rf"^argus-phase21-pre-prefill-\d{{8}}T\d{{12}}Z-{re.escape(application_id[:8])}\.db$"
    )
    if path.parent != expected_parent or not filename_pattern.fullmatch(path.name):
        raise HarnessFailure("backup_location_invalid")
    if not path.is_file() or path.stat().st_size <= 0:
        raise HarnessFailure("backup_verification_failed")
    uri = path.as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        integrity = _read_integrity(connection)
        foreign_keys = _read_foreign_keys(connection)
    evidence = dict(bundle.evidence)
    if (
        evidence.get("filename") != path.name
        or evidence.get("bytes") != path.stat().st_size
        or evidence.get("sha256") != _sha256(path)
        or evidence.get("integrity_check") != "ok"
        or evidence.get("foreign_key_violations") != 0
        or evidence.get("source_integrity_check") != "ok"
        or evidence.get("source_foreign_key_violations") != 0
        or evidence.get("method") != "sqlite_online_backup"
        or integrity != "ok"
        or foreign_keys != 0
    ):
        raise HarnessFailure("backup_verification_failed")
    return evidence


def _worker_failure(
    application_id: str,
    code: str,
    *,
    backup: Mapping[str, object] | None = None,
    cleanup_attempted: bool | None = None,
    cleanup_complete: bool | None = None,
    worker_alive: bool | None = None,
    process_tree_terminated: bool | None = None,
) -> dict[str, object]:
    result = _failure_result(application_id, code)
    result["submission"] = "unknown"
    if backup is not None:
        result["backup"] = dict(backup)
    result["cleanup"] = {
        "attempted": cleanup_attempted,
        "cleanup_complete": cleanup_complete,
        "retry_cleanup_used": False,
        "worker_alive": worker_alive,
        "process_tree_terminated": process_tree_terminated,
    }
    return result


def _new_backup_paths(data_dir: Path, application_id: str) -> tuple[Path, Path]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    final_path = Path(data_dir).resolve() / "backups" / (
        f"argus-phase21-pre-prefill-{stamp}-{application_id[:8]}.db"
    )
    return final_path, Path(str(final_path) + ".partial")


def _validate_backup_worker_context(
    context: Mapping[str, object],
) -> tuple[str, Path, Path, Path, Path]:
    application_id = str(context.get("application_id") or "")
    token = str(context.get("token") or "")
    inherited = str(os.environ.pop(_BACKUP_TOKEN_ENV, "") or "")
    if not token or not inherited or not secrets.compare_digest(token, inherited):
        raise HarnessFailure("backup_parent_context_invalid")
    if application_id not in _APPROVED_SET:
        raise HarnessFailure("backup_parent_context_invalid")
    data_dir = Path(str(context.get("data_dir") or "")).resolve()
    if data_dir != _canonical_live_data_directory(os.environ):
        raise HarnessFailure("backup_parent_context_invalid")
    database_path = Path(str(context.get("database_path") or "")).resolve()
    backup_path = Path(str(context.get("backup_path") or "")).resolve()
    partial_path = Path(str(context.get("backup_partial_path") or "")).resolve()
    expected_parent = (data_dir / "backups").resolve()
    filename_pattern = re.compile(
        rf"^argus-phase21-pre-prefill-\d{{8}}T\d{{12}}Z-"
        rf"{re.escape(application_id[:8])}\.db$"
    )
    if (
        database_path != (data_dir / "argus.db").resolve()
        or backup_path.parent != expected_parent
        or not filename_pattern.fullmatch(backup_path.name)
        or partial_path != Path(str(backup_path) + ".partial")
    ):
        raise HarnessFailure("backup_parent_context_invalid")
    return application_id, data_dir, database_path, backup_path, partial_path


def _backup_worker_entry(
    send_connection: Any,
    gate_receive_connection: Any,
    context: Mapping[str, object],
) -> None:
    """Private stdlib-only backup preflight; it cannot import ARGUS."""

    application_id = str(context.get("application_id") or "")
    validated_paths: tuple[Path, Path] | None = None
    try:
        if (
            not gate_receive_connection.poll(30.0)
            or gate_receive_connection.recv() is not True
        ):
            raise HarnessFailure("backup_job_assignment_timeout")
        _apply_safety_environment(os.environ)
        application_id, data_dir, database_path, backup_path, partial_path = (
            _validate_backup_worker_context(context)
        )
        validated_paths = (backup_path, partial_path)
        bundle = _backup_database_to_path(
            database_path,
            backup_path,
            application_id,
            partial_path=partial_path,
        )
        evidence = _verified_backup_evidence(
            bundle,
            data_dir=data_dir,
            application_id=application_id,
        )
        result: dict[str, object] = {
            "ok": True,
            "application_id": application_id,
            "backup_path": str(bundle.path.resolve()),
            "backup": evidence,
        }
    except HarnessFailure as exc:
        result = _worker_failure(
            application_id,
            exc.code,
            cleanup_attempted=False,
            cleanup_complete=None,
            worker_alive=False,
            process_tree_terminated=False,
        )
    except BaseException as exc:
        result = _worker_failure(
            application_id,
            "backup_failed",
            cleanup_attempted=False,
            cleanup_complete=None,
            worker_alive=False,
            process_tree_terminated=False,
        )
        result["error"]["exception_type"] = type(exc).__name__
    finally:
        if validated_paths is not None and not result.get("ok"):
            for path in validated_paths:
                path.unlink(missing_ok=True)
    try:
        gate_receive_connection.close()
        send_connection.send(result)
    finally:
        send_connection.close()


def _validate_worker_context(context: Mapping[str, object]) -> tuple[str, Path, BackupBundle]:
    application_id = str(context.get("application_id") or "")
    token = str(context.get("token") or "")
    inherited = str(os.environ.pop(_WORKER_TOKEN_ENV, "") or "")
    if not token or not inherited or not secrets.compare_digest(token, inherited):
        raise HarnessFailure("worker_parent_context_invalid")
    if application_id not in _APPROVED_SET:
        raise HarnessFailure("worker_parent_context_invalid")
    data_dir = Path(str(context.get("data_dir") or "")).resolve()
    if data_dir != _canonical_live_data_directory(os.environ):
        raise HarnessFailure("worker_parent_context_invalid")
    evidence_value = context.get("backup")
    if not isinstance(evidence_value, Mapping):
        raise HarnessFailure("worker_parent_context_invalid")
    bundle = BackupBundle(Path(str(context.get("backup_path") or "")), dict(evidence_value))
    evidence = _verified_backup_evidence(
        bundle,
        data_dir=data_dir,
        application_id=application_id,
    )
    return application_id, data_dir, BackupBundle(bundle.path, evidence)


def _worker_entry(
    send_connection: Any,
    gate_receive_connection: Any,
    context: Mapping[str, object],
) -> None:
    """Private spawned entry. It never prints and cannot run without parent proof."""

    application_id = str(context.get("application_id") or "")
    backup_value = context.get("backup")
    backup = dict(backup_value) if isinstance(backup_value, Mapping) else None
    runtime_started = False
    try:
        if (
            not gate_receive_connection.poll(30.0)
            or gate_receive_connection.recv() is not True
        ):
            raise HarnessFailure("worker_job_assignment_timeout")
        _apply_safety_environment(os.environ)
        application_id, data_dir, bundle = _validate_worker_context(context)
        runtime_started = True
        with open(os.devnull, "w", encoding="utf-8") as sink:
            from contextlib import redirect_stderr, redirect_stdout

            with redirect_stdout(sink), redirect_stderr(sink):
                result = _execute_verified_backup(
                    application_id,
                    data_dir=data_dir,
                    backup=bundle,
                )
    except HarnessFailure as exc:
        result = _worker_failure(
            application_id,
            exc.code,
            backup=backup,
            cleanup_attempted=None if runtime_started else False,
            cleanup_complete=False if runtime_started else None,
            worker_alive=None if runtime_started else False,
            process_tree_terminated=False,
        )
    except BaseException as exc:
        result = _worker_failure(
            application_id,
            "worker_failed",
            backup=backup,
            cleanup_attempted=None if runtime_started else False,
            cleanup_complete=False if runtime_started else None,
            worker_alive=None if runtime_started else False,
            process_tree_terminated=False,
        )
        result["error"]["exception_type"] = type(exc).__name__
    try:
        gate_receive_connection.close()
        send_connection.send(result)
    finally:
        send_connection.close()


class _WindowsKillOnCloseJob:
    """Minimal Windows Job Object wrapper with kill-on-close semantics."""

    _KILL_ON_JOB_CLOSE = 0x00002000
    _EXTENDED_LIMIT_INFORMATION = 9

    def __init__(self) -> None:
        if os.name != "nt":
            raise HarnessFailure("windows_job_object_required")

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", ctypes.c_ulong),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_ulong),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_ulong),
                ("SchedulingClass", ctypes.c_ulong),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        class BasicAccountingInformation(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_longlong),
                ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong),
                ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", ctypes.c_ulong),
                ("TotalProcesses", ctypes.c_ulong),
                ("ActiveProcesses", ctypes.c_ulong),
                ("TotalTerminatedProcesses", ctypes.c_ulong),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.SetInformationJobObject.argtypes = (
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_ulong,
        )
        kernel32.AssignProcessToJobObject.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
        kernel32.TerminateJobObject.argtypes = (ctypes.c_void_p, ctypes.c_uint)
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.QueryInformationJobObject.argtypes = (
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
        )
        kernel32.SetInformationJobObject.restype = ctypes.c_int
        kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        kernel32.TerminateJobObject.restype = ctypes.c_int
        kernel32.CloseHandle.restype = ctypes.c_int
        kernel32.QueryInformationJobObject.restype = ctypes.c_int
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise HarnessFailure("job_object_create_failed")
        limits = ExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = self._KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            handle,
            self._EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            kernel32.CloseHandle(handle)
            raise HarnessFailure("job_object_configure_failed")
        self._kernel32 = kernel32
        self._handle = handle
        self._accounting_information_class = BasicAccountingInformation

    def assign(self, process: Any) -> None:
        if not self._kernel32.AssignProcessToJobObject(
            self._handle,
            ctypes.c_void_p(int(process.sentinel)),
        ):
            raise HarnessFailure("job_object_assignment_failed")

    def terminate(self) -> bool:
        if not self._handle or not self._kernel32.TerminateJobObject(self._handle, 1):
            raise HarnessFailure("job_object_terminate_failed")
        return True

    def is_empty(self) -> bool:
        if not self._handle:
            raise HarnessFailure("job_object_query_failed")
        information = self._accounting_information_class()
        returned = ctypes.c_ulong()
        if not self._kernel32.QueryInformationJobObject(
            self._handle,
            1,
            ctypes.byref(information),
            ctypes.sizeof(information),
            ctypes.byref(returned),
        ):
            raise HarnessFailure("job_object_query_failed")
        return int(information.ActiveProcesses) == 0

    def close(self) -> bool:
        if not self._handle:
            return True
        if not self._kernel32.CloseHandle(self._handle):
            raise HarnessFailure("job_object_close_failed")
        self._handle = None
        return True


class _WindowsJobController:
    def __init__(
        self,
        *,
        platform_name: str | None = None,
        process_context_factory: Callable[[str], Any] | None = None,
        job_factory: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        target: Callable[..., None] = _worker_entry,
        token_environment_key: str = _WORKER_TOKEN_ENV,
        process_name: str = "argus-phase21-prefill-worker",
    ) -> None:
        self._platform_name = os.name if platform_name is None else platform_name
        self._process_context_factory = (
            multiprocessing.get_context
            if process_context_factory is None
            else process_context_factory
        )
        self._job_factory = _WindowsKillOnCloseJob if job_factory is None else job_factory
        self._clock = clock
        self._target = target
        self._token_environment_key = token_environment_key
        self._process_name = process_name

    @staticmethod
    def _alive(process: Any) -> bool | None:
        try:
            return bool(process.is_alive())
        except BaseException:
            return None

    @classmethod
    def _terminate_assigned_tree(
        cls,
        job: Any,
        process: Any,
    ) -> tuple[bool, bool | None, BaseException | None]:
        try:
            requested = job.terminate() is True
        except BaseException as exc:
            requested = False
            failure: BaseException | None = exc
        else:
            failure = None
        try:
            process.join(timeout=5.0)
        except BaseException as exc:
            failure = failure or exc
        alive = cls._alive(process)
        empty = False
        if requested and alive is False:
            try:
                empty = job.is_empty() is True
            except BaseException as exc:
                failure = failure or exc
        return requested and alive is False and empty, alive, failure

    @classmethod
    def _terminate_unassigned_process(
        cls,
        process: Any,
    ) -> tuple[bool, bool | None, BaseException | None]:
        try:
            process.terminate()
            process.join(timeout=5.0)
        except BaseException as exc:
            failure: BaseException | None = exc
        else:
            failure = None
        alive = cls._alive(process)
        return alive is False and failure is None, alive, failure

    def run(self, context: Mapping[str, object], timeout: float) -> dict[str, object]:
        started_at = self._clock()
        application_id = str(context.get("application_id") or "")
        backup_value = context.get("backup")
        backup = backup_value if isinstance(backup_value, Mapping) else None
        if self._platform_name != "nt":
            return _worker_failure(
                application_id,
                "windows_job_object_required",
                backup=backup,
                cleanup_attempted=False,
                worker_alive=False,
                process_tree_terminated=False,
            )
        process_context = self._process_context_factory("spawn")
        receive_connection, send_connection = process_context.Pipe(duplex=False)
        gate_receive_connection, gate_send_connection = process_context.Pipe(duplex=False)
        process = process_context.Process(
            target=self._target,
            args=(send_connection, gate_receive_connection, dict(context)),
            name=self._process_name,
        )
        token = str(context.get("token") or "")
        previous_token = os.environ.get(self._token_environment_key)
        os.environ[self._token_environment_key] = token
        job: Any | None = None
        process_started = False
        job_assigned = False
        gate_opened = False
        result_received = False
        natural_exit = False
        job_handle_closed = False
        tree_terminated = False
        worker_alive: bool | None = False
        close_failure: BaseException | None = None
        result: dict[str, object] | None = None
        try:
            process.start()
            process_started = True
            send_connection.close()
            send_connection = None
            gate_receive_connection.close()
            gate_receive_connection = None
            job = self._job_factory()
            job.assign(process)
            job_assigned = True
            gate_send_connection.send(True)
            gate_opened = True
            gate_send_connection.close()
            gate_send_connection = None
            remaining = max(0.0, float(timeout) - (self._clock() - started_at))
            if not receive_connection.poll(remaining):
                tree_terminated, worker_alive, termination_failure = (
                    self._terminate_assigned_tree(job, process)
                )
                result = _worker_failure(
                    application_id,
                    "deadline_exceeded" if tree_terminated else "controller_failed",
                    backup=backup,
                    cleanup_attempted=None,
                    cleanup_complete=False,
                    worker_alive=worker_alive,
                    process_tree_terminated=tree_terminated,
                )
                if termination_failure is not None:
                    result["error"]["exception_type"] = type(termination_failure).__name__
            else:
                received = receive_connection.recv()
                result_received = True
                process.join(timeout=5.0)
                worker_alive = self._alive(process)
                if worker_alive is not False:
                    tree_terminated, worker_alive, termination_failure = (
                        self._terminate_assigned_tree(job, process)
                    )
                    result = _worker_failure(
                        application_id,
                        "worker_shutdown_timeout" if tree_terminated else "controller_failed",
                        backup=backup,
                        cleanup_attempted=None,
                        cleanup_complete=False,
                        worker_alive=worker_alive,
                        process_tree_terminated=tree_terminated,
                    )
                    if termination_failure is not None:
                        result["error"]["exception_type"] = type(termination_failure).__name__
                elif isinstance(received, Mapping):
                    natural_exit = True
                    result = dict(received)
                else:
                    result = _worker_failure(
                        application_id,
                        "worker_result_invalid",
                        backup=backup,
                    )
        except BaseException as exc:
            worker_alive = self._alive(process) if process_started else False
            termination_failure: BaseException | None = None
            if process_started and worker_alive is not False:
                if job_assigned and job is not None:
                    tree_terminated, worker_alive, termination_failure = (
                        self._terminate_assigned_tree(job, process)
                    )
                else:
                    tree_terminated, worker_alive, termination_failure = (
                        self._terminate_unassigned_process(process)
                    )
            result = _worker_failure(
                application_id,
                "controller_failed",
                backup=backup,
                cleanup_attempted=None if gate_opened else False,
                cleanup_complete=False if gate_opened else None,
                worker_alive=worker_alive,
                process_tree_terminated=tree_terminated,
            )
            result["error"]["exception_type"] = type(termination_failure or exc).__name__
        finally:
            for connection in (
                receive_connection,
                send_connection,
                gate_receive_connection,
                gate_send_connection,
            ):
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass
            if job is not None:
                try:
                    job_handle_closed = job.close() is True
                except BaseException as exc:
                    close_failure = exc
                    job_handle_closed = False
            if process_started and job_handle_closed and self._alive(process) is True:
                try:
                    process.join(timeout=5.0)
                except BaseException:
                    pass
            if process_started:
                worker_alive = self._alive(process)
            if process_started and worker_alive is False:
                close_process = getattr(process, "close", None)
                if callable(close_process):
                    close_process()
            if previous_token is None:
                os.environ.pop(self._token_environment_key, None)
            else:
                os.environ[self._token_environment_key] = previous_token
        if close_failure is not None or (job is not None and not job_handle_closed):
            result = _worker_failure(
                application_id,
                "controller_failed",
                backup=backup,
                cleanup_attempted=None if gate_opened else False,
                cleanup_complete=False if gate_opened else None,
                worker_alive=worker_alive,
                process_tree_terminated=tree_terminated,
            )
            if close_failure is not None:
                result["error"]["exception_type"] = type(close_failure).__name__
        if result is None:
            result = _worker_failure(application_id, "controller_result_invalid", backup=backup)
        result["controller_lifecycle"] = {
            "job_assigned": job_assigned,
            "gate_opened": gate_opened,
            "result_received": result_received,
            "natural_exit": natural_exit,
            "job_handle_closed": job_handle_closed,
        }
        return result


def _run_public_controller(
    application_id: str,
    *,
    env: MutableMapping[str, str] | None = None,
    backup_controller: Any | None = None,
    controller: Any | None = None,
    deadline: float = PHASE21_TOTAL_DEADLINE_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, object]:
    """Public execute parent: canonical backup first, then one bounded worker."""

    if application_id not in _APPROVED_SET:
        result = _failure_result(application_id, "application_not_approved")
        result["submission"] = "unknown"
        return result
    deadline_at = clock() + float(deadline)
    environment = os.environ if env is None else env
    _apply_safety_environment(environment)
    try:
        data_dir = _canonical_live_data_directory(environment)
    except HarnessFailure as exc:
        result = _failure_result(application_id, exc.code)
        result["submission"] = "unknown"
        return result
    backup_path, partial_path = _new_backup_paths(data_dir, application_id)
    backup_context = {
        "application_id": application_id,
        "data_dir": str(data_dir),
        "database_path": str((data_dir / "argus.db").resolve()),
        "backup_path": str(backup_path),
        "backup_partial_path": str(partial_path),
        "token": secrets.token_hex(32),
    }
    remaining = deadline_at - clock()
    if remaining <= 0:
        return _worker_failure(
            application_id,
            "deadline_exceeded",
            cleanup_attempted=False,
            cleanup_complete=None,
            worker_alive=False,
            process_tree_terminated=False,
        )
    selected_backup = backup_controller or _WindowsJobController(
        target=_backup_worker_entry,
        token_environment_key=_BACKUP_TOKEN_ENV,
        process_name="argus-phase21-backup-worker",
    )
    backup_result = selected_backup.run(backup_context, remaining)
    if not isinstance(backup_result, Mapping) or backup_result.get("ok") is not True:
        cleanup_ok = True
        for path in (partial_path, backup_path):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                cleanup_ok = False
        if not isinstance(backup_result, Mapping):
            return _worker_failure(
                application_id,
                "backup_result_invalid",
                cleanup_attempted=False,
                cleanup_complete=None,
                worker_alive=None,
                process_tree_terminated=None,
            )
        result = dict(backup_result)
        if not cleanup_ok:
            result = _worker_failure(
                application_id,
                "backup_cleanup_failed",
                cleanup_attempted=False,
                cleanup_complete=None,
                worker_alive=result.get("cleanup", {}).get("worker_alive")
                if isinstance(result.get("cleanup"), Mapping)
                else None,
                process_tree_terminated=False,
            )
        return result
    evidence_value = backup_result.get("backup")
    evidence = dict(evidence_value) if isinstance(evidence_value, Mapping) else {}
    if (
        backup_result.get("application_id") != application_id
        or Path(str(backup_result.get("backup_path") or "")).resolve() != backup_path
        or not backup_path.is_file()
        or partial_path.exists()
        or evidence.get("filename") != backup_path.name
        or evidence.get("bytes") != backup_path.stat().st_size
        or evidence.get("integrity_check") != "ok"
        or evidence.get("foreign_key_violations") != 0
        or evidence.get("source_integrity_check") != "ok"
        or evidence.get("source_foreign_key_violations") != 0
        or evidence.get("method") != "sqlite_online_backup"
        or not re.fullmatch(r"[0-9a-f]{64}", str(evidence.get("sha256") or ""))
    ):
        partial_path.unlink(missing_ok=True)
        backup_path.unlink(missing_ok=True)
        return _worker_failure(
            application_id,
            "backup_verification_failed",
            cleanup_attempted=False,
            cleanup_complete=None,
            worker_alive=False,
            process_tree_terminated=False,
        )
    remaining = deadline_at - clock()
    if remaining <= 0:
        return _worker_failure(
            application_id,
            "deadline_exceeded",
            backup=evidence,
            cleanup_attempted=False,
            cleanup_complete=None,
            worker_alive=False,
            process_tree_terminated=False,
        )
    backup = BackupBundle(backup_path, evidence)
    context = {
        "application_id": application_id,
        "data_dir": str(data_dir),
        "backup_path": str(backup.path.resolve()),
        "backup": evidence,
        "token": secrets.token_hex(32),
    }
    selected = controller or _WindowsJobController()
    result = selected.run(context, remaining)
    if not isinstance(result, Mapping):
        return _worker_failure(application_id, "controller_result_invalid", backup=evidence)
    return dict(result)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bounded Phase 21 PREFILL measurement")
    parser.add_argument("--application-id", required=True, choices=APPROVED_APPLICATION_IDS)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Create the verified backup and execute the one allowed PREFILL request",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.execute:
        result = {
            "application_id": args.application_id,
            "execute": False,
            "operation": "phase21_prefill_measurement_plan",
            "request": _RUN_PATH.format(application_id=args.application_id),
        }
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    result = _run_public_controller(args.application_id)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
