from pathlib import Path

import pytest

from app.config import AutomationMode, Settings


def test_settings_default_to_local_only_and_automation_off(tmp_path: Path) -> None:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})

    assert settings.host == "127.0.0.1"
    assert settings.live_submit_enabled is False
    assert settings.trackr_live_enabled is False
    assert settings.automation_mode is AutomationMode.OFF
    assert settings.submission_armed is False
    assert settings.autopilot_enabled is False
    assert settings.sweep_interval_hours == 0
    assert settings.live_domain_allowlist == frozenset()
    assert settings.database_url.endswith("argus.db")
    assert settings.data_dir == tmp_path.resolve()


def test_settings_parse_allowlist_and_explicit_live_submit(tmp_path: Path) -> None:
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_ENABLE_LIVE_SUBMIT": "true",
            "ARGUS_LIVE_DOMAIN_ALLOWLIST": "jobs.example.com, careers.example.org ",
        }
    )

    assert settings.live_submit_enabled is True
    assert settings.live_domain_allowlist == frozenset(
        {"jobs.example.com", "careers.example.org"}
    )


@pytest.mark.parametrize(
    ("mode", "armed"),
    [
        ("OFF", False),
        ("REVIEW_ONLY", False),
        ("ARMED", True),
        ("RUNNING", True),
    ],
)
def test_settings_parse_explicit_automation_modes(
    tmp_path: Path, mode: str, armed: bool
) -> None:
    settings = Settings.load(
        {"ARGUS_DATA_DIR": str(tmp_path), "ARGUS_AUTOMATION_MODE": mode}
    )

    assert settings.automation_mode is AutomationMode(mode)
    assert settings.submission_armed is armed


def test_legacy_live_submit_flag_arms_automation_without_mode(tmp_path: Path) -> None:
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_ENABLE_LIVE_SUBMIT": "true",
        }
    )

    assert settings.live_submit_enabled is True
    assert settings.automation_mode is AutomationMode.ARMED
    assert settings.submission_armed is True


def test_stale_legacy_live_submit_false_keeps_automation_off(tmp_path: Path) -> None:
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_ENABLE_LIVE_SUBMIT": "false",
        }
    )

    assert settings.automation_mode is AutomationMode.OFF
    assert settings.autopilot_enabled is False
    assert settings.live_submit_enabled is False


@pytest.mark.parametrize("raw_interval", ["0", "-1", "-0.25", "", "   "])
def test_nonpositive_or_blank_sweep_interval_disables_scheduling(
    tmp_path: Path, raw_interval: str
) -> None:
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_SWEEP_INTERVAL_HOURS": raw_interval,
            "ARGUS_AUTOMATION_MODE": "REVIEW_ONLY",
        }
    )

    assert settings.sweep_interval_hours == 0


@pytest.mark.parametrize("raw_interval", ["1e-12", "1e-300", "5e-324"])
def test_tiny_positive_sweep_interval_fails_closed_to_disabled(
    tmp_path: Path, raw_interval: str
) -> None:
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_SWEEP_INTERVAL_HOURS": raw_interval,
            "ARGUS_AUTOMATION_MODE": "REVIEW_ONLY",
        }
    )

    assert settings.sweep_interval_hours == 0


@pytest.mark.parametrize("raw_interval", ["not-a-number", "nan", "inf", "-inf"])
def test_ambiguous_sweep_interval_is_rejected_fail_closed(
    tmp_path: Path, raw_interval: str
) -> None:
    with pytest.raises(ValueError, match="ARGUS_SWEEP_INTERVAL_HOURS"):
        Settings.load(
            {
                "ARGUS_DATA_DIR": str(tmp_path),
                "ARGUS_SWEEP_INTERVAL_HOURS": raw_interval,
                "ARGUS_AUTOMATION_MODE": "REVIEW_ONLY",
            }
        )


def test_explicit_mode_is_authoritative_over_legacy_live_submit_flag(tmp_path: Path) -> None:
    review = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path / "review"),
            "ARGUS_AUTOMATION_MODE": "REVIEW_ONLY",
            "ARGUS_ENABLE_LIVE_SUBMIT": "true",
        }
    )
    armed = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path / "armed"),
            "ARGUS_AUTOMATION_MODE": "ARMED",
            "ARGUS_ENABLE_LIVE_SUBMIT": "false",
        }
    )

    assert review.live_submit_enabled is False
    assert armed.live_submit_enabled is True


def test_settings_parse_explicit_trackr_live_opt_in(tmp_path: Path) -> None:
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_ENABLE_TRACKR_LIVE": "true",
        }
    )

    assert settings.trackr_live_enabled is True


def test_settings_reject_unknown_automation_mode(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="ARGUS_AUTOMATION_MODE"):
        Settings.load(
            {
                "ARGUS_DATA_DIR": str(tmp_path),
                "ARGUS_AUTOMATION_MODE": "MAYBE",
            }
        )


def test_ensure_directories_creates_private_runtime_tree(tmp_path: Path) -> None:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})

    settings.ensure_directories()

    assert settings.documents_dir.is_dir()
    assert settings.artifacts_dir.is_dir()
    assert settings.traces_dir.is_dir()
    assert settings.screenshots_dir.is_dir()


def test_capture_token_persists_after_runtime_initialisation(tmp_path: Path) -> None:
    first = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    first.ensure_directories()
    second = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})

    assert first.api_token == second.api_token
    assert first.api_token_path.read_text(encoding="utf-8").strip() == first.api_token


def test_settings_reject_non_loopback_bind_addresses(tmp_path: Path) -> None:
    for host in ("0.0.0.0", "192.168.1.50", "example.com"):
        with pytest.raises(ValueError, match="loopback"):
            Settings.load({"ARGUS_DATA_DIR": str(tmp_path / host.replace('.', '_')), "ARGUS_HOST": host})
