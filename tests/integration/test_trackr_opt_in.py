from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def _client(tmp_path: Path, *, live_enabled: bool = False) -> TestClient:
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_ENABLE_TRACKR_LIVE": "true" if live_enabled else "false",
        }
    )
    return TestClient(create_app(settings))


def test_live_trackr_route_fails_closed_before_fetch_when_opt_in_is_disabled(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[str] = []

    def forbidden_fetch() -> list[object]:
        calls.append("fetch_programmes")
        raise AssertionError("live Trackr fetch was not opted in")

    monkeypatch.setattr("app.scouting.trackr_live.fetch_programmes", forbidden_fetch)

    with _client(tmp_path) as client:
        response = client.post("/api/scout/scrape-live")

    assert response.status_code == 403
    assert "ARGUS_ENABLE_TRACKR_LIVE" in response.json()["detail"]
    assert calls == []


def test_live_trackr_route_fetches_when_opt_in_is_enabled(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []

    def permitted_fetch() -> list[object]:
        calls.append("fetch_programmes")
        return []

    monkeypatch.setattr("app.scouting.trackr_live.fetch_programmes", permitted_fetch)

    with _client(tmp_path, live_enabled=True) as client:
        response = client.post("/api/scout/scrape-live")

    assert response.status_code == 200
    assert response.json()["live_scrape"] == 0
    assert calls == ["fetch_programmes"]


def test_scout_page_disables_live_run_but_keeps_local_import_available(
    tmp_path: Path,
) -> None:
    with _client(tmp_path) as client:
        page = client.get("/scout")
        local_import = client.post(
            "/api/scout/upload-trackr-html",
            files={"file": ("saved-trackr.html", b"<html></html>", "text/html")},
        )

    assert page.status_code == 200
    assert 'data-scout-action="scrape-live"' not in page.text
    assert 'disabled aria-disabled="true"' in page.text
    assert "Live Trackr scraping is disabled" in page.text
    assert "ARGUS_ENABLE_TRACKR_LIVE=true" in page.text
    assert local_import.status_code == 200


def test_scout_page_exposes_live_run_after_explicit_opt_in(tmp_path: Path) -> None:
    with _client(tmp_path, live_enabled=True) as client:
        page = client.get("/scout")

    assert page.status_code == 200
    assert 'data-scout-action="scrape-live"' in page.text
    assert "Live Trackr scraping is disabled" not in page.text
