"""Disposable launcher isolation; no server or production data is accessed."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import launcher
from app.config import Settings


@pytest.mark.parametrize("frozen", [False, True])
@pytest.mark.parametrize("mode", ["ARMED", "REVIEW_ONLY"])
def test_sandbox_launch_isolated_before_settings(monkeypatch, tmp_path, capsys, frozen, mode):
    root = tmp_path / "fresh-sandbox"
    production = tmp_path / "production-never-created"
    inherited = {
        "ARGUS_DATA_DIR": str(production), "LOCALAPPDATA": str(production),
        "ARGUS_AUTOMATION_MODE": mode, "ARGUS_ENABLE_LIVE_SUBMIT": "true",
        "ARGUS_ENABLE_TRACKR_LIVE": "true", "ARGUS_SWEEP_INTERVAL_HOURS": "1",
        "ARGUS_PORT": "8787", "ARGUS_BROWSER_OPEN": "true",
        "ARGUS_BROWSER_HEADLESS": "false", "ARGUS_PROFILE_PATH": str(production),
        "ARGUS_API_TOKEN": "synthetic-inherited-token", "HTTPS_PROXY": "http://invalid.test",
        "PLAYWRIGHT_BROWSERS_PATH": str(production), "PYTHONPATH": str(production),
        "UNRELATED_PROVIDER_CREDENTIAL": "synthetic", "ARGUS_ENABLE_NOTIFICATIONS": "true",
    }
    monkeypatch.setattr(launcher.os, "environ", inherited)
    monkeypatch.setattr(launcher.sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(launcher, "_trusted_runtime_data_dir", lambda: production)
    monkeypatch.setattr(launcher, "_playwright_browsers_env", lambda env: {})
    monkeypatch.setattr(launcher, "_app_root", lambda: tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail("sandbox contacted a health endpoint or opened a browser")
    monkeypatch.setattr(launcher, "_healthy", forbidden)
    monkeypatch.setattr(launcher.threading, "Thread", forbidden)
    captured = {}
    original_load = Settings.load
    def load(env):
        assert root.is_dir()
        assert env["ARGUS_DATA_DIR"] == str(root.resolve())
        assert not production.exists()
        settings = original_load(env)
        assert settings.automation_mode.value == "OFF"
        assert not settings.live_submit_enabled
        assert not settings.live_submit_environment_enabled
        assert not settings.trackr_live_enabled
        assert not settings.notifications_enabled
        assert settings.sweep_interval_hours == 0
        captured["settings"] = settings
        return settings
    monkeypatch.setattr(Settings, "load", load)
    def popen(command, *, cwd, env):
        captured.update(env=dict(env), port=int(command[command.index("--port") + 1]))
        return SimpleNamespace(wait=lambda: 0)
    def run(*args, **kwargs):
        captured.update(env=dict(os.environ), port=kwargs["port"])
        assert kwargs["host"] == "127.0.0.1"
    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=run))
    assert launcher.main(["--sandbox-root", str(root)]) == 0
    env = captured["env"]
    assert env["ARGUS_BROWSER_OPEN"] == "false"
    assert env["ARGUS_BROWSER_HEADLESS"] == "true"
    assert captured["port"] != 8787
    assert env["ARGUS_PORT"] == str(captured["port"])
    for key in ("ARGUS_PROFILE_PATH", "ARGUS_API_TOKEN", "HTTPS_PROXY", "PYTHONPATH", "UNRELATED_PROVIDER_CREDENTIAL"):
        assert key not in env
    assert not production.exists()
    line = next(line for line in capsys.readouterr().out.splitlines() if line.startswith("ARGUS_SANDBOX "))
    manifest = json.loads(line.removeprefix("ARGUS_SANDBOX "))
    assert manifest["data_dir"] == str(root.resolve())
    assert manifest["port"] == captured["port"]
    assert manifest["automation_mode"] == "OFF"
    assert manifest["live_submit"] is False
    assert "token" not in line.lower()
    assert json.loads((root / "sandbox-manifest.json").read_text()) == manifest
    assert env["ARGUS_FORCE_HEADLESS"] == "true"
    for key in ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "HOME"):
        assert env[key] == str(root.resolve())


@pytest.mark.parametrize("case", ["relative", "existing", "production", "inside-production", "filesystem", "missing-parent", "traversal", "unknown", "missing-value", "duplicate"])
def test_invalid_sandbox_arguments_fail_before_settings(monkeypatch, tmp_path, case):
    production = tmp_path / "production"
    root = tmp_path / "fresh"
    monkeypatch.setattr(launcher, "_trusted_runtime_data_dir", lambda: production)
    def forbidden(*args, **kwargs):
        pytest.fail("invalid args reached Settings or server")
    monkeypatch.setattr(Settings, "load", forbidden)
    paths = {
        "relative": "relative", "existing": str(tmp_path), "production": str(production),
        "inside-production": str(production / "child"), "filesystem": tmp_path.anchor,
        "missing-parent": str(tmp_path / "missing" / "child"),
        "traversal": str(tmp_path / "unused" / ".." / "fresh"),
    }
    args = ["--sandbox-root", paths.get(case, str(root))]
    if case == "unknown":
        args += ["--allow-live-submit"]
    elif case == "missing-value":
        args = ["--sandbox-root"]
    elif case == "duplicate":
        args += ["--sandbox-root", str(root)]
    with pytest.raises(SystemExit) as exc:
        launcher.main(args)
    assert exc.value.code == 2
    assert not root.exists()
    assert not production.exists()
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize("kind", ["symlink", "reparse", "dangling-reparse"])
def test_sandbox_rejects_alias_ancestor(monkeypatch, tmp_path, kind):
    parent = tmp_path / "alias"
    parent.mkdir()
    root = parent / "fresh"
    monkeypatch.setattr(launcher, "_trusted_runtime_data_dir", lambda: tmp_path / "production")
    if kind == "symlink":
        original = Path.is_symlink
        monkeypatch.setattr(Path, "is_symlink", lambda p: p == parent or original(p))
    else:
        original = Path.lstat
        monkeypatch.setattr(Path, "lstat", lambda p: SimpleNamespace(st_file_attributes=0x400, st_mode=0o040700) if p == parent else original(p))
    if kind == "dangling-reparse":
        exists = Path.exists
        monkeypatch.setattr(Path, "exists", lambda p: False if p == parent else exists(p))
    with pytest.raises(ValueError, match="links or reparse"):
        launcher._sandbox_environment(str(root))
    assert not root.exists()


def test_default_launcher_preserves_environment_failure(monkeypatch):
    def fail():
        raise RuntimeError("synthetic known-folder resolution failure")
    monkeypatch.setattr(launcher, "_safe_child_environment", fail)
    with pytest.raises(RuntimeError, match="known-folder"):
        launcher.main()


def test_sandbox_rejects_unc_before_filesystem_access(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("UNC path reached filesystem")
    monkeypatch.setattr(Path, "is_symlink", forbidden)
    with pytest.raises(ValueError, match="local"):
        launcher._sandbox_environment("//synthetic.invalid/share/fresh")
