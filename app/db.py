from __future__ import annotations

import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import Settings


LOGGER = logging.getLogger(__name__)
SCHEMA_VERSION = 10

# ``create_all`` adds this index for new databases through the model's
# ``UniqueConstraint``.  Existing 0.1.x databases need an explicit additive
# migration because SQLAlchemy intentionally does not alter an existing table.
_SUBMISSION_AUTHORITY_UNIQUE_INDEX = "uq_submission_authority_application_session"
_TRACKR_PROGRAMME_ID_UNIQUE_INDEX = "uq_opportunities_trackr_programme_id"

# These additions are intentionally nullable.  They make the old integer
# ``risk_level=0`` compatible while allowing new code to distinguish an
# assessed R0 from an unassessed legacy record.
_ADDITIVE_MIGRATIONS: dict[str, dict[str, str]] = {
    "opportunities": {
        "opening_date": "DATE",
        "application_window_status": "VARCHAR(40) NOT NULL DEFAULT 'UNKNOWN'",
        "trackr_programme_id": "VARCHAR(128)",
        "user_status": "VARCHAR(40) NOT NULL DEFAULT 'NOT_APPLIED'",
        "user_status_updated_at": "DATETIME",
        "user_status_actor": "VARCHAR(20) NOT NULL DEFAULT 'default'",
        "application_url_provenance": "VARCHAR(40) NOT NULL DEFAULT ''",
    },
    "applications": {
        "risk_assessed_at": "DATETIME",
        "risk_assessment_source": "VARCHAR(120)",
    },
    "automation_runs": {
        "risk_assessed_at": "DATETIME",
        "risk_assessment_source": "VARCHAR(120)",
    },
    "candidate_profiles": {
        "admissible_graduation_years_json": "TEXT NOT NULL DEFAULT '[]'",
    },
}

# Audit-only additive migration.  The outbox/state tables are created by
# ``Base.metadata.create_all``; this column keeps an existing event history
# byte-for-byte while allowing future rows to identify an independent epoch.
_AUDIT_ADDITIVE_MIGRATIONS: dict[str, dict[str, str]] = {
    "audit_events": {
        "epoch": "INTEGER NOT NULL DEFAULT 0",
    },
    "audit_outbox": {
        "intent_fingerprint": "TEXT NOT NULL DEFAULT ''",
    },
}

_TERMINAL_RUN_STATES = frozenset(
    {
        "NEEDS_USER",
        "NEEDS_OA",
        "FAILED_RETRYABLE",
        "BLOCKED",
        "READY_TO_SUBMIT",
        "SUBMITTED",
        "CONFIRMATION_VERIFIED",
        "OA_PENDING",
        "INTERVIEW",
        "REJECTED",
        "OFFER",
    }
)

_OPPORTUNITY_REBUILD_COLUMNS = frozenset(
    {
        "id",
        "employer",
        "role_title",
        "division",
        "programme_group",
        "location",
        "cycle",
        "url",
        "source_fingerprint",
        "application_url",
        "target_status",
        "resolved_ats_type",
        "resolution_evidence_json",
        "resolved_at",
        "resolution_attempted_at",
        "source",
        "ats_type",
        "opening_date",
        "deadline",
        "rolling",
        "application_window_status",
        "min_graduation_year",
        "max_graduation_year",
        "sponsorship_supported",
        "cv_required",
        "cover_letter_required",
        "written_answers_required",
        "notes",
        "user_status",
        "user_status_updated_at",
        "user_status_actor",
        "created_at",
        "updated_at",
    }
)


class Base(DeclarativeBase):
    pass


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.last_migration_backup_path: Path | None = None
        self.engine = create_engine(
            settings.database_url,
            connect_args={
                "check_same_thread": False,
                "timeout": 30,  # wait up to 30s for locks instead of failing
            },
            future=True,
        )
        # SQLite foreign-key enforcement is connection-local. Configure it at
        # the pool boundary so disposed/reopened and overflow connections have
        # exactly the same integrity guarantees as the first connection.
        @event.listens_for(self.engine, "connect")
        def _configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=30000")
            finally:
                cursor.close()

        # WAL mode: readers don't block writers - critical because the autopilot
        # runs Playwright (own connections) while the request session stays open.
        with self.engine.connect() as connection:
            connection.execute(text("PRAGMA journal_mode=WAL"))
        from app.security.audit import AuditSession, SQLITE_DRAIN_BEGIN_OPTION

        @event.listens_for(self.engine, "savepoint")
        def _activate_sqlite_root_before_savepoint(connection, _name) -> None:
            """Ensure SQLite has a physical root before issuing SAVEPOINT.

            sqlite3 defers the physical BEGIN until the first write.  When a
            SQLAlchemy root contains no physical read/write yet, SAVEPOINT
            would otherwise become SQLite's top-level transaction and its
            release would commit data beyond a later outer rollback.  Activate
            only at the savepoint boundary so ordinary concurrent read-then-
            write sessions retain SQLite's normal writer behavior.
            """

            if (
                connection.dialect.name != "sqlite"
                or connection.get_execution_options().get(SQLITE_DRAIN_BEGIN_OPTION, False)
            ):
                return
            raw = connection.connection
            driver_connection = getattr(raw, "driver_connection", raw)
            if not driver_connection.in_transaction:
                connection.exec_driver_sql("BEGIN")

        self.SessionLocal = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
            class_=AuditSession,
            audit_engine=self.engine,
        )
        self.notification_service = None
        if settings.notifications_enabled:
            try:
                from app.services.notifications import (
                    NotificationService,
                    install_application_notification_observer,
                    install_discovery_notification_observer,
                )

                notifier = NotificationService.from_settings(settings)
                if notifier.enabled:
                    install_application_notification_observer(self, notifier)
                    install_discovery_notification_observer(self, notifier)
                    self.notification_service = notifier
            except Exception as exc:  # noqa: BLE001 - notifications are optional
                LOGGER.warning(
                    "notification observer unavailable; database remains usable (%s)",
                    type(exc).__name__,
                )

    def create_schema(self) -> None:
        # Import model modules before metadata creation.
        from app import models as _models  # noqa: F401

        # Existing tables must receive the additive identity column/index
        # before metadata tries to create its declared partial index.  The v2
        # source/target rebuild intentionally runs first and does not know
        # about this later provider-specific column.
        self._upgrade_opportunities_v2()
        self._upgrade_trackr_identity_schema()
        Base.metadata.create_all(self.engine)
        self._upgrade_schema()
        self._install_audit_chain_trigger()
        if self.notification_service is not None:
            from app.services.notifications import reconcile_existing_human_applications

            reconcile_existing_human_applications(self, self.notification_service)
        database_path = self.settings.data_dir / "argus.db"
        if os.name != "nt" and database_path.exists():
            os.chmod(database_path, 0o600)

    def _install_audit_chain_trigger(self) -> None:
        """Enforce audit-chain head integrity at the database level.

        The production fork at event 4819 happened because two sessions read
        the same head and both appended.  The epoch-aware trigger and durable
        head row make an invalid future fork unrepresentable while retaining
        the historical broken range as evidence.
        """

        from app.security.audit import install_chain_trigger

        install_chain_trigger(self.engine)

    def _upgrade_schema(self) -> None:
        """Apply idempotent additive migrations for databases made by 0.1.x."""

        self._upgrade_opportunities_v2()
        self._upgrade_trackr_identity_schema()
        self._upgrade_audit_schema()
        self._upgrade_submission_authority_schema()

        with self.engine.begin() as connection:
            inspector = inspect(connection)
            for table, columns in _ADDITIVE_MIGRATIONS.items():
                if table not in inspector.get_table_names():
                    continue
                existing = {column["name"] for column in inspector.get_columns(table)}
                for column, sql_type in columns.items():
                    if column in existing:
                        continue
                    # Names/types come only from the module constant above;
                    # quote identifiers to keep this safe if a future table
                    # name overlaps a SQL keyword.
                    connection.execute(
                        text(
                            f'ALTER TABLE "{table}" ADD COLUMN "{column}" {sql_type}'
                        )
                    )
            if "opportunities" in inspector.get_table_names():
                connection.execute(
                    text(
                        "UPDATE opportunities "
                        "SET user_status_updated_at = COALESCE(" 
                        "user_status_updated_at, updated_at, created_at, CURRENT_TIMESTAMP) "
                        "WHERE user_status_updated_at IS NULL"
                    )
                )
                missing_status_timestamps = connection.execute(
                    text(
                        "SELECT COUNT(*) FROM opportunities "
                        "WHERE user_status_updated_at IS NULL"
                    )
                ).scalar_one()
                if missing_status_timestamps:
                    raise RuntimeError(
                        "Cannot complete schema version 9; user status timestamps remain null"
                    )
                connection.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS "
                        "ix_opportunities_application_window_status "
                        "ON opportunities(application_window_status)"
                    )
                )
                connection.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_opportunities_user_status "
                        "ON opportunities(user_status)"
                    )
                )
            connection.execute(text(f"PRAGMA user_version = {SCHEMA_VERSION}"))
        self._backfill_legacy_risk()
        self._reconcile_legacy_ready_targets()

    def _upgrade_trackr_identity_schema(self) -> None:
        """Install the nullable Trackr identity and its partial uniqueness.

        A partially deployed database with duplicate non-null IDs is evidence
        of conflicting ownership.  Fail before any index DDL or version bump;
        never select a winner or rewrite either row.
        """

        with self.engine.begin() as connection:
            inspector = inspect(connection)
            if "opportunities" not in inspector.get_table_names():
                return
            columns = {column["name"] for column in inspector.get_columns("opportunities")}
            named_object = connection.execute(
                text(
                    "SELECT type, tbl_name, sql FROM sqlite_master "
                    "WHERE name = :name"
                ),
                {"name": _TRACKR_PROGRAMME_ID_UNIQUE_INDEX},
            ).first()
            if named_object is not None:
                self._assert_trackr_identity_index(connection, named_object)
            if "trackr_programme_id" not in columns:
                connection.execute(
                    text(
                        "ALTER TABLE opportunities ADD COLUMN "
                        "trackr_programme_id VARCHAR(128)"
                    )
                )
            duplicate = connection.execute(
                text(
                    "SELECT trackr_programme_id, COUNT(*) AS row_count "
                    "FROM opportunities "
                    "WHERE trackr_programme_id IS NOT NULL "
                    "GROUP BY trackr_programme_id "
                    "HAVING COUNT(*) > 1 "
                    "ORDER BY trackr_programme_id LIMIT 1"
                )
            ).first()
            if duplicate is not None:
                raise RuntimeError(
                    "Cannot install Trackr identity uniqueness; duplicate Trackr "
                    f"programme ID exists: {duplicate[0]!r} ({duplicate[1]} rows)"
                )
            if named_object is None:
                connection.execute(
                    text(
                        f'CREATE UNIQUE INDEX "{_TRACKR_PROGRAMME_ID_UNIQUE_INDEX}" '
                        "ON opportunities(trackr_programme_id) "
                        "WHERE trackr_programme_id IS NOT NULL"
                    )
                )
                created = connection.execute(
                    text(
                        "SELECT type, tbl_name, sql FROM sqlite_master "
                        "WHERE name = :name"
                    ),
                    {"name": _TRACKR_PROGRAMME_ID_UNIQUE_INDEX},
                ).one()
                self._assert_trackr_identity_index(connection, created)

    @staticmethod
    def _assert_trackr_identity_index(connection, named_object) -> None:
        """Require the exact named unique partial index; never repair by replacement."""

        object_type, table_name, raw_sql = named_object
        index_rows = list(connection.execute(text("PRAGMA index_list('opportunities')")))
        index_row = next(
            (
                row
                for row in index_rows
                if str(row[1]) == _TRACKR_PROGRAMME_ID_UNIQUE_INDEX
            ),
            None,
        )
        columns = tuple(
            str(row[2])
            for row in connection.execute(
                text(
                    f'PRAGMA index_info("{_TRACKR_PROGRAMME_ID_UNIQUE_INDEX}")'
                )
            )
        )
        folded_sql = re.sub(
            r'["`\[\]()]',
            " ",
            str(raw_sql or "").casefold(),
        )
        folded_sql = " ".join(folded_sql.rstrip(" ;").split())
        predicate = folded_sql.partition(" where ")[2]
        valid = bool(
            object_type == "index"
            and table_name == "opportunities"
            and index_row is not None
            and bool(index_row[2])
            and len(index_row) > 4
            and bool(index_row[4])
            and columns == ("trackr_programme_id",)
            and predicate == "trackr_programme_id is not null"
        )
        if not valid:
            raise RuntimeError(
                f"Trackr identity index mismatch; refusing schema version {SCHEMA_VERSION}: "
                f"type={object_type!r}, table={table_name!r}, "
                f"unique={bool(index_row[2]) if index_row is not None else False}, "
                "partial="
                f"{bool(index_row[4]) if index_row is not None and len(index_row) > 4 else False}, "
                f"columns={columns!r}, predicate={predicate!r}"
            )

    def _reconcile_legacy_ready_targets(self) -> None:
        """Demote source-only legacy rows that predate verified targets.

        The v2 opportunity migration deliberately keeps every old URL as a
        discovery/source URL.  A legacy application may nevertheless already
        be ``READY_TO_SUBMIT`` from the pre-v2 runner.  Leaving that child state
        untouched strands the UI in a ready state with no verified destination.
        This repair is idempotent and runs on every startup so databases that
        were upgraded by an earlier 0.2 build are corrected as well.
        """

        with self.engine.begin() as connection:
            inspector = inspect(connection)
            if not {"applications", "opportunities"} <= set(
                inspector.get_table_names()
            ):
                return
            connection.execute(
                text(
                    """
                    UPDATE applications
                    SET state = 'NEEDS_USER',
                        next_action = 'Resolve application target'
                    WHERE state = 'READY_TO_SUBMIT'
                      AND EXISTS (
                        SELECT 1
                        FROM opportunities
                        WHERE opportunities.id = applications.opportunity_id
                          AND (
                            opportunities.target_status NOT IN (
                              'APPLICATION_ENTRY', 'APPLICATION_FORM'
                            )
                            OR TRIM(COALESCE(opportunities.application_url, '')) = ''
                            OR opportunities.resolved_at IS NULL
                          )
                      )
                    """
                )
            )

    def _upgrade_submission_authority_schema(self) -> None:
        """Install the one-authority-per-application/session invariant.

        The authority table was introduced after the original schema.  A
        populated database can therefore contain the table without the
        unique constraint that new metadata declares.  Add a named unique
        index only after checking for duplicates; silently deleting one of two
        historical authorization rows would destroy evidence and could make a
        prior click untraceable.
        """

        with self.engine.begin() as connection:
            inspector = inspect(connection)
            if "submission_authorities" not in inspector.get_table_names():
                return
            matching_unique = False
            for index in inspector.get_indexes("submission_authorities"):
                if not index.get("unique"):
                    continue
                columns = tuple(index.get("column_names") or ())
                if columns == ("application_id", "session_id"):
                    matching_unique = True
                    break
            if not matching_unique:
                duplicate = connection.execute(
                    text(
                        "SELECT application_id, session_id, COUNT(*) AS row_count "
                        "FROM submission_authorities "
                        "GROUP BY application_id, session_id "
                        "HAVING COUNT(*) > 1 LIMIT 1"
                    )
                ).first()
                if duplicate is not None:
                    raise RuntimeError(
                        "Cannot install submission-authority uniqueness; duplicate "
                        f"application/session rows exist: {duplicate[0]!r}/{duplicate[1]!r}"
                    )
                connection.execute(
                    text(
                        f'CREATE UNIQUE INDEX IF NOT EXISTS "{_SUBMISSION_AUTHORITY_UNIQUE_INDEX}" '
                        "ON submission_authorities(application_id, session_id)"
                    )
                )

    def _upgrade_audit_schema(self) -> None:
        """Apply only additive audit migrations; never rewrite event history."""

        with self.engine.begin() as connection:
            inspector = inspect(connection)
            for table, columns in _AUDIT_ADDITIVE_MIGRATIONS.items():
                if table not in inspector.get_table_names():
                    continue
                existing = {column["name"] for column in inspector.get_columns(table)}
                for column, sql_type in columns.items():
                    if column in existing:
                        continue
                    connection.execute(
                        text(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {sql_type}')
                    )

    def _upgrade_opportunities_v2(self) -> None:
        """Separate source and application targets and remove URL uniqueness.

        SQLite cannot drop the v1 ``UNIQUE(url)`` auto-index, so the parent
        table is rebuilt while foreign-key enforcement is temporarily disabled.
        IDs and child references are preserved byte-for-byte.  Existing URLs
        remain source-only and are never promoted by the migration.
        """

        from app.domain.targets import source_fingerprint

        raw = self.engine.raw_connection()
        dbapi_connection = getattr(raw, "driver_connection", raw)
        dbapi_connection.row_factory = sqlite3.Row
        try:
            cursor = raw.cursor()
            table_exists = cursor.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='opportunities'"
            ).fetchone()
            if not table_exists:
                return
            columns = [row[1] for row in cursor.execute("PRAGMA table_info(opportunities)")]
            index_rows = list(cursor.execute("PRAGMA index_list(opportunities)"))
            unique_index_columns: set[tuple[str, ...]] = set()
            for index_row in index_rows:
                if not index_row[2]:
                    continue
                index_name = str(index_row[1]).replace('"', '""')
                unique_index_columns.add(
                    tuple(
                        str(item[2])
                        for item in cursor.execute(
                            f'PRAGMA index_info("{index_name}")'
                        )
                    )
                )
            required = {
                "source_fingerprint",
                "application_url",
                "target_status",
                "resolved_ats_type",
                "resolution_evidence_json",
                "resolved_at",
                "resolution_attempted_at",
            }
            url_is_unique = ("url",) in unique_index_columns
            fingerprint_is_unique = ("source_fingerprint",) in unique_index_columns
            if (
                required <= set(columns)
                and not url_is_unique
                and not fingerprint_is_unique
            ):
                return

            preserved_objects = self._opportunity_schema_objects(cursor)
            database_path = self._sqlite_file_path()
            if database_path is not None:
                self._create_verified_migration_backup(database_path)
            else:
                self._assert_sqlite_integrity(dbapi_connection, label="in-memory source")
            self._assert_opportunity_rebuild_supported(cursor, columns, index_rows)

            cursor.execute("PRAGMA foreign_keys=OFF")
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute("DROP TABLE IF EXISTS opportunities_v2_migration")
            cursor.execute(
                """
                CREATE TABLE opportunities_v2_migration (
                  id VARCHAR(36) NOT NULL PRIMARY KEY,
                  employer VARCHAR(240) NOT NULL,
                  role_title VARCHAR(320) NOT NULL,
                  division VARCHAR(240) NOT NULL DEFAULT '',
                  programme_group VARCHAR(120) NOT NULL DEFAULT '',
                  location VARCHAR(240) NOT NULL DEFAULT '',
                  cycle VARCHAR(40) NOT NULL,
                  url TEXT NOT NULL,
                  source_fingerprint VARCHAR(64) NOT NULL,
                  application_url TEXT,
                  target_status VARCHAR(40) NOT NULL DEFAULT 'UNRESOLVED',
                  resolved_ats_type VARCHAR(80) NOT NULL DEFAULT '',
                  resolution_evidence_json TEXT NOT NULL DEFAULT '{}',
                  resolved_at DATETIME,
                  resolution_attempted_at DATETIME,
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
                  user_status VARCHAR(40) NOT NULL DEFAULT 'NOT_APPLIED',
                  user_status_updated_at DATETIME NOT NULL,
                  user_status_actor VARCHAR(20) NOT NULL DEFAULT 'default',
                  created_at DATETIME NOT NULL,
                  updated_at DATETIME NOT NULL
                )
                """
            )
            rows = list(cursor.execute("SELECT * FROM opportunities"))
            insert_sql = """
                INSERT INTO opportunities_v2_migration (
                  id, employer, role_title, division, programme_group, location,
                  cycle, url, source_fingerprint, application_url, target_status,
                  resolved_ats_type, resolution_evidence_json, resolved_at,
                  resolution_attempted_at, source, ats_type, deadline, rolling,
                  min_graduation_year, max_graduation_year, sponsorship_supported,
                  cv_required, cover_letter_required, written_answers_required,
                  notes, user_status, user_status_updated_at, user_status_actor,
                  created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """
            for row in rows:
                values = dict(row)
                fingerprint = source_fingerprint(
                    employer=str(values.get("employer") or ""),
                    role_title=str(values.get("role_title") or ""),
                    cycle=str(values.get("cycle") or ""),
                    source_url=str(values.get("url") or ""),
                    location=str(values.get("location") or ""),
                    division=str(values.get("division") or ""),
                )
                cursor.execute(
                    insert_sql,
                    (
                        values["id"], values["employer"], values["role_title"],
                        values.get("division", ""), values.get("programme_group", ""),
                        values.get("location", ""), values["cycle"], values["url"],
                        fingerprint, values.get("application_url"),
                        values.get("target_status") or "UNRESOLVED",
                        values.get("resolved_ats_type") or "",
                        values.get("resolution_evidence_json") or "{}",
                        values.get("resolved_at"), values.get("resolution_attempted_at"),
                        values.get("source") or "manual", values.get("ats_type") or "unknown",
                        values.get("deadline"), values.get("rolling", 0),
                        values.get("min_graduation_year"), values.get("max_graduation_year"),
                        values.get("sponsorship_supported"), values.get("cv_required", 1),
                        values.get("cover_letter_required", 0),
                         values.get("written_answers_required", 0), values.get("notes") or "",
                         values.get("user_status") or "NOT_APPLIED",
                         values.get("user_status_updated_at")
                         or values.get("updated_at")
                         or values.get("created_at"),
                         values.get("user_status_actor") or "default",
                         values["created_at"], values["updated_at"],
                    ),
                )
            cursor.execute("DROP TABLE opportunities")
            cursor.execute("ALTER TABLE opportunities_v2_migration RENAME TO opportunities")
            self._create_opportunity_indexes(cursor)
            for _object_type, _name, sql in preserved_objects:
                cursor.execute(sql)
            self._assert_sqlite_integrity(dbapi_connection, label="rebuilt database")
            raw.commit()
        except BaseException:
            raw.rollback()
            raise
        finally:
            try:
                raw.execute("PRAGMA foreign_keys=ON")
            finally:
                raw.close()

    def _sqlite_file_path(self) -> Path | None:
        database = self.engine.url.database
        if self.engine.dialect.name != "sqlite" or not database or database == ":memory:":
            return None
        return Path(database).resolve()

    def _create_verified_migration_backup(self, database_path: Path) -> Path:
        suffix = uuid4().hex
        backup_path = database_path.with_name(
            f"{database_path.name}.pre-opportunities-v2-{suffix}.bak"
        )
        descriptor = os.open(
            backup_path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
        os.close(descriptor)
        self.last_migration_backup_path = backup_path
        source_uri = database_path.as_uri() + "?mode=ro"
        with sqlite3.connect(source_uri, uri=True) as source:
            with sqlite3.connect(backup_path) as destination:
                source.backup(destination)
                destination.commit()
                self._assert_sqlite_integrity(destination, label="migration backup")
        return backup_path

    @staticmethod
    def _assert_sqlite_integrity(connection, *, label: str) -> None:
        integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
        if not integrity_rows or any(str(row[0]).casefold() != "ok" for row in integrity_rows):
            raise RuntimeError(f"{label} integrity check failed: {integrity_rows!r}")
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise RuntimeError(
                f"{label} foreign key check failed: {foreign_key_errors!r}"
            )

    @staticmethod
    def _assert_opportunity_rebuild_supported(cursor, columns, index_rows) -> None:
        """Fail before DDL when the fixed v2 table would remove legacy schema.

        Explicit compatible indexes and triggers are recreated separately. A
        column or table constraint that the v2 definition cannot reproduce is
        never guessed away: the verified backup remains available and the
        original table is left untouched for an explicit migration.
        """

        unknown_columns = sorted(set(columns) - _OPPORTUNITY_REBUILD_COLUMNS)
        hazards: list[str] = []
        if unknown_columns:
            hazards.append(f"unknown columns: {', '.join(unknown_columns)}")

        table_row = cursor.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='opportunities'"
        ).fetchone()
        table_sql = str(table_row[0] or "") if table_row else ""
        folded_sql = " ".join(table_sql.upper().split())
        if re.search(r"\bCHECK\s*\(", table_sql, flags=re.IGNORECASE):
            hazards.append("CHECK constraints")
        if re.search(r"\bCOLLATE\b", table_sql, flags=re.IGNORECASE):
            hazards.append("custom collations")
        if (
            re.search(r"\bGENERATED\b", table_sql, flags=re.IGNORECASE)
            or " WITHOUT ROWID" in folded_sql
        ):
            hazards.append("generated/row-layout constraints")
        if cursor.execute("PRAGMA foreign_key_list(opportunities)").fetchall():
            hazards.append("foreign-key constraints")

        primary_key_columns = [
            str(row[1])
            for row in sorted(
                (row for row in cursor.execute("PRAGMA table_info(opportunities)") if row[5]),
                key=lambda row: row[5],
            )
        ]
        if primary_key_columns != ["id"]:
            hazards.append(
                "primary-key constraint: " + ", ".join(primary_key_columns or ["<none>"])
            )

        for index_row in index_rows:
            if not index_row[2]:
                continue
            index_name = str(index_row[1]).replace('"', '""')
            indexed_columns = tuple(
                str(item[2])
                for item in cursor.execute(f'PRAGMA index_info("{index_name}")')
            )
            origin = str(index_row[3]) if len(index_row) > 3 else ""
            if origin == "u" and indexed_columns not in {
                ("url",),
                ("source_fingerprint",),
            }:
                hazards.append(
                    "table UNIQUE constraint: " + ", ".join(indexed_columns)
                )

        if hazards:
            raise RuntimeError(
                "Cannot safely rebuild opportunities; " + "; ".join(hazards)
            )

    @staticmethod
    def _opportunity_schema_objects(cursor) -> list[tuple[str, str, str]]:
        managed_names = {
            "ix_opportunities_source_fingerprint",
            "ix_opportunities_employer",
            "ix_opportunities_cycle",
            "ix_opportunities_target_status",
            "ix_opportunities_employer_cycle",
            "ix_opportunities_user_status",
        }
        preserved: list[tuple[str, str, str]] = []
        rows = cursor.execute(
            """
            SELECT type, name, sql FROM sqlite_master
            WHERE tbl_name='opportunities'
              AND type IN ('index', 'trigger')
              AND sql IS NOT NULL
            ORDER BY type, name
            """
        ).fetchall()
        index_rows = {
            str(row[1]): row for row in cursor.execute("PRAGMA index_list(opportunities)")
        }
        for object_type, name, sql in rows:
            name = str(name)
            if name in managed_names:
                continue
            if object_type == "index":
                index = index_rows.get(name)
                escaped_name = name.replace('"', '""')
                columns = tuple(
                    str(item[2])
                    for item in cursor.execute(
                        f'PRAGMA index_info("{escaped_name}")'
                    )
                )
                if index is not None and index[2] and columns in {
                    ("url",),
                    ("source_fingerprint",),
                }:
                    continue
            preserved.append((str(object_type), name, str(sql)))
        return preserved

    @staticmethod
    def _create_opportunity_indexes(cursor) -> None:
        cursor.execute(
            "CREATE INDEX ix_opportunities_source_fingerprint "
            "ON opportunities(source_fingerprint)"
        )
        cursor.execute("CREATE INDEX ix_opportunities_employer ON opportunities(employer)")
        cursor.execute("CREATE INDEX ix_opportunities_cycle ON opportunities(cycle)")
        cursor.execute(
            "CREATE INDEX ix_opportunities_target_status ON opportunities(target_status)"
        )
        cursor.execute(
            "CREATE INDEX ix_opportunities_employer_cycle "
            "ON opportunities(employer, cycle)"
        )
        cursor.execute(
            "CREATE INDEX ix_opportunities_user_status ON opportunities(user_status)"
        )

    def _backfill_legacy_risk(self) -> None:
        """Backfill assessment metadata only where a finished terminal run proves it.

        Existing ARGUS databases used ``risk_level=0`` as both a real R0 and
        an uninitialised value.  A finished terminal automation run is the
        only legacy evidence strong enough to mark the value assessed.  The
        operation is additive and idempotent: rows with metadata already set
        are never overwritten, and unfinished/non-terminal runs are ignored.
        """

        from sqlalchemy import select

        from app.models import Application, AutomationRun

        with self.SessionLocal() as session:
            runs = list(
                session.scalars(
                    select(AutomationRun).where(
                        AutomationRun.finished_at.is_not(None),
                        AutomationRun.state.in_(_TERMINAL_RUN_STATES),
                    )
                ).all()
            )
            eligible: list[AutomationRun] = []
            for run in runs:
                if run.risk_is_assessed or not isinstance(run.risk_level, int):
                    continue
                if not 0 <= run.risk_level <= 4:
                    # Invalid legacy values are not evidence; preserve them
                    # for explicit human review rather than normalising them.
                    continue
                assessed_at = run.finished_at or run.created_at
                run.mark_risk_assessed(
                    run.risk_level,
                    source=f"legacy_automation_run:{run.id}",
                    assessed_at=assessed_at,
                )
                eligible.append(run)

            # A pre-existing application assessment wins.  Otherwise choose
            # the latest finished terminal run as the evidence source.
            latest_by_application: dict[str, AutomationRun] = {}
            for run in runs:
                if not isinstance(run.risk_level, int) or not 0 <= run.risk_level <= 4:
                    continue
                current = latest_by_application.get(run.application_id)
                run_key = (run.finished_at or run.created_at, run.created_at, run.id)
                current_key = (
                    (current.finished_at or current.created_at, current.created_at, current.id)
                    if current is not None
                    else None
                )
                if current is None or run_key > current_key:
                    latest_by_application[run.application_id] = run

            for application_id, run in latest_by_application.items():
                application = session.get(Application, application_id)
                if application is None or application.risk_is_assessed:
                    continue
                application.mark_risk_assessed(
                    run.risk_level,
                    source=f"legacy_automation_run:{run.id}",
                    assessed_at=run.finished_at or run.created_at,
                )
            if eligible or latest_by_application:
                session.commit()

    @contextmanager
    def session_scope(self) -> Iterator[Session]:
        session = self.SessionLocal()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()


_default_database: Database | None = None


def configure_database(settings: Settings) -> Database:
    global _default_database
    _default_database = Database(settings)
    return _default_database


@contextmanager
def session_scope() -> Iterator[Session]:
    if _default_database is None:
        raise RuntimeError("Database has not been configured")
    with _default_database.session_scope() as session:
        yield session
