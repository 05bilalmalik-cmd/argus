#!/usr/bin/env python3
"""Offline, fail-fast ARGUS verification orchestrator.

The PowerShell and POSIX entry points delegate here so the two release lanes
run the same checks and emit the same evidence contract.  Every executed
step gets a timestamped raw log; the JSON manifest contains only commands,
exit codes, hashes, and sanitized source metadata.
"""
from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib import error as urllib_error
from urllib import request as urllib_request

# Direct execution (the release scripts use ``python scripts/verify.py``)
# starts with ``scripts/`` on sys.path; add the repository root before the
# shared package import without relying on installation state.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.package_release import source_snapshot


_ISOLATED_PROFILE_ROOTS: set[Path] = set()


def _cleanup_isolated_profiles() -> None:
    for profile in tuple(_ISOLATED_PROFILE_ROOTS):
        shutil.rmtree(profile, ignore_errors=True)
        _ISOLATED_PROFILE_ROOTS.discard(profile)


atexit.register(_cleanup_isolated_profiles)


_DANGEROUS_ENV_NAMES = frozenset(
    {
        "ARGUS_ENABLE_LIVE_SUBMIT",
        "ARGUS_ENABLE_TRACKR_LIVE",
        "ARGUS_AUTOMATION_MODE",
        "ARGUS_AUTOMATION_STATE",
        "ARGUS_AUTOPILOT_MODE",
        "ARGUS_AUTOMATION",
        "ARGUS_LIVE_DOMAIN_ALLOWLIST",
        "ARGUS_PROFILE_PATH",
        "ARGUS_PROFILE_JSON",
        "ARGUS_CV_ROOT",
        "ARGUS_OLLAMA_URL",
        "ARGUS_OLLAMA_MODEL",
        "ARGUS_API_TOKEN",
    }
)
_DANGEROUS_PREFIXES = ("ARGUS_LIVE_", "ARGUS_TRACKR_", "ARGUS_SUBMIT_")
_DANGEROUS_GENERIC_TOKENS = (
    "TRACKR", "SUBMISSION", "LIVE", "SSLKEYLOGFILE", "COOKIE", "PROXY",
    "PROFILE", "SECRET", "TOKEN", "KEY",
)
_SAFE_INHERITED_ENV = frozenset(
    {
        "PATH", "PATHEXT", "COMSPEC", "SYSTEMROOT", "WINDIR", "TEMP", "TMP",
        "LANG", "LC_ALL", "LC_CTYPE",
    }
)
_ARCHIVE_MAX_BYTES = 256 * 1024 * 1024
_ARCHIVE_MAX_MEMBER_BYTES = 64 * 1024 * 1024
_ARCHIVE_MAX_MEMBERS = 4096
_PROXY_NAMES = frozenset(
    {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    }
)
_MIGRATION_SMOKE = (
    "import sqlite3,tempfile,shutil;"
    "from app.config import Settings;"
    "from app.db import Database;"
    "from pathlib import Path;"
    "d=tempfile.mkdtemp(prefix='argus-verify-migration-');"
    "s=Settings.load({'ARGUS_DATA_DIR':d,'ARGUS_API_TOKEN':'verify-token','ARGUS_AUTOMATION_MODE':'OFF','ARGUS_ENABLE_LIVE_SUBMIT':'false','ARGUS_ENABLE_TRACKR_LIVE':'false'});"
    "s.ensure_directories(); db=Database(s); db.create_schema(); db.create_schema();"
    "c=sqlite3.connect(str(Path(s.data_dir)/'argus.db'));c.execute('PRAGMA foreign_keys=ON')"
)
_MIGRATION_SMOKE += ";assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok';assert c.execute('PRAGMA foreign_keys').fetchone()[0]==1;c.close();db.engine.dispose();shutil.rmtree(d);print('fresh migration OK')"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _python_bin(root: Path, configured: str | None) -> str:
    if configured:
        return configured
    candidate = root / ".venv" / "Scripts" / "python.exe"
    if candidate.is_file():
        return str(candidate)
    candidate = root / ".venv" / "bin" / "python"
    if candidate.is_file():
        return str(candidate)
    return sys.executable


def _validate_python_bin(python_bin: str, *, root: Path, environment: Mapping[str, str]) -> tuple[Path, str]:
    candidate = Path(python_bin).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve(strict=True)
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError("configured Python interpreter is not a regular file")
    if candidate.suffix.casefold() in {".cmd", ".bat", ".com", ".ps1", ".py", ".sh"}:
        raise ValueError("configured Python interpreter must be a native executable, not a script shim")
    magic = candidate.read_bytes()[:4]
    if os.name == "nt":
        native = magic[:2] == b"MZ"
    else:
        native = magic.startswith(b"\x7fELF") or magic[:4] in {b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe"}
    if not native:
        raise ValueError("configured Python interpreter is not a native executable")
    identity_probe = (
        "import json,sys;"
        "print(json.dumps({'implementation':sys.implementation.name,'version':sys.version.split()[0],"
        "'executable':str(__import__('pathlib').Path(sys.executable).resolve())},sort_keys=True))"
    )
    probe = subprocess.run(
        [str(candidate), "-c", identity_probe],
        cwd=root,
        env=dict(environment),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if probe.returncode != 0 or not probe.stdout.strip():
        raise ValueError("configured Python interpreter failed real execution")
    try:
        identity = json.loads(probe.stdout.strip().splitlines()[-1])
    except json.JSONDecodeError as exc:
        raise ValueError("configured Python interpreter returned an invalid identity probe") from exc
    if identity.get("implementation") != "cpython" or Path(str(identity.get("executable", ""))).resolve() != candidate:
        raise ValueError("configured Python interpreter is not the requested CPython executable")
    version = identity.get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("configured Python interpreter did not report a version")
    return candidate, version


def safe_environment(root: Path) -> dict[str, str]:
    """Return an environment that cannot opt verification into live traffic."""

    environment: dict[str, str] = {
        key: value for key, value in os.environ.items() if key.upper() in _SAFE_INHERITED_ENV
    }
    isolated_home = Path(
        tempfile.mkdtemp(prefix=f"argus-verify-home-{os.getpid()}-")
    )
    _ISOLATED_PROFILE_ROOTS.add(isolated_home)
    isolated_local_app_data = isolated_home / "AppData" / "Local"
    isolated_roaming_app_data = isolated_home / "AppData" / "Roaming"
    isolated_local_app_data.mkdir(parents=True, exist_ok=True)
    isolated_roaming_app_data.mkdir(parents=True, exist_ok=True)
    original_home = Path.home()
    if os.name == "nt":
        browser_cache = original_home / "AppData" / "Local" / "ms-playwright"
    else:
        browser_cache = original_home / ".cache" / "ms-playwright"
    environment.update(
        {
            "ARGUS_ENABLE_LIVE_SUBMIT": "false",
            "ARGUS_ENABLE_TRACKR_LIVE": "false",
            "ARGUS_AUTOMATION_MODE": "OFF",
            "ARGUS_AUTOMATION_STATE": "OFF",
            "ARGUS_SWEEP_INTERVAL_HOURS": "0",
            "ARGUS_LIVE_DOMAIN_ALLOWLIST": "",
            "ARGUS_BROWSER_HEADLESS": "true",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "HOME": str(isolated_home),
            "USERPROFILE": str(isolated_home),
            "LOCALAPPDATA": str(isolated_local_app_data),
            "APPDATA": str(isolated_roaming_app_data),
            "PLAYWRIGHT_BROWSERS_PATH": str(browser_cache) if browser_cache.is_dir() else "0",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "PYTHONPATH": str(root)
            + (os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else ""),
        }
    )
    return environment


_CORE_E2E_FILES = frozenset(
    {
        "test_adversarial_flows.py",
        "test_application_flows.py",
        "test_application_navigator_journeys.py",
        "test_apply_ui.py",
        "test_dashboard.py",
        "test_lab_adapter_variants.py",
        "test_submission_unknown.py",
    }
)
_OPTIONAL_E2E_FILES = frozenset(
    {
        "test_default_source_resolution.py",
        "test_phase17_egress_journeys.py",
        "test_preparation_bridge_demo.py",
    }
)


def _e2e_files(root: Path) -> tuple[Path, ...]:
    base = root / "tests" / "e2e"
    root_resolved = root.resolve(strict=True)
    current = root_resolved
    try:
        relative_parts = base.relative_to(root_resolved).parts
    except ValueError as exc:
        raise ValueError("E2E directory is outside the verification root") from exc
    for component in relative_parts:
        current = current / component
        try:
            info = current.lstat()
        except FileNotFoundError:
            break
        if stat_is_reparse(info):
            raise ValueError("E2E discovery rejected link/reparse point in base path")
    if not base.is_dir():
        raise ValueError("E2E directory is missing")
    files: list[Path] = []
    for path in sorted(base.glob("test_*.py"), key=lambda item: item.name.casefold()):
        if path.is_symlink() or bool(getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400):
            raise ValueError(f"E2E discovery rejected link/reparse point: {path.name}")
        if path.is_file():
            files.append(path)
    discovered_names = {path.name for path in files}
    missing = _CORE_E2E_FILES - discovered_names
    unexpected = discovered_names - _CORE_E2E_FILES - _OPTIONAL_E2E_FILES
    if missing or unexpected:
        details = []
        if missing:
            details.append("missing=" + ",".join(sorted(missing)))
        if unexpected:
            details.append("unexpected=" + ",".join(sorted(unexpected)))
        raise ValueError("verification requires the approved E2E file set (" + "; ".join(details) + ")")
    return tuple(files)


def stat_is_reparse(info: os.stat_result) -> bool:
    return bool((getattr(info, "st_file_attributes", 0) & 0x400) or stat.S_ISLNK(info.st_mode))


def _validate_output_path(path: Path, root: Path, *, kind: str) -> Path:
    """Allow output only under the checkout or the OS temp directory."""
    root = root.resolve(strict=True)
    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=False)
    allowed_roots = (root, Path(tempfile.gettempdir()).resolve())
    if not any(resolved == allowed or allowed in resolved.parents for allowed in allowed_roots):
        raise ValueError(f"{kind} output must be contained by the verification root or OS temp directory")
    current = resolved
    while current != current.parent:
        if current.exists() and (current.is_symlink() or bool(getattr(current.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400)):
            raise ValueError(f"{kind} output path contains a link/reparse point")
        current = current.parent
    return resolved


def _plan_digest(plan: Sequence[Mapping[str, Any]]) -> str:
    stable: list[dict[str, Any]] = []
    for item in plan:
        commands = item.get("commands") or [item.get("command")]
        stable.append({"name": item.get("name"), "kind": item.get("kind"), "commands": commands})
    return hashlib.sha256(json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _command_hash(command: Sequence[str]) -> str:
    return hashlib.sha256(json.dumps([str(part) for part in command], ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def _safe_log_name(run_stamp: str, index: int, name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "step"
    suffix = hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]
    return f"{run_stamp}-{index:02d}-{safe}-{suffix}.log"


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


def _command_display(command: Sequence[str]) -> list[str]:
    return [str(item) for item in command]


def _step_plan(root: Path, python_bin: str, packaged_smoke: Path | None) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = [
        {
            "name": "unit",
            "kind": "unit",
            "command": [python_bin, "-m", "pytest", "-q", "tests/unit", "--confcutdir=tests/e2e"],
        },
        {
            "name": "integration",
            "kind": "integration",
            "command": [python_bin, "-m", "pytest", "-q", "tests/integration", "--confcutdir=tests/e2e"],
        },
    ]
    for path in _e2e_files(root):
        relative = _relative(path, root)
        steps.append(
            {
                "name": relative,
                "kind": "e2e",
                "command": [python_bin, "-m", "pytest", "-q", relative],
            }
        )
    steps.extend(
        [
            {
                "name": "compile",
                "kind": "compile",
                "command": [python_bin, "-m", "compileall", "-q", "app", "scripts", "launcher.py"],
            },
            {
                "name": "cli_help",
                "kind": "cli",
                "command": [python_bin, "-m", "app.cli", "--help"],
            },
            {
                "name": "cli_safe_smoke",
                "kind": "cli",
                "fresh_data": True,
                "commands": [
                    [python_bin, "-m", "app.cli", "init"],
                    [python_bin, "-m", "app.cli", "audit"],
                ],
            },
            {
                "name": "audit_fresh_temp",
                "kind": "audit",
                "fresh_data": True,
                "commands": [
                    [python_bin, "-m", "app.cli", "init"],
                    [python_bin, "-m", "app.cli", "audit"],
                ],
            },
            {
                "name": "migration_fresh_temp",
                "kind": "migration",
                "command": [python_bin, "-c", _MIGRATION_SMOKE],
            },
            {
                "name": "privacy_source",
                "kind": "privacy",
                "command": [
                    python_bin,
                    "scripts/privacy_scan.py",
                    "--root",
                    ".",
                    "--config",
                    "packaging/privacy_scan_config.json",
                ],
            },
        ]
    )
    if packaged_smoke is not None:
        steps.append(
            {
                "name": "packaged_smoke",
                "kind": "packaged",
                "command": [python_bin, "-m", "app.cli", "--help"],
                "packaged_path": str(packaged_smoke),
            }
        )
    return steps


def _run_process(
    command: Sequence[str],
    *,
    root: Path,
    environment: Mapping[str, str],
    output,
) -> int:
    try:
        completed = subprocess.run(
            list(command),
            cwd=root,
            env=dict(environment),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        output.write(f"process launch failed: {exc}\n")
        return 127
    if completed.stdout:
        output.write(completed.stdout)
    if completed.stderr:
        output.write(completed.stderr)
    return int(completed.returncode)


def _validate_archive_members(archive_path: Path) -> None:
    """Validate ZIP structure before extracting any member."""
    if archive_path.stat().st_size > _ARCHIVE_MAX_BYTES:
        raise ValueError("packaged archive exceeds the compressed size cap")
    seen: set[str] = set()
    file_keys: set[str] = set()
    total = 0
    with zipfile.ZipFile(archive_path) as archive:
        infos = archive.infolist()
        if len(infos) > _ARCHIVE_MAX_MEMBERS:
            raise ValueError("packaged archive contains too many members")
        for info in infos:
            raw = info.filename.replace("\\", "/")
            normal = unicodedata.normalize("NFKC", raw)
            parts = normal.rstrip("/").split("/")
            if (not normal or normal != raw or any(ord(ch) > 0x7F for ch in normal)
                    or normal.startswith("/") or re.match(r"^[A-Za-z]:", normal)
                    or any(part in {"", ".", ".."} for part in parts)):
                raise ValueError("packaged archive contains an unsafe member path")
            key = normal.casefold().rstrip("/")
            if key in seen:
                raise ValueError("packaged archive contains duplicate members")
            seen.add(key)
            if not normal.endswith("/"):
                if any(key.startswith(parent + "/") for parent in file_keys):
                    raise ValueError("packaged archive contains a file/directory member collision")
                file_keys.add(key)
            elif any(existing.startswith(key + "/") for existing in file_keys):
                raise ValueError("packaged archive contains a file/directory member collision")
            if info.file_size > _ARCHIVE_MAX_MEMBER_BYTES:
                raise ValueError("packaged archive member exceeds the uncompressed size cap")
            total += info.file_size
            if total > _ARCHIVE_MAX_BYTES:
                raise ValueError("packaged archive exceeds the uncompressed size cap")


class _DuplicateHealthJsonKey(ValueError):
    """Raised when a health object contains a duplicate JSON member name."""


def _reject_duplicate_health_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise _DuplicateHealthJsonKey(f"duplicate health JSON key: {key}")
        payload[key] = value
    return payload


def _is_json_content_type(headers: Any) -> bool:
    """Accept only application/json with at most a UTF-8 charset parameter."""

    values = headers.get_all("Content-Type") if headers is not None else None
    if not isinstance(values, list) or len(values) != 1:
        return False
    parts = [part.strip() for part in values[0].split(";")]
    if not parts or parts[0].casefold() != "application/json":
        return False
    charset_seen = False
    for parameter in parts[1:]:
        if not parameter or "=" not in parameter:
            return False
        name, value = parameter.split("=", 1)
        if name.strip().casefold() != "charset" or charset_seen:
            return False
        charset_seen = True
        normalized = value.strip().strip('"').casefold()
        if normalized not in {"utf-8", "utf8"}:
            return False
    return True


class _NoRedirectHandler(urllib_request.HTTPRedirectHandler):
    """Make packaged health checks fail closed instead of following redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib_error.HTTPError(req.full_url, code, "redirects disabled", headers, None)


_PROCESS_TEARDOWN_SECONDS = 8.0


def _terminate_process_tree(process: subprocess.Popen, *, output) -> None:
    """Stop a smoke process and every child it may have spawned.

    A packaged launcher normally has a second process below it (for example a
    frozen wrapper starting an ASGI server).  Terminating only the wrapper can
    leave that child holding the loopback port and the wrapper's stdout pipe.
    Windows has a native, bounded tree operation; POSIX smokes are launched in
    their own process group and receive the equivalent group signal.
    """

    pid = getattr(process, "pid", None)
    if isinstance(pid, int) and pid > 0 and os.name == "nt":
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                check=False,
                timeout=_PROCESS_TEARDOWN_SECONDS,
            )
            if result.returncode not in {0, 128} and output is not None:
                detail = (result.stderr or result.stdout or "").strip()
                output.write(f"packaged smoke process-tree cleanup returned {result.returncode}: {detail}\n")
        except (OSError, subprocess.TimeoutExpired) as exc:
            if output is not None:
                output.write(f"packaged smoke process-tree cleanup failed: {exc}\n")
    elif isinstance(pid, int) and pid > 0:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            try:
                process.terminate()
            except OSError:
                pass
    else:
        try:
            process.terminate()
        except OSError:
            pass

    try:
        process.wait(timeout=_PROCESS_TEARDOWN_SECONDS)
    except subprocess.TimeoutExpired:
        if isinstance(pid, int) and pid > 0 and os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=2,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        elif isinstance(pid, int) and pid > 0:
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            if output is not None:
                output.write("packaged smoke process-tree cleanup exceeded its bound\n")


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _wait_for_port_free(port: int, *, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_is_free(port):
            return True
        time.sleep(0.05)
    return _port_is_free(port)


def _run_packaged_smoke(
    package_path: Path,
    *,
    root: Path,
    python_bin: str,
    environment: Mapping[str, str],
    output,
    expected_version: str | None = None,
) -> int:
    package_path = package_path.expanduser().resolve()
    if not package_path.exists():
        output.write(f"packaged smoke input does not exist: {package_path.name}\n")
        return 2
    with tempfile.TemporaryDirectory(prefix="argus-packaged-smoke-") as temporary:
        destination = Path(temporary)
        if package_path.is_file() and package_path.suffix.casefold() == ".zip":
            try:
                _validate_archive_members(package_path)
            except (OSError, ValueError) as exc:
                output.write(f"packaged archive rejected: {exc}\n")
                return 2
            with zipfile.ZipFile(package_path) as archive:
                members = archive.namelist()
                for name in members:
                    normal = name.replace("\\", "/")
                    if (not normal or unicodedata.normalize("NFKC", normal) != normal or any(ord(char) > 0x7F for char in normal) or normal.startswith("/") or re.match(r"^[A-Za-z]:", normal) or any(part in {"", ".", ".."} for part in normal.split("/"))):
                        output.write("packaged archive contains an unsafe member path\n")
                        return 2
                    target = (destination / normal).resolve()
                    if destination.resolve() not in target.parents:
                        output.write("packaged archive member escapes extraction root\n")
                        return 2
                    if name.endswith("/"):
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.read(name))
            candidates = [path for path in destination.iterdir() if path.is_dir()]
            if len(candidates) != 1:
                output.write("packaged archive must contain one top-level directory\n")
                return 2
            smoke_root = candidates[0]
        elif package_path.is_dir():
            smoke_root = package_path
        elif package_path.is_file() and package_path.suffix.casefold() in {".exe", ".cmd", ".bat", ".py"}:
            smoke_root = destination
        else:
            output.write("packaged smoke input must be a directory, ZIP, or executable\n")
            return 2
        smoke_env = dict(environment)
        smoke_env.update({
            "ARGUS_ENABLE_LIVE_SUBMIT": "false",
            "ARGUS_ENABLE_TRACKR_LIVE": "false",
            "ARGUS_AUTOMATION_MODE": "OFF",
            "ARGUS_AUTOMATION_STATE": "OFF",
            "ARGUS_SWEEP_INTERVAL_HOURS": "0",
            "ARGUS_DATA_DIR": str(destination / "data"),
            "ARGUS_BROWSER_OPEN": "false",
        })
        smoke_env["PYTHONPATH"] = str(smoke_root)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        smoke_env["ARGUS_PORT"] = str(port)
        if package_path.is_file() and package_path.suffix.casefold() in {".exe", ".cmd", ".bat", ".py"}:
            if package_path.suffix.casefold() == ".py":
                command = [python_bin, str(package_path)]
            elif package_path.suffix.casefold() in {".cmd", ".bat"}:
                command = ["cmd", "/c", str(package_path)]
            else:
                command = [str(package_path)]
        else:
            launcher = smoke_root / "launcher.py"
            if not launcher.is_file():
                return _run_process([python_bin, "-m", "app.cli", "--help"], root=smoke_root, environment=smoke_env, output=output)
            command = [python_bin, str(launcher)]
        if expected_version is None:
            version_file = smoke_root / "app" / "version.py"
            if version_file.is_file():
                try:
                    version_text = version_file.read_text(encoding="utf-8")
                    match = re.search(r"__version__\s*=\s*['\"]([^'\"]+)['\"]", version_text)
                    if match:
                        expected_version = match.group(1)
                except OSError:
                    expected_version = None
        try:
            process_kwargs = {
                "cwd": smoke_root,
                "env": smoke_env,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
            }
            if os.name != "nt":
                process_kwargs["start_new_session"] = True
            process = subprocess.Popen(command, **process_kwargs)
        except OSError as exc:
            output.write(f"packaged smoke launch failed: {exc}\n")
            return 127
        healthy = False
        health_error: str | None = None
        deadline = time.monotonic() + 30
        health_url = f"http://127.0.0.1:{port}/healthz"
        # Do not consult proxy environment variables and never follow a
        # redirect away from the loopback health endpoint.
        health_opener = urllib_request.build_opener(
            urllib_request.ProxyHandler({}),
            _NoRedirectHandler(),
        )
        while time.monotonic() < deadline and process.poll() is None:
            try:
                with health_opener.open(health_url, timeout=1) as response:
                    if response.status != 200:
                        health_error = f"HTTP {response.status}"
                    elif response.geturl() != health_url:
                        health_error = "health response URL changed unexpectedly"
                    elif not _is_json_content_type(response.headers):
                        health_error = "health content type is not application/json"
                    else:
                        payload = json.loads(
                            response.read().decode("utf-8"),
                            object_pairs_hook=_reject_duplicate_health_json_keys,
                        )
                        expected_keys = {
                            "status", "service", "version", "automation_mode",
                            "live_submit", "trackr_live",
                        }
                        if not isinstance(payload, dict):
                            health_error = "health response is not a JSON object"
                        elif set(payload) != expected_keys:
                            health_error = "health response must contain exactly six keys"
                        elif payload.get("status") != "ok":
                            health_error = "health status is not ok"
                        elif payload.get("service") != "ARGUS":
                            health_error = "health service is not ARGUS"
                        elif payload.get("automation_mode") != "OFF":
                            health_error = "health automation_mode is not OFF"
                        elif payload.get("live_submit") is not False:
                            health_error = "health live_submit is not false"
                        elif payload.get("trackr_live") is not False:
                            health_error = "health trackr_live is not false"
                        elif not isinstance(payload.get("version"), str) or not payload.get("version"):
                            health_error = "health version is missing or invalid"
                        elif expected_version is not None and payload.get("version") != expected_version:
                            health_error = "health version does not match the packaged version"
                        else:
                            healthy = True
                            health_error = None
            except _DuplicateHealthJsonKey as exc:
                health_error = str(exc)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
                # The process may still be starting. Keep polling until the
                # bounded deadline, but never treat a non-JSON health body as
                # a successful smoke result.
                if health_error is None:
                    health_error = "health endpoint unavailable or invalid"
            if healthy:
                break
            time.sleep(0.1)
        _terminate_process_tree(process, output=output)
        port_free = _wait_for_port_free(port)
        if process.stdout is not None:
            try:
                captured, _ = process.communicate(timeout=1)
                output.write((captured or "")[:4096])
            except subprocess.TimeoutExpired:
                output.write(
                    "packaged smoke output pipe remained open after bounded tree teardown\n"
                )
            except OSError:
                pass
        if not port_free:
            output.write("packaged smoke loopback port was not freed after process-tree teardown\n")
            return 1
        if not healthy:
            if health_error:
                output.write(f"packaged smoke health validation failed: {health_error}\n")
            output.write("packaged smoke did not produce HTTP 200 /healthz on loopback\n")
            return 1
        output.write("packaged smoke passed isolated loopback health check\n")
        return 0


def _write_manifest(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the offline ARGUS verification gate")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--python", dest="python_bin")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--packaged-smoke", type=Path)
    parser.add_argument(
        "--package-output",
        type=Path,
        help="After a passed verification, package from the same process using an in-memory capability",
    )
    parser.add_argument("--package-version")
    parser.add_argument("--package-executable", type=Path)
    parser.add_argument("--package-executable-version")
    parser.add_argument("--package-executable-signature", type=Path)
    parser.add_argument("--package-executable-signature-public-key", type=Path)
    parser.add_argument("--package-allow-unsigned-executable", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    root = args.root.expanduser().resolve(strict=True)
    configured_python = _python_bin(root, args.python_bin or os.environ.get("PYTHON_BIN"))
    environment = safe_environment(root)
    python_path, python_version = _validate_python_bin(configured_python, root=root, environment=environment)
    python_bin = str(python_path)
    started = _utc_now()
    run_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    evidence_dir = (args.evidence_dir or root / "release-evidence").expanduser()
    evidence_dir = _validate_output_path(evidence_dir, root, kind="evidence")
    manifest_path = args.manifest or evidence_dir / f"verification-{run_stamp}.json"
    manifest_path = _validate_output_path(manifest_path, root, kind="manifest")
    plan = _step_plan(root, python_bin, args.packaged_smoke)
    plan_sha256 = _plan_digest(plan)
    run_nonce = secrets.token_urlsafe(24)
    expected_packaged_version: str | None = None
    if args.packaged_smoke is not None:
        version_file = root / "app" / "version.py"
        if version_file.is_file():
            try:
                match = re.search(r"__version__\s*=\s*['\"]([^'\"]+)['\"]", version_file.read_text(encoding="utf-8"))
                expected_packaged_version = match.group(1) if match else None
            except OSError:
                expected_packaged_version = None
    steps: list[dict[str, Any]] = []
    logs: list[dict[str, Any]] = []
    status = "dry-run" if args.dry_run else "passed"
    failure: str | None = None
    failure_code: int | None = None

    if args.dry_run:
        for planned in plan:
            step = dict(planned)
            step["status"] = "planned"
            steps.append(step)
    else:
        evidence_dir.mkdir(parents=True, exist_ok=True)
        if evidence_dir.is_symlink() or bool(getattr(evidence_dir.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400):
            raise ValueError("release-evidence must not be a link or reparse point")
        for index, planned in enumerate(plan, start=1):
            step_started = time.monotonic()
            log_path = evidence_dir / _safe_log_name(run_stamp, index, str(planned["name"]))
            if log_path.exists() and (log_path.is_symlink() or bool(getattr(log_path.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400)):
                raise ValueError("verification log path is a link or reparse point")
            result_code = 0
            step_environment = dict(environment)
            temporary_data: tempfile.TemporaryDirectory[str] | None = None
            if planned.get("fresh_data"):
                temporary_data = tempfile.TemporaryDirectory(prefix="argus-verify-data-")
                step_environment["ARGUS_DATA_DIR"] = temporary_data.name
                step_environment["ARGUS_API_TOKEN"] = "verify-token"
            with log_path.open("w", encoding="utf-8", newline="\n") as output:
                output.write(f"started_at={_utc_now()}\n")
                output.write(f"step={planned['name']}\n")
                commands = planned.get("commands") or [planned["command"]]
                output.write(f"provenance_nonce={run_nonce}\n")
                output.write(f"plan_sha256={plan_sha256}\n")
                output.write(f"command_count={len(commands)}\n")
                for command in commands:
                    output.write(f"command_sha256={_command_hash(command)}\n")
                if planned.get("kind") == "packaged":
                    result_code = _run_packaged_smoke(
                        Path(planned["packaged_path"]),
                        root=root,
                        python_bin=python_bin,
                        environment=step_environment,
                        output=output,
                        expected_version=expected_packaged_version,
                    )
                else:
                    for command in commands:
                        output.write("$ " + " ".join(_command_display(command)) + "\n")
                        result_code = _run_process(
                            command,
                            root=root,
                            environment=step_environment,
                            output=output,
                        )
                        if result_code:
                            break
                output.write(f"finished_at={_utc_now()}\n")
                output.write(f"exit_code={result_code}\n")
            if temporary_data is not None:
                temporary_data.cleanup()
            log_entry = {
                "path": _relative(log_path, root),
                "sha256": _sha256(log_path),
                "size": log_path.stat().st_size,
            }
            logs.append(log_entry)
            step = dict(planned)
            step.pop("commands", None)
            step["status"] = "passed" if result_code == 0 else "failed"
            step["exit_code"] = result_code
            step["duration_seconds"] = round(time.monotonic() - step_started, 3)
            step["provenance_nonce"] = run_nonce
            step["plan_sha256"] = plan_sha256
            step["command_hashes"] = [_command_hash(command) for command in (planned.get("commands") or [planned.get("command")])]
            step["log"] = dict(log_entry)
            steps.append(step)
            if result_code:
                status = "failed"
                failure = planned["name"]
                failure_code = result_code
                break

    try:
        source = source_snapshot(root)
    except Exception as exc:  # pragma: no cover - defensive evidence path
        source = {"error": type(exc).__name__}
        if status == "passed":
            status = "failed"
            failure = "source_snapshot"
    payload: dict[str, Any] = {
        "schema_version": 2,
        "tool": "ARGUS offline verification",
        "status": status,
        "started_at": started,
        "finished_at": _utc_now(),
        "root": root.name,
        "python": {
            "path": str(python_path),
            "sha256": _sha256(python_path),
            "version": python_version,
            "implementation": "cpython",
            "executable": str(python_path),
            "real": True,
        },
        "live_network": False,
        "environment": {
            key: environment[key]
            for key in (
                "ARGUS_ENABLE_LIVE_SUBMIT",
                "ARGUS_ENABLE_TRACKR_LIVE",
                "ARGUS_AUTOMATION_MODE",
                "ARGUS_AUTOMATION_STATE",
                "ARGUS_SWEEP_INTERVAL_HOURS",
                "ARGUS_LIVE_DOMAIN_ALLOWLIST",
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
            )
        },
        "source": source,
        "steps": steps,
        "logs": logs,
        "provenance": {
            "schema_version": 1,
            "run_nonce": run_nonce,
            "plan_sha256": plan_sha256,
            "executed": not args.dry_run,
        },
    }
    if failure:
        payload["failed_step"] = failure
        payload["failed_exit_code"] = failure_code

    # Packaging is intentionally a same-process continuation of a passed
    # verifier run. The private capability contains an unpredictable secret
    # and is never serialized into this manifest. A later invocation that
    # only has this JSON cannot authorize a release.
    if args.package_output is not None:
        if args.dry_run:
            status = "failed"
            failure = "package_release"
            failure_code = 2
            payload["package_error"] = "package output is not permitted during a dry run"
        elif status != "passed":
            status = "failed"
            failure = "package_release"
            failure_code = int(failure_code or 1)
            payload["package_error"] = "packaging requires every verification step to pass"
        else:
            try:
                from scripts.package_release import (
                    _issue_release_capability,
                    _read_version,
                    build_release,
                )

                def _rooted(value: Path | None) -> Path | None:
                    if value is None:
                        return None
                    return value if value.is_absolute() else root / value

                package_output = _rooted(args.package_output)
                package_executable = _rooted(args.package_executable)
                package_signature = _rooted(args.package_executable_signature)
                package_public_key = _rooted(args.package_executable_signature_public_key)
                package_version = args.package_version or _read_version(root)
                capability = _issue_release_capability(payload, root=root)
                result = build_release(
                    root=root,
                    output_dir=package_output,
                    version=package_version,
                    test_evidence=payload,
                    verification_capability=capability,
                    executable=package_executable,
                    executable_version=args.package_executable_version,
                    executable_signature=package_signature,
                    executable_signature_public_key=package_public_key,
                    allow_unsigned_executable=args.package_allow_unsigned_executable,
                )
                payload["package"] = {
                    "archive": result.archive.name,
                    "checksum": result.checksum_file.name,
                    "manifest": result.manifest_file.name,
                    "sha256": result.sha256,
                    "file_count": result.file_count,
                    "archive_verified_twice": result.archive_verified_twice,
                }
            except Exception as exc:  # pragma: no cover - release boundary
                status = "failed"
                failure = "package_release"
                failure_code = 1
                payload["package_error"] = str(exc)[:500]
        payload["status"] = status
        payload["finished_at"] = _utc_now()
        if failure:
            payload["failed_step"] = failure
            payload["failed_exit_code"] = failure_code
    profile_root = Path(environment["USERPROFILE"])
    try:
        shutil.rmtree(profile_root)
        _ISOLATED_PROFILE_ROOTS.discard(profile_root)
    except OSError as exc:
        if status in {"passed", "dry-run"}:
            status = "failed"
            failure = "isolated_profile_cleanup"
            failure_code = 1
            payload["status"] = status
            payload["failed_step"] = failure
            payload["failed_exit_code"] = failure_code
            payload["profile_cleanup_error"] = type(exc).__name__
            payload["finished_at"] = _utc_now()
    _write_manifest(manifest_path, payload)
    print(f"Verification {status}: {manifest_path}")
    if status in {"passed", "dry-run"}:
        return 0
    return int(failure_code or 1)


if __name__ == "__main__":
    raise SystemExit(main())
