from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

from app.automation.targets import TargetResolution
from app.config import Settings
from app.db import Database
from app.domain.targets import TargetKind
from app.models import Opportunity
from app.services.target_resolution import TargetResolutionService
from app.automation.host_policy import origin_for_url


@dataclass(frozen=True, slots=True)
class LiveServer:
    base_url: str
    data_dir: Path
    log_path: Path


def persist_verified_lab_target(
    server: LiveServer,
    opportunity_id: str,
    target_url: str,
    provider: str,
) -> None:
    """Mark only this process's loopback lab URL as a verified test target."""

    expected_prefix = f"{server.base_url}/lab/ats/"
    if not target_url.startswith(expected_prefix):
        raise ValueError("E2E target verification is restricted to the loopback ATS lab")
    settings = Settings.load(
        {
            "ARGUS_DATA_DIR": str(server.data_dir),
            "ARGUS_API_TOKEN": "e2e-token",
        }
    )
    database = Database(settings)
    try:
        with database.session_scope() as session:
            opportunity = session.get(Opportunity, opportunity_id)
            if opportunity is None:
                raise KeyError(opportunity_id)
            TargetResolutionService(session).record(
                opportunity_id,
                TargetResolution(
                    # The persisted listing/source URL is evidence only; the
                    # browser destination is the independently verified
                    # application URL below.
                    source_url=opportunity.navigation_url,
                    final_url=target_url,
                    kind=TargetKind.APPLICATION_ENTRY,
                    provider=provider,
                    identity_verified=True,
                    reason_codes=("synthetic_loopback_lab_fixture",),
                    evidence={
                        "synthetic_lab": True,
                        "provider": provider,
                        "application_origin": origin_for_url(target_url),
                        "employer": "ARGUS Test Capital",
                        "role": "Summer Analyst",
                        "requisition": urlsplit(target_url).path,
                        # Path-derived (never the raw URL tail): a
                        # query-bearing lab URL keeps the same requisition
                        # and form identity as its canonical target.
                        "form_identity": urlsplit(target_url).path.rstrip("/").rsplit("/", 1)[-1],
                    },
                ),
            )
    finally:
        database.engine.dispose()


def _build_e2e_environment(data_dir: Path, port: int) -> dict[str, str]:
    """Build a subprocess environment with no inherited ARGUS settings."""

    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.casefold().startswith("argus_")
    }
    environment.update(
        {
            "ARGUS_DATA_DIR": str(data_dir),
            "ARGUS_API_TOKEN": "e2e-token",
            "ARGUS_PORT": str(port),
            "ARGUS_AUTOMATION_MODE": "REVIEW_ONLY",
            "ARGUS_ENABLE_LIVE_SUBMIT": "false",
            "ARGUS_ENABLE_TRACKR_LIVE": "false",
            "ARGUS_ENABLE_APPLY_CLICK": "false",
            "ARGUS_LIVE_DOMAIN_ALLOWLIST": "",
            "ARGUS_BROWSER_HEADLESS": "true",
            "ARGUS_FORCE_HEADLESS": "true",
            "PYTHONUNBUFFERED": "1",
        }
    )
    return environment


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture()
def live_server(tmp_path: Path) -> LiveServer:
    port = _free_port()
    data_dir = tmp_path / "argus-data"
    log_path = tmp_path / "server.log"
    env = _build_e2e_environment(data_dir, port)
    log = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"ARGUS server exited early:\n{log_path.read_text()}")
            try:
                response = httpx.get(f"{base_url}/healthz", timeout=0.5)
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError(f"ARGUS server did not become ready:\n{log_path.read_text()}")
        yield LiveServer(base_url, data_dir, log_path)
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        log.close()
