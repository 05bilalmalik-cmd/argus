from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlalchemy import select

from app.config import Settings
from app.db import Database
from app.models import AuditEvent
from app.security.audit import (
    AuditInput,
    _hash_event,
    append_audit,
    verify_audit_chain,
)


def test_broken_legacy_epoch_is_preserved_while_future_epoch_verifies(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    database_path = data_dir / "argus.db"
    created_one = "2026-08-24T00:00:00.000000+00:00"
    created_two = "2026-08-24T00:00:01.000000+00:00"
    details_one = '{"n":1}'
    details_two = '{"n":2}'
    first_hash = _hash_event(
        "", created_one, "legacy", "legacy.one", "fixture", "1", details_one
    )
    # Deliberately fork event 2 from genesis, preserving the historical shape.
    fork_hash = _hash_event(
        "", created_two, "legacy", "legacy.two", "fixture", "2", details_two
    )
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE audit_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              created_at VARCHAR(40) NOT NULL,
              actor VARCHAR(120) NOT NULL,
              event_type VARCHAR(160) NOT NULL,
              entity_type VARCHAR(120) NOT NULL,
              entity_id VARCHAR(120) NOT NULL,
              details_json TEXT NOT NULL,
              previous_hash VARCHAR(64) NOT NULL DEFAULT '',
              event_hash VARCHAR(64) NOT NULL UNIQUE
            );
            """
        )
        connection.executemany(
            """
            INSERT INTO audit_events
              (id, created_at, actor, event_type, entity_type, entity_id,
               details_json, previous_hash, event_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (1, created_one, "legacy", "legacy.one", "fixture", "1", details_one, "", first_hash),
                (2, created_two, "legacy", "legacy.two", "fixture", "2", details_two, "", fork_hash),
            ],
        )

    database = Database(Settings.load({"ARGUS_DATA_DIR": str(data_dir)}))
    database.create_schema()
    with database.SessionLocal() as session:
        historical = verify_audit_chain(session)
        assert historical.valid is False
        assert historical.broken_event_id == 2

        append_audit(
            session,
            AuditInput("future", "future.event", "fixture", "3", {"n": 3}),
        )
        session.commit()

    with database.SessionLocal() as session:
        events = list(session.scalars(select(AuditEvent).order_by(AuditEvent.id)).all())
        assert [event.epoch for event in events] == [0, 0, 1]
        assert verify_audit_chain(session).valid is False
        future = verify_audit_chain(session, epoch=1)
        assert future.valid is True
        assert future.checked_events == 1
