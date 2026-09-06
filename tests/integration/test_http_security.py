from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def _client(tmp_path: Path) -> TestClient:
    settings = Settings.load(
        {"ARGUS_DATA_DIR": str(tmp_path), "ARGUS_API_TOKEN": "security-token"}
    )
    return TestClient(create_app(settings))


def _opportunity_payload(url: str = "https://jobs.example.test/intern") -> dict[str, object]:
    return {
        "employer": "Security Test Capital",
        "role_title": "Summer Analyst",
        "cycle": "2027",
        "url": url,
    }


def test_dynamic_responses_apply_browser_security_headers(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    policy = response.headers["content-security-policy"]
    assert "default-src 'self'" in policy
    assert "script-src 'self'" in policy
    assert "'unsafe-inline'" not in policy
    assert "frame-ancestors 'none'" in policy


def test_cross_site_mutations_and_dns_rebinding_hosts_are_refused(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        cross_site = client.post(
            "/api/opportunities",
            json=_opportunity_payload(),
            headers={
                "Origin": "https://attacker.example",
                "Sec-Fetch-Site": "cross-site",
            },
        )
        rebound = client.get(
            "/healthz",
            headers={"Host": "attacker.example:8787"},
        )
        same_origin = client.post(
            "/api/opportunities",
            json=_opportunity_payload("https://jobs.example.test/safe"),
            headers={
                "Origin": "http://testserver",
                "Sec-Fetch-Site": "same-origin",
            },
        )

    assert cross_site.status_code == 403
    assert cross_site.json()["detail"] == "Cross-site state change refused"
    assert rebound.status_code == 400
    assert rebound.json()["detail"] == "Untrusted Host header"
    assert same_origin.status_code == 201


def test_token_authenticated_extension_capture_survives_cross_origin_guard(tmp_path: Path) -> None:
    payload = {
        "employer": "Captured Firm",
        "role_title": "Analyst Intern",
        "cycle": "2027",
        "url": "https://jobs.example.test/captured",
    }
    with _client(tmp_path) as client:
        response = client.post(
            "/api/capture",
            json=payload,
            headers={
                "X-Argus-Token": "security-token",
                "Origin": "chrome-extension://abcdefghijklmnop",
                "Sec-Fetch-Site": "cross-site",
            },
        )

    assert response.status_code == 201


def test_capture_token_uses_constant_time_comparison(tmp_path: Path, monkeypatch) -> None:
    import secrets

    comparisons: list[tuple[str, str]] = []
    original = secrets.compare_digest

    def record(left: str, right: str) -> bool:
        comparisons.append((left, right))
        return original(left, right)

    monkeypatch.setattr(secrets, "compare_digest", record)
    payload = {
        "employer": "Constant Time Capital",
        "role_title": "Analyst Intern",
        "cycle": "2027",
        "url": "https://jobs.example.test/constant-time",
    }
    with _client(tmp_path) as client:
        response = client.post(
            "/api/capture",
            json=payload,
            headers={"X-Argus-Token": "security-token"},
        )

    assert response.status_code == 201
    assert comparisons == [("security-token", "security-token")]
