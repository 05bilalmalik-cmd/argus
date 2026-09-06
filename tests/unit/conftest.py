from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from e2e.conftest import LiveServer  # noqa: E402,F401 - reuse the fixture shape


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def live_server():
    """A REVIEW_ONLY server for API-contract assertions."""
    port = _free_port()
    data_dir = Path(os.environ.get("TEMP", "/tmp")) / f"argus-r6-{port}"
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.casefold().startswith("argus_")
    }
    env.update(
        {
            "ARGUS_DATA_DIR": str(data_dir),
            "ARGUS_API_TOKEN": "r6-token",
            "ARGUS_PORT": str(port),
            "ARGUS_AUTOMATION_MODE": "REVIEW_ONLY",
            "ARGUS_ENABLE_LIVE_SUBMIT": "false",
            "PYTHONUNBUFFERED": "1",
        }
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(Path(__file__).resolve().parents[2]),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            httpx.get(f"{base_url}/healthz", timeout=1)
            break
        except Exception:
            time.sleep(0.5)
    yield type("LS", (), {"base_url": base_url, "data_dir": data_dir})()
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
