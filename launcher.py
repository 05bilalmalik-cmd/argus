"""Safe one-click launcher for the local ARGUS command centre.

The launcher uses one configured loopback port and a process lock.  It never
silently moves to another port, enables live submission, or configures a
Trackr source.  Explicit environment settings remain available for an
operator who intentionally opts in outside this launcher.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path
from typing import Mapping

from app.runtime_lock import RuntimeAlreadyRunning, RuntimeLock, runtime_lock_path

HOST = "127.0.0.1"
PREFERRED_PORT = 8787

_HEALTH_KEYS = frozenset(
    {
        "status",
        "service",
        "version",
        "automation_mode",
        "live_submit",
        "trackr_live",
    }
)
_ALLOWED_INHERITED_ARGUS = frozenset(
    {
        "ARGUS_BROWSER_OPEN",
        "ARGUS_BROWSER_HEADLESS",
        "ARGUS_PORT",
        # Presentation only: selects which template set renders.  It cannot arm
        # automation, enable submission, choose credentials, pick a browser
        # profile, or widen egress, so inheriting it is not an operator
        # confirmation of anything dangerous.  Kept inheritable so the v1 UI
        # stays reachable for comparison.
        "ARGUS_UI_V2",
    }
)
_OS_ENV_NAMES = frozenset(
    {
        "ALLUSERSPROFILE",
        "APPDATA",
        "COMSPEC",
        "HOMEDRIVE",
        "HOMEPATH",
        "LOCALAPPDATA",
        "NUMBER_OF_PROCESSORS",
        "OS",
        "PATH",
        "PATHEXT",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERDOMAIN",
        "USERNAME",
        "USERPROFILE",
        "WINDIR",
    }
)

# These values are written into every child environment.  They are
# deliberately assignments rather than defaults: a stale shell, shortcut, or
# desktop process must not be able to arm the packaged runtime by inheritance.
DEFAULT_ENV = {
    "ARGUS_HOST": HOST,
    "ARGUS_AUTOMATION_MODE": "OFF",
    "ARGUS_AUTOMATION_STATE": "OFF",
    "ARGUS_AUTOPILOT_MODE": "OFF",
    "ARGUS_AUTOMATION": "OFF",
    "ARGUS_ENABLE_LIVE_SUBMIT": "false",
    "ARGUS_ENABLE_TRACKR_LIVE": "false",
    "ARGUS_AUTOPILOT_SUBMIT": "false",
    "ARGUS_LIVE_DOMAIN_ALLOWLIST": "",
    "ARGUS_BROWSER_HEADLESS": "true",
    "ARGUS_BROWSER_OPEN": "false",
    "ARGUS_SWEEP_INTERVAL_HOURS": "0",
    # The v2 dashboard is the interface this build is maintained against; the
    # packaged launcher previously stripped the flag and always showed v1.
    # Unlike the controls above this is a default, not a lock: an explicit
    # ARGUS_UI_V2 in the environment is re-applied after this dict and wins.
    "ARGUS_UI_V2": "true",
}
_BROWSER_OPEN_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})

# These inputs can select private data, credentials, a browser/profile, or an
# outbound provider.  They are removed from launcher-created environments;
# the runtime can create/read its own token under the standard user-local data
# directory instead.  Other explicit, non-dangerous process settings remain
# available to operators (for example PATH and the opt-in browser opener).
_PRIVATE_ENV_NAMES = frozenset(
    {
        "ARGUS_DATA_DIR",
        "ARGUS_API_TOKEN",
        "ARGUS_PROFILE_PATH",
        "ARGUS_PROFILE_JSON",
        "ARGUS_CV_ROOT",
        "ARGUS_OLLAMA_URL",
        "ARGUS_OLLAMA_MODEL",
        "ARGUS_CHROMIUM_EXECUTABLE",
        "PLAYWRIGHT_BROWSERS_PATH",
        "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD",
    }
)
_PRIVATE_ENV_PREFIXES = ("ARGUS_LIVE_", "ARGUS_TRACKR_", "ARGUS_SUBMIT_")
_PRIVATE_ENV_TOKENS = (
    "COOKIE",
    "CREDENTIAL",
    "KEY",
    "LIVE",
    "PASSWORD",
    "PROFILE",
    "PROXY",
    "SECRET",
    "SSLKEYLOGFILE",
    "SUBMISSION",
    "SUBMIT",
    "TRACKR",
    "TOKEN",
)


def _trusted_local_app_data() -> Path | None:
    """Read the OS-maintained, already-expanded Windows LocalAppData path.

    SHGetFolderPathW can expand USERPROFILE from the process environment in
    a fresh child.  Use only the Shell Folders REG_SZ value instead, never
    User Shell Folders / REG_EXPAND_SZ or inherited profile paths.  A missing
    or malformed OS value fails closed; non-Windows uses its normal convention.
    """

    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
        ) as key:
            value, kind = winreg.QueryValueEx(key, "Local AppData")
        if kind != winreg.REG_SZ or not isinstance(value, str):
            return None
        if not value.strip() or "%" in value or "\x00" in value:
            return None
        folder = Path(value)
        if not folder.is_absolute():
            return None
        return folder.resolve()
    except (ImportError, AttributeError, OSError, TypeError, ValueError):
        return None


def _trusted_runtime_data_dir() -> Path:
    """Return the persistent ARGUS data root selected by the OS, not env."""

    local_app_data = _trusted_local_app_data()
    if local_app_data is not None:
        return local_app_data / "ARGUS"
    if os.name == "nt":
        # If the OS folder value is unavailable, fail closed rather than
        # falling back to an attacker-controlled USERPROFILE/APPDATA value.
        raise RuntimeError(
            "ARGUS could not resolve the Windows LocalAppData folder safely."
        )
    return Path.home() / ".local" / "share" / "argus"


def _explicit_data_root(data_root: Path | str) -> Path:
    """Validate a caller-supplied disposable/test root before passing it on."""

    candidate = Path(data_root).expanduser()
    if not candidate.is_absolute():
        raise ValueError("ARGUS explicit data root must be an absolute path")
    resolved = candidate.resolve(strict=False)
    if resolved == Path(resolved.anchor):
        raise ValueError("ARGUS explicit data root must not be a filesystem root")
    return resolved


def _browser_open_enabled(environment: Mapping[str, str] | None = None) -> bool:
    """Return whether the launcher may open a dashboard browser window.

    Browser opening is deliberately opt-in.  Unknown values fail closed so a
    packaged/headless verification run cannot accidentally launch a desktop
    browser because of a typo or inherited environment variable.
    """

    source = os.environ if environment is None else environment
    raw = source.get("ARGUS_BROWSER_OPEN", "false")
    return str(raw).strip().casefold() in _BROWSER_OPEN_TRUE_VALUES


def _playwright_browsers_env(environment: Mapping[str, str] | None = None) -> dict[str, str]:
    """Frozen builds bundle the Playwright driver but not Chromium; point it
    at the standard ms-playwright cache when present."""

    source = os.environ if environment is None else environment
    local_path = _trusted_local_app_data()
    if local_path is None and os.name != "nt":
        raw_local = source.get("LOCALAPPDATA")
        local_path = Path(raw_local) if raw_local else None
    if local_path is None:
        return {}
    browsers = local_path / "ms-playwright"
    if (browsers / "chromium_headless_shell-1234").is_dir() or any(
        browsers.glob("chromium-*")
    ):
        return {"PLAYWRIGHT_BROWSERS_PATH": str(browsers)}
    return {}


def _app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _port_free(port: int) -> bool:
    with socket.socket() as sock:
        try:
            sock.bind((HOST, port))
            return True
        except OSError:
            return False


def _pick_port(configured: int | None = None) -> int:
    """Validate exactly one port; never silently select a fallback port."""

    candidate = PREFERRED_PORT if configured is None else int(configured)
    if not 1 <= candidate <= 65535:
        raise SystemExit(f"Invalid ARGUS port {candidate}.")
    if not _port_free(candidate):
        raise SystemExit(
            f"ARGUS port {candidate} is already in use; refusing to select another port."
        )
    return candidate


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep health checks on the exact loopback endpoint."""

    def redirect_request(self, *args: object, **kwargs: object):
        return None


def _health_urlopen(url: str):
    # Do not inherit a proxy handler for a security decision.  In particular,
    # a hostile HTTP 302 must never make this local check visit another host.
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirectHandler(),
    )
    return opener.open(url, timeout=2)


def _healthy(port: int) -> bool:
    expected_url = f"http://{HOST}:{port}/healthz"

    try:
        with _health_urlopen(expected_url) as resp:
            if resp.status != 200:
                return False
            response_url = getattr(resp, "geturl", lambda: expected_url)()
            if response_url != expected_url:
                return False
            body = resp.read()
            if isinstance(body, bytes):
                body = body.decode("utf-8")
            if not isinstance(body, str):
                return False
            payload = json.loads(body)
            if not isinstance(payload, dict) or set(payload) != _HEALTH_KEYS:
                return False
            version = payload.get("version")
            return (
                payload.get("status") == "ok"
                and payload.get("service") == "ARGUS"
                and version == _runtime_version()
                and payload.get("automation_mode") == "OFF"
                and payload.get("live_submit") is False
                and payload.get("trackr_live") is False
            )
    except (AttributeError, OSError, TypeError, UnicodeError, ValueError):
        return False


def _safe_child_environment(
    source: Mapping[str, str] | None = None,
    *,
    data_root: Path | str | None = None,
) -> dict[str, str]:
    """Build a launcher environment with safety controls bound fail-closed.

    The launcher is a trust boundary: environment variables inherited from a
    shell, old shortcut, or IDE are not operator confirmation.  We preserve
    ordinary process settings but overwrite all automation controls and drop
    private-data/provider overrides.  The launcher writes an explicit
    ``ARGUS_DATA_DIR`` chosen from the Windows OS folder value (or the
    caller-supplied disposable root), so inherited ``LOCALAPPDATA`` and
    ``APPDATA`` remain available to Windows/Playwright without selecting the
    ARGUS database.
    """

    inherited = os.environ if source is None else source
    environment: dict[str, str] = {}
    operator_settings: dict[str, str] = {}
    for key, value in inherited.items():
        upper = key.upper()
        if upper.startswith("ARGUS_"):
            if upper in _ALLOWED_INHERITED_ARGUS:
                operator_settings[upper] = value
            continue
        # Preserve the ordinary Windows process contract, but do not pass
        # interpreter injection, proxy, credential, or browser-profile paths
        # from an untrusted parent into the source child.
        if upper in {"PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONINSPECT"}:
            continue
        if upper in _OS_ENV_NAMES:
            environment[key] = value
            continue
        if (
            upper in _PRIVATE_ENV_NAMES
            or upper.startswith(_PRIVATE_ENV_PREFIXES)
            or any(token in upper for token in _PRIVATE_ENV_TOKENS)
        ):
            continue
        if not upper.startswith("PLAYWRIGHT_"):
            environment[key] = value
    environment.update(operator_settings)
    # Assignment is intentional.  Never use setdefault for a dangerous
    # control: inherited ARMED/true/positive values must be overwritten.
    environment.update(DEFAULT_ENV)
    # Browser visibility is not an automation opt-in.  Preserve explicit
    # operator choices while retaining the fail-closed defaults when absent.
    environment.update(operator_settings)
    # The autopilot lock above exists to stop a parent process ARMING
    # submission, not to forbid automation outright.  REVIEW_ONLY runs
    # discovery and field-mapping review while ``submission_armed`` is False
    # by construction, so an inherited non-armed mode is re-applied here.
    # ARMED/RUNNING are still overwritten with OFF: arming stays a deliberate,
    # in-file decision that cannot be made by an environment variable.
    inherited_mode = str(inherited.get("ARGUS_AUTOMATION_MODE", "")).strip().upper()
    if inherited_mode in {"OFF", "REVIEW_ONLY"}:
        environment["ARGUS_AUTOMATION_MODE"] = inherited_mode
        # Sweeps are discovery only -- they find newly opened internships and
        # can never fill or submit -- so an interval is honoured alongside a
        # non-armed mode.  A malformed or negative value falls back to 0.
        raw_interval = str(inherited.get("ARGUS_SWEEP_INTERVAL_HOURS", "")).strip()
        if raw_interval.isdigit() and int(raw_interval) > 0:
            environment["ARGUS_SWEEP_INTERVAL_HOURS"] = raw_interval
    safe_root = (
        _trusted_runtime_data_dir()
        if data_root is None
        else _explicit_data_root(data_root)
    )
    environment["ARGUS_DATA_DIR"] = str(safe_root)
    return environment


def _apply_process_environment(environment: Mapping[str, str]) -> None:
    """Replace the in-process environment for frozen one-file execution."""

    for key in tuple(os.environ):
        if key not in environment:
            os.environ.pop(key, None)
    os.environ.update(environment)


def _open_when_ready(port: int, *, browser_open: bool | None = None) -> None:
    if browser_open is None:
        browser_open = _browser_open_enabled()
    if not browser_open:
        return

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if _healthy(port):
            print("ARGUS ready - opening dashboard.")
            webbrowser.open(f"http://{HOST}:{port}")
            return
        time.sleep(0.5)


def _runtime_version() -> str:
    try:
        from app.version import __version__

        return __version__
    except (ImportError, AttributeError):
        return "0.2.0"


def _sandbox_environment(data_root: str) -> dict[str, str]:
    """Claim a new disposable root, rejecting aliases and production overlap.

    This is data/config isolation, not an OS security sandbox. Callers must
    use synthetic loopback targets only. The owned root is retained for audit
    and must only be removed after the owned runtime has stopped.
    """
    candidate = Path(data_root)
    if str(data_root).replace("\\", "/").startswith("//"):
        raise ValueError("sandbox root must be a local path, not UNC or a device path")
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("sandbox root must be absolute without '..' components")
    for component in (candidate, *candidate.parents):
        try:
            attributes = getattr(component.lstat(), "st_file_attributes", 0)
        except FileNotFoundError:
            attributes = 0
        # lstat, not exists(): dangling junctions are still aliases.
        if component.is_symlink() or attributes & 0x400:
            raise ValueError("sandbox root and ancestors must not be links or reparse points")
    root = _explicit_data_root(candidate)
    production = _trusted_runtime_data_dir().resolve()
    if root == production or root in production.parents or production in root.parents:
        raise ValueError("sandbox root must not overlap the production data directory")
    if root.exists() or not root.parent.is_dir():
        raise ValueError("sandbox root must not exist and its parent must already exist")
    # Claim exclusively: a concurrent creator cannot turn this into reuse.
    root.mkdir(mode=0o700, exist_ok=False)
    # An allowlist, not a denylist: no inherited application, provider,
    # credential, proxy, interpreter, or browser settings enter this mode.
    inherited = {key: value for key, value in os.environ.items() if key.upper() in _OS_ENV_NAMES}
    env = _safe_child_environment(inherited, data_root=root)
    env.update(DEFAULT_ENV)
    env["ARGUS_FORCE_HEADLESS"] = "true"
    for name in ("TEMP", "TMP", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        env[name] = str(root)
    env["HOMEDRIVE"] = root.drive
    env["HOMEPATH"] = str(root)[len(root.drive):]
    with socket.socket() as sock:
        sock.bind((HOST, 0))
        port = sock.getsockname()[1]
    if port == PREFERRED_PORT:
        raise RuntimeError("sandbox ephemeral port unexpectedly matches production port")
    env["ARGUS_PORT"] = str(port)
    return env


def main(argv: list[str] | tuple[str, ...] = ()) -> int:
    """Launch normally, or use --sandbox-root ABSOLUTE_NEW_DIRECTORY.

    main() retains its historical no-argument programmatic contract; the CLI
    entry point passes sys.argv explicitly. Sandbox manifests are discovery
    information, not a readiness claim. No automatic cleanup is performed.
    """
    import argparse

    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--sandbox-root", metavar="ABSOLUTE_NEW_DIRECTORY",
        help="disposable synthetic-test data root (must not exist; parent must exist); "
             "OFF, no live submit/sweeps/browser opener, headless, unused loopback port; "
             "retained for readback and explicit cleanup after stopping the runtime",
    )
    args = parser.parse_args(argv)
    if sum(arg == "--sandbox-root" or arg.startswith("--sandbox-root=") for arg in argv) > 1:
        parser.error("--sandbox-root may only be specified once")
    if args.sandbox_root is None:
        env = _safe_child_environment()
    else:
        try:
            env = _sandbox_environment(args.sandbox_root)
        except (ValueError, OSError, RuntimeError) as exc:
            parser.error(str(exc))

    from app.config import Settings

    # Sanitize before loading Settings, which can read a persisted API token.
    env.update(_playwright_browsers_env(env))
    settings = Settings.load(env)
    settings.ensure_directories()
    port = _pick_port(settings.port)
    env["ARGUS_PORT"] = str(port)
    # Frozen in-process mode reads os.environ; source mode passes ``env`` to
    # the child process.  Keep both paths on exactly the same safe contract.
    _apply_process_environment(env)
    browser_open = _browser_open_enabled(env)

    lock = RuntimeLock(
        runtime_lock_path(settings.data_dir),
        host=HOST,
        port=port,
        version=_runtime_version(),
    )
    try:
        lock.acquire()
    except RuntimeAlreadyRunning as exc:
        raise SystemExit(str(exc)) from exc

    try:
        if args.sandbox_root is not None:
            manifest = json.dumps({
                "data_dir": str(settings.data_dir), "host": HOST, "port": port,
                "automation_mode": "OFF", "live_submit": False,
            }, sort_keys=True)
            (settings.data_dir / "sandbox-manifest.json").write_text(manifest + "\n", encoding="utf-8")
            if sys.stdout is not None:
                print("ARGUS_SANDBOX " + manifest, flush=True)
        if getattr(sys, "frozen", False):
            # ---- frozen .exe: serve in-process ----
            import uvicorn

            if browser_open:
                opener = threading.Thread(target=_open_when_ready, args=(port,), daemon=True)
                opener.start()
            uvicorn.run(
                "app.main:app",
                host=HOST,
                port=port,
                log_level="warning",
                access_log=False,
            )
            return 0

        root = _app_root()
        venv_python = root / ".venv" / "Scripts" / "python.exe"
        command = [
            str(venv_python if venv_python.is_file() else sys.executable),
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            HOST,
            "--port",
            str(port),
            "--log-level",
            "warning",
        ]
        print(f"ARGUS starting on http://{HOST}:{port} ...")
        process = subprocess.Popen(command, cwd=str(root), env=env)
        if browser_open:
            opener = threading.Thread(target=_open_when_ready, args=(port,), daemon=True)
            opener.start()
        try:
            return process.wait()
        except KeyboardInterrupt:
            process.terminate()
            return process.wait()
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
