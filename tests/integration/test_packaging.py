from __future__ import annotations

import hashlib
import subprocess
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.package_release import _issue_release_capability, build_release, source_snapshot
from scripts.verify import _command_hash, _plan_digest, _step_plan


def _write(root: Path, relative: str, content: str = "fixture") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _fixture_repository(root: Path) -> None:
    for relative in (
        "app/main.py",
        "extension/manifest.json",
        "samples/trackr_import_template.csv",
        "scripts/setup.sh",
        "tests/unit/test_smoke.py",
        "docs/superpowers/specs/design.md",
        ".env.example",
        "pyproject.toml",
        "requirements.txt",
        "README.md",
        "SECURITY.md",
        "OPERATIONS.md",
        "VERIFICATION.md",
        "tests/e2e/test_adversarial_flows.py",
        "tests/e2e/test_application_flows.py",
        "tests/e2e/test_application_navigator_journeys.py",
        "tests/e2e/test_apply_ui.py",
        "tests/e2e/test_dashboard.py",
        "tests/e2e/test_lab_adapter_variants.py",
        "tests/e2e/test_local_portal_end_to_end.py",
        "tests/e2e/test_static_legal_boundaries_campaign.py",
        "tests/e2e/test_submission_unknown.py",
    ):
        _write(root, relative)
    _write(root, "pyproject.toml", '[project]\nname = "fixture"\nversion = "0.1.0"\n')
    project_root = Path(__file__).resolve().parents[2]
    _write(root, "packaging/privacy_scan_config.json", (project_root / "packaging/privacy_scan_config.json").read_text(encoding="utf-8"))
    _write(root, "scripts/privacy_scan.py", (project_root / "scripts/privacy_scan.py").read_text(encoding="utf-8"))


def _git(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.test"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "ARGUS Test"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True)


def _evidence(root: Path) -> dict[str, object]:
    evidence_root = root / "release-evidence"
    evidence_root.mkdir()
    names = ["unit", "integration", "tests/e2e/test_adversarial_flows.py", "tests/e2e/test_application_flows.py", "tests/e2e/test_application_navigator_journeys.py", "tests/e2e/test_apply_ui.py", "tests/e2e/test_dashboard.py", "tests/e2e/test_lab_adapter_variants.py", "tests/e2e/test_local_portal_end_to_end.py", "tests/e2e/test_static_legal_boundaries_campaign.py", "tests/e2e/test_submission_unknown.py", "compile", "cli_help", "cli_safe_smoke", "audit_fresh_temp", "migration_fresh_temp", "privacy_source"]
    logs, steps = [], []
    nonce = "packaging-fixture-provenance-20260825"
    interpreter = Path(sys.executable).resolve()
    plan = _step_plan(root, str(interpreter), None)
    plan_sha256 = _plan_digest(plan)
    command_hashes_by_name = {item["name"]: [_command_hash(command) for command in (item.get("commands") or [item.get("command")])] for item in plan}
    for index, name in enumerate(names, 1):
        hashes = command_hashes_by_name[name]
        command_lines = "".join(f"command_sha256={value}\n" for value in hashes)
        path = evidence_root / f"{index:02d}.log"; path.write_text(f"started_at=2026-08-25T00:00:00.000Z\nstep={name}\nprovenance_nonce={nonce}\nplan_sha256={plan_sha256}\ncommand_count={len(hashes)}\n{command_lines}finished_at=2026-08-25T00:00:01.000Z\nexit_code=0\n", encoding="utf-8")
        entry = {"path": path.relative_to(root).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size": path.stat().st_size}
        logs.append(entry); steps.append({"name": name, "status": "passed", "exit_code": 0, "log": entry, "provenance_nonce": nonce, "plan_sha256": plan_sha256, "command_hashes": hashes})
    started = datetime.now(timezone.utc)
    return {"schema_version": 2, "status": "passed", "started_at": started.isoformat().replace("+00:00", "Z"), "finished_at": (started + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"), "live_network": False, "python": {"path": str(interpreter), "sha256": hashlib.sha256(interpreter.read_bytes()).hexdigest(), "version": sys.version.split()[0], "implementation": "cpython", "executable": str(interpreter), "real": True}, "source": source_snapshot(root), "steps": steps, "logs": logs, "provenance": {"schema_version": 1, "run_nonce": nonce, "plan_sha256": plan_sha256, "executed": True}}


def test_release_archive_is_allowlisted_and_contains_no_runtime_secrets(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    output = root / "dist"
    _fixture_repository(root)
    _git(root)
    _write(root, ".env", "ARGUS_SECRET=leak")
    _write(root, "data/argus.db", "private")
    _write(root, "data/secret.key", "private")
    _write(root, "artifacts/verification/trace.zip", "private")
    _write(root, "app/__pycache__/main.pyc", "cache")
    _write(root, "tests/.pytest_cache/state", "cache")
    _write(root, "dist/old.zip", "stale")
    _write(root, "random-notes.txt", "not allowlisted")
    outside = _write(tmp_path, "outside.txt", "outside")
    unsafe_link_created = False
    try:
        (root / "app" / "unsafe-link").symlink_to(outside)
        unsafe_link_created = True
    except OSError:
        # The deterministic reparse probe in Task8b covers this branch when
        # Windows symlink privileges are unavailable.
        pass

    result = build_release(
        root=root,
        output_dir=output,
        version="0.1.0",
        test_evidence=(evidence := _evidence(root)),
        verification_capability=_issue_release_capability(evidence, root=root),
    )

    assert result.archive == output / "argus-0.1.0.zip"
    assert result.checksum_file == output / "argus-0.1.0.zip.sha256"
    expected_digest = hashlib.sha256(result.archive.read_bytes()).hexdigest()
    assert result.sha256 == expected_digest
    assert result.checksum_file.read_text(encoding="utf-8") == (
        f"{expected_digest}  argus-0.1.0.zip\n"
    )

    with zipfile.ZipFile(result.archive) as archive:
        names = set(archive.namelist())

    prefix = "ARGUS-0.1.0/"
    assert prefix + "app/main.py" in names
    assert prefix + "extension/manifest.json" in names
    assert prefix + "README.md" in names
    assert prefix + "VERIFICATION.md" in names
    assert not any(".env" == name.removeprefix(prefix) for name in names)
    assert not any("data/" in name for name in names)
    assert not any("artifacts/" in name for name in names)
    assert not any("__pycache__" in name or ".pytest_cache" in name for name in names)
    assert not unsafe_link_created or not any("unsafe-link" in name for name in names)
    assert not any("random-notes.txt" in name for name in names)
    assert all(".." not in Path(name).parts for name in names)


def test_release_excludes_stale_generated_egg_info_metadata(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    output = root / "dist"
    _fixture_repository(root)
    _git(root)
    # A local setuptools build can leave an old PKG-INFO beside the source.
    # It is not a release input, so the archive cannot expose a contradictory
    # version even when the generated metadata is stale.
    _write(root, "argus_applications.egg-info/PKG-INFO", "Version: 0.1.0\n")
    evidence = _evidence(root)

    result = build_release(
        root=root,
        output_dir=output,
        version="0.1.0",
        test_evidence=evidence,
        verification_capability=_issue_release_capability(evidence, root=root),
    )

    with zipfile.ZipFile(result.archive) as archive:
        names = {name.casefold() for name in archive.namelist()}
    assert not any("egg-info" in name or name.endswith("pkg-info") for name in names)


def test_release_requires_core_project_files(tmp_path: Path) -> None:
    root = tmp_path / "incomplete"
    root.mkdir()

    with pytest.raises(ValueError, match="missing required release files"):
        build_release(root=root, output_dir=root / "dist", version="0.1.0")


def test_release_excludes_case_variants_of_forbidden_names_and_directories(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    output = root / "dist"
    _fixture_repository(root)
    _git(root)
    for relative in (
        "app/.ENV",
        "app/.ENV.LOCAL",
        "app/.ENV.EXAMPLE",
        "app/SECRET.KEY",
        "app/API_TOKEN.TXT",
        "app/ARGUS.DB",
        "app/.GIT/config",
        "app/.WORKTREES/state",
        "app/.VENV/bin/python",
        "app/__PYcAcHe__/module.pyc",
        "app/.PyTeSt_CaChE/state",
        "app/DATA/argus.db",
        "app/ArTiFaCtS/trace.txt",
        "app/DIST/old.zip",
        "app/TEST-RESULTS/result.txt",
        "app/PLAYWRIGHT-REPORT/report.html",
    ):
        _write(root, relative, "private")

    result = build_release(
        root=root,
        output_dir=output,
        version="0.1.0",
        test_evidence=(evidence := _evidence(root)),
        verification_capability=_issue_release_capability(evidence, root=root),
    )

    with zipfile.ZipFile(result.archive) as archive:
        members = {
            name.removeprefix("ARGUS-0.1.0/").casefold()
            for name in archive.namelist()
        }

    assert "app/.env" not in members
    assert "app/.env.local" not in members
    assert "app/.env.example" not in members
    assert "app/secret.key" not in members
    assert "app/api_token.txt" not in members
    assert "app/argus.db" not in members
    for directory in (
        ".git",
        ".worktrees",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        "data",
        "artifacts",
        "dist",
        "test-results",
        "playwright-report",
    ):
        assert not any(
            part == directory
            for member in members
            for part in Path(member).parts
        )
