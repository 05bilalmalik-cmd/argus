"""Tests for POST /api/review-queue/confirm."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_confirm_rejects_empty_list(tmp_path: Path) -> None:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    with TestClient(create_app(settings)) as client:
        with patch(
            "app.routers.review_queue.ScoutService"
        ) as mock_service_cls:
            response = client.post(
                "/api/review-queue/confirm", json={"application_ids": []}
            )
            assert response.status_code == 400
            mock_service_cls.assert_not_called()


def test_confirm_rejects_missing_list(tmp_path: Path) -> None:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    with TestClient(create_app(settings)) as client:
        with patch(
            "app.routers.review_queue.ScoutService"
        ) as mock_service_cls:
            response = client.post("/api/review-queue/confirm", json={})
            assert response.status_code == 400
            mock_service_cls.assert_not_called()


def test_confirm_rejects_more_than_25(tmp_path: Path) -> None:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    ids = [f"app-{i}" for i in range(26)]
    with TestClient(create_app(settings)) as client:
        with patch(
            "app.routers.review_queue.ScoutService"
        ) as mock_service_cls:
            response = client.post(
                "/api/review-queue/confirm", json={"application_ids": ids}
            )
            assert response.status_code == 400
            mock_service_cls.assert_not_called()


def test_confirm_passes_ids_through_to_run_autopilot(tmp_path: Path) -> None:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    with TestClient(create_app(settings)) as client:
        with patch(
            "app.routers.review_queue.ScoutService"
        ) as mock_service_cls:
            mock_service = MagicMock()
            mock_service.run_autopilot.return_value = {
                "processed": 2,
                "submitted": 0,
            }
            mock_service_cls.return_value = mock_service
            response = client.post(
                "/api/review-queue/confirm",
                json={"application_ids": ["app-a", "app-b", "app-a"]},
            )
            assert response.status_code == 200
            assert response.json() == {"processed": 2, "submitted": 0}
            mock_service.run_autopilot.assert_called_once()
            _, kwargs = mock_service.run_autopilot.call_args
            assert kwargs["confirmed_application_ids"] == ["app-a", "app-b"]
            assert kwargs["submit"] is True
            assert kwargs["max_runs"] == 2


def test_confirm_does_not_bypass_armed_gate(tmp_path: Path) -> None:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    with TestClient(create_app(settings)) as client:
        app_settings = client.app.state.settings
        assert app_settings.submission_armed is False
        mode_before = app_settings.automation_mode
        with patch(
            "app.routers.review_queue.ScoutService"
        ) as mock_service_cls:
            mock_service = MagicMock()
            mock_service.run_autopilot.return_value = {
                "processed": 1,
                "submitted": 0,
                "submission_armed": False,
            }
            mock_service_cls.return_value = mock_service
            response = client.post(
                "/api/review-queue/confirm",
                json={"application_ids": ["app-1"]},
            )
            assert response.status_code == 200
            mock_service.run_autopilot.assert_called_once()
            _, kwargs = mock_service.run_autopilot.call_args
            assert kwargs["confirmed_application_ids"] == ["app-1"]
            assert kwargs["submit"] is True
        assert app_settings.submission_armed is False
        assert app_settings.automation_mode == mode_before
