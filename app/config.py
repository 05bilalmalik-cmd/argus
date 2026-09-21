from __future__ import annotations

import ipaddress
import math
import os
import secrets
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Mapping

_TRUE_VALUES = {"1", "true", "yes", "on"}
_DEFAULT_SWEEP_INTERVAL_HOURS = 0.0
_DEFAULT_NOTIFY_BATCH_WINDOW_SECONDS = 30.0
_DEFAULT_NOTIFY_RATE_LIMIT_PER_HOUR = 6
_DEFAULT_APPLY_CLICK_RUN_CAP = 25
_DEFAULT_APPLY_CLICK_TIMEOUT_SECONDS = 10.0
# A positive interval must still produce a real wait. One second is the
# minimum so malformed scientific/subnormal values cannot create a busy loop.
MIN_SWEEP_INTERVAL_SECONDS = 1.0
_MIN_SWEEP_INTERVAL_HOURS = MIN_SWEEP_INTERVAL_SECONDS / 3600.0


class AutomationMode(StrEnum):
    """The explicit safety state for scheduled automation.

    ``OFF`` disables the autopilot, ``REVIEW_ONLY`` permits discovery and
    field-mapping review, and ``ARMED``/``RUNNING`` permit a caller to request
    submission after all other guards pass.  The runner remains separately
    protected by the live-submit flag and host policy.
    """

    OFF = "OFF"
    REVIEW_ONLY = "REVIEW_ONLY"
    ARMED = "ARMED"
    RUNNING = "RUNNING"

    @property
    def submission_armed(self) -> bool:
        return self in {AutomationMode.ARMED, AutomationMode.RUNNING}

    @property
    def autopilot_enabled(self) -> bool:
        return self is not AutomationMode.OFF


def _parse_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in _TRUE_VALUES


def _parse_automation_mode(source: Mapping[str, str], *, legacy_live_submit: bool) -> AutomationMode:
    configured = (
        source.get("ARGUS_AUTOMATION_MODE")
        or source.get("ARGUS_AUTOMATION_STATE")
        or source.get("ARGUS_AUTOPILOT_MODE")
        or source.get("ARGUS_AUTOMATION")
        or ""
    ).strip()
    if not configured:
        # The legacy live-submit flag remains an explicit compatibility
        # opt-in.  An absent or false legacy flag must leave the runtime OFF;
        # it must never turn a missing mode into a scheduled review sweep.
        return AutomationMode.ARMED if legacy_live_submit else AutomationMode.OFF
    normalised = configured.upper().replace("-", "_").replace(" ", "_")
    try:
        return AutomationMode(normalised)
    except ValueError as exc:
        valid = ", ".join(mode.value for mode in AutomationMode)
        raise ValueError(
            f"ARGUS_AUTOMATION_MODE must be one of {valid}; got {configured!r}"
        ) from exc


def _parse_sweep_interval(value: str | None) -> float:
    """Parse a sweep interval, disabling non-positive/subminimum values."""

    raw = "" if value is None else str(value).strip()
    if not raw:
        return _DEFAULT_SWEEP_INTERVAL_HOURS
    try:
        interval = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "ARGUS_SWEEP_INTERVAL_HOURS must be a finite number; "
            f"got {value!r}"
        ) from exc
    if not math.isfinite(interval):
        raise ValueError(
            "ARGUS_SWEEP_INTERVAL_HOURS must be a finite number; "
            f"got {value!r}"
        )
    if 0 < interval < _MIN_SWEEP_INTERVAL_HOURS:
        return _DEFAULT_SWEEP_INTERVAL_HOURS
    return max(interval, _DEFAULT_SWEEP_INTERVAL_HOURS)


def _parse_notify_batch_window(value: str | None) -> float:
    raw = "" if value is None else str(value).strip()
    if not raw:
        return _DEFAULT_NOTIFY_BATCH_WINDOW_SECONDS
    try:
        window = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "ARGUS_NOTIFY_BATCH_WINDOW_SECONDS must be a finite non-negative number"
        ) from exc
    if not math.isfinite(window) or window < 0:
        raise ValueError(
            "ARGUS_NOTIFY_BATCH_WINDOW_SECONDS must be a finite non-negative number"
        )
    return window


def _parse_notify_rate_limit(value: str | None) -> int:
    raw = "" if value is None else str(value).strip()
    if not raw:
        return _DEFAULT_NOTIFY_RATE_LIMIT_PER_HOUR
    try:
        limit = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "ARGUS_NOTIFY_RATE_LIMIT_PER_HOUR must be a positive integer"
        ) from exc
    if limit < 1:
        raise ValueError("ARGUS_NOTIFY_RATE_LIMIT_PER_HOUR must be a positive integer")
    return limit


def _parse_apply_click_run_cap(value: str | None) -> int:
    raw = "" if value is None else str(value).strip()
    if not raw:
        return _DEFAULT_APPLY_CLICK_RUN_CAP
    try:
        limit = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "ARGUS_APPLY_CLICK_RUN_CAP must be an integer between 1 and 100"
        ) from exc
    if not 1 <= limit <= 100:
        raise ValueError(
            "ARGUS_APPLY_CLICK_RUN_CAP must be an integer between 1 and 100"
        )
    return limit


def _parse_apply_click_timeout_ms(value: str | None) -> int:
    raw = "" if value is None else str(value).strip()
    if not raw:
        seconds = _DEFAULT_APPLY_CLICK_TIMEOUT_SECONDS
    else:
        try:
            seconds = float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "ARGUS_APPLY_CLICK_TIMEOUT_SECONDS must be finite and between 0.1 and 60"
            ) from exc
    if not math.isfinite(seconds) or not 0.1 <= seconds <= 60.0:
        raise ValueError(
            "ARGUS_APPLY_CLICK_TIMEOUT_SECONDS must be finite and between 0.1 and 60"
        )
    return int(seconds * 1000)


def _normalise_allowlist_entry(value: str) -> str:
    value = value.strip().casefold()
    if value.startswith("*."):
        return "*." + value[2:].rstrip(".")
    return value.rstrip(".")


def _default_data_dir(env: Mapping[str, str]) -> Path:
    if os.name == "nt":
        root = env.get("LOCALAPPDATA") or env.get("APPDATA") or str(Path.home())
        return Path(root) / "ARGUS"
    root = env.get("XDG_DATA_HOME")
    if root:
        return Path(root) / "argus"
    return Path(env.get("HOME", str(Path.home()))) / ".local" / "share" / "argus"


@dataclass(frozen=True, slots=True)
class Settings:
    host: str
    port: int
    data_dir: Path
    database_url: str
    documents_dir: Path
    artifacts_dir: Path
    traces_dir: Path
    screenshots_dir: Path
    secret_key_path: Path
    live_submit_enabled: bool
    automation_mode: AutomationMode
    live_domain_allowlist: frozenset[str]
    api_token: str
    api_token_path: Path
    ollama_url: str
    ollama_model: str | None
    browser_headless: bool
    sweep_interval_hours: float
    trackr_live_enabled: bool
    notifications_enabled: bool
    ntfy_topic: str
    ntfy_server: str
    notify_webhook_url: str
    hermes_notify_target: str | None
    hermes_bin: str
    notify_batch_window_seconds: float
    notify_rate_limit_per_hour: int
    apply_click_enabled: bool
    apply_click_run_cap: int
    apply_click_timeout_ms: int
    role_match_v2_enabled: bool = False
    egress_impact_classification_enabled: bool = True
    autopilot_skip_login_required: bool = True
    # Preserve the named environment authority separately from the effective
    # mode. Runtime UI posture changes may narrow `live_submit_enabled`, but
    # must never manufacture this restart-bound opt-in.
    live_submit_environment_enabled: bool = False

    @classmethod
    def load(cls, env: Mapping[str, str] | None = None) -> "Settings":
        source: Mapping[str, str] = os.environ if env is None else env
        configured_data_dir = source.get("ARGUS_DATA_DIR", "").strip()
        data_dir = (
            Path(configured_data_dir).expanduser()
            if configured_data_dir
            else _default_data_dir(source)
        ).resolve()
        database_path = data_dir / "argus.db"
        domains = frozenset(
            _normalise_allowlist_entry(item)
            for item in source.get("ARGUS_LIVE_DOMAIN_ALLOWLIST", "").split(",")
            if item.strip()
        )
        api_token_path = data_dir / "api_token.txt"
        configured_token = source.get("ARGUS_API_TOKEN", "").strip()
        persisted_token = ""
        if not configured_token and api_token_path.is_file():
            persisted_token = api_token_path.read_text(encoding="utf-8").strip()
        api_token = configured_token or persisted_token or secrets.token_urlsafe(24)
        ollama_model = source.get("ARGUS_OLLAMA_MODEL", "").strip() or None
        host = source.get("ARGUS_HOST", "127.0.0.1").strip() or "127.0.0.1"
        if host.casefold() != "localhost":
            try:
                bind_address = ipaddress.ip_address(host)
            except ValueError as exc:
                raise ValueError("ARGUS_HOST must be a loopback IPv4 address or localhost") from exc
            if bind_address.version != 4 or not bind_address.is_loopback:
                raise ValueError("ARGUS_HOST must be a loopback IPv4 address or localhost")
        legacy_live_submit_enabled = _parse_bool(
            source.get("ARGUS_ENABLE_LIVE_SUBMIT"), default=False
        )
        automation_mode = _parse_automation_mode(
            source, legacy_live_submit=legacy_live_submit_enabled
        )
        has_explicit_mode = any(
            source.get(name, "").strip()
            for name in (
                "ARGUS_AUTOMATION_MODE",
                "ARGUS_AUTOMATION_STATE",
                "ARGUS_AUTOPILOT_MODE",
                "ARGUS_AUTOMATION",
            )
        )
        # An explicit mode is authoritative.  This keeps deployments from
        # accidentally combining REVIEW_ONLY/OFF with a stale legacy true
        # flag, while preserving the old flag's behaviour when no mode exists.
        live_submit_enabled = (
            automation_mode.submission_armed
            if has_explicit_mode
            else legacy_live_submit_enabled
        )
        return cls(
            host=host,
            port=int(source.get("ARGUS_PORT", "8787")),
            data_dir=data_dir,
            database_url=f"sqlite:///{database_path.as_posix()}",
            documents_dir=data_dir / "documents",
            artifacts_dir=data_dir / "artifacts",
            traces_dir=data_dir / "artifacts" / "traces",
            screenshots_dir=data_dir / "artifacts" / "screenshots",
            secret_key_path=data_dir / "secret.key",
            live_submit_enabled=live_submit_enabled,
            automation_mode=automation_mode,
            live_domain_allowlist=domains,
            api_token=api_token,
            api_token_path=api_token_path,
            ollama_url=source.get("ARGUS_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/"),
            ollama_model=ollama_model,
            browser_headless=_parse_bool(
                source.get("ARGUS_BROWSER_HEADLESS"), default=True
            ),
            sweep_interval_hours=_parse_sweep_interval(
                source.get("ARGUS_SWEEP_INTERVAL_HOURS")
            ),
            trackr_live_enabled=_parse_bool(
                source.get("ARGUS_ENABLE_TRACKR_LIVE"), default=False
            ),
            notifications_enabled=_parse_bool(
                source.get("ARGUS_ENABLE_NOTIFICATIONS"), default=False
            ),
            ntfy_topic=source.get("ARGUS_NTFY_TOPIC", "").strip(),
            ntfy_server=(
                source.get("ARGUS_NTFY_SERVER", "https://ntfy.sh").strip()
                or "https://ntfy.sh"
            ).rstrip("/"),
            notify_webhook_url=source.get("ARGUS_NOTIFY_WEBHOOK_URL", "").strip(),
            hermes_notify_target=(
                source.get("ARGUS_HERMES_NOTIFY_TARGET", "").strip()
                if "ARGUS_HERMES_NOTIFY_TARGET" in source
                else None
            ),
            hermes_bin=(source.get("ARGUS_HERMES_BIN", "hermes").strip() or "hermes"),
            notify_batch_window_seconds=_parse_notify_batch_window(
                source.get("ARGUS_NOTIFY_BATCH_WINDOW_SECONDS")
            ),
            notify_rate_limit_per_hour=_parse_notify_rate_limit(
                source.get("ARGUS_NOTIFY_RATE_LIMIT_PER_HOUR")
            ),
            apply_click_enabled=_parse_bool(
                source.get("ARGUS_ENABLE_APPLY_CLICK"), default=False
            ),
            apply_click_run_cap=_parse_apply_click_run_cap(
                source.get("ARGUS_APPLY_CLICK_RUN_CAP")
            ),
            apply_click_timeout_ms=_parse_apply_click_timeout_ms(
                source.get("ARGUS_APPLY_CLICK_TIMEOUT_SECONDS")
            ),
            role_match_v2_enabled=_parse_bool(
                source.get("ARGUS_ENABLE_ROLE_MATCH_V2"), default=False
            ),
            egress_impact_classification_enabled=_parse_bool(
                source.get("ARGUS_ENABLE_EGRESS_IMPACT_CLASSIFICATION"),
                default=True,
            ),
            autopilot_skip_login_required=_parse_bool(
                source.get("ARGUS_AUTOPILOT_SKIP_LOGIN_REQUIRED"),
                default=True,
            ),
            live_submit_environment_enabled=legacy_live_submit_enabled,
        )

    @property
    def submission_armed(self) -> bool:
        """Whether the explicit automation state permits a submit request."""

        return self.automation_mode.submission_armed

    @property
    def autopilot_enabled(self) -> bool:
        return self.automation_mode.autopilot_enabled

    @property
    def automation_state(self) -> AutomationMode:
        """Compatibility alias for callers that model mode as a state."""

        return self.automation_mode

    @property
    def autopilot_mode(self) -> AutomationMode:
        return self.automation_mode

    def ensure_directories(self) -> None:
        for directory in (
            self.data_dir,
            self.documents_dir,
            self.artifacts_dir,
            self.traces_dir,
            self.screenshots_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if os.name != "nt":
                os.chmod(directory, 0o700)
        if not self.api_token_path.exists():
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            try:
                descriptor = os.open(self.api_token_path, flags, 0o600)
            except FileExistsError:
                pass
            else:
                try:
                    os.write(descriptor, (self.api_token + "\n").encode("utf-8"))
                finally:
                    os.close(descriptor)
        if os.name != "nt" and self.api_token_path.exists():
            os.chmod(self.api_token_path, 0o600)
