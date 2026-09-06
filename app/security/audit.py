"""Durable, tamper-evident audit intents and a serialized SQLite drain.

The historical production chain forked at event 4819 because two sessions
read the same head and each flushed a child before either caller committed.
The old process-local lock could not cover that commit-order window and did
not work across processes.

New writes therefore use a transactional outbox:

* :func:`append_audit` adds an ``audit_outbox`` intent to the caller's
  transaction without flushing or taking a process-global lock.  The business
  mutation and its audit intent commit (or roll back) together.
* an ``after_commit`` session hook invokes :func:`drain_audit_outbox`.  The
  drain takes SQLite's short-lived ``BEGIN IMMEDIATE`` write reservation,
  emits pending events and advances the durable chain head in one transaction,
  then releases it.  A second process waits for that transaction rather than
  constructing a competing child.
* a crash before or during the drain leaves the intent ``PENDING``.  Recovery
  can call the same drain after restart; no audit evidence is silently
  discarded and no already-committed business mutation is rolled back.

The legacy rows are never rewritten.  If retained history is already broken
before a durable state row exists, the first future drain starts a new epoch at
a fresh genesis marker.  Once a state row exists, any current-epoch mismatch
is refused rather than normalized.  Verification reports whole-history
failure honestly while allowing an individual future epoch or explicit range
to be checked independently.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from sqlalchemy import event, func, inspect as sa_inspect, select, text
from sqlalchemy.orm import Session

from app.models import AuditChainState, AuditEvent, AuditOutbox


@dataclass(frozen=True, slots=True)
class AuditInput:
    actor: str
    event_type: str
    entity_type: str
    entity_id: str
    details: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class AuditVerification:
    valid: bool
    checked_events: int
    broken_event_id: int | None
    expected_hash: str | None = None
    actual_hash: str | None = None
    epoch: int | None = None
    start_event_id: int | None = None
    end_event_id: int | None = None
    pending_events: int = 0
    complete: bool = True
    state_consistent: bool = True

    @property
    def pending_count(self) -> int:
        """Compatibility spelling for callers that expose a count to users."""

        return self.pending_events

    @property
    def incomplete(self) -> bool:
        """Whether durable evidence still needs recovery or is inconsistent."""

        return not self.complete


@dataclass(frozen=True, slots=True)
class AuditDrainResult:
    emitted_events: int
    pending_events: int
    epoch: int | None


class AuditDrainError(RuntimeError):
    """Raised when pending audit evidence cannot be emitted safely."""


class AuditChainConflict(AuditDrainError):
    """Compatibility error for callers that used the former retry API."""


class AuditSession(Session):
    """Session carrying its engine so commit hooks can drain its outbox."""

    def __init__(self, *args: Any, audit_engine: Any = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if audit_engine is not None:
            self.info["_argus_audit_engine"] = audit_engine


_PENDING_KEY = "_argus_pending_audit_records"
_LOCAL_ID_KEY = "_argus_next_pending_audit_id"
# The Database engine installs a savepoint-boundary SQLite root BEGIN hook. The
# drain opts out because it issues its own stronger BEGIN IMMEDIATE.
SQLITE_DRAIN_BEGIN_OPTION = "argus_skip_sqlite_root_begin"


AUDIT_CHAIN_TRIGGER_SQL = """
CREATE TRIGGER IF NOT EXISTS trg_audit_events_chain_integrity
BEFORE INSERT ON audit_events
WHEN EXISTS (
  SELECT 1
  FROM audit_chain_state
  WHERE id = 1
    AND (
      COALESCE(NEW.epoch, 0) <> epoch
      OR COALESCE(NEW.previous_hash, '') <> head_hash
    )
)
BEGIN
  SELECT RAISE(ABORT, 'audit_chain_fork_refused');
END;
"""

AUDIT_CHAIN_STATE_TRIGGER_SQL = """
CREATE TRIGGER IF NOT EXISTS trg_audit_events_chain_state
AFTER INSERT ON audit_events
WHEN EXISTS (
  SELECT 1
  FROM audit_chain_state
  WHERE id = 1
    AND COALESCE(NEW.epoch, 0) = epoch
    AND COALESCE(NEW.previous_hash, '') = head_hash
)
BEGIN
  UPDATE audit_chain_state
  SET head_event_id = NEW.id,
      head_hash = NEW.event_hash
  WHERE id = 1;
END;
"""


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _event_fingerprint(
    created_at: str,
    actor: str,
    event_type: str,
    entity_type: str,
    entity_id: str,
    details_json: str,
) -> str:
    return json.dumps(
        [created_at, actor, event_type, entity_type, entity_id, details_json],
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _hash_event(
    previous_hash: str,
    created_at: str,
    actor: str,
    event_type: str,
    entity_type: str,
    entity_id: str,
    details_json: str,
) -> str:
    payload = "\x1f".join(
        [
            previous_hash,
            created_at,
            actor,
            event_type,
            entity_type,
            entity_id,
            details_json,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _pending_pairs(session: Session) -> list[tuple[AuditEvent, AuditOutbox]]:
    return session.info.setdefault(_PENDING_KEY, [])


def _next_local_id(session: Session) -> int:
    value = int(session.info.get(_LOCAL_ID_KEY, -1))
    session.info[_LOCAL_ID_KEY] = value - 1
    return value


def _session_chain_head(session: Session) -> tuple[int, str]:
    """Return the durable epoch/head visible to this session."""

    state = session.scalar(select(AuditChainState).where(AuditChainState.id == 1))
    if state is not None:
        return int(state.epoch), state.head_hash or ""
    previous = session.scalar(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(1))
    if previous is None:
        return 0, ""
    return int(getattr(previous, "epoch", 0) or 0), previous.event_hash


def _before_flush(session: Session, _flush_context: Any, _instances: Any) -> None:
    """Copy compatibility-object mutations into their durable intents."""

    for record, intent in _pending_pairs(session):
        # Returning a transient AuditEvent preserves the historical API used
        # by diagnostics/tests.  Its mutable fields remain the source of truth
        # until the enclosing transaction flushes.
        intent.created_at = record.created_at
        intent.actor = record.actor
        intent.event_type = record.event_type
        intent.entity_type = record.entity_type
        intent.entity_id = record.entity_id
        intent.details_json = record.details_json


def _after_flush_postexec(session: Session, _flush_context: Any) -> None:
    for record, intent in _pending_pairs(session):
        if intent.id is not None:
            # This ID is only a useful compatibility handle while the intent
            # is in-flight; the eventual AuditEvent gets its own DB sequence.
            record.id = int(intent.id)


def _after_rollback(session: Session) -> None:
    # A savepoint rollback must not discard intents queued by the enclosing
    # transaction.  The root rollback clears all compatibility objects below.
    if session.in_nested_transaction():
        pending = _pending_pairs(session)
        pending[:] = [
            (record, intent)
            for record, intent in pending
            if sa_inspect(intent).persistent
        ]
        return
    _pending_pairs(session).clear()


def _after_commit(session: Session) -> None:
    # SQLAlchemy emits after_commit for SAVEPOINT release as well as for the
    # root transaction.  Draining here would race the still-open outer
    # transaction and could emit an intent that the outer rollback removes.
    if session.in_nested_transaction():
        return
    engine = session.info.get("_argus_audit_engine")
    if engine is None:
        return
    pending = _pending_pairs(session)
    if not pending:
        # A caller may commit ordinary business work or a legacy direct
        # AuditEvent row.  Do not re-open the audit writer on every commit;
        # only a durable pending outbox is a reason to drain.  This also lets
        # the verifier/reporting path observe a tampered chain before an
        # explicit recovery attempt refuses it.
        with engine.connect() as connection:
            has_pending = connection.execute(
                text(
                    "SELECT 1 FROM audit_outbox WHERE status=:status LIMIT 1"
                ),
                {"status": AuditOutbox.PENDING},
            ).first()
        if has_pending is None:
            return
    try:
        # The business transaction is already committed at this point.  A
        # drain error is intentionally surfaced to the caller, but rollback
        # cannot undo the business mutation; the durable outbox remains.
        drain_audit_outbox(engine)
    finally:
        pending.clear()


# Hooks live on the dedicated session class, so unrelated SQLAlchemy sessions
# (including read-only tools) do not unexpectedly open a writer on commit.
event.listen(AuditSession, "before_flush", _before_flush)
event.listen(AuditSession, "after_flush_postexec", _after_flush_postexec)
event.listen(AuditSession, "after_rollback", _after_rollback)
event.listen(AuditSession, "after_commit", _after_commit)


def _verify_rows(
    events: Iterable[Any],
    *,
    all_events: list[Any],
    start_event_id: int | None,
    end_event_id: int | None,
) -> AuditVerification:
    selected = list(events)
    if not selected:
        return AuditVerification(
            valid=True,
            checked_events=0,
            broken_event_id=None,
            epoch=None,
            start_event_id=start_event_id,
            end_event_id=end_event_id,
        )

    # Events are globally ordered by their immutable database ID.  Pending
    # compatibility records are appended after committed rows and carry
    # negative local IDs only until their outbox flushes.
    selected_epochs: list[int] = []
    for event_row in selected:
        epoch = int(getattr(event_row, "epoch", 0) or 0)
        if epoch not in selected_epochs:
            selected_epochs.append(epoch)

    checked = 0
    for epoch in selected_epochs:
        group = [row for row in selected if int(getattr(row, "epoch", 0) or 0) == epoch]
        # Committed rows have immutable positive IDs and are already globally
        # ordered.  Pending compatibility records use negative local IDs;
        # sorting those numerically would reverse the caller's append order.
        if not any(row.id is not None and int(row.id) < 0 for row in group):
            group.sort(key=lambda row: (int(row.id) if row.id is not None else 0))
        previous_hash = ""
        if start_event_id is not None:
            prior = [
                row
                for row in all_events
                if int(getattr(row, "epoch", 0) or 0) == epoch
                and row.id is not None
                and int(row.id) < start_event_id
            ]
            if prior:
                prior.sort(key=lambda row: int(row.id))
                previous_hash = prior[-1].event_hash
        for event_row in group:
            expected = _hash_event(
                previous_hash,
                event_row.created_at,
                event_row.actor,
                event_row.event_type,
                event_row.entity_type,
                event_row.entity_id,
                event_row.details_json,
            )
            if event_row.previous_hash != previous_hash or event_row.event_hash != expected:
                return AuditVerification(
                    valid=False,
                    checked_events=checked,
                    broken_event_id=event_row.id,
                    expected_hash=expected,
                    actual_hash=event_row.event_hash,
                    epoch=epoch,
                    start_event_id=start_event_id,
                    end_event_id=end_event_id,
                )
            checked += 1
            previous_hash = event_row.event_hash
    return AuditVerification(
        valid=True,
        checked_events=checked,
        broken_event_id=None,
        epoch=selected_epochs[0] if len(selected_epochs) == 1 else None,
        start_event_id=start_event_id,
        end_event_id=end_event_id,
    )


def verify_audit_chain(
    session: Session,
    *,
    epoch: int | None = None,
    start_event_id: int | None = None,
    end_event_id: int | None = None,
) -> AuditVerification:
    """Verify all retained epochs or an explicitly selected epoch/range.

    With no filter, ``valid`` means every retained epoch is valid.  A broken
    historical epoch therefore remains a visible failure even when a later
    epoch is healthy.  ``epoch=...`` or an ID range gives callers an honest,
    bounded verification result for future evidence.
    """

    committed = list(session.scalars(select(AuditEvent).order_by(AuditEvent.id)).all())
    pending_pairs = _pending_pairs(session)
    pending = [record for record, _intent in pending_pairs]
    all_events: list[Any] = committed + pending

    # The outbox is durable evidence even when this is a brand-new process.
    # Do not let a fresh verifier call a history complete while a committed
    # business change is still waiting for audit emission.  Unflushed local
    # compatibility intents are counted separately; after a flush they are
    # already visible to the same transaction's query.
    with session.no_autoflush:
        durable_pending = int(
            session.scalar(
                select(func.count())
                .select_from(AuditOutbox)
                .where(AuditOutbox.status == AuditOutbox.PENDING)
            )
            or 0
        )
    unflushed_pending = sum(1 for _record, intent in pending_pairs if intent.id is None)
    pending_count = durable_pending + unflushed_pending

    state_consistent = _verify_durable_tail(session, committed)

    if start_event_id is not None or end_event_id is not None:
        all_events = [
            row
            for row in all_events
            if row.id is not None
            and (start_event_id is None or int(row.id) >= start_event_id)
            and (end_event_id is None or int(row.id) <= end_event_id)
        ]
    if epoch is not None:
        all_events = [
            row for row in all_events if int(getattr(row, "epoch", 0) or 0) == epoch
        ]
    result = _verify_rows(
        all_events,
        all_events=committed + pending,
        start_event_id=start_event_id,
        end_event_id=end_event_id,
    )
    local_pending = bool(pending_pairs)
    # A caller inspecting its own uncommitted compatibility objects can still
    # validate those objects, but the durable result is explicitly incomplete.
    # A fresh process has no such local view and therefore fails closed.
    valid = result.valid and state_consistent and (pending_count == 0 or local_pending)
    complete = result.valid and state_consistent and pending_count == 0
    return replace(
        result,
        valid=valid,
        pending_events=pending_count,
        complete=complete,
        state_consistent=state_consistent,
    )


def _verify_durable_tail(session: Session, committed: list[Any]) -> bool:
    """Compare the durable state row with the surviving current-epoch tail.

    A deleted or rewritten tail can leave the remaining rows internally
    linear.  The state row is an independent witness of what was last
    committed, so any disagreement is a failed verification rather than an
    opportunity to normalize the state to the damaged rows.
    """

    state = session.scalar(select(AuditChainState).where(AuditChainState.id == 1))
    if state is None:
        return True
    current_epoch = int(state.epoch)
    rows = [
        row
        for row in committed
        if int(getattr(row, "epoch", 0) or 0) == current_epoch
    ]
    rows.sort(key=lambda row: int(row.id))
    previous_hash = ""
    for row in rows:
        expected = _hash_event(
            previous_hash,
            row.created_at,
            row.actor,
            row.event_type,
            row.entity_type,
            row.entity_id,
            row.details_json,
        )
        if row.previous_hash != previous_hash or row.event_hash != expected:
            return False
        previous_hash = row.event_hash
    tail_id = int(rows[-1].id) if rows else None
    stored_id = int(state.head_event_id) if state.head_event_id is not None else None
    return stored_id == tail_id and (state.head_hash or "") == previous_hash


def _validate_epoch_rows(rows: list[dict[str, Any]], epoch: int) -> tuple[bool, str, int | None]:
    previous_hash = ""
    for row in rows:
        if int(row.get("epoch", 0) or 0) != epoch:
            continue
        expected = _hash_event(
            previous_hash,
            str(row["created_at"]),
            str(row["actor"]),
            str(row["event_type"]),
            str(row["entity_type"]),
            str(row["entity_id"]),
            str(row["details_json"]),
        )
        if row["previous_hash"] != previous_hash or row["event_hash"] != expected:
            return False, "", None
        previous_hash = str(row["event_hash"])
    tail = rows[-1] if rows else None
    return True, previous_hash, int(tail["id"]) if tail is not None else None


def _ensure_chain_state(connection) -> tuple[int, str, int | None]:
    row = connection.execute(
        text(
            "SELECT id, epoch, head_event_id, head_hash "
            "FROM audit_chain_state WHERE id=1"
        )
    ).mappings().first()
    if row is not None:
        current_epoch = int(row["epoch"])
        current_rows = [
            dict(item)
            for item in connection.execute(
                text(
                    "SELECT id, COALESCE(epoch, 0) AS epoch, created_at, actor, "
                    "event_type, entity_type, entity_id, details_json, previous_hash, "
                    "event_hash FROM audit_events WHERE COALESCE(epoch, 0)=:epoch "
                    "ORDER BY id"
                ),
                {"epoch": current_epoch},
            ).mappings()
        ]
        valid, computed_hash, computed_id = _validate_epoch_rows(
            current_rows, current_epoch
        )
        if not valid:
            # Once a durable state exists, a damaged current epoch is an
            # unreconciled evidence condition.  Refuse the drain rather than
            # making a second silent repair decision; operators can preserve
            # this epoch and explicitly create a future epoch after review.
            raise AuditDrainError(
                "audit chain state disagrees with surviving current-epoch evidence"
            )
        stored_hash = str(row["head_hash"] or "")
        stored_id = row["head_event_id"]
        if stored_hash != computed_hash or stored_id != computed_id:
            raise AuditDrainError(
                "audit chain state head does not match the surviving current-epoch tail"
            )
        return current_epoch, computed_hash, computed_id

    rows = [
        dict(item)
        for item in connection.execute(
            text(
                "SELECT id, COALESCE(epoch, 0) AS epoch, created_at, actor, "
                "event_type, entity_type, entity_id, details_json, previous_hash, "
                "event_hash FROM audit_events ORDER BY id"
            )
        ).mappings()
    ]
    current_epoch = max((int(item["epoch"]) for item in rows), default=0)
    current_rows = [item for item in rows if int(item["epoch"]) == current_epoch]
    valid, head_hash, head_id = _validate_epoch_rows(current_rows, current_epoch)
    if not valid:
        # Never repair or rewrite the old rows.  A new epoch is a clean future
        # range whose genesis is explicit and independently verifiable.
        current_epoch += 1
        head_hash = ""
        head_id = None

    connection.execute(
        text(
            "INSERT INTO audit_chain_state(id, epoch, head_event_id, head_hash) "
            "VALUES (1, :epoch, :head_event_id, :head_hash)"
        ),
        {
            "epoch": current_epoch,
            "head_event_id": head_id,
            "head_hash": head_hash,
        },
    )
    return current_epoch, head_hash, head_id


def install_chain_trigger(engine) -> None:
    """Install the epoch-aware fork trigger and initialize its durable head."""

    with engine.begin() as connection:
        # The additive migration normally adds this column before setup.  The
        # defensive branch keeps the public installer usable on an older DB
        # opened directly by a maintenance script.
        columns = {
            str(row[1]) for row in connection.execute(text("PRAGMA table_info(audit_events)"))
        }
        if columns and "epoch" not in columns:
            connection.execute(
                text("ALTER TABLE audit_events ADD COLUMN epoch INTEGER NOT NULL DEFAULT 0")
            )
        connection.execute(
            text(
                "CREATE TABLE IF NOT EXISTS audit_chain_state ("
                "id INTEGER PRIMARY KEY, epoch INTEGER NOT NULL DEFAULT 0, "
                "head_event_id INTEGER, head_hash VARCHAR(64) NOT NULL DEFAULT ''"
                ")"
            )
        )
        connection.execute(text("DROP TRIGGER IF EXISTS trg_audit_events_chain_integrity"))
        connection.execute(text("DROP TRIGGER IF EXISTS trg_audit_events_chain_state"))
        connection.execute(text(AUDIT_CHAIN_TRIGGER_SQL))
        connection.execute(text(AUDIT_CHAIN_STATE_TRIGGER_SQL))
        _ensure_chain_state(connection)


def _engine_for(bind: Any):
    if hasattr(bind, "engine"):
        return bind.engine
    if isinstance(bind, Session):
        return bind.get_bind()
    return bind


def _validated_intent_fingerprint(value: Any) -> tuple[str, str, str, str, str, str]:
    """Decode the immutable intent signature without accepting corruption."""

    try:
        decoded = json.loads(value if isinstance(value, str) else str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AuditDrainError("audit intent fingerprint is malformed JSON") from exc
    if (
        not isinstance(decoded, list)
        or len(decoded) != 6
        or any(not isinstance(item, str) for item in decoded)
    ):
        raise AuditDrainError("audit intent fingerprint has an invalid shape")
    try:
        json.loads(decoded[5])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AuditDrainError("audit intent details JSON is malformed") from exc
    return tuple(decoded)  # type: ignore[return-value]


def drain_audit_outbox(
    bind: Any,
    *,
    max_events: int | None = None,
    fail_after: int | None = None,
) -> AuditDrainResult:
    """Emit pending intents in one short, cross-process SQLite transaction.

    ``fail_after`` is intentionally supported for deterministic crash/recovery
    tests.  Raising rolls back the entire drain, leaving every intent pending.
    """

    engine = _engine_for(bind)
    connection = engine.connect().execution_options(**{SQLITE_DRAIN_BEGIN_OPTION: True})
    committed = False
    emitted = 0
    epoch: int | None = None
    try:
        # No Python lock is held here.  SQLite serializes this tiny critical
        # section for threads and independent processes alike.
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        epoch_value, head_hash, _head_id = _ensure_chain_state(connection)
        epoch = epoch_value
        query = (
            "SELECT id, created_at, actor, event_type, entity_type, entity_id, "
            "details_json, intent_fingerprint FROM audit_outbox "
            "WHERE status=:status ORDER BY id"
        )
        params: dict[str, Any] = {"status": AuditOutbox.PENDING}
        if max_events is not None:
            query += " LIMIT :limit"
            params["limit"] = max_events
        rows = list(connection.execute(text(query), params).mappings())
        for row in rows:
            created_at = str(row["created_at"])
            actor = str(row["actor"])
            event_type = str(row["event_type"])
            entity_type = str(row["entity_type"])
            entity_id = str(row["entity_id"])
            details_json = str(row["details_json"])
            try:
                json.loads(details_json)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise AuditDrainError("audit intent details JSON is malformed") from exc
            original_values = _validated_intent_fingerprint(row["intent_fingerprint"])
            current_fingerprint = _event_fingerprint(
                created_at,
                actor,
                event_type,
                entity_type,
                entity_id,
                details_json,
            )
            event_hash = _hash_event(
                head_hash,
                created_at,
                actor,
                event_type,
                entity_type,
                entity_id,
                details_json,
            )
            # Do not silently re-sign a compatibility object that was mutated
            # after append_audit returned.  Keeping its original signature
            # makes the persisted evidence fail verification just as the old
            # direct-row API did.
            if str(row["intent_fingerprint"] or "") != current_fingerprint:
                event_hash = _hash_event(head_hash, *original_values)
            result = connection.execute(
                text(
                    "INSERT INTO audit_events "
                    "(epoch, created_at, actor, event_type, entity_type, entity_id, "
                    "details_json, previous_hash, event_hash) "
                    "VALUES (:epoch, :created_at, :actor, :event_type, :entity_type, "
                    ":entity_id, :details_json, :previous_hash, :event_hash)"
                ),
                {
                    "epoch": epoch_value,
                    "created_at": created_at,
                    "actor": actor,
                    "event_type": event_type,
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "details_json": details_json,
                    "previous_hash": head_hash,
                    "event_hash": event_hash,
                },
            )
            event_id = int(result.lastrowid)
            connection.execute(
                text(
                    "UPDATE audit_outbox SET status=:emitted, attempts=attempts+1, "
                    "emitted_event_id=:event_id, emitted_at=:emitted_at, last_error='' "
                    "WHERE id=:id AND status=:pending"
                ),
                {
                    "emitted": AuditOutbox.EMITTED,
                    "event_id": event_id,
                    "emitted_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                    "id": row["id"],
                    "pending": AuditOutbox.PENDING,
                },
            )
            head_hash = event_hash
            connection.execute(
                text(
                    "UPDATE audit_chain_state SET head_event_id=:event_id, head_hash=:head_hash "
                    "WHERE id=1"
                ),
                {"event_id": event_id, "head_hash": head_hash},
            )
            emitted += 1
            if fail_after is not None and emitted >= fail_after:
                raise AuditDrainError("injected audit drain crash")
        connection.commit()
        committed = True
        pending_count = int(
            connection.execute(
                text("SELECT COUNT(*) FROM audit_outbox WHERE status=:status"),
                {"status": AuditOutbox.PENDING},
            ).scalar_one()
        )
        return AuditDrainResult(emitted, pending_count, epoch)
    except AuditDrainError:
        connection.rollback()
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed with evidence retained
        connection.rollback()
        raise AuditDrainError(f"audit drain refused: {exc}") from exc
    finally:
        if not committed:
            # A rollback after an injected failure is already done above; this
            # is defensive for drivers that raise during rollback itself.
            try:
                connection.rollback()
            except Exception:
                pass
        connection.close()


def recover_pending_audit(bind: Any, *, max_events: int | None = None) -> AuditDrainResult:
    """Explicit restart/recovery entry point for pending audit intents."""

    return drain_audit_outbox(bind, max_events=max_events)


def _committed_head(session: Session) -> str:
    """Compatibility helper returning the durable state head."""

    state = session.scalar(select(AuditChainState).where(AuditChainState.id == 1))
    if state is not None:
        return state.head_hash or ""
    previous = session.scalar(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(1))
    return previous.event_hash if previous else ""


def _session_tail(session: Session) -> str:
    pending = _pending_pairs(session)
    if pending:
        return pending[-1][0].event_hash
    return _committed_head(session)


def append_audit(
    session: Session,
    event_input: AuditInput | None = None,
    *,
    event: AuditInput | None = None,
    max_attempts: int = 12,
    retry_delay: float = 0.05,
) -> AuditEvent:
    """Queue a transactional audit intent without flushing or global locking.

    ``max_attempts`` and ``retry_delay`` remain accepted for source
    compatibility with the former implementation; serialization/retry now
    belongs to the durable drain and never rolls back this caller's session.
    """

    del max_attempts, retry_delay
    if event_input is None:
        event_input = event
    elif event is not None:
        raise TypeError("append_audit received both positional and keyword audit input")
    if event_input is None:
        raise TypeError("append_audit requires an AuditInput")
    created_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    details_json = _canonical_json(event_input.details)
    pending = _pending_pairs(session)
    if pending:
        epoch, previous_hash = pending[-1][0].epoch, pending[-1][0].event_hash
    else:
        epoch, previous_hash = _session_chain_head(session)
    record = AuditEvent(
        id=_next_local_id(session),
        epoch=epoch,
        created_at=created_at,
        actor=event_input.actor,
        event_type=event_input.event_type,
        entity_type=event_input.entity_type,
        entity_id=event_input.entity_id,
        details_json=details_json,
        previous_hash=previous_hash,
        event_hash=_hash_event(
            previous_hash,
            created_at,
            event_input.actor,
            event_input.event_type,
            event_input.entity_type,
            event_input.entity_id,
            details_json,
        ),
    )
    intent = AuditOutbox(
        created_at=created_at,
        actor=event_input.actor,
        event_type=event_input.event_type,
        entity_type=event_input.entity_type,
        entity_id=event_input.entity_id,
        details_json=details_json,
        intent_fingerprint=_event_fingerprint(
            created_at,
            event_input.actor,
            event_input.event_type,
            event_input.entity_type,
            event_input.entity_id,
            details_json,
        ),
        status=AuditOutbox.PENDING,
    )
    session.add(intent)
    pending.append((record, intent))
    return record
