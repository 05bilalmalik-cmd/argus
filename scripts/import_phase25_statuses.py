"""Backup-gated import of the candidate-supplied Phase 25 statuses.

Dry-run is the default.  Matching is an exact normalized employer, programme
name, and programme-group triple.  This script does not import or call any
automation, navigation, form, or submission component.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable, Sequence, TypeVar
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import func, inspect as sqlalchemy_inspect, select  # noqa: E402
from sqlalchemy.exc import OperationalError as SqlAlchemyOperationalError  # noqa: E402

from app.config import Settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.models import AuditOutbox, Opportunity  # noqa: E402
from app.runtime_lock import (  # noqa: E402
    RuntimeAlreadyRunning,
    RuntimeLock,
    runtime_lock_path,
)
from app.services.user_statuses import (  # noqa: E402
    UserStatusImportReport,
    UserStatusImportRow,
    import_user_statuses,
    parse_user_status,
    status_import_cohort_sha256,
)


_SUMMER = """
Not Applied | Wincent | Quant Research/Trading Internship - Summer 2027
Not Applied | AmplifyME | Summer Analyst Training Programme
Interested | D.E. Shaw | Trader/Analyst Intern (London) - Summer 2027
Not Applied | Barclays | Summer Internship Programme 2027
Interested | UBS | 2027 Summer Internship
Not Applied | Goldman Sachs | 2027 Summer Analyst Programme
Not Applied | Bank of America | Summer 2027 Analyst Programme
Not Applied | Centerview Partners | 2027 Summer Internship Programme
Not Applied | Ardea Partners | 2027 Summer Analyst Programme
Not Applied | Raine Group | 2027 Summer Analyst Program
Not Applied | Perella Weinberg Partners | 2027 Advisory Summer Analyst Programme
Interested | Rothschild & Co. | 2027 Summer Analyst Programme
Interested | Moelis & Co | 2027 Summer Analyst, Investment Banking
Not Interested | Evercore | Summer Internship (2027)
Not Applied | LionTree | 2027 Summer Internship Programme
Not Applied | Qatalyst Partners | 2027 M&A Advisory Summer Internship
Not Applied | Wells Fargo | EMEA Summer Analyst
Not Applied | Guggenheim Partners | 2027 Investment Banking Analyst Intern
Not Applied | BNP Paribas | 2027 Summer Internship Programme
Online Assessment | Standard Chartered | Internship Programme 2027
Not Applied | SMBC | Summer Intern Programme 2027
Not Applied | BTG Pactual | Summer Undergrad 2027
Not Applied | CD&R | Private Equity Summer Internship 2027
Application Submitted | Ares Management | Summer Analyst Programme 2027
Not Applied | Atlas Holdings | Private Equity Summer Internship 2027
Not Applied | Millennium Management | 2027 Non-Investment Summer Intern
Not Applied | Blackstone | 2027 Summer Analyst
Not Applied | GIC | GIC Internship Programme 2027
Not Applied | Citadel | 2027 Associate Intern
Interested | Capula Investment Management | Trading & Research Summer Internship 2027
Interested | Redburn | 2027 Redburn Summer Intern Programme
Interested | Walter Scott | Summer Internship Opportunities 2027
Interested | Hines | 2026 Summer Analyst
Not Interested | BlackRock | 2027 Summer Internship Program - EMEA
Not Applied | Inizio Ignite | Summer Associate Consultant - 2029 Internship Program
Not Applied | Optiver | Quantitative Trading Internship (2027 Start)
Not Interested | Xantium | Quantitative Researcher Internship
Not Interested | iSAM | Quantitative Research Internship
Application Submitted | DV Trading | Trading Intern - Summer 2027
Not Interested | Quantbot Technologies | Quantitative Researcher Internship - 2027
Application Submitted | Hudson River Trading | Quant Research & Trading Internship - Summer 2027
Not Applied | Jane Street | Quantitative Trading Intern Summer 2027
Not Applied | IMC Trading | Trader Intern (2027)
Not Applied | Millennium Management | 2027 Quant Researcher Intern
Not Applied | Squarepoint Capital | Intern Quant Researcher
Not Applied | G-Research | Quant Research Internship
Not Applied | Chicago Trading Company | Quant Trading Internship - Summer 2027
Not Applied | Castleton Commodities International | Summer Analyst Internship Programme (Summer 2027)
Not Applied | Susquehanna International Group | Quantitative Trading Internship: Summer 2027
Not Applied | Da Vinci Trading | 2027 Quant Trading Intern
Not Applied | DRW | Quantitative Trading Analyst Intern
Not Applied | Jump Trading | Campus Quantitative Trader (Intern)
Not Applied | GSA Capital | Quantitative Reseacher - Intern
Not Applied | Virtu Financial | 2027 Internship - Quantitative Trading
Not Applied | GSA Capital | Women in Trading Insight Programme - 2027
Not Applied | Citadel Securities | Trading & Quant Internship 2027
Not Applied | Aquatic Capital Management | Quantitative Researcher, Intern (Summer 2027)
Not Applied | Capital One | Strategy Analyst Summer Internship
Not Applied | BNY Mellon | 2027 Summer Internship Program
Not Applied | Revolut | Internship Programme 2027
"""

_SPRING_WEEK = """
Not Applied | AmplifyME | 2026 In-Person Finance Bootcamp
Not Applied | GIC | GIC Internship Programme 2027
Not Interested | BlackRock | 2027 Spring Insight Event - EMEA
Not Applied | Inizio Ignite | Summer Associate Consultant - Internship Program (2029 Grads)
Not Applied | Jane Street | FOCUS / FTTP
Not Applied | GSA Capital | Women in Trading Insight Programme - 2027
"""

_YEAR_IN_INDUSTRY = """
Not Applied | AlphaSights | Placement, Client Service, 2027
HireVue | Evercore | 2027 Private Funds Group - Industrial Placement
Not Applied | ExxonMobil | Industrial Placement - Global Trading
Not Applied | Goldman Sachs | 2027 One Year Work Placement
HireVue | BlackRock | 2027 Placement Program - EMEA
"""


def _build_manifest() -> tuple[UserStatusImportRow, ...]:
    rows: list[UserStatusImportRow] = []
    for programme_group, block in (
        ("summer", _SUMMER),
        ("spring_week", _SPRING_WEEK),
        ("year_in_industry", _YEAR_IN_INDUSTRY),
    ):
        for line in block.strip().splitlines():
            status, employer, programme_name = (part.strip() for part in line.split("|", 2))
            rows.append(
                UserStatusImportRow(
                    source_line=len(rows) + 1,
                    status=parse_user_status(status).value,
                    employer=employer,
                    programme_name=programme_name,
                    programme_group=programme_group,
                )
            )
    return tuple(rows)


SUPPLIED_STATUS_ROWS = _build_manifest()


# Reviewed against the Phase 25 live snapshot. Each entry is fail-closed: the
# importer uses it only when the current normalized triple produces this exact
# candidate-ID set. A missing, added, or replaced opportunity restores the
# line to ambiguity instead of extending the decision by inference.
REVIEWED_DUPLICATE_CANDIDATES: dict[int, frozenset[str]] = {
    1: frozenset(
        {
            "db251a06-348e-4aed-98cd-399b57da7955",
            "2f6fc9ec-6f2a-4812-af1c-e5a5d0e0a7ef",
        }
    ),
    3: frozenset(
        {
            "e3cba8bd-e16c-4bcf-b893-161bdaba7cd0",
            "03f66b97-048c-427b-8633-b0f9bccf6038",
        }
    ),
    5: frozenset(
        {
            "099ab7f7-f4ae-4345-8114-6b82792118a1",
            "bac3a931-9c7d-4101-81f0-d7cba8294444",
        }
    ),
    7: frozenset(
        {
            "6808e0fe-f9d3-400b-aba1-cc216ab27570",
            "7f17cd25-1e15-46ee-8c97-eae3d7d6d90e",
        }
    ),
    12: frozenset(
        {
            "80b8d79f-36bf-4b46-90e6-8bb302e70b61",
            "65868284-e399-4511-89ab-820849531772",
        }
    ),
    14: frozenset(
        {
            "17e194ef-c677-4883-9e21-a54ad1956732",
            "7f850c87-4c82-4f64-977a-b10b9a08ba83",
        }
    ),
    15: frozenset(
        {
            "d2d3eeba-91b1-42be-a727-04e528d0d10d",
            "22ea1b16-1662-49cd-a909-77360379bc2c",
            "df34a9f6-565d-4005-a2f3-51e94d3b9dfd",
            "24cc167f-85a5-40d1-98b2-6180cc2595e8",
        }
    ),
    20: frozenset(
        {
            "7b6ae643-992c-401a-97f6-41657dd93137",
            "baeca102-5525-43ac-bd99-de9c8ffaf040",
        }
    ),
    27: frozenset(
        {
            "c9d2c047-8a5d-4028-b62f-74101b282205",
            "62decf5f-d0b0-4de0-8db2-0413772c088d",
        }
    ),
    29: frozenset(
        {
            "2c2f6586-3afd-4ab3-b30a-d1f14240797d",
            "8ed3f808-be1d-4715-b462-de628aa6877d",
        }
    ),
    33: frozenset(
        {
            "ff2760e0-b933-4034-bbfe-2806c040452c",
            "0300137e-5002-46b0-8c77-ff19c866c31f",
        }
    ),
    36: frozenset(
        {
            "eb3fee6a-6ff3-43b2-94be-29727607ecc4",
            "312d61a6-017b-4601-9991-3ace960feafa",
        }
    ),
    37: frozenset(
        {
            "e75bbe5d-04d6-4bd1-affd-4e4834aeb1f4",
            "6717f948-97b3-46d6-9025-6b5f50b6e5ab",
        }
    ),
    38: frozenset(
        {
            "8dfd4024-ce5c-4d98-b941-32f093c48320",
            "2b7fbfa8-6a45-469f-9813-fb8d05a17a5e",
        }
    ),
    39: frozenset(
        {
            "b9b58a51-304c-473e-89d3-a948cdd3273a",
            "176b64ed-afd4-4b35-9478-6a3b313602ea",
        }
    ),
    42: frozenset(
        {
            "0804b1c1-48dc-4ebe-ba3f-9a9430568c99",
            "dd29871c-29ba-4aa9-8464-e0e7caf77f40",
        }
    ),
    45: frozenset(
        {
            "57cbc72f-d011-4a21-b232-f9b1e6fe597b",
            "69a5037d-c6b9-4742-909b-db0e6ec18eb2",
        }
    ),
    46: frozenset(
        {
            "2e5bd0a3-835d-4824-b7fd-2d17e7f2ae98",
            "d739cad4-8a4a-40c1-b494-8f23fd440229",
        }
    ),
    47: frozenset(
        {
            "bae7fc14-f51a-4fd1-9fa2-696b8a474e7f",
            "b4d46371-6fd6-4c32-acf9-c8babe857cd0",
        }
    ),
    48: frozenset(
        {
            "7a7d6d95-31dc-48f7-8c5c-49e0562585a5",
            "c11ce86b-2ae0-48d0-9c45-95298d770287",
        }
    ),
    54: frozenset(
        {
            "21a2afe2-af1d-4c0e-beb6-e58c6ea42610",
            "fbebf589-c34c-45b7-b5b8-bc717bdb82d3",
            "dc6daf2e-66e3-4ccf-913c-d56fd15eb400",
        }
    ),
    56: frozenset(
        {
            "be53f0ef-8c68-43c3-a079-2fc64212aa5e",
            "c8a85732-1ef7-4be1-8c33-591d00066cbd",
        }
    ),
    57: frozenset(
        {
            "652ac782-a30a-4041-bc1d-dd1e363753f8",
            "0eb4c55b-32c3-43b4-a737-50ffa18e90ba",
            "ca368e24-2996-4674-be65-caa5402972ba",
        }
    ),
}


REVIEWED_DUPLICATE_ATTESTATIONS: dict[int, str] = {
    1: "be04ab658b45d50cd239be7e163d2a7a58303cae300570c9aa8631d36ab78c26",
    3: "18444c228d015c09b8a0e5841f5d79411b9e730e06f4a344c15ae0c2fc84c9b7",
    5: "676a366e3885ebc43904192a3c4992fc7d14f0c3b36e147f3a9e7f2d1d2b3dae",
    7: "0bb06473540ec6a72a1211d0f50042f1ad580590194c52fb4359ed05ebad3e2f",
    12: "36a016d70879ba7053d0c553a56ac02cb3f928433fee4a76efd292113d8d2562",
    14: "98d822f89eb191f1031e117900efea2beeb2c6b8d408bdd52c85b11b036aaf4b",
    15: "6da9a9f0bc524619377988130001bfa03edc91c01ba807e010a3dff3476bd82a",
    20: "726258a3ae157dd10a7afc6fb6cd5bdee8ce3b961a52ed46dd9d8e19a32ca925",
    27: "94deff33d3a40e3a76f455e7d48ab379421ff4bfdf5e20cf46ab922aa474852d",
    29: "33e7ab09805684fe4c0066a89ffb676c2cf84e31af2a95ebeb70a79ab43aa592",
    33: "ff3dc3cdf13d99a95cd81f9d2e3510e385a6777f7c80d7ec3629eddb11101278",
    36: "c93cbaa4d75e7fa29f743c188ba7fa96c8324211b38073390d2816d4379d2588",
    37: "3f980831d85860eb0bb3f391fd56f2a8f5f6c3d35f9f61a2c1d61373438e7099",
    38: "7f37a6821e47c2d8e370fc9b38fc4ffe66af1cf73ba0a83b602e21ab59bd6c4a",
    39: "c84eb97006656b2189c0642d2feaa821b55e4d6c8a29320aa89a514b333b26ad",
    42: "81610d4b163aca14190d07259c0f1b0b328a3d857dcb652269873fecc8a07890",
    45: "38de9e1c65457c65b41077705ebe7407d367b3f3c991c4c7a717b84857be4816",
    46: "aa85098c8dbd69ddba938238903b7c9d013492050bf576f514ac0dabb6742eae",
    47: "14e098415eefb95797dcc1bf77be48b49e91b05ac453be7d8ff88b568aa11524",
    48: "af9cd4aaaa381cf201f0aaafef2ec7c39b2efe8b7b981d031994be2da07757ef",
    54: "68a95c345b8d5c510766d639592407f6479d6be48509d017cf845201f71490e8",
    56: "7991a53f5b06b67e34888da78fba55ecb7c40cc5d0fd92cdc7e4ab914e067b64",
    57: "b1a0870f6bc20aaabea29706e5f4e5f9174f9de81206cdd1d2edb1d9ef3f6fe0",
}


_REPORT_SCHEMA_VERSION = 3
_SUPPORTED_REPORT_SCHEMA_VERSIONS = frozenset({1, 2, 3})
_T = TypeVar("_T")


def _readonly_connection(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _assert_integrity(connection: sqlite3.Connection, *, label: str) -> None:
    integrity = connection.execute("PRAGMA integrity_check").fetchall()
    if not integrity or any(str(row[0]).casefold() != "ok" for row in integrity):
        raise RuntimeError(f"{label} integrity check failed: {integrity!r}")
    foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_keys:
        raise RuntimeError(f"{label} foreign-key check failed: {foreign_keys!r}")


def _online_copy(source_path: Path, destination_path: Path) -> Path:
    source_path = source_path.resolve()
    destination_path = destination_path.resolve()
    if destination_path.exists():
        raise FileExistsError(destination_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(_readonly_connection(source_path)) as source:
        _assert_integrity(source, label="source")
        with closing(sqlite3.connect(destination_path)) as destination:
            source.backup(destination)
            destination.commit()
            _assert_integrity(destination, label="backup")
    return destination_path


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def retry_database_locked(
    operation: Callable[[], _T],
    *,
    attempts: int,
    retry_delay: float,
) -> _T:
    """Retry only bounded SQLite lock contention; propagate every other error."""

    if attempts < 1:
        raise ValueError("database lock attempts must be positive")
    if retry_delay < 0:
        raise ValueError("database lock retry delay cannot be negative")
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except (sqlite3.OperationalError, SqlAlchemyOperationalError) as error:
            if "database is locked" not in str(error).casefold() or attempt == attempts:
                raise
            time.sleep(retry_delay)
    raise AssertionError("unreachable")


def create_verified_backup(database_path: Path, backup_dir: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = backup_dir.resolve() / (
        f"argus.phase25-status-preapply-{stamp}-{uuid4().hex}.db"
    )
    return _online_copy(database_path, destination)


def _settings_for_database(database_path: Path) -> Settings:
    database_path = database_path.resolve()
    if database_path.name.casefold() != "argus.db":
        raise ValueError("status import database must be named argus.db")
    return Settings.load(
        {
            "ARGUS_DATA_DIR": str(database_path.parent),
            "ARGUS_API_TOKEN": "phase25-status-import",
            "ARGUS_AUTOMATION_MODE": "OFF",
            "ARGUS_ENABLE_LIVE_SUBMIT": "false",
            "ARGUS_ENABLE_TRACKR_LIVE": "false",
            "ARGUS_ENABLE_NOTIFICATIONS": "false",
        }
    )


def _table_counts(database_path: Path) -> dict[str, int]:
    with closing(_readonly_connection(database_path)) as connection:
        _assert_integrity(connection, label="database")
        tables = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        )
        return {
            table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in tables
        }


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _database_value(value: object) -> object:
    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    if value is None or isinstance(value, (str, int, float)):
        return value
    return str(value)


def _database_safety_snapshot(database_path: Path) -> dict[str, object]:
    """Capture privacy-safe identities and side-effect surfaces from SQLite."""

    with closing(_readonly_connection(database_path)) as connection:
        _assert_integrity(connection, label="database")
        tables = tuple(
            sorted(
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
            )
        )
        primary_keys: dict[str, frozenset[str]] = {}
        counts: dict[str, int] = {}
        logical_table_hashes: dict[str, str] = {}
        submission_surfaces: dict[str, frozenset[str]] = {}
        automation_run_surfaces: frozenset[str] = frozenset()
        question_record_surfaces: frozenset[str] = frozenset()
        submission_tables = {
            "applications",
            "submission_intents",
            "submission_authorities",
            "submission_review_bindings",
            "lab_submissions",
        }

        for table in tables:
            quoted_table = _quote_identifier(table)
            info = connection.execute(f"PRAGMA table_info({quoted_table})").fetchall()
            columns = tuple(str(row[1]) for row in info)
            quoted_columns = ", ".join(_quote_identifier(column) for column in columns)
            rows = connection.execute(
                f"SELECT {quoted_columns} FROM {quoted_table}"
            ).fetchall()
            counts[table] = len(rows)
            row_hash_list = tuple(
                hashlib.sha256(
                    _canonical_json([_database_value(value) for value in row]).encode(
                        "utf-8"
                    )
                ).hexdigest()
                for row in rows
            )
            row_hashes = frozenset(row_hash_list)
            logical_table_hashes[table] = hashlib.sha256(
                _canonical_json(
                    {"count": len(row_hash_list), "rows": sorted(row_hash_list)}
                ).encode("utf-8")
            ).hexdigest()

            pk_columns = tuple(
                str(row[1])
                for row in sorted(info, key=lambda item: int(item[5] or 0))
                if int(row[5] or 0) > 0
            )
            if pk_columns:
                pk_indexes = tuple(columns.index(column) for column in pk_columns)
                primary_keys[table] = frozenset(
                    hashlib.sha256(
                        _canonical_json(
                            [_database_value(row[index]) for index in pk_indexes]
                        ).encode("utf-8")
                    ).hexdigest()
                    for row in rows
                )
            else:
                # A full-row identity remains conservative for a legacy PK-less table.
                primary_keys[table] = row_hashes

            if table in submission_tables:
                submission_surfaces[table] = row_hashes
            if table == "automation_runs":
                # Every supported run mode can own a browser journey. Treat
                # any new or modified run as conservative evidence of possible
                # form activity; do not key this invariant to mode vocabulary.
                automation_run_surfaces = row_hashes
            if table == "question_records":
                question_record_surfaces = row_hashes

    digest_payload = {
        "primary_keys": {
            table: sorted(values) for table, values in sorted(primary_keys.items())
        },
        "logical_table_hashes": logical_table_hashes,
        "counts": counts,
        "submission_surfaces": {
            table: sorted(values)
            for table, values in sorted(submission_surfaces.items())
        },
        "automation_run_surfaces": sorted(automation_run_surfaces),
        "question_record_surfaces": sorted(question_record_surfaces),
    }
    return {
        "counts": counts,
        "primary_keys": primary_keys,
        "submission_surfaces": submission_surfaces,
        "automation_run_surfaces": automation_run_surfaces,
        "question_record_surfaces": question_record_surfaces,
        "logical_sha256": hashlib.sha256(
            _canonical_json(digest_payload).encode("utf-8")
        ).hexdigest(),
    }


def _safety_metrics(
    before: dict[str, object],
    after: dict[str, object],
) -> dict[str, int]:
    before_primary = before["primary_keys"]
    after_primary = after["primary_keys"]
    assert isinstance(before_primary, dict)
    assert isinstance(after_primary, dict)
    rows_deleted = sum(
        len(set(identities) - set(after_primary.get(table, frozenset())))
        for table, identities in before_primary.items()
    )

    before_submission = before["submission_surfaces"]
    after_submission = after["submission_surfaces"]
    assert isinstance(before_submission, dict)
    assert isinstance(after_submission, dict)
    submissions = sum(
        len(
            set(before_submission.get(table, frozenset()))
            ^ set(after_submission.get(table, frozenset()))
        )
        for table in set(before_submission) | set(after_submission)
    )

    before_runs = set(before["automation_run_surfaces"])
    after_runs = set(after["automation_run_surfaces"])
    before_questions = set(before["question_record_surfaces"])
    after_questions = set(after["question_record_surfaces"])
    forms_filled = len(after_runs ^ before_runs) + len(
        after_questions ^ before_questions
    )
    return {
        "rows_deleted": rows_deleted,
        "submissions": submissions,
        "forms_filled": forms_filled,
    }


def _assert_business_counts_unchanged(
    before: dict[str, object],
    after: dict[str, object],
) -> None:
    before_counts = before["counts"]
    after_counts = after["counts"]
    assert isinstance(before_counts, dict)
    assert isinstance(after_counts, dict)
    mutable_audit_tables = {"audit_events", "audit_outbox", "audit_chain_state"}
    changed = {
        table: {"before": count, "after": after_counts.get(table, 0)}
        for table, count in before_counts.items()
        if table not in mutable_audit_tables and after_counts.get(table, 0) != count
    }
    if changed:
        raise RuntimeError(f"status import changed business row counts: {changed}")


def _statuses_guessed(report: dict[str, object]) -> int:
    approved_match_reasons = {
        "unique_match",
        "equivalent_duplicate_role",
        "reviewed_duplicate_candidate_set",
    }
    outcomes = report.get("outcomes", [])
    if not isinstance(outcomes, list):
        raise TypeError("status report outcomes must be a list")
    return sum(
        len(outcome.get("candidates", []))
        for outcome in outcomes
        if isinstance(outcome, dict)
        and outcome.get("disposition") == "matched"
        and outcome.get("reason") not in approved_match_reasons
    )


def _assert_import_session_scope(session: object) -> None:
    """Fail before commit if the status importer touches any other surface."""

    deleted = tuple(getattr(session, "deleted"))
    if deleted:
        raise RuntimeError("status import attempted to delete persisted rows")
    unexpected_new = tuple(
        record
        for record in getattr(session, "new")
        if not isinstance(record, AuditOutbox)
    )
    if unexpected_new:
        raise RuntimeError("status import attempted to insert non-audit rows")

    allowed_opportunity_fields = {
        "user_status",
        "user_status_actor",
        "user_status_updated_at",
        "updated_at",
    }
    for record in tuple(getattr(session, "dirty")):
        if not isinstance(record, Opportunity):
            raise RuntimeError("status import attempted to update a non-opportunity row")
        changed_fields = {
            attribute.key
            for attribute in sqlalchemy_inspect(record).attrs
            if attribute.history.has_changes()
        }
        unexpected_fields = changed_fields - allowed_opportunity_fields
        if unexpected_fields:
            raise RuntimeError(
                "status import attempted to update non-status opportunity fields: "
                f"{sorted(unexpected_fields)}"
            )


def _report_dict(report: UserStatusImportReport) -> dict[str, object]:
    def row_dict(row: UserStatusImportRow) -> dict[str, object]:
        return {
            "source_line": row.source_line,
            "status": parse_user_status(row.status).value,
            "employer": row.employer,
            "programme_name": row.programme_name,
            "programme_group": row.programme_group,
        }

    outcomes = []
    for outcome in report.outcomes:
        outcomes.append(
            {
                **row_dict(outcome.row),
                "disposition": outcome.disposition,
                "reason": outcome.reason,
                "candidates": [
                    {
                        "opportunity_id": candidate.opportunity_id,
                        "employer": candidate.employer,
                        "role_title": candidate.role_title,
                        "programme_group": candidate.programme_group,
                        "cycle": candidate.cycle,
                        "location": candidate.location,
                        "division": candidate.division,
                        "source": candidate.source,
                        "target_status": candidate.target_status,
                        "user_status": candidate.user_status,
                        "user_status_actor": candidate.user_status_actor,
                        "identity_sha256": candidate.identity_sha256,
                        "destination_sha256": candidate.destination_sha256,
                    }
                    for candidate in outcome.candidates
                ],
            }
        )

    return {
        "total": report.total_count,
        "matched": report.matched_count,
        "matched_candidates": report.matched_candidate_count,
        "changed": report.changed_count,
        "unchanged": report.unchanged_count,
        "user_owned_preserved": report.user_owned_count,
        "unmatched": report.unmatched_count,
        "ambiguous": report.ambiguous_count,
        "unmatched_lines": [row.source_line for row in report.unmatched_rows],
        "ambiguous_lines": [row.source_line for row in report.ambiguous_rows],
        "user_owned_lines": [row.source_line for row in report.user_owned_rows],
        "outcomes": outcomes,
    }


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _supplied_rows_manifest() -> list[dict[str, object]]:
    return [
        {
            "source_line": row.source_line,
            "status": parse_user_status(row.status).value,
            "employer": row.employer,
            "programme_name": row.programme_name,
            "programme_group": row.programme_group,
        }
        for row in SUPPLIED_STATUS_ROWS
    ]


def _legacy_manifest_sha256() -> str:
    """Return the row-only digest written by schema-v1 reports."""

    return hashlib.sha256(
        _canonical_json(_supplied_rows_manifest()).encode("utf-8")
    ).hexdigest()


def _manifest_sha256() -> str:
    """Bind schema-v2+ reports to rows and every reviewed cohort decision."""

    if set(REVIEWED_DUPLICATE_ATTESTATIONS) != set(REVIEWED_DUPLICATE_CANDIDATES):
        raise RuntimeError("every reviewed candidate cohort requires one attestation")
    manifest = {
        "rows": _supplied_rows_manifest(),
        "reviewed_duplicates": [
            {
                "source_line": source_line,
                "candidate_ids": sorted(REVIEWED_DUPLICATE_CANDIDATES[source_line]),
                "cohort_sha256": REVIEWED_DUPLICATE_ATTESTATIONS[source_line],
            }
            for source_line in sorted(REVIEWED_DUPLICATE_CANDIDATES)
        ],
    }
    return hashlib.sha256(_canonical_json(manifest).encode("utf-8")).hexdigest()


def _report_document(payload: dict[str, object]) -> dict[str, object]:
    canonical_payload = _canonical_json(payload)
    envelope = {
        "schema_version": _REPORT_SCHEMA_VERSION,
        "manifest_sha256": _manifest_sha256(),
        "payload_sha256": hashlib.sha256(
            canonical_payload.encode("utf-8")
        ).hexdigest(),
        "payload": payload,
    }
    return {
        **envelope,
        "envelope_sha256": hashlib.sha256(
            _canonical_json(envelope).encode("utf-8")
        ).hexdigest(),
    }


def _write_report_document_exclusive(
    path: Path,
    document: dict[str, object],
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    return path


class ReportPublicationError(RuntimeError):
    """Final report could not replace its durable prepared checkpoint."""

    def __init__(self, destination: Path, staged_report_path: Path) -> None:
        super().__init__(
            "status import committed report publication failed; "
            "the prepared checkpoint and complete final stage were preserved"
        )
        self.destination = destination
        self.staged_report_path = staged_report_path


def reserve_status_import_report(path: Path, payload: dict[str, object]) -> Path:
    """Exclusively reserve a durable, integrity-tagged report checkpoint."""

    destination = path.resolve()
    return _write_report_document_exclusive(destination, _report_document(payload))


def _atomic_replace(source: Path, destination: Path) -> None:
    os.replace(source, destination)


def replace_status_import_report(path: Path, payload: dict[str, object]) -> Path:
    """Replace an owned checkpoint while preserving a failed final stage."""

    destination = path.resolve()
    if not destination.is_file():
        raise FileNotFoundError(destination)
    stage = destination.parent / f".{destination.name}.final-{uuid4().hex}.json"
    _write_report_document_exclusive(stage, _report_document(payload))
    try:
        _atomic_replace(stage, destination)
    except BaseException as error:
        raise ReportPublicationError(destination, stage) from error
    return destination


def save_status_import_report(path: Path, payload: dict[str, object]) -> Path:
    """Exclusively create an integrity-tagged report without overwriting evidence."""

    return reserve_status_import_report(path, payload)


def load_status_import_report(
    path: Path,
    *,
    require_current_manifest: bool = False,
) -> dict[str, object]:
    """Verify a current or historical report without destroying provenance."""

    document = json.loads(path.read_text(encoding="utf-8"))
    payload = document.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("status import report payload must be an object")
    actual_hash = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    if document.get("payload_sha256") != actual_hash:
        raise ValueError("status import report payload digest mismatch")
    envelope_hash = document.get("envelope_sha256")
    if envelope_hash is not None:
        envelope = {
            key: value for key, value in document.items() if key != "envelope_sha256"
        }
        actual_envelope_hash = hashlib.sha256(
            _canonical_json(envelope).encode("utf-8")
        ).hexdigest()
        if envelope_hash != actual_envelope_hash:
            raise ValueError("status import report envelope digest mismatch")
    schema_version = document.get("schema_version")
    if schema_version not in _SUPPORTED_REPORT_SCHEMA_VERSIONS:
        raise ValueError("unsupported status import report schema")
    if schema_version == 3 and envelope_hash is None:
        raise ValueError("status import report envelope digest missing")
    expected_manifest = (
        _legacy_manifest_sha256() if schema_version == 1 else _manifest_sha256()
    )
    manifest_matches_current = document.get("manifest_sha256") == expected_manifest
    if require_current_manifest and not manifest_matches_current:
        raise ValueError("status import report manifest digest mismatch")
    return {
        **document,
        "verification": {
            "current_schema_version": _REPORT_SCHEMA_VERSION,
            "current_manifest_sha256": expected_manifest,
            "manifest_matches_current": manifest_matches_current,
        },
    }


def _default_report_path(database_path: Path, *, mode: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return (
        database_path.parent
        / "artifacts"
        / "status-imports"
        / f"phase25b-status-import-{mode}-{stamp}-{uuid4().hex}.json"
    )


def _portable_path(path: Path, *, database_path: Path, external_root: str) -> str:
    try:
        relative = path.resolve().relative_to(database_path.parent.resolve())
    except ValueError:
        return f"<{external_root}>/{path.name}"
    return f"<data-dir>/{relative.as_posix()}"


def _run_once(database_path: Path, *, dry_run: bool) -> tuple[dict[str, object], int]:
    settings = _settings_for_database(database_path)
    database = Database(settings)
    try:
        with database.session_scope() as session:
            report = import_user_statuses(
                session,
                SUPPLIED_STATUS_ROWS,
                dry_run=dry_run,
                reviewed_duplicate_candidates=REVIEWED_DUPLICATE_CANDIDATES,
                reviewed_duplicate_attestations=REVIEWED_DUPLICATE_ATTESTATIONS,
            )
            if not dry_run:
                _assert_import_session_scope(session)
                session.flush()
            excluded = int(
                session.scalar(
                    select(func.count())
                    .select_from(Opportunity)
                    .where(
                        (Opportunity.user_status.is_(None))
                        | (
                            Opportunity.user_status.not_in(
                                ("NOT_APPLIED", "INTERESTED")
                            )
                        )
                    )
                )
                or 0
            )
    finally:
        database.engine.dispose()
    return _report_dict(report), excluded


def _acquire_with_retries(
    database_path: Path, *, attempts: int, retry_delay: float
) -> RuntimeLock:
    if attempts < 1:
        raise ValueError("lock attempts must be positive")
    lock = RuntimeLock(
        runtime_lock_path(database_path.parent),
        port=0,
        metadata={"operation": "phase25_user_status_import"},
    )
    for attempt in range(1, attempts + 1):
        try:
            return lock.acquire()
        except RuntimeAlreadyRunning:
            if attempt == attempts:
                raise
            time.sleep(retry_delay)
    raise AssertionError("unreachable")


def _persist_result(
    result: dict[str, object],
    *,
    database_path: Path,
    report_path: Path,
) -> dict[str, object]:
    persisted = {
        **result,
        "report_file": report_path.name,
        "report_path": _portable_path(
            report_path,
            database_path=database_path,
            external_root="report-dir",
        ),
    }
    save_status_import_report(report_path, persisted)
    return persisted


def _project_status_import(
    database_path: Path,
    *,
    database_lock_attempts: int,
    database_lock_retry_delay: float,
) -> tuple[dict[str, object], int, dict[str, object], dict[str, object]]:
    with TemporaryDirectory(prefix="argus-phase25-status-") as directory:
        projected_path = Path(directory) / "argus.db"
        retry_database_locked(
            lambda: _online_copy(database_path, projected_path),
            attempts=database_lock_attempts,
            retry_delay=database_lock_retry_delay,
        )
        projected_report, projected_excluded = retry_database_locked(
            lambda: _run_once(projected_path, dry_run=False),
            attempts=database_lock_attempts,
            retry_delay=database_lock_retry_delay,
        )
        projected_idempotency, _ = retry_database_locked(
            lambda: _run_once(projected_path, dry_run=True),
            attempts=database_lock_attempts,
            retry_delay=database_lock_retry_delay,
        )
        projected_snapshot = _database_safety_snapshot(projected_path)
    if int(projected_idempotency["changed"]) != 0:
        raise RuntimeError("projected status import second pass was not idempotent")
    return (
        projected_report,
        projected_excluded,
        projected_idempotency,
        projected_snapshot,
    )


def run_status_import(
    database_path: Path,
    *,
    apply: bool = False,
    backup_dir: Path | None = None,
    report_path: Path | None = None,
    lock_attempts: int = 5,
    lock_retry_delay: float = 0.2,
    database_lock_attempts: int = 5,
    database_lock_retry_delay: float = 0.2,
) -> dict[str, object]:
    """Project or apply the complete supplied manifest with invariants."""

    database_path = database_path.resolve()
    if not database_path.is_file():
        raise FileNotFoundError(database_path)
    resolved_report_path = (
        report_path.resolve()
        if report_path is not None
        else _default_report_path(
            database_path,
            mode="apply" if apply else "dry-run",
        ).resolve()
    )
    if resolved_report_path.exists():
        raise FileExistsError(resolved_report_path)

    if not apply:
        before = retry_database_locked(
            lambda: _database_safety_snapshot(database_path),
            attempts=database_lock_attempts,
            retry_delay=database_lock_retry_delay,
        )
        (
            projected_report,
            projected_excluded,
            projected_idempotency,
            projected_snapshot,
        ) = _project_status_import(
            database_path,
            database_lock_attempts=database_lock_attempts,
            database_lock_retry_delay=database_lock_retry_delay,
        )
        after = retry_database_locked(
            lambda: _database_safety_snapshot(database_path),
            attempts=database_lock_attempts,
            retry_delay=database_lock_retry_delay,
        )
        safety = _safety_metrics(before, after)
        if before["logical_sha256"] != after["logical_sha256"]:
            raise RuntimeError("dry-run source database changed during projection")
        result = {
            "mode": "dry_run",
            "commit_state": "not_applicable",
            "report": projected_report,
            "idempotency": projected_idempotency,
            "excluded_by_status": projected_excluded,
            "backup": None,
            "source_sha256": before["logical_sha256"],
            "projected_post_sha256": projected_snapshot["logical_sha256"],
            **safety,
            "statuses_guessed": _statuses_guessed(projected_report),
            "discrepancy": (
                "Evercore placement is HIREVUE; both supplied Goldman Sachs rows are "
                "NOT_APPLIED despite the separate recollection that Goldman was applied."
            ),
        }
        return _persist_result(
            result,
            database_path=database_path,
            report_path=resolved_report_path,
        )

    lock = _acquire_with_retries(
        database_path,
        attempts=lock_attempts,
        retry_delay=lock_retry_delay,
    )
    try:
        before = retry_database_locked(
            lambda: _database_safety_snapshot(database_path),
            attempts=database_lock_attempts,
            retry_delay=database_lock_retry_delay,
        )
        (
            projected_report,
            projected_excluded,
            projected_idempotency,
            projected_snapshot,
        ) = _project_status_import(
            database_path,
            database_lock_attempts=database_lock_attempts,
            database_lock_retry_delay=database_lock_retry_delay,
        )
        resolved_backup_dir = backup_dir or database_path.parent / "backups"
        backup_path = retry_database_locked(
            lambda: create_verified_backup(database_path, resolved_backup_dir),
            attempts=database_lock_attempts,
            retry_delay=database_lock_retry_delay,
        )
        backup_hash = _hash_file(backup_path)
        backup_snapshot = retry_database_locked(
            lambda: _database_safety_snapshot(backup_path),
            attempts=database_lock_attempts,
            retry_delay=database_lock_retry_delay,
        )
        if backup_snapshot["logical_sha256"] != before["logical_sha256"]:
            raise RuntimeError("verified backup does not match the locked baseline")
        _assert_business_counts_unchanged(before, projected_snapshot)
        projected_safety = _safety_metrics(before, projected_snapshot)
        if any(projected_safety.values()):
            raise RuntimeError(
                f"projected status import violated safety invariants: {projected_safety}"
            )

        prepared = {
            "mode": "apply",
            "commit_state": "prepared",
            "report": projected_report,
            "idempotency": projected_idempotency,
            "excluded_by_status": projected_excluded,
            "backup": _portable_path(
                backup_path,
                database_path=database_path,
                external_root="backup-dir",
            ),
            "backup_file": backup_path.name,
            "backup_sha256": backup_hash,
            "baseline_sha256": before["logical_sha256"],
            "projected_post_sha256": projected_snapshot["logical_sha256"],
            **projected_safety,
            "statuses_guessed": _statuses_guessed(projected_report),
            "recovery": (
                "If this checkpoint remains, compare the live database with the "
                "baseline and projected status evidence before retrying."
            ),
            "discrepancy": (
                "Evercore placement is HIREVUE; both supplied Goldman Sachs rows are "
                "NOT_APPLIED despite the separate recollection that Goldman was applied."
            ),
        }
        prepared = {
            **prepared,
            "report_file": resolved_report_path.name,
            "report_path": _portable_path(
                resolved_report_path,
                database_path=database_path,
                external_root="report-dir",
            ),
        }
        reserve_status_import_report(resolved_report_path, prepared)

        # This write is attempted exactly once. A commit-stage exception may
        # occur after SQLite committed (for example during audit drain), so a
        # blind retry could duplicate side effects. The prepared checkpoint
        # and verified backup are the recovery boundary in that case.
        report, _ = _run_once(database_path, dry_run=False)
        idempotency, excluded = _run_once(database_path, dry_run=True)
        after = retry_database_locked(
            lambda: _database_safety_snapshot(database_path),
            attempts=database_lock_attempts,
            retry_delay=database_lock_retry_delay,
        )
        if report != projected_report or excluded != projected_excluded:
            raise RuntimeError("live status import diverged from its locked projection")
        if idempotency != projected_idempotency:
            raise RuntimeError("live status import idempotency diverged from projection")
        if int(idempotency["changed"]) != 0:
            raise RuntimeError("status import second pass was not idempotent")
        _assert_business_counts_unchanged(before, after)
        safety = _safety_metrics(before, after)
        if any(safety.values()):
            raise RuntimeError(f"status import violated safety invariants: {safety}")
        statuses_guessed = _statuses_guessed(report)
        if statuses_guessed:
            raise RuntimeError(f"status import guessed {statuses_guessed} statuses")

        result = {
            **prepared,
            "commit_state": "verified",
            "report": report,
            "idempotency": idempotency,
            "excluded_by_status": excluded,
            "post_apply_sha256": after["logical_sha256"],
            **safety,
            "statuses_guessed": statuses_guessed,
        }
        replace_status_import_report(resolved_report_path, result)
        return result
    finally:
        lock.release()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="apply after a verified backup")
    parser.add_argument("--database", type=Path, help="explicit argus.db path")
    parser.add_argument("--backup-dir", type=Path, help="apply-mode backup directory")
    parser.add_argument("--report", type=Path, help="new JSON report path")
    parser.add_argument("--lock-attempts", type=int, default=5)
    parser.add_argument("--lock-retry-delay", type=float, default=0.2)
    parser.add_argument("--database-lock-attempts", type=int, default=5)
    parser.add_argument("--database-lock-retry-delay", type=float, default=0.2)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = Settings.load()
    database_path = (
        args.database.resolve()
        if args.database is not None
        else settings.data_dir / "argus.db"
    )
    result = run_status_import(
        database_path,
        apply=args.apply,
        backup_dir=args.backup_dir,
        report_path=args.report,
        lock_attempts=args.lock_attempts,
        lock_retry_delay=args.lock_retry_delay,
        database_lock_attempts=args.database_lock_attempts,
        database_lock_retry_delay=args.database_lock_retry_delay,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
