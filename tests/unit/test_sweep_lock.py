from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.db import Database
from app.security.crypto import CryptoBox
from app.scouting import scheduler
from app.scouting.sweep_lock import SweepLock


def test_sweep_lock_is_exclusive_between_process_like_owners(tmp_path: Path) -> None:
    path = tmp_path / "scout-sweep.lock"
    first = SweepLock(path)
    second = SweepLock(path)

    assert first.acquire() is True
    assert second.acquire() is False

    first.release()

    assert second.acquire() is True
    second.release()


def test_scheduler_skips_when_another_process_holds_sweep_lock(
    tmp_path: Path, monkeypatch
) -> None:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    app = SimpleNamespace(state=SimpleNamespace(settings=settings))
    held = SweepLock(settings.data_dir / "scout-sweep.lock")
    assert held.acquire() is True
    monkeypatch.setattr(
        scheduler,
        "_run_sweep_locked",
        lambda app: (_ for _ in ()).throw(AssertionError("lock was ignored")),
    )

    try:
        result = scheduler.run_sweep(app)
    finally:
        held.release()

    assert result["skipped"] is True
    assert result["reason"] == "sweep_locked"


def test_scheduler_keeps_live_trackr_disabled_without_explicit_opt_in(
    tmp_path: Path, monkeypatch
) -> None:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    settings.ensure_directories()
    database = Database(settings)
    database.create_schema()
    app = SimpleNamespace(
        state=SimpleNamespace(
            settings=settings,
            db=database,
            crypto=CryptoBox.from_path(settings.secret_key_path),
        )
    )

    def fail_if_called():
        raise AssertionError("live Trackr fetch was not opted in")

    monkeypatch.setattr("app.scouting.trackr_live.fetch_programmes", fail_if_called)

    result = scheduler._run_sweep_locked(app)

    assert result["live_scrape"] == "disabled"


def test_scheduler_off_mode_skips_discovery_and_autopilot(tmp_path: Path, monkeypatch) -> None:
    settings = Settings.load(
        {"ARGUS_DATA_DIR": str(tmp_path), "ARGUS_AUTOMATION_MODE": "OFF"}
    )
    app = SimpleNamespace(state=SimpleNamespace(settings=settings))
    monkeypatch.setattr(
        scheduler,
        "_run_sweep_locked",
        scheduler._run_sweep_locked,
    )

    result = scheduler.run_sweep(app)

    assert result["skipped"] is True
    assert result["reason"] == "automation_off"


def test_off_mode_does_not_create_a_scheduler_thread_or_boot_sweep(
    tmp_path: Path, monkeypatch
) -> None:
    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    app = SimpleNamespace(state=SimpleNamespace(settings=settings))
    created: list[object] = []

    class UnexpectedThread:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            created.append((args, kwargs))
            raise AssertionError("OFF must not create a scheduler thread")

    monkeypatch.setattr(scheduler.threading, "Thread", UnexpectedThread)
    monkeypatch.setattr(
        scheduler,
        "run_sweep",
        lambda app: (_ for _ in ()).throw(AssertionError("OFF must not boot sweep")),
    )
    scheduler._THREAD = None
    scheduler._STOP.clear()

    scheduler.start_scheduler(app, interval_hours=1)

    assert created == []
    assert scheduler._THREAD is None


@pytest.mark.parametrize("interval_hours", [0, -1, -0.25, 1e-12, 1e-300, 5e-324])
def test_nonpositive_interval_does_not_create_a_scheduler_thread(
    tmp_path: Path, monkeypatch, interval_hours: float
) -> None:
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_AUTOMATION_MODE": "REVIEW_ONLY",
        }
    )
    app = SimpleNamespace(state=SimpleNamespace(settings=settings))
    created: list[object] = []

    class UnexpectedThread:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            created.append((args, kwargs))
            raise AssertionError("nonpositive intervals must disable scheduling")

    monkeypatch.setattr(scheduler.threading, "Thread", UnexpectedThread)
    scheduler._THREAD = None
    scheduler._STOP.clear()

    scheduler.start_scheduler(app, interval_hours=interval_hours)

    assert created == []
    assert scheduler._THREAD is None


def test_positive_review_only_interval_creates_one_scheduler_thread(
    tmp_path: Path, monkeypatch
) -> None:
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(tmp_path),
            "ARGUS_AUTOMATION_MODE": "REVIEW_ONLY",
        }
    )
    app = SimpleNamespace(state=SimpleNamespace(settings=settings))
    created: list[tuple[object, object]] = []

    class CapturedThread:
        def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
            created.append((args, kwargs))

        def start(self) -> None:
            return None

        def is_alive(self) -> bool:
            return True

    monkeypatch.setattr(scheduler.threading, "Thread", CapturedThread)
    scheduler._THREAD = None
    scheduler._STOP.clear()

    try:
        scheduler.start_scheduler(app, interval_hours=1)

        assert len(created) == 1
        args, kwargs = created[0]
        assert args == ()
        assert kwargs["target"] is scheduler._loop
        assert kwargs["args"] == (app, 1)
        assert kwargs["name"] == "argus-scout-sweep"
        assert kwargs["daemon"] is True
    finally:
        scheduler.stop_scheduler()
        scheduler._THREAD = None
        scheduler._STOP.clear()
