"""Windows folder trust boundary: import-only children, no runtime or real data."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.verify import safe_environment

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows folder resolution")
ROOT = Path(__file__).resolve().parents[2]
SHELL_FOLDERS = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders"


def test_fresh_process_ignores_hostile_profile_environment():
    import winreg

    # Read only the nonsecret, already-expanded OS folder value; no ARGUS data.
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, SHELL_FOLDERS) as key:
        value, kind = winreg.QueryValueEx(key, "Local AppData")
    assert kind == winreg.REG_SZ
    expected = Path(value).resolve()
    env = safe_environment(ROOT)
    assert Path(env["USERPROFILE"]) != expected.parent.parent
    probe = (
        "import json,launcher; "
        "print(json.dumps({'local':str(launcher._trusted_local_app_data()),"
        "'runtime':str(launcher._trusted_runtime_data_dir()),"
        "'browsers':launcher._playwright_browsers_env({})}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], cwd=ROOT, env=env,
        capture_output=True, text=True, check=False, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    print(json.dumps({"expected_local": str(expected), "actual": payload}, sort_keys=True))
    assert Path(payload["local"]) == expected
    assert Path(payload["runtime"]) == expected / "ARGUS"
    cache = expected / "ms-playwright"
    expected_browsers = (
        {"PLAYWRIGHT_BROWSERS_PATH": str(cache)}
        if (cache / "chromium_headless_shell-1234").is_dir() or any(cache.glob("chromium-*"))
        else {}
    )
    assert payload["browsers"] == expected_browsers


def _fake_registry(monkeypatch, value, kind=1, failure=None):
    from contextlib import nullcontext
    from types import SimpleNamespace

    key = object()

    def open_key(hive, path):
        assert hive == "HKCU"
        assert path == SHELL_FOLDERS
        if failure == "open":
            raise OSError("synthetic access denied")
        return nullcontext(key)

    def query(handle, name):
        assert handle is key
        assert name == "Local AppData"
        if failure == "query":
            raise FileNotFoundError("synthetic missing value")
        return value, kind

    monkeypatch.setitem(sys.modules, "winreg", SimpleNamespace(
        HKEY_CURRENT_USER="HKCU", REG_SZ=1, OpenKey=open_key, QueryValueEx=query,
    ))


@pytest.mark.parametrize("value,kind,failure", [
    (r"C:\synthetic\Local", 2, None),  # REG_EXPAND_SZ, even without placeholders
    (r"C:\synthetic\Local", 4, None),
    (b"C:/synthetic/Local", 1, None),
    (None, 1, None),
    ("", 1, None),
    ("   ", 1, None),
    ("relative/Local", 1, None),
    (r"C:relative\Local", 1, None),
    (r"\rooted-without-drive", 1, None),
    (r"%USERPROFILE%\AppData\Local", 1, None),
    (r"C:\%USERNAME%\Local", 1, None),
    ("C:/synthetic/\x00Local", 1, None),
    (r"C:\synthetic\Local", 1, "open"),
    (r"C:\synthetic\Local", 1, "query"),
])
def test_untrusted_registry_result_fails_closed(monkeypatch, tmp_path, value, kind, failure):
    import launcher

    _fake_registry(monkeypatch, value, kind, failure)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert launcher._trusted_local_app_data() is None
    with pytest.raises(RuntimeError, match="safely"):
        launcher._trusted_runtime_data_dir()


def test_registry_absolute_sz_preserves_default_root(monkeypatch, tmp_path):
    import launcher

    local = tmp_path / "OS-configured-local"
    _fake_registry(monkeypatch, str(local))
    assert launcher._trusted_local_app_data() == local.resolve()
    assert launcher._safe_child_environment({"ARGUS_DATA_DIR": "hostile"})["ARGUS_DATA_DIR"] == str(local.resolve() / "ARGUS")
    assert not local.exists()


def test_windows_browser_cache_does_not_fall_back_to_parent(monkeypatch, tmp_path):
    import launcher

    _fake_registry(monkeypatch, None, failure="open")
    (tmp_path / "ms-playwright" / "chromium-hostile").mkdir(parents=True)
    assert launcher._playwright_browsers_env({"LOCALAPPDATA": str(tmp_path)}) == {}


def test_sandbox_overlap_gate_uses_os_root_despite_hostile_profile(monkeypatch, tmp_path):
    import launcher

    _fake_registry(monkeypatch, str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "hostile-profile"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "hostile-local"))
    root = tmp_path / "ARGUS"
    with pytest.raises(ValueError, match="overlap"):
        launcher._sandbox_environment(str(root))
    assert not root.exists()
