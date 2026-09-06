from __future__ import annotations

import json
import hashlib
import subprocess
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.package_release import _issue_release_capability, build_release, source_snapshot
from scripts.privacy_scan import load_config, scan_artifact, scan_source
from scripts.verify import _command_hash, _plan_digest, _step_plan


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "packaging" / "privacy_scan_config.json"


def test_production_source_has_no_candidate_pii_or_user_paths() -> None:
    findings = scan_source(ROOT, load_config(CONFIG))

    assert findings == [], "production source privacy findings: " + json.dumps(
        [finding.as_dict() for finding in findings], sort_keys=True
    )


def test_scanner_rejects_personal_literals_in_non_fixture_source(tmp_path: Path) -> None:
    source = tmp_path / "app"
    source.mkdir()
    (source / "unsafe.py").write_text(
        """
first_name = "Ada"
last_name = "Lovelace"
email = "ada.lovelace@private.example"
phone = "+44 7700 900123"
work_authorisation = "UK citizen with the right to work in the UK; no sponsorship required"
cv_root = r"C:/Users/candidate/Documents/CVs"
""",
        encoding="utf-8",
    )

    findings = scan_source(tmp_path, load_config(CONFIG))
    categories = {finding.category for finding in findings}

    assert {"name", "email", "phone", "legal_status", "user_path"} <= categories


def test_explicit_synthetic_literals_are_allowed(tmp_path: Path) -> None:
    source = tmp_path / "app"
    source.mkdir()
    (source / "fixture.py").write_text(
        """
first_name = "Demo"
last_name = "Candidate"
email = "demo@argus.local"
work_authorisation = "LAB-ONLY DEMO VALUE — replace before any real use"
""",
        encoding="utf-8",
    )

    assert scan_source(tmp_path, load_config(CONFIG)) == []


def test_scanner_rejects_json_and_dict_name_fields(tmp_path: Path) -> None:
    source = tmp_path / "app"
    source.mkdir()
    (source / "profile.json").write_text(
        json.dumps({"first_name": "Ada", "last_name": "Lovelace"}, indent=2),
        encoding="utf-8",
    )
    (source / "profile.py").write_text(
        'profile = {\n  "first_name": "Grace",\n  "last_name": "Hopper"\n}\n',
        encoding="utf-8",
    )

    findings = scan_source(tmp_path, load_config(CONFIG))

    assert sum(finding.category == "name" for finding in findings) == 4


def test_scanner_detects_digits_only_phone_fields_but_not_numeric_ids(
    tmp_path: Path,
) -> None:
    source = tmp_path / "app"
    source.mkdir()
    (source / "profile.json").write_text(
        json.dumps(
            {
                "phone": "447700900123",
                "mobile": "07700900123",
                "request_id": "123456789012",
                "build_number": 20260824,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    findings = scan_source(tmp_path, load_config(CONFIG))

    assert sum(finding.category == "phone" for finding in findings) == 2


def test_scanner_covers_shipped_text_suffixes_and_extension_packaging_roots(
    tmp_path: Path,
) -> None:
    payloads = {
        "app/runtime.cfg": 'contact = "private.person@private.example"\n',
        "packaging/release.ini": 'contact = "private.person@private.example"\n',
        "extension/options.ts": 'const contact = "private.person@private.example";\n',
        "app/launcher.spec": 'contact = "private.person@private.example"\n',
    }
    for relative, content in payloads.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    findings = scan_source(tmp_path, load_config(CONFIG))

    assert sum(finding.category == "email" for finding in findings) == len(payloads)


def test_scanner_ignores_dates_ips_and_digits_only_identifiers(tmp_path: Path) -> None:
    source = tmp_path / "app"
    source.mkdir()
    (source / "noise.py").write_text(
        "build_number = 20260824\nrequest_id = 123456789012\nhost = '127.0.0.1'\n",
        encoding="utf-8",
    )

    assert scan_source(tmp_path, load_config(CONFIG)) == []


def test_requisition_or_job_id_on_same_line_does_not_hide_a_phone(tmp_path: Path) -> None:
    source = tmp_path / "app"
    source.mkdir()
    (source / "same_line.py").write_text(
        'requisition_id = "REQ-2027-00431"; contact_phone = "+44 7700 900123"\n',
        encoding="utf-8",
    )
    findings = scan_source(tmp_path, load_config(CONFIG))
    assert sum(finding.category == "phone" for finding in findings) == 1


def test_privacy_config_cannot_expand_exclusions_or_synthetic_allowlists(tmp_path: Path) -> None:
    payload = json.loads(CONFIG.read_text(encoding="utf-8"))
    payload["excluded_paths"].append("app")
    path = tmp_path / "expanded-exclusions.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    import pytest

    with pytest.raises(ValueError, match="expands|baseline"):
        load_config(path)

    payload = json.loads(CONFIG.read_text(encoding="utf-8"))
    payload["synthetic_email_domains"].append("private.example")
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="expands|baseline"):
        load_config(path)


def test_privacy_config_requires_immutable_baseline_attestation(tmp_path: Path) -> None:
    payload = json.loads(CONFIG.read_text(encoding="utf-8"))
    payload["baseline_sha256"] = "0" * 64
    path = tmp_path / "wrong-baseline.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    import pytest

    with pytest.raises(ValueError, match="immutable|baseline"):
        load_config(path)


def test_scanner_covers_tests_docs_root_and_utf16_or_binary_payloads(tmp_path: Path) -> None:
    utf16 = tmp_path / "tests" / "leak.py"
    utf16.parent.mkdir(parents=True)
    utf16.write_bytes('email = "private.person@private.example"\n'.encode("utf-16-le"))
    binary = tmp_path / "docs" / "leak.bin"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"\x00\x01private.person@private.example\x00\xff")
    root_file = tmp_path / "README.md"
    root_file.write_text("contact: private.person@private.example\n", encoding="utf-8")

    findings = scan_source(tmp_path, load_config(CONFIG))

    assert sum(finding.category == "email" for finding in findings) >= 3


def test_packaged_release_artifact_is_scanned_after_build(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    for relative in ("app/main.py", "extension/manifest.json", "scripts/setup.sh", "pyproject.toml", "requirements.txt", "README.md", "SECURITY.md", "OPERATIONS.md", "VERIFICATION.md", "tests/e2e/test_adversarial_flows.py", "tests/e2e/test_application_flows.py", "tests/e2e/test_application_navigator_journeys.py", "tests/e2e/test_apply_ui.py", "tests/e2e/test_dashboard.py", "tests/e2e/test_lab_adapter_variants.py", "tests/e2e/test_submission_unknown.py"):
        path = root / relative; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(relative, encoding="utf-8")
    (root / "pyproject.toml").write_text('[project]\nname = "fixture"\nversion = "0.2.0"\n', encoding="utf-8")
    (root / "packaging" / "privacy_scan_config.json").parent.mkdir(parents=True, exist_ok=True)
    (root / "packaging" / "privacy_scan_config.json").write_text((ROOT / "packaging" / "privacy_scan_config.json").read_text(encoding="utf-8"), encoding="utf-8")
    (root / "scripts" / "privacy_scan.py").write_text((ROOT / "scripts" / "privacy_scan.py").read_text(encoding="utf-8"), encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "fixture@example.test"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "ARGUS Fixture"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True)
    evidence_root = root / "release-evidence"
    evidence_root.mkdir()
    names = ["unit", "integration", "tests/e2e/test_adversarial_flows.py", "tests/e2e/test_application_flows.py", "tests/e2e/test_application_navigator_journeys.py", "tests/e2e/test_apply_ui.py", "tests/e2e/test_dashboard.py", "tests/e2e/test_lab_adapter_variants.py", "tests/e2e/test_submission_unknown.py", "compile", "cli_help", "cli_safe_smoke", "audit_fresh_temp", "migration_fresh_temp", "privacy_source"]
    logs, steps = [], []
    nonce = "privacy-fixture-provenance-20260825"
    interpreter = Path(sys.executable).resolve(); started = datetime.now(timezone.utc)
    plan = _step_plan(root, str(interpreter), None)
    plan_sha256 = _plan_digest(plan)
    command_hashes_by_name = {item["name"]: [_command_hash(command) for command in (item.get("commands") or [item.get("command")])] for item in plan}
    for index, name in enumerate(names, 1):
        command_hashes = command_hashes_by_name[name]
        command_lines = "".join(f"command_sha256={value}\n" for value in command_hashes)
        log = evidence_root / f"privacy-test-{index:02d}.log"; log.write_text(f"started_at=2026-08-25T00:00:00.000Z\nstep={name}\nprovenance_nonce={nonce}\nplan_sha256={plan_sha256}\ncommand_count={len(command_hashes)}\n{command_lines}finished_at=2026-08-25T00:00:01.000Z\nexit_code=0\n", encoding="utf-8")
        entry = {"path": log.relative_to(root).as_posix(), "sha256": hashlib.sha256(log.read_bytes()).hexdigest(), "size": log.stat().st_size}
        logs.append(entry); steps.append({"name": name, "status": "passed", "exit_code": 0, "log": entry, "provenance_nonce": nonce, "plan_sha256": plan_sha256, "command_hashes": command_hashes})
    evidence = {"schema_version": 2, "status": "passed", "started_at": started.isoformat().replace("+00:00", "Z"), "finished_at": (started + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"), "live_network": False, "python": {"path": str(interpreter), "sha256": hashlib.sha256(interpreter.read_bytes()).hexdigest(), "version": sys.version.split()[0], "implementation": "cpython", "executable": str(interpreter), "real": True}, "source": source_snapshot(root), "steps": steps, "logs": logs, "provenance": {"schema_version": 1, "run_nonce": nonce, "plan_sha256": plan_sha256, "executed": True}}
    result = build_release(
        root=root,
        output_dir=root / "dist",
        version="0.2.0",
        test_evidence=evidence,
        verification_capability=_issue_release_capability(evidence, root=root),
    )

    findings = scan_artifact(result.archive, load_config(CONFIG))

    assert findings == [], "packaged artifact privacy findings: " + json.dumps(
        [finding.as_dict() for finding in findings], sort_keys=True
    )


def test_scanner_reads_text_members_from_zip_artifact(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.zip"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr(
            "ARGUS/app/unsafe.py",
            'email = "private.person@private.example"\n',
        )

    findings = scan_artifact(artifact, load_config(CONFIG))

    assert any(finding.category == "email" for finding in findings)


def test_scanner_reads_printable_strings_from_frozen_artifact(tmp_path: Path) -> None:
    artifact = tmp_path / "ARGUS.exe"
    artifact.write_bytes(b"binary-header\x00private.person@private.example\x00tail")

    findings = scan_artifact(artifact, load_config(CONFIG))

    assert any(finding.category == "email" for finding in findings)


def test_packaged_artifact_scans_extension_and_packaging_text_members(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "artifact.zip"
    with zipfile.ZipFile(artifact, "w") as archive:
        archive.writestr(
            "ARGUS/extension/options.ts",
            'const contact = "private.person@private.example";\n',
        )
        archive.writestr(
            "ARGUS/packaging/release.ini",
            'contact = "private.person@private.example"\n',
        )

    findings = scan_artifact(artifact, load_config(CONFIG))

    assert sum(finding.category == "email" for finding in findings) == 2
