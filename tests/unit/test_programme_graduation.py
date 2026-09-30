"""Tests for programme graduation year logic and migration."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from app.config import Settings, AutomationMode
from app.db import Database, SCHEMA_VERSION
from app.models import CandidateProfile, Base
from app.scouting.programmes import ProgrammeFraming, ProgrammeType, classify_programme
from app.security.crypto import CryptoBox
from app.services.profile import ProfileService, ProfileUpdate
from app.domain.questions import CanonicalKey


def _make_settings(db_path: str, tmp: Path) -> Settings:
    return Settings(
        host="127.0.0.1",
        port=8787,
        data_dir=tmp,
        database_url=f"sqlite:///{db_path}",
        documents_dir=tmp / "documents",
        artifacts_dir=tmp / "artifacts",
        traces_dir=tmp / "traces",
        screenshots_dir=tmp / "screenshots",
        secret_key_path=tmp / "secret.key",
        live_submit_enabled=False,
        automation_mode=AutomationMode.REVIEW_ONLY,
        live_domain_allowlist=frozenset(),
        api_token="test",
        api_token_path=tmp / "api_token.txt",
        ollama_url="http://localhost:11434",
        ollama_model=None,
        browser_headless=True,
        sweep_interval_hours=1.0,
        trackr_live_enabled=False,
        notifications_enabled=False,
        ntfy_topic="",
        ntfy_server="",
        notify_webhook_url="",
        hermes_notify_target=None,
        hermes_bin="",
        notify_batch_window_seconds=60.0,
        notify_rate_limit_per_hour=100,
        apply_click_enabled=False,
        apply_click_run_cap=10,
        apply_click_timeout_ms=30000,
    )


def _run_with_db(test_func):
    """Run a test function with a temporary database, ensuring proper cleanup."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = str(tmp_path / "test.db")
        settings = _make_settings(db_path, tmp_path)
        crypto = CryptoBox.from_path(settings.secret_key_path)
        db = Database(settings)
        Base.metadata.create_all(db.engine)
        try:
            test_func(db, crypto, settings)
        finally:
            db.engine.dispose()


def _run_with_existing_db(test_func):
    """Run a test function with a pre-existing database at schema v9."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = str(tmp_path / "test.db")

        # Create a database at schema version 9 WITHOUT the new column
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE candidate_profiles (
                id INTEGER PRIMARY KEY DEFAULT 1,
                first_name VARCHAR(120) DEFAULT '',
                last_name VARCHAR(120) DEFAULT '',
                preferred_name VARCHAR(120) DEFAULT '',
                email VARCHAR(320) DEFAULT '',
                phone VARCHAR(80) DEFAULT '',
                address_line1 VARCHAR(240) DEFAULT '',
                city VARCHAR(120) DEFAULT '',
                postcode VARCHAR(32) DEFAULT '',
                country VARCHAR(120) DEFAULT 'United Kingdom',
                linkedin_url VARCHAR(500) DEFAULT '',
                university VARCHAR(240) DEFAULT '',
                degree VARCHAR(240) DEFAULT '',
                graduation_year INTEGER,
                current_study_year VARCHAR(80) DEFAULT '',
                preferred_locations_json TEXT DEFAULT '[]',
                work_authorisation_ciphertext TEXT DEFAULT '',
                sponsorship_required_ciphertext TEXT DEFAULT '',
                work_authorisation_approved BOOLEAN DEFAULT 0,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("INSERT INTO candidate_profiles DEFAULT VALUES")
        cursor.execute("PRAGMA user_version = 9")
        conn.commit()
        conn.close()

        settings = _make_settings(db_path, tmp_path)
        crypto = CryptoBox.from_path(settings.secret_key_path)
        db = Database(settings)
        try:
            test_func(db, crypto, settings)
        finally:
            db.engine.dispose()


class TestProgrammeGraduationLogic:
    """Tests for the admissible graduation years vs framing logic."""

    def test_admissible_include_summer_framing_no_conflict(self) -> None:
        """admissible {2028,2029} + summer framing (2028) -> fills 2028, NO conflict."""
        def _test(db, crypto, settings):
            with db.session_scope() as session:
                service = ProfileService(session, crypto)
                service.update(
                    ProfileUpdate(
                        graduation_year=2029,
                        admissible_graduation_years=(2028, 2029),
                    )
                )

            with db.session_scope() as session:
                service = ProfileService(session, crypto)
                framing = ProgrammeFraming(3, 2028, "summer-cv")
                data = service.get_automation_data(framing)

            assert data.get(CanonicalKey.GRADUATION_YEAR.value) == 2028
            assert data.get(CanonicalKey.EDUCATION_END_YEAR.value) == 2028
            assert CanonicalKey.PROGRAMME_GRADUATION_CONFLICT.value not in data

        _run_with_db(_test)

    def test_admissible_include_spring_week_framing_no_conflict(self) -> None:
        """admissible {2028,2029} + spring_week framing (2029) -> fills 2029, NO conflict."""
        def _test(db, crypto, settings):
            with db.session_scope() as session:
                service = ProfileService(session, crypto)
                service.update(
                    ProfileUpdate(
                        graduation_year=2028,
                        admissible_graduation_years=(2028, 2029),
                    )
                )

            with db.session_scope() as session:
                service = ProfileService(session, crypto)
                framing = ProgrammeFraming(4, 2029, "yii-cv")
                data = service.get_automation_data(framing)

            assert data.get(CanonicalKey.GRADUATION_YEAR.value) == 2029
            assert data.get(CanonicalKey.EDUCATION_END_YEAR.value) == 2029
            assert CanonicalKey.PROGRAMME_GRADUATION_CONFLICT.value not in data

        _run_with_db(_test)

    def test_admissible_exclude_framing_conflict_raised(self) -> None:
        """admissible {2028,2029} + framing demanding 2030 -> conflict STILL raised."""
        def _test(db, crypto, settings):
            with db.session_scope() as session:
                service = ProfileService(session, crypto)
                service.update(
                    ProfileUpdate(
                        graduation_year=2028,
                        admissible_graduation_years=(2028, 2029),
                    )
                )

            with db.session_scope() as session:
                service = ProfileService(session, crypto)
                framing = ProgrammeFraming(4, 2030, "yii-cv")
                data = service.get_automation_data(framing)

            assert data.get(CanonicalKey.PROGRAMME_GRADUATION_CONFLICT.value) is True
            assert data.get("guard.programme_graduation_stored") == 2028
            assert data.get("guard.programme_graduation_tier") == 2030
            assert data.get("guard.programme_admissible_years") == (2028, 2029)
            assert CanonicalKey.GRADUATION_YEAR.value not in data

        _run_with_db(_test)

    def test_no_admissible_stored_matches_summer_framing_no_conflict(self) -> None:
        """no admissible set, stored 2028, summer framing 2028 -> no conflict (regression)."""
        def _test(db, crypto, settings):
            with db.session_scope() as session:
                service = ProfileService(session, crypto)
                service.update(ProfileUpdate(graduation_year=2028))

            with db.session_scope() as session:
                service = ProfileService(session, crypto)
                framing = ProgrammeFraming(3, 2028, "summer-cv")
                data = service.get_automation_data(framing)

            assert data.get(CanonicalKey.GRADUATION_YEAR.value) == 2028
            assert CanonicalKey.PROGRAMME_GRADUATION_CONFLICT.value not in data

        _run_with_db(_test)

    def test_no_admissible_stored_differs_summer_framing_conflict(self) -> None:
        """no admissible set, stored 2029, summer framing 2028 -> conflict (regression guard)."""
        def _test(db, crypto, settings):
            with db.session_scope() as session:
                service = ProfileService(session, crypto)
                service.update(ProfileUpdate(graduation_year=2029))

            with db.session_scope() as session:
                service = ProfileService(session, crypto)
                framing = ProgrammeFraming(3, 2028, "summer-cv")
                data = service.get_automation_data(framing)

            assert data.get(CanonicalKey.PROGRAMME_GRADUATION_CONFLICT.value) is True
            assert data.get("guard.programme_graduation_stored") == 2029
            assert data.get("guard.programme_graduation_tier") == 2028
            assert CanonicalKey.GRADUATION_YEAR.value not in data

        _run_with_db(_test)

    def test_framing_none_uses_stored_year(self) -> None:
        """framing None -> stored year used, as today."""
        def _test(db, crypto, settings):
            with db.session_scope() as session:
                service = ProfileService(session, crypto)
                service.update(ProfileUpdate(graduation_year=2029))

            with db.session_scope() as session:
                service = ProfileService(session, crypto)
                data = service.get_automation_data(framing=None)

            assert data.get(CanonicalKey.GRADUATION_YEAR.value) == 2029
            assert data.get(CanonicalKey.EDUCATION_END_YEAR.value) == 2029
            assert CanonicalKey.PROGRAMME_GRADUATION_CONFLICT.value not in data

        _run_with_db(_test)


class TestMigration:
    """Test that existing databases get the new column on startup."""

    def test_existing_db_without_column_gets_it_added(self) -> None:
        """An existing DB without the column gets it added and profile reads succeed."""
        def _test(db, crypto, settings):
            # Run just the additive migration part (what adds our column)
            with db.engine.begin() as connection:
                from sqlalchemy import inspect, text
                inspector = inspect(connection)
                from app.db import _ADDITIVE_MIGRATIONS
                for table, columns in _ADDITIVE_MIGRATIONS.items():
                    if table not in inspector.get_table_names():
                        continue
                    existing = {column["name"] for column in inspector.get_columns(table)}
                    for column, sql_type in columns.items():
                        if column in existing:
                            continue
                        connection.execute(
                            text(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {sql_type}')
                        )
                connection.execute(text(f"PRAGMA user_version = {SCHEMA_VERSION}"))

            # Verify column exists and has default
            conn = sqlite3.connect(str(db.engine.url.database))
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(candidate_profiles);")
            columns = {row[1] for row in cursor.fetchall()}
            assert "admissible_graduation_years_json" in columns

            cursor.execute("SELECT admissible_graduation_years_json FROM candidate_profiles;")
            row = cursor.fetchone()
            assert row[0] == "[]"
            cursor.execute("PRAGMA user_version;")
            assert cursor.fetchone()[0] == SCHEMA_VERSION
            conn.close()

            # Verify profile reads work via raw SQL (avoiding audit system)
            conn = sqlite3.connect(str(db.engine.url.database))
            cursor = conn.cursor()
            cursor.execute("SELECT admissible_graduation_years_json FROM candidate_profiles;")
            row = cursor.fetchone()
            assert row[0] == "[]"
            conn.close()

        _run_with_existing_db(_test)


class TestProfileUpdate:
    """Tests for updating admissible graduation years via ProfileUpdate."""

    def test_update_admissible_graduation_years(self) -> None:
        """Updating admissible_graduation_years persists correctly."""
        def _test(db, crypto, settings):
            with db.session_scope() as session:
                service = ProfileService(session, crypto)
                service.update(
                    ProfileUpdate(
                        graduation_year=2028,
                        admissible_graduation_years=(2028, 2029, 2030),
                    )
                )

            with db.session_scope() as session:
                service = ProfileService(session, crypto)
                profile = service.get_model()
                admissible = json.loads(profile.admissible_graduation_years_json)
                assert admissible == [2028, 2029, 2030]

            with db.session_scope() as session:
                service = ProfileService(session, crypto)
                public = service.public_dict()
                assert public["admissible_graduation_years"] == [2028, 2029, 2030]

        _run_with_db(_test)


class TestProgrammeClassification:
    """Regression tests for programme type classification."""

    def test_summer_classification(self) -> None:
        assert classify_programme("Summer Internship") == ProgrammeType.SUMMER
        assert classify_programme("Summer Analyst") == ProgrammeType.SUMMER

    def test_spring_week_classification(self) -> None:
        assert classify_programme("Spring Week") == ProgrammeType.SPRING_WEEK
        assert classify_programme("Spring Insight") == ProgrammeType.SPRING_WEEK

    def test_year_in_industry_classification(self) -> None:
        assert classify_programme("Year in Industry") == ProgrammeType.YEAR_IN_INDUSTRY
        assert classify_programme("Industrial Placement") == ProgrammeType.YEAR_IN_INDUSTRY
        assert classify_programme("Placement Year") == ProgrammeType.YEAR_IN_INDUSTRY


if __name__ == "__main__":
    pytest.main([__file__, "-v"])