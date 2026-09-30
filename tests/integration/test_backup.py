from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services import backup as backup_module
from tests.document_helpers import cv_docx_bytes


def _client(tmp_path: Path) -> TestClient:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    app = create_app(settings)
    app.state.ui_v2_enabled = True
    return TestClient(app)


def test_backup_round_trip_captures_db_keys_and_documents(tmp_path: Path) -> None:
    with _client(tmp_path) as api:
        created = api.post(
            "/api/documents",
            data={"kind": "cv", "approved": "true", "tags": "london"},
            files={"file": ("Backup CV.docx", cv_docx_bytes(2028, "Backup fixture"), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        )
        assert created.status_code in (200, 201), created.text

        response = api.post("/api/backup")
        assert response.status_code == 201, response.text
        summary = response.json()
        assert summary["backup_id"].startswith("argus-backup-")
        assert summary["files"] >= 3
        assert len(summary["db_sha256"]) == 64

        target = tmp_path / "backups" / summary["backup_id"]
        manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["backup_id"] == summary["backup_id"]
        assert manifest["db_sha256"] == summary["db_sha256"]
        assert any("trackr_saves" in item for item in manifest["excluded"])

        live_tables = _table_names(tmp_path / "argus.db")
        copy_tables = _table_names(target / "argus.db")
        assert live_tables == copy_tables and "opportunities" in copy_tables

        status = api.get("/api/backup")
        assert status.status_code == 200
        assert status.json()["latest"]["backup_id"] == summary["backup_id"]

        control = api.get("/control")
        assert control.status_code == 200
        assert summary["backup_id"] in control.text


def test_backup_refused_without_database(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(backup_module.BackupError, match="no database"):
        backup_module.create_backup(
            tmp_path,
            database_path=tmp_path / "missing.db",
            documents_dir=tmp_path / "documents",
            secret_key_path=tmp_path / "secret.key",
            api_token_path=tmp_path / "api_token.txt",
        )


def test_backup_endpoint_maps_refusal_to_409(tmp_path: Path, monkeypatch) -> None:
    with _client(tmp_path) as api:
        monkeypatch.setattr(
            backup_module,
            "create_backup",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                backup_module.BackupError("no database to back up")
            ),
        )
        response = api.post("/api/backup")
        assert response.status_code == 409
        assert response.json()["detail"].startswith("Backup refused")


def test_backup_prunes_beyond_retention(tmp_path: Path) -> None:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    settings.ensure_directories()
    (tmp_path / "argus.db").touch()
    import time as _time

    for _ in range(3):
        _time.sleep(1.1)  # timestamped slots have one-second resolution
        backup_module.create_backup(
            tmp_path,
            database_path=tmp_path / "argus.db",
            documents_dir=tmp_path / "documents",
            secret_key_path=tmp_path / "secret.key",
            api_token_path=tmp_path / "api_token.txt",
            retain=2,
        )
    assert len(backup_module.list_backups(tmp_path)) == 2


def _table_names(db_path: Path) -> set[str]:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        connection.close()
