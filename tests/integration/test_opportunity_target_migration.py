from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from app.config import Settings
from app.db import Database
from app.domain.states import ApplicationState
from app.models import Application, Opportunity


def _create_version_one_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(
        """
        PRAGMA user_version = 1;
        CREATE TABLE opportunities (
          id VARCHAR(36) NOT NULL PRIMARY KEY,
          employer VARCHAR(240) NOT NULL,
          role_title VARCHAR(320) NOT NULL,
          division VARCHAR(240) NOT NULL DEFAULT '',
          programme_group VARCHAR(120) NOT NULL DEFAULT '',
          location VARCHAR(240) NOT NULL DEFAULT '',
          cycle VARCHAR(40) NOT NULL,
          url TEXT NOT NULL UNIQUE,
          source VARCHAR(120) NOT NULL DEFAULT 'manual',
          ats_type VARCHAR(80) NOT NULL DEFAULT 'unknown',
          deadline DATE,
          rolling BOOLEAN NOT NULL DEFAULT 0,
          min_graduation_year INTEGER,
          max_graduation_year INTEGER,
          sponsorship_supported BOOLEAN,
          cv_required BOOLEAN NOT NULL DEFAULT 1,
          cover_letter_required BOOLEAN NOT NULL DEFAULT 0,
          written_answers_required BOOLEAN NOT NULL DEFAULT 0,
          notes TEXT NOT NULL DEFAULT '',
          created_at DATETIME NOT NULL,
          updated_at DATETIME NOT NULL
        );
        CREATE TABLE applications (
          id VARCHAR(36) NOT NULL PRIMARY KEY,
          opportunity_id VARCHAR(36) NOT NULL UNIQUE
            REFERENCES opportunities(id) ON DELETE CASCADE,
          state VARCHAR(60) NOT NULL,
          priority INTEGER NOT NULL DEFAULT 50,
          risk_level INTEGER NOT NULL DEFAULT 0,
          risk_assessed_at DATETIME,
          risk_assessment_source VARCHAR(120),
          selected_cv_id VARCHAR(36),
          selected_cover_letter_id VARCHAR(36),
          eligibility_json TEXT NOT NULL DEFAULT '{}',
          conflict_json TEXT NOT NULL DEFAULT '{}',
          next_action VARCHAR(240) NOT NULL DEFAULT '',
          next_action_deadline DATETIME,
          submission_reference VARCHAR(240) NOT NULL DEFAULT '',
          applied_at DATETIME,
          created_at DATETIME NOT NULL,
          updated_at DATETIME NOT NULL
        );
        INSERT INTO opportunities (
          id, employer, role_title, cycle, url, source, ats_type,
          created_at, updated_at
        ) VALUES (
          'opp-v1', 'Acme Capital', 'Summer Analyst', '2027',
          'https://careers.example.test/programmes?jobId=R1',
          'trackr_html:old.html', 'unknown',
          '2026-08-20T10:00:00+00:00', '2026-08-20T10:00:00+00:00'
        );
        INSERT INTO applications (
          id, opportunity_id, state, created_at, updated_at
        ) VALUES (
          'app-v1', 'opp-v1', 'DISCOVERED',
          '2026-08-20T10:00:00+00:00', '2026-08-20T10:00:00+00:00'
        );
        """
    )
    connection.commit()
    connection.close()


def _add_canonical_collision_and_schema_objects(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(
        """
        CREATE TABLE opportunity_trigger_log (opportunity_id TEXT NOT NULL);
        CREATE INDEX ix_opportunities_role_custom ON opportunities(role_title);
        CREATE TRIGGER trg_opportunities_update_custom
        AFTER UPDATE ON opportunities
        BEGIN
          INSERT INTO opportunity_trigger_log(opportunity_id) VALUES (NEW.id);
        END;
        INSERT INTO opportunities (
          id, employer, role_title, cycle, url, source, ats_type,
          created_at, updated_at
        ) VALUES (
          'opp-collision', 'Acme Capital', 'Summer Analyst', '2027',
          'https://careers.example.test/programmes/?utm_source=feed&jobId=R1#apply',
          'trackr_html:second.html', 'unknown',
          '2026-08-21T10:00:00+00:00', '2026-08-21T10:00:00+00:00'
        );
        INSERT INTO applications (
          id, opportunity_id, state, created_at, updated_at
        ) VALUES (
          'app-collision', 'opp-collision', 'DISCOVERED',
          '2026-08-21T10:00:00+00:00', '2026-08-21T10:00:00+00:00'
        );
        """
    )
    connection.commit()
    connection.close()


def _add_known_column_check_constraint(path: Path) -> None:
    """Rebuild the synthetic v1 parent with an extra table CHECK constraint."""

    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=OFF")
    original_sql = connection.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type='table' AND name='opportunities'"
    ).fetchone()[0]
    checked_sql = original_sql.replace(
        "CREATE TABLE opportunities",
        "CREATE TABLE opportunities_checked",
        1,
    ).rsplit(")", 1)[0]
    checked_sql += ", CONSTRAINT role_guard CHECK(length(role_title) > 0))"
    connection.execute(checked_sql)
    connection.execute("INSERT INTO opportunities_checked SELECT * FROM opportunities")
    connection.execute("DROP TABLE opportunities")
    connection.execute("ALTER TABLE opportunities_checked RENAME TO opportunities")
    connection.commit()
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()


def test_version_one_opportunities_migrate_source_only_without_losing_relations(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "argus.db"
    _create_version_one_database(database_path)
    database = Database(Settings.load({"ARGUS_DATA_DIR": str(tmp_path)}))

    database.create_schema()
    database.create_schema()  # idempotence is part of the migration contract

    with database.engine.connect() as connection:
        columns = {item["name"] for item in inspect(connection).get_columns("opportunities")}
        migrated = connection.execute(
            text(
                "SELECT id, url, source_fingerprint, application_url, target_status, "
                "resolved_ats_type, resolution_evidence_json, resolved_at, "
                "resolution_attempted_at FROM opportunities WHERE id='opp-v1'"
            )
        ).mappings().one()
        version = connection.execute(text("PRAGMA user_version")).scalar_one()
        foreign_key_errors = list(connection.execute(text("PRAGMA foreign_key_check")))

    assert {
        "source_fingerprint",
        "application_url",
        "target_status",
        "resolved_ats_type",
        "resolution_evidence_json",
        "resolved_at",
        "resolution_attempted_at",
    } <= columns
    assert migrated["id"] == "opp-v1"
    assert migrated["url"] == "https://careers.example.test/programmes?jobId=R1"
    assert migrated["source_fingerprint"]
    assert migrated["application_url"] is None
    assert migrated["target_status"] == "UNRESOLVED"
    assert migrated["resolved_ats_type"] == ""
    assert migrated["resolution_evidence_json"] == "{}"
    assert migrated["resolved_at"] is None
    assert migrated["resolution_attempted_at"] is None
    assert version >= 2
    assert foreign_key_errors == []

    with database.session_scope() as session:
        original = session.get(Opportunity, "opp-v1")
        assert original is not None
        assert original.application is not None
        assert original.application.id == "app-v1"


def test_migration_demotes_legacy_ready_application_without_verified_target(
    tmp_path: Path,
) -> None:
    """A source-only v1 row must not remain actionable after target separation."""

    database_path = tmp_path / "argus.db"
    _create_version_one_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE applications SET state=?, next_action=? WHERE id=?",
            ("READY_TO_SUBMIT", "Review and submit", "app-v1"),
        )
        connection.commit()

    database = Database(Settings.load({"ARGUS_DATA_DIR": str(tmp_path)}))
    database.create_schema()
    database.create_schema()  # repair is idempotent on every startup

    with database.session_scope() as session:
        application = session.get(Application, "app-v1")
        assert application is not None
        assert application.state == ApplicationState.NEEDS_USER.value
        assert application.next_action == "Resolve application target"


def test_version_two_allows_distinct_roles_to_share_a_source_url(tmp_path: Path) -> None:
    database_path = tmp_path / "argus.db"
    _create_version_one_database(database_path)
    database = Database(Settings.load({"ARGUS_DATA_DIR": str(tmp_path)}))
    database.create_schema()

    with database.session_scope() as session:
        second = Opportunity(
            employer="Acme Capital",
            role_title="Spring Insight",
            cycle="2027",
            url="https://careers.example.test/programmes?jobId=R1",
        )
        session.add(second)
        session.flush()
        assert second.id != "opp-v1"
        assert second.source_fingerprint != session.get(Opportunity, "opp-v1").source_fingerprint


def test_foreign_keys_are_enabled_after_pool_dispose_and_cascade_delete(
    tmp_path: Path,
) -> None:
    """A newly opened pooled connection must not silently disable cascades."""

    database = Database(Settings.load({"ARGUS_DATA_DIR": str(tmp_path)}))
    database.create_schema()
    with database.session_scope() as session:
        opportunity = Opportunity(
            employer="Cascade Capital",
            role_title="Summer Analyst",
            cycle="2027",
            url="https://careers.example.test/cascade",
        )
        session.add(opportunity)
        session.flush()
        application = Application(
            opportunity_id=opportunity.id,
            state=ApplicationState.DISCOVERED.value,
        )
        session.add(application)
        session.flush()
        opportunity_id = opportunity.id
        application_id = application.id

    database.engine.dispose()
    with database.engine.begin() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
        connection.execute(
            text("DELETE FROM opportunities WHERE id = :id"),
            {"id": opportunity_id},
        )

    database.engine.dispose()
    with database.engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
        remaining = connection.execute(
            text("SELECT COUNT(*) FROM applications WHERE id = :id"),
            {"id": application_id},
        ).scalar_one()
    assert remaining == 0


def test_migration_preserves_canonical_fingerprint_collisions_without_unique_index(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "argus.db"
    _create_version_one_database(database_path)
    _add_canonical_collision_and_schema_objects(database_path)
    database = Database(Settings.load({"ARGUS_DATA_DIR": str(tmp_path)}))

    database.create_schema()

    with database.engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT id, source_fingerprint FROM opportunities "
                "WHERE id IN ('opp-v1', 'opp-collision') ORDER BY id"
            )
        ).all()
        application_count = connection.execute(
            text(
                "SELECT COUNT(*) FROM applications "
                "WHERE id IN ('app-v1', 'app-collision')"
            )
        ).scalar_one()
        indexes = connection.execute(text("PRAGMA index_list(opportunities)")).all()

    assert len(rows) == 2
    assert rows[0].source_fingerprint == rows[1].source_fingerprint
    assert application_count == 2
    fingerprint_indexes = []
    with sqlite3.connect(database_path) as connection:
        for index in indexes:
            columns = [
                item[2]
                for item in connection.execute(
                    f'PRAGMA index_info("{index[1]}")'
                ).fetchall()
            ]
            if columns == ["source_fingerprint"]:
                fingerprint_indexes.append(index)
    assert fingerprint_indexes
    assert all(index[2] == 0 for index in fingerprint_indexes)


def test_file_migration_creates_verified_backup_and_preserves_schema_objects(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "argus.db"
    _create_version_one_database(database_path)
    _add_canonical_collision_and_schema_objects(database_path)
    database = Database(Settings.load({"ARGUS_DATA_DIR": str(tmp_path)}))

    database.create_schema()

    backup_path = database.last_migration_backup_path
    assert backup_path is not None
    assert backup_path.is_file()
    assert backup_path != database_path
    with sqlite3.connect(backup_path) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute("PRAGMA foreign_key_check").fetchall() == []
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 1
        assert backup.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0] == 2

    with database.engine.begin() as connection:
        objects = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE name IN "
                    "('ix_opportunities_role_custom', 'trg_opportunities_update_custom')"
                )
            )
        }
        connection.execute(
            text("UPDATE opportunities SET notes='trigger-check' WHERE id='opp-v1'")
        )
        trigger_rows = connection.execute(
            text(
                "SELECT COUNT(*) FROM opportunity_trigger_log "
                "WHERE opportunity_id='opp-v1'"
            )
        ).scalar_one()
    assert objects == {
        "ix_opportunities_role_custom",
        "trg_opportunities_update_custom",
    }
    assert trigger_rows == 1

    database.create_schema()
    assert database.last_migration_backup_path == backup_path


def test_migration_refuses_unknown_constrained_column_without_mutating_source(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "argus.db"
    _create_version_one_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "ALTER TABLE opportunities ADD COLUMN custom_data TEXT NOT NULL "
            "DEFAULT 'legacy-value' CHECK (length(custom_data) > 0)"
        )
        connection.commit()
        original_sql = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='opportunities'"
        ).fetchone()[0]
        original_row = connection.execute(
            "SELECT id, custom_data FROM opportunities WHERE id='opp-v1'"
        ).fetchone()

    database = Database(Settings.load({"ARGUS_DATA_DIR": str(tmp_path)}))

    with pytest.raises(RuntimeError, match=r"unknown.*custom_data"):
        database.create_schema()

    backup_path = database.last_migration_backup_path
    assert backup_path is not None and backup_path.is_file()
    with sqlite3.connect(backup_path) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute("PRAGMA foreign_key_check").fetchall() == []
        assert backup.execute(
            "SELECT custom_data FROM opportunities WHERE id='opp-v1'"
        ).fetchone() == ("legacy-value",)

    with sqlite3.connect(database_path) as connection:
        current_sql = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='opportunities'"
        ).fetchone()[0]
        current_row = connection.execute(
            "SELECT id, custom_data FROM opportunities WHERE id='opp-v1'"
        ).fetchone()
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        version = connection.execute("PRAGMA user_version").fetchone()[0]

    assert current_sql == original_sql
    assert current_row == original_row == ("opp-v1", "legacy-value")
    assert "opportunities_v2_migration" not in tables
    assert version == 1


def test_migration_refuses_unknown_check_constraint_on_known_columns(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "argus.db"
    _create_version_one_database(database_path)
    _add_known_column_check_constraint(database_path)
    with sqlite3.connect(database_path) as connection:
        original_sql = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='opportunities'"
        ).fetchone()[0]

    database = Database(Settings.load({"ARGUS_DATA_DIR": str(tmp_path)}))

    with pytest.raises(RuntimeError, match="CHECK constraints"):
        database.create_schema()

    assert database.last_migration_backup_path is not None
    assert database.last_migration_backup_path.is_file()
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='opportunities'"
        ).fetchone()[0] == original_sql
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


class _FailingCursor:
    def __init__(self, cursor, failure_prefix: str):
        self._cursor = cursor
        self._failure_prefix = failure_prefix

    def execute(self, sql, parameters=()):
        compact = " ".join(str(sql).split())
        if compact.startswith(self._failure_prefix):
            raise sqlite3.OperationalError("injected DDL-boundary failure")
        return self._cursor.execute(sql, parameters)

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _FailingConnection:
    def __init__(self, connection, failure_prefix: str):
        self._connection = connection
        self._failure_prefix = failure_prefix

    def cursor(self, *args, **kwargs):
        return _FailingCursor(
            self._connection.cursor(*args, **kwargs), self._failure_prefix
        )

    def __getattr__(self, name):
        return getattr(self._connection, name)


@pytest.mark.parametrize(
    "failure_prefix",
    [
        "ALTER TABLE opportunities_v2_migration RENAME TO opportunities",
        "CREATE INDEX ix_opportunities_source_fingerprint",
    ],
)
def test_ddl_boundary_failure_rolls_back_and_preserves_original(
    tmp_path: Path,
    monkeypatch,
    failure_prefix: str,
) -> None:
    database_path = tmp_path / "argus.db"
    _create_version_one_database(database_path)
    database = Database(Settings.load({"ARGUS_DATA_DIR": str(tmp_path)}))
    real_raw_connection = database.engine.raw_connection

    def failing_raw_connection():
        return _FailingConnection(real_raw_connection(), failure_prefix)

    monkeypatch.setattr(database.engine, "raw_connection", failing_raw_connection)

    with pytest.raises(sqlite3.OperationalError, match="DDL-boundary"):
        database.create_schema()

    assert database.last_migration_backup_path is not None
    assert database.last_migration_backup_path.is_file()
    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        opportunity = connection.execute(
            "SELECT id, url FROM opportunities WHERE id='opp-v1'"
        ).fetchone()
        application = connection.execute(
            "SELECT opportunity_id FROM applications WHERE id='app-v1'"
        ).fetchone()
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    assert "opportunities" in tables
    assert "opportunities_v2_migration" not in tables
    assert opportunity == (
        "opp-v1",
        "https://careers.example.test/programmes?jobId=R1",
    )
    assert application == ("opp-v1",)
    assert foreign_key_errors == []


def test_invalid_legacy_foreign_keys_abort_before_rebuild(tmp_path: Path) -> None:
    database_path = tmp_path / "argus.db"
    _create_version_one_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            """
            INSERT INTO applications (
              id, opportunity_id, state, created_at, updated_at
            ) VALUES ('orphan', 'missing', 'DISCOVERED', '2026-08-22', '2026-08-22')
            """
        )
        connection.commit()
    database = Database(Settings.load({"ARGUS_DATA_DIR": str(tmp_path)}))

    with pytest.raises(RuntimeError, match="foreign key"):
        database.create_schema()

    assert database.last_migration_backup_path is not None
    with sqlite3.connect(database_path) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(opportunities)")]
        assert "source_fingerprint" not in columns
        assert connection.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0] == 1


def test_in_memory_v1_migration_needs_no_filesystem_backup(tmp_path: Path) -> None:
    settings = replace(
        Settings.load({"ARGUS_DATA_DIR": str(tmp_path)}),
        database_url="sqlite:///:memory:",
    )
    database = Database(settings)
    raw = database.engine.raw_connection()
    try:
        raw.executescript(
            """
            CREATE TABLE opportunities (
              id TEXT PRIMARY KEY, employer TEXT NOT NULL, role_title TEXT NOT NULL,
              division TEXT NOT NULL DEFAULT '', programme_group TEXT NOT NULL DEFAULT '',
              location TEXT NOT NULL DEFAULT '', cycle TEXT NOT NULL, url TEXT NOT NULL UNIQUE,
              source TEXT NOT NULL DEFAULT 'manual', ats_type TEXT NOT NULL DEFAULT 'unknown',
              deadline DATE, rolling BOOLEAN NOT NULL DEFAULT 0,
              min_graduation_year INTEGER, max_graduation_year INTEGER,
              sponsorship_supported BOOLEAN, cv_required BOOLEAN NOT NULL DEFAULT 1,
              cover_letter_required BOOLEAN NOT NULL DEFAULT 0,
              written_answers_required BOOLEAN NOT NULL DEFAULT 0, notes TEXT NOT NULL DEFAULT '',
              created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
            );
            INSERT INTO opportunities (
              id, employer, role_title, cycle, url, created_at, updated_at
            ) VALUES ('memory-opp', 'Memory', 'Summer Analyst', '2027',
              'https://example.test/memory', '2026-08-20', '2026-08-20');
            """
        )
        raw.commit()
    finally:
        raw.close()

    database.create_schema()

    with database.engine.connect() as connection:
        assert connection.execute(
            text("SELECT COUNT(*) FROM opportunities WHERE id='memory-opp'")
        ).scalar_one() == 1
    assert database.last_migration_backup_path is None
