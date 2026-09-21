from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

from sqlalchemy import func, select

from app.config import Settings
from app.db import Database
from app.models import AuditEvent, CandidateProfile, Document, Opportunity
from app.security.audit import AuditInput, _hash_event, append_audit


ROOT = Path(__file__).resolve().parents[2]


def run_cli(
    data_dir: Path,
    *args: str,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["ARGUS_DATA_DIR"] = str(data_dir)
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-m", "app.cli", *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def test_init_creates_database_key_and_persistent_capture_token(tmp_path: Path) -> None:
    result = run_cli(tmp_path, "init")

    assert result.returncode == 0, result.stderr
    assert "ARGUS initialised" in result.stdout
    assert (tmp_path / "argus.db").is_file()
    assert (tmp_path / "secret.key").is_file()
    assert (tmp_path / "api_token.txt").is_file()
    if os.name != "nt":
        assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
        assert stat.S_IMODE((tmp_path / "argus.db").stat().st_mode) == 0o600
        assert stat.S_IMODE((tmp_path / "secret.key").stat().st_mode) == 0o600
        assert stat.S_IMODE((tmp_path / "api_token.txt").stat().st_mode) == 0o600


def test_seed_is_idempotent_and_builds_safe_lab_fixture(tmp_path: Path) -> None:
    assert run_cli(tmp_path, "seed").returncode == 0
    assert run_cli(tmp_path, "seed").returncode == 0

    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    db = Database(settings)
    with db.session_scope() as session:
        assert session.scalar(select(func.count()).select_from(CandidateProfile)) == 1
        assert session.scalar(select(func.count()).select_from(Document)) == 1
        assert session.scalar(select(func.count()).select_from(Opportunity)) == 5
        opportunities = list(session.scalars(select(Opportunity)).all())
        assert {item.role_title for item in opportunities} == {"Summer Analyst"}
        assert {item.programme_group for item in opportunities} == {
            "standard", "sensitive", "essay", "assessment", "mismatch"
        }


def test_audit_command_returns_nonzero_for_tampered_chain(tmp_path: Path) -> None:
    assert run_cli(tmp_path, "init").returncode == 0
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    db = Database(settings)
    with db.session_scope() as session:
        event = append_audit(
            session,
            AuditInput("test", "fixture.created", "fixture", "1", {"safe": True}),
        )
        event.details_json = '{"safe":false}'

    result = run_cli(tmp_path, "audit")

    assert result.returncode == 2
    assert "BROKEN" in result.stdout


def test_audit_command_does_not_create_a_missing_runtime(tmp_path: Path) -> None:
    result = run_cli(tmp_path, "audit")

    assert result.returncode == 2
    assert "database does not exist" in result.stderr
    assert not (tmp_path / "argus.db").exists()
    assert not (tmp_path / "secret.key").exists()
    assert not (tmp_path / "api_token.txt").exists()


def test_audit_cli_defaults_to_current_epoch_without_hiding_broken_history(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "argus.db"
    created_one = "2026-08-24T00:00:00.000000+00:00"
    created_two = "2026-08-24T00:00:01.000000+00:00"
    details_one = '{"n":1}'
    details_two = '{"n":2}'
    first_hash = _hash_event(
        "", created_one, "legacy", "legacy.one", "fixture", "1", details_one
    )
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
                (
                    1, created_one, "legacy", "legacy.one", "fixture", "1",
                    details_one, "", first_hash,
                ),
                (
                    2, created_two, "legacy", "legacy.two", "fixture", "2",
                    details_two, "", fork_hash,
                ),
            ],
        )

    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    database = Database(settings)
    database.create_schema()
    with database.session_scope() as session:
        append_audit(
            session,
            AuditInput("future", "future.event", "fixture", "3", {"n": 3}),
        )

    current = run_cli(tmp_path, "audit")
    explicit = run_cli(tmp_path, "audit", "--epoch", "1")
    historical = run_cli(tmp_path, "audit", "--all-epochs")

    assert current.returncode == 0, current.stdout + current.stderr
    assert "CURRENT EPOCH VALID" in current.stdout
    assert "epoch 1" in current.stdout
    assert explicit.returncode == 0, explicit.stdout + explicit.stderr
    assert "EPOCH VALID" in explicit.stdout
    assert historical.returncode == 2
    assert "AUDIT BROKEN" in historical.stdout


def test_submit_cli_requires_retyping_the_exact_application_id(tmp_path: Path) -> None:
    missing = run_cli(
        tmp_path,
        "apply",
        "exact-application",
        "--mode",
        "submit",
    )
    mismatched = run_cli(
        tmp_path,
        "apply",
        "exact-application",
        "--mode",
        "submit",
        "--confirm-submit",
        "different-application",
    )

    assert missing.returncode == 2
    assert mismatched.returncode == 2
    assert "Retype the exact application ID" in missing.stderr
    assert "Retype the exact application ID" in mismatched.stderr


def test_seed_real_requires_explicit_profile_and_does_not_print_identity(
    tmp_path: Path,
) -> None:
    result = run_cli(tmp_path, "seed-real")

    assert result.returncode == 2
    assert "explicit profile" in result.stderr.lower()
    assert "demo" not in (result.stdout + result.stderr).lower()


def test_seed_real_accepts_profile_file_and_cv_root_without_identity_output(
    tmp_path: Path,
) -> None:
    profile_path = tmp_path / "candidate-profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "first_name": "Ada",
                "last_name": "Lovelace",
                "email": "ada@example.test",
                "phone": "+44 7000 000000",
                "city": "London",
                "country": "United Kingdom",
                "university": "Synthetic University",
                "degree": "BSc Computing",
                "graduation_year": 2029,
                "current_study_year": "Final year",
                "preferred_locations": ["London"],
                "work_authorisation": "SYNTHETIC FIXTURE ONLY",
                "requires_sponsorship": False,
                "work_authorisation_approved": True,
            }
        ),
        encoding="utf-8",
    )
    cv_root = tmp_path / "cv-library"
    cv_path = cv_root / "Finance" / "Example Firm" / "Investment Banking" / "role.docx"
    cv_path.parent.mkdir(parents=True)
    cv_path.write_bytes(b"synthetic cv fixture")

    result = run_cli(
        tmp_path,
        "seed-real",
        "--profile",
        str(profile_path),
        "--cv-root",
        str(cv_root),
    )

    assert result.returncode == 0, result.stderr
    assert "Ada Lovelace" not in (result.stdout + result.stderr)
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    db = Database(settings)
    with db.session_scope() as session:
        assert session.scalar(select(func.count()).select_from(CandidateProfile)) == 1
        assert session.scalar(select(func.count()).select_from(Document)) == 1


def test_seed_real_accepts_profile_path_from_environment(tmp_path: Path) -> None:
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "first_name": "Grace",
                "last_name": "Hopper",
                "email": "grace@example.test",
                "university": "Synthetic University",
                "graduation_year": 2029,
                "preferred_locations": ["London"],
                "work_authorisation": "SYNTHETIC FIXTURE ONLY",
                "requires_sponsorship": False,
                "work_authorisation_approved": True,
            }
        ),
        encoding="utf-8",
    )

    result = run_cli(
        tmp_path,
        "seed-real",
        env_overrides={
            "ARGUS_PROFILE_PATH": str(profile_path),
            "ARGUS_CV_ROOT": str(tmp_path / "empty-cv-root"),
        },
    )

    assert result.returncode == 0, result.stderr
    assert "Grace Hopper" not in (result.stdout + result.stderr)
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    db = Database(settings)
    with db.session_scope() as session:
        assert session.scalar(select(CandidateProfile.first_name)) == "Grace"
