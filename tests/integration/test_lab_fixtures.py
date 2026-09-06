from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_lab_exposes_distinct_local_ats_fixtures(tmp_path: Path) -> None:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    with TestClient(create_app(settings)) as client:
        greenhouse = client.get("/lab/ats/greenhouse")
        lever = client.get("/lab/ats/lever")
        workday = client.get("/lab/ats/workday")

    assert greenhouse.status_code == 200
    assert 'data-ats="greenhouse"' in greenhouse.text
    assert 'id="grnhse_app"' in greenhouse.text
    assert lever.status_code == 200
    assert 'data-ats="lever"' in lever.text
    assert "posting-apply" in lever.text
    assert workday.status_code == 200
    assert 'data-ats="workday"' in workday.text
    assert 'data-automation-id="wd-application"' in workday.text
