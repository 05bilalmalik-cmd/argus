from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_health_endpoint_reports_ready(tmp_path: Path) -> None:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "ARGUS",
        "version": "0.2.0",
        "automation_mode": "OFF",
        "live_submit": False,
        "trackr_live": False,
    }
    assert (tmp_path / "argus.db").exists()
