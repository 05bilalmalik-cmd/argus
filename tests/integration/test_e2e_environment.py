from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_e2e_conftest():  # noqa: ANN202 - test helper returns the loaded module
    path = Path(__file__).parents[1] / "e2e" / "conftest.py"
    spec = importlib.util.spec_from_file_location("argus_e2e_conftest", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_e2e_environment_clears_inherited_live_settings(monkeypatch, tmp_path: Path) -> None:
    module = _load_e2e_conftest()
    monkeypatch.setenv("ARGUS_ENABLE_LIVE_SUBMIT", "true")
    monkeypatch.setenv("ARGUS_ENABLE_TRACKR_LIVE", "true")
    monkeypatch.setenv("ARGUS_AUTOMATION_MODE", "ARMED")
    monkeypatch.setenv("ARGUS_LIVE_DOMAIN_ALLOWLIST", "jobs.example.com")
    monkeypatch.setenv("ARGUS_SIGNING_PASSWORD", "must-not-leak")
    monkeypatch.setenv("ARGUS_CHROMIUM_EXECUTABLE", "C:\\unsafe\\browser.exe")

    environment = module._build_e2e_environment(tmp_path / "data", 8787)

    assert environment["ARGUS_AUTOMATION_MODE"] == "REVIEW_ONLY"
    assert environment["ARGUS_ENABLE_LIVE_SUBMIT"] == "false"
    assert environment["ARGUS_ENABLE_TRACKR_LIVE"] == "false"
    assert environment["ARGUS_LIVE_DOMAIN_ALLOWLIST"] == ""
    assert "ARGUS_SIGNING_PASSWORD" not in environment
    assert "ARGUS_CHROMIUM_EXECUTABLE" not in environment
