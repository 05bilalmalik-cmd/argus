from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.package_release import (
    _issue_release_capability,
    _executable_metadata,
    _is_safe_release_file,
    _iter_release_files,
    _safe_evidence_for_manifest,
    _scan_archive_all_members,
    _validate_test_evidence,
    _validate_archive_members,
    build_release,
    source_snapshot,
)
from scripts.verify import _command_hash, _e2e_files, _plan_digest, _run_packaged_smoke, _safe_log_name, _step_plan, _validate_output_path, _validate_python_bin, safe_environment


def _write(root: Path, relative: str, content: str = "fixture") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _fixture_repository(root: Path) -> None:
    for relative in (
        "app/main.py",
        "extension/manifest.json",
        "scripts/setup.sh",
        "tests/unit/test_smoke.py",
        "docs/design.md",
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
        "tests/e2e/test_submission_unknown.py",
    ):
        _write(root, relative)
    _write(root, "pyproject.toml", '[project]\nname = "fixture"\nversion = "0.2.0"\n')
    project_root = Path(__file__).resolve().parents[2]
    _write(root, "packaging/privacy_scan_config.json", (project_root / "packaging/privacy_scan_config.json").read_text(encoding="utf-8"))
    _write(root, "scripts/privacy_scan.py", (project_root / "scripts/privacy_scan.py").read_text(encoding="utf-8"))


def _git_fixture(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "argus-test@example.test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "config", "user.name", "ARGUS Test"],
        check=True,
    )
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-qm", "fixture"],
        check=True,
    )


def _evidence(root: Path, log: Path | None = None) -> dict[str, object]:
    evidence_root = root / "release-evidence"
    evidence_root.mkdir(parents=True, exist_ok=True)
    required = [
        "unit", "integration", "tests/e2e/test_adversarial_flows.py",
        "tests/e2e/test_application_flows.py", "tests/e2e/test_application_navigator_journeys.py",
        "tests/e2e/test_apply_ui.py", "tests/e2e/test_dashboard.py",
        "tests/e2e/test_lab_adapter_variants.py", "tests/e2e/test_submission_unknown.py",
        "compile", "cli_help", "cli_safe_smoke", "audit_fresh_temp", "migration_fresh_temp", "privacy_source",
    ]
    logs = []
    steps = []
    nonce = "fixture-provenance-nonce-20260825"
    interpreter = Path(sys.executable).resolve()
    plan = _step_plan(root, str(interpreter), None)
    plan_sha256 = _plan_digest(plan)
    command_hashes_by_name = {item["name"]: [_command_hash(command) for command in (item.get("commands") or [item.get("command")])] for item in plan}
    for index, name in enumerate(required, start=1):
        command_lines = "".join(f"command_sha256={value}\n" for value in command_hashes_by_name[name])
        path = evidence_root / f"step-{index:02d}.log"
        path.write_text(
            f"started_at=2026-08-25T00:00:00.000Z\nstep={name}\n"
            f"provenance_nonce={nonce}\nplan_sha256={plan_sha256}\n"
            f"command_count={len(command_hashes_by_name[name])}\n"
            f"{command_lines}finished_at=2026-08-25T00:00:01.000Z\nexit_code=0\n",
            encoding="utf-8",
        )
        entry = {"path": path.relative_to(root).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size": path.stat().st_size}
        logs.append(entry)
        steps.append({"name": name, "status": "passed", "exit_code": 0, "log": entry, "provenance_nonce": nonce, "plan_sha256": plan_sha256, "command_hashes": command_hashes_by_name[name]})
    started = datetime.now(timezone.utc)
    finished = started + timedelta(seconds=1)
    return {
        "schema_version": 2,
        "status": "passed",
        "source": source_snapshot(root),
        "started_at": started.isoformat().replace("+00:00", "Z"),
        "finished_at": finished.isoformat().replace("+00:00", "Z"),
        "live_network": False,
        "python": {"path": str(interpreter), "sha256": hashlib.sha256(interpreter.read_bytes()).hexdigest(), "version": sys.version.split()[0], "implementation": "cpython", "executable": str(interpreter), "real": True},
        "steps": steps,
        "logs": logs,
        "provenance": {"schema_version": 1, "run_nonce": nonce, "plan_sha256": plan_sha256, "executed": True},
    }


def _capability(root: Path, evidence: dict[str, object]):
    return _issue_release_capability(evidence, root=root)


def test_git_release_requires_machine_bound_test_evidence(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _fixture_repository(root)
    _git_fixture(root)

    with pytest.raises(ValueError, match="test evidence"):
        build_release(root=root, output_dir=root / "dist", version="0.2.0")


def test_standalone_evidence_json_cannot_authorize_packaging(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    _fixture_repository(repo_root)
    _git_fixture(repo_root)
    evidence = _evidence(repo_root)
    # The evidence is complete and machine-bound, but no in-memory capability
    # crossed the verifier/package boundary.
    with pytest.raises(ValueError, match="standalone|same invocation"):
        build_release(
            root=repo_root,
            output_dir=repo_root / "dist",
            version="0.2.0",
            test_evidence=json.loads(json.dumps(evidence)),
        )


def test_capability_is_bound_to_unchanged_evidence(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _fixture_repository(root)
    _git_fixture(root)
    evidence = _evidence(root)
    capability = _capability(root, evidence)
    evidence["status"] = "failed"
    with pytest.raises(ValueError, match="capability|evidence|status"):
        build_release(
            root=root,
            output_dir=root / "dist",
            version="0.2.0",
            test_evidence=evidence,
            verification_capability=capability,
        )


def test_release_capability_rejects_byte_identical_checkout_clone(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _fixture_repository(root)
    _git_fixture(root)
    evidence = _evidence(root)
    capability = _capability(root, evidence)

    clone = tmp_path / "byte-identical-clone"
    shutil.copytree(root, clone)
    # The evidence and source bytes are intentionally identical.  The
    # process-local capability must still refuse a different canonical root.
    with pytest.raises(ValueError, match="capability|root|identity"):
        build_release(
            root=clone,
            output_dir=clone / "dist",
            version="0.2.0",
            test_evidence=evidence,
            verification_capability=capability,
        )


def test_non_git_release_rejects_even_a_passed_fixture(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _fixture_repository(root)
    with pytest.raises(ValueError, match="Git repository|standalone"):
        build_release(root=root, output_dir=root / "dist", version="0.2.0", test_evidence={"schema_version": 2, "status": "passed"})


def test_git_release_rejects_evidence_after_source_changes(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _fixture_repository(root)
    _git_fixture(root)
    evidence = _evidence(root)
    _write(root, "app/main.py", "changed")

    with pytest.raises(ValueError, match="stale|source"):
        build_release(
            root=root,
            output_dir=root / "dist",
            version="0.2.0",
            test_evidence=evidence,
            verification_capability=_capability(root, evidence),
        )


def test_git_release_rejects_stale_test_log_hash(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _fixture_repository(root)
    _git_fixture(root)
    evidence = _evidence(root)
    log = root / "release-evidence" / "step-01.log"
    log.write_text("tampered\n", encoding="utf-8")

    with pytest.raises(ValueError, match="log|evidence"):
        build_release(
            root=root,
            output_dir=root / "dist",
            version="0.2.0",
            test_evidence=evidence,
            verification_capability=_capability(root, evidence),
        )


def test_source_binding_ignores_timestamped_evidence_outputs(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _fixture_repository(root)
    _git_fixture(root)
    before = source_snapshot(root)
    _write(root, "release-evidence/20260824T220000Z-unit.log", "passed\n")
    _write(root, "release-evidence/verification.json", "{}\n")

    assert source_snapshot(root) == before


def test_release_manifest_records_supplied_executable_and_signature(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _fixture_repository(root)
    _git_fixture(root)
    executable = _write(tmp_path, "ARGUS.exe", "synthetic executable")
    signature = _write(tmp_path, "ARGUS.exe.sig", "synthetic signature")
    with pytest.raises(ValueError, match="PE|signature"):
        evidence = _evidence(root)
        build_release(root=root, output_dir=root / "dist", version="0.2.0", test_evidence=evidence, verification_capability=_capability(root, evidence), executable=executable, executable_version="0.2.0.0", executable_signature=signature)


def test_release_excludes_candidate_documents_and_private_browser_state(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _fixture_repository(root)
    _git_fixture(root)
    _write(root, "docs/candidate_profile.json", "private")
    _write(root, "docs/resume.pdf", "private")
    _write(root, "app/browser-profiles/cookies.json", "private")
    result = build_release(
        root=root,
        output_dir=root / "dist",
        version="0.2.0",
        test_evidence=(evidence := _evidence(root)),
        verification_capability=_capability(root, evidence),
    )

    with zipfile.ZipFile(result.archive) as archive:
        members = {name.casefold() for name in archive.namelist()}
    assert not any("candidate_profile" in name or "resume.pdf" in name for name in members)
    assert not any("browser-profiles" in name or "cookies" in name for name in members)


def test_verify_dry_run_emits_safe_machine_manifest(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    manifest = tmp_path / "verification.json"
    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts" / "verify.py"),
            "--root",
            str(root),
            "--dry-run",
            "--manifest",
            str(manifest),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["status"] == "dry-run"
    names = {step["name"] for step in payload["steps"]}
    assert {"unit", "integration", "compile", "cli_safe_smoke", "audit_fresh_temp", "migration_fresh_temp", "privacy_source"} <= names
    assert {
        "tests/e2e/test_application_navigator_journeys.py",
        "tests/e2e/test_apply_ui.py",
        "tests/e2e/test_submission_unknown.py",
    } <= {step["name"] for step in payload["steps"] if step["kind"] == "e2e"}
    assert payload["environment"]["ARGUS_ENABLE_LIVE_SUBMIT"] == "false"
    assert payload["environment"]["ARGUS_ENABLE_TRACKR_LIVE"] == "false"
    assert payload["environment"]["ARGUS_AUTOMATION_MODE"] == "OFF"


def test_verification_environment_discards_inherited_live_and_local_data(monkeypatch) -> None:
    monkeypatch.setenv("ARGUS_ENABLE_LIVE_SUBMIT", "true")
    monkeypatch.setenv("ARGUS_ENABLE_TRACKR_LIVE", "true")
    monkeypatch.setenv("ARGUS_DATA_DIR", "C:/private/live-data")
    monkeypatch.setenv("HTTPS_PROXY", "https://proxy.invalid")

    environment = safe_environment(Path(__file__).resolve().parents[2])

    assert environment["ARGUS_ENABLE_LIVE_SUBMIT"] == "false"
    assert environment["ARGUS_ENABLE_TRACKR_LIVE"] == "false"
    assert environment["ARGUS_AUTOMATION_MODE"] == "OFF"
    assert "ARGUS_DATA_DIR" not in environment
    assert "HTTPS_PROXY" not in environment


def test_verification_environment_scrubs_generic_live_and_secret_names(monkeypatch) -> None:
    for name in ("TRACKR_COOKIE", "SUBMISSION_TOKEN", "LIVE_SUBMIT", "SSLKEYLOGFILE", "HTTP_PROXY", "BROWSER_PROFILE"):
        monkeypatch.setenv(name, "private")
    environment = safe_environment(Path(__file__).resolve().parents[2])
    assert all(name not in environment for name in ("TRACKR_COOKIE", "SUBMISSION_TOKEN", "LIVE_SUBMIT", "SSLKEYLOGFILE", "HTTP_PROXY", "BROWSER_PROFILE"))


def test_verification_environment_does_not_inherit_generic_credentials_or_data_paths(monkeypatch) -> None:
    for name in ("AUTHORIZATION", "CREDENTIALS", "DATABASE_URL", "DATA_DIR", "IMAP_PASSWORD", "SMTP_PASSWORD", "ATS_USERNAME"):
        monkeypatch.setenv(name, "private")
    environment = safe_environment(Path(__file__).resolve().parents[2])
    assert all(name not in environment for name in ("AUTHORIZATION", "CREDENTIALS", "DATABASE_URL", "DATA_DIR", "IMAP_PASSWORD", "SMTP_PASSWORD", "ATS_USERNAME"))


def test_evidence_manifest_projection_drops_arbitrary_private_values() -> None:
    projected = _safe_evidence_for_manifest({"schema_version": 2, "status": "passed", "secret_note": "Ada Lovelace private.person@example.com", "stdout": "token"})
    assert "secret_note" not in projected
    assert "stdout" not in projected


def test_source_snapshot_binds_untracked_dirty_tree_file(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _fixture_repository(root)
    _git_fixture(root)
    before = source_snapshot(root)
    _write(root, "notes.md", "new dirty source\n")
    after = source_snapshot(root)
    assert after["git"]["dirty_tree_inventory_sha256"] != before["git"]["dirty_tree_inventory_sha256"]
    assert any(item["path"] == "notes.md" for item in after["git"]["dirty_tree_inventory"])


def test_source_snapshot_binds_ignored_source_override(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _fixture_repository(root)
    _write(root, ".gitignore", "app/local_override.py\n")
    _write(root, "app/local_override.py", "override = True\n")
    _git_fixture(root)
    before = source_snapshot(root)
    _write(root, "app/local_override.py", "override = False\n")
    after = source_snapshot(root)
    assert after["git"]["dirty_tree_inventory_sha256"] != before["git"]["dirty_tree_inventory_sha256"]
    assert any(item["path"] == "app/local_override.py" for item in after["git"]["dirty_tree_inventory"])


def test_e2e_discovery_rejects_a_junction_at_the_base_directory(tmp_path: Path) -> None:
    target = tmp_path / "outside-e2e"
    target.mkdir()
    for index in range(7):
        _write(target, f"test_{index}.py")
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    link = root / "tests" / "e2e"
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
    if result.returncode != 0:
        pytest.skip("junction creation unavailable")
    with pytest.raises(ValueError, match="link/reparse"):
        _e2e_files(root)


def test_fake_python_command_shim_is_not_a_real_interpreter(tmp_path: Path) -> None:
    shim = tmp_path / "fake-python.cmd"
    shim.write_text("@echo 9.9.9\n", encoding="utf-8")
    with pytest.raises(ValueError, match="CPython|executable|interpreter"):
        _validate_python_bin(str(shim), root=tmp_path, environment=safe_environment(tmp_path))


def test_release_rejects_key_and_old_release_path_variants(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    for relative in ("app/key.txt", "app/keys.txt", "app/old_release.py", "app/old/thing.py"):
        path = _write(root, relative)
        assert not _is_safe_release_file(path, root)


def test_required_file_that_privacy_excludes_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _fixture_repository(root)
    _git_fixture(root)
    _write(root, "VERIFICATION.md", "private C:/Users/example/backup/secret.key\n")
    with pytest.raises(ValueError, match="required release files|excluded"):
        evidence = _evidence(root)
        build_release(root=root, output_dir=root / "dist", version="0.2.0", test_evidence=evidence, verification_capability=_capability(root, evidence))


def test_unsigned_pe_requires_explicit_acknowledgement(tmp_path: Path, monkeypatch) -> None:
    executable = tmp_path / "ARGUS.exe"
    executable.write_bytes(b"MZ" + b"\x00" * 128)
    monkeypatch.setattr("scripts.package_release._inspect_pe_version", lambda _path: "0.2.0.0")
    with pytest.raises(ValueError, match="unsigned|acknowledge"):
        _executable_metadata(executable, version="0.2.0.0", signature=None, allow_unsigned=False)
    metadata = _executable_metadata(executable, version="0.2.0.0", signature=None, allow_unsigned=True)
    assert metadata["signature"] == {"state": "unsigned", "verified": False, "acknowledged": True}


def test_source_snapshot_is_rechecked_after_evidence_validation(tmp_path: Path, monkeypatch) -> None:
    import scripts.package_release as release_tooling

    root = tmp_path / "repo"
    _fixture_repository(root)
    _git_fixture(root)
    evidence = _evidence(root)
    captured = source_snapshot(root)
    changed = dict(captured)
    changed["binding_sha256"] = "f" * 64
    snapshots = iter((captured, changed))
    monkeypatch.setattr(release_tooling, "source_snapshot", lambda _root: next(snapshots))
    with pytest.raises(ValueError, match="source changed|immutable"):
        build_release(root=root, output_dir=root / "dist", version="0.2.0", test_evidence=evidence, verification_capability=_capability(root, evidence))


def test_log_names_are_collision_proof_for_confusable_step_names() -> None:
    left = _safe_log_name("20260825T000000Z", 1, "tests/e2e/test_a.b.py")
    right = _safe_log_name("20260825T000000Z", 1, "tests/e2e/test_a_b.py")
    assert left != right


def test_manifest_and_evidence_outputs_must_be_contained_or_temp_scoped(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    outside = Path("C:/outside/verification.json")
    with pytest.raises(ValueError, match="output|contained"):
        _validate_output_path(outside, root, kind="manifest")


def test_archive_member_limits_reject_duplicate_and_oversized_members(tmp_path: Path) -> None:
    archive_path = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("ARGUS-0.2.0/app/main.py", "one")
        archive.writestr("ARGUS-0.2.0/app/main.py", "two")
    with pytest.raises(ValueError, match="duplicate"):
        _validate_archive_members(archive_path)


def test_release_output_must_be_approved_in_root_and_not_a_junction(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _fixture_repository(root)
    _git_fixture(root)
    evidence = _evidence(root)
    with pytest.raises(ValueError, match="approved release root|contained"):
        build_release(
            root=root,
            output_dir=tmp_path / "outside",
            version="0.2.0",
            test_evidence=evidence,
            verification_capability=_capability(root, evidence),
        )
    outside = tmp_path / "outside-target"
    outside.mkdir()
    junction = root / "dist"
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(outside)], capture_output=True, text=True)
    if result.returncode == 0:
        with pytest.raises(ValueError, match="link|reparse"):
            build_release(
                root=root,
                output_dir=junction,
                version="0.2.0",
                test_evidence=evidence,
                verification_capability=_capability(root, evidence),
            )


def test_archive_aborts_if_checkout_changes_during_archiving(tmp_path: Path, monkeypatch) -> None:
    import scripts.package_release as release_tooling

    repo_root = tmp_path / "repo"
    _fixture_repository(repo_root)
    _git_fixture(repo_root)
    evidence = _evidence(repo_root)
    original = release_tooling._create_archive
    changed = {"done": False}

    def mutate_after_stage(path, files, *, root: Path, prefix):
        result = original(path, files, root=root, prefix=prefix)
        if not changed["done"]:
            changed["done"] = True
            (repo_root / "app" / "main.py").write_text("changed during archive", encoding="utf-8")
        return result

    monkeypatch.setattr(release_tooling, "_create_archive", mutate_after_stage)
    with pytest.raises(ValueError, match="source changed|archive"):
        build_release(
            root=repo_root,
            output_dir=repo_root / "dist",
            version="0.2.0",
            test_evidence=evidence,
            verification_capability=_capability(repo_root, evidence),
        )


def test_release_rechecks_checkout_after_privacy_scan_before_publish(tmp_path: Path, monkeypatch) -> None:
    import scripts.package_release as release_tooling

    root = tmp_path / "repo"
    _fixture_repository(root)
    _git_fixture(root)
    evidence = _evidence(root)
    original_scan = release_tooling._scan_privacy

    def mutate_during_privacy(stage_root: Path, archive: Path) -> None:
        original_scan(stage_root, archive)
        (root / "app" / "main.py").write_text("changed during privacy scan", encoding="utf-8")

    monkeypatch.setattr(release_tooling, "_scan_privacy", mutate_during_privacy)
    with pytest.raises(ValueError, match="source changed|privacy"):
        build_release(
            root=root,
            output_dir=root / "dist",
            version="0.2.0",
            test_evidence=evidence,
            verification_capability=_capability(root, evidence),
        )
    assert not (root / "dist" / "argus-0.2.0.zip").exists()
    assert not (root / "dist" / "argus-0.2.0.manifest.json").exists()


def test_release_rechecks_checkout_after_manifest_write_before_return(tmp_path: Path, monkeypatch) -> None:
    import scripts.package_release as release_tooling

    root = tmp_path / "repo"
    _fixture_repository(root)
    _git_fixture(root)
    evidence = _evidence(root)
    original_write = release_tooling._write_manifest

    def mutate_after_manifest(path: Path, payload: dict[str, object]) -> None:
        original_write(path, payload)
        (root / "app" / "main.py").write_text("changed during manifest publication", encoding="utf-8")

    monkeypatch.setattr(release_tooling, "_write_manifest", mutate_after_manifest)
    with pytest.raises(ValueError, match="source changed|manifest"):
        build_release(
            root=root,
            output_dir=root / "dist",
            version="0.2.0",
            test_evidence=evidence,
            verification_capability=_capability(root, evidence),
        )
    assert not (root / "dist" / "argus-0.2.0.zip").exists()
    assert not (root / "dist" / "argus-0.2.0.manifest.json").exists()


def test_supplied_executable_is_revalidated_from_frozen_stage_bytes(tmp_path: Path, monkeypatch) -> None:
    import scripts.package_release as release_tooling

    root = tmp_path / "repo"
    _fixture_repository(root)
    _git_fixture(root)
    evidence = _evidence(root)
    executable = tmp_path / "ARGUS.exe"
    original_bytes = b"MZ" + b"frozen-executable" * 16
    executable.write_bytes(original_bytes)
    monkeypatch.setattr(release_tooling, "_inspect_pe_version", lambda _path: "0.2.0.0")
    original_metadata = release_tooling._executable_metadata
    observed: dict[str, Path] = {}

    def inspect_staged(path: Path | None, **kwargs: object) -> dict[str, object] | None:
        assert path is not None
        observed["path"] = path
        metadata = original_metadata(path, **kwargs)
        # The caller can replace the external input after it has been frozen;
        # the manifest must continue to describe the prior staged bytes.
        executable.write_bytes(b"MZ" + b"mutated-after-freeze")
        return metadata

    monkeypatch.setattr(release_tooling, "_executable_metadata", inspect_staged)
    result = build_release(
        root=root,
        output_dir=root / "dist",
        version="0.2.0",
        test_evidence=evidence,
        verification_capability=_capability(root, evidence),
        executable=executable,
        executable_version="0.2.0.0",
        allow_unsigned_executable=True,
    )
    assert observed["path"] != executable
    assert "__inputs__" in observed["path"].parts
    manifest = json.loads(result.manifest_file.read_text(encoding="utf-8"))
    assert manifest["executable"]["sha256"] == hashlib.sha256(original_bytes).hexdigest()
    assert manifest["executable"]["size"] == len(original_bytes)


def test_archive_privacy_scan_reads_utf16_member_without_echoing_content(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _fixture_repository(root)
    _write(root, "packaging/privacy_scan_config.json", (Path(__file__).resolve().parents[2] / "packaging/privacy_scan_config.json").read_text(encoding="utf-8"))
    _write(root, "scripts/privacy_scan.py", (Path(__file__).resolve().parents[2] / "scripts/privacy_scan.py").read_text(encoding="utf-8"))
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("ARGUS-0.2.0/docs/public.md", 'email = "private.person@private.example"'.encode("utf-16-le"))
    with pytest.raises(ValueError, match="privacy scan"):
        _scan_archive_all_members(root, archive)


def test_release_rejects_evidence_with_planned_or_missing_required_step(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _fixture_repository(root)
    _git_fixture(root)
    evidence = _evidence(root)
    evidence["steps"] = list(evidence["steps"][:-1])
    evidence["logs"] = list(evidence["logs"][:-1])
    with pytest.raises(ValueError, match="exactly|required|step"):
        build_release(root=root, output_dir=root / "dist", version="0.2.0", test_evidence=evidence, verification_capability=_capability(root, evidence))


def test_release_rejects_fabricated_all_pass_logs_without_verifier_provenance(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _fixture_repository(root)
    _git_fixture(root)
    evidence = _evidence(root)
    evidence.pop("provenance")
    with pytest.raises(ValueError, match="provenance|nonce|authentic"):
        _validate_test_evidence(root, evidence)


def test_release_tree_rejects_reparse_probe_without_symlink_privilege(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _fixture_repository(root)
    probe = root / "app" / "junction-probe"
    probe.mkdir()
    real = __import__("scripts.package_release", fromlist=["_has_reparse_point"])
    original = real._has_reparse_point
    monkeypatch.setattr(real, "_has_reparse_point", lambda path: path == probe or original(path))
    with pytest.raises(ValueError, match="link/reparse"):
        list(_iter_release_files(root))


def test_packaged_smoke_launches_and_checks_loopback_health_then_terminates(tmp_path: Path) -> None:
    executable = tmp_path / "smoke.py"
    executable.write_text(
        "import http.server, os\n"
        "class H(http.server.BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200 if self.path == '/healthz' else 404); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(b'{\"status\":\"ok\",\"service\":\"ARGUS\",\"version\":\"test\",\"automation_mode\":\"OFF\",\"live_submit\":false,\"trackr_live\":false}' if self.path == '/healthz' else b'{}')\n"
        "    def log_message(self, *args): pass\n"
        "http.server.ThreadingHTTPServer(('127.0.0.1', int(os.environ['ARGUS_PORT'])), H).serve_forever()\n",
        encoding="utf-8",
    )
    log = tmp_path / "smoke.log"
    with log.open("w", encoding="utf-8") as handle:
        code = _run_packaged_smoke(executable, root=tmp_path, python_bin=sys.executable, environment=safe_environment(tmp_path), output=handle)
    assert code == 0


def _pid_exists(pid: int) -> bool:
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0 and str(pid) in result.stdout
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def test_safe_environment_creates_separate_windows_profile_roots(tmp_path: Path) -> None:
    environment = safe_environment(tmp_path)
    profile = Path(environment["USERPROFILE"])
    local = Path(environment["LOCALAPPDATA"])
    roaming = Path(environment["APPDATA"])

    assert local == profile / "AppData" / "Local"
    assert roaming == profile / "AppData" / "Roaming"
    assert local.is_dir()
    assert roaming.is_dir()
    assert local != profile
    assert roaming != profile


def test_safe_environment_uses_a_fresh_profile_for_each_verifier_run(tmp_path: Path) -> None:
    first = Path(safe_environment(tmp_path)["USERPROFILE"])
    second = Path(safe_environment(tmp_path)["USERPROFILE"])
    try:
        assert first != second
        assert first.is_dir()
        assert second.is_dir()
    finally:
        import shutil

        shutil.rmtree(first, ignore_errors=True)
        shutil.rmtree(second, ignore_errors=True)


def test_packaged_smoke_terminates_descendant_and_frees_port(tmp_path: Path) -> None:
    child_pid_file = tmp_path / "child.pid"
    child_code = (
        "import http.server, os\n"
        "class H(http.server.BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200 if self.path == '/healthz' else 404)\n"
        "        self.send_header('Content-Type', 'application/json')\n"
        "        self.end_headers()\n"
        "        self.wfile.write(b'{\"status\":\"ok\",\"service\":\"ARGUS\",\"version\":\"test\",\"automation_mode\":\"OFF\",\"live_submit\":false,\"trackr_live\":false}' if self.path == '/healthz' else b'{}')\n"
        "    def log_message(self, *args): pass\n"
        "http.server.ThreadingHTTPServer(('127.0.0.1', int(os.environ['ARGUS_PORT'])), H).serve_forever()\n"
    )
    executable = tmp_path / "tree-smoke.py"
    executable.write_text(
        "import os, subprocess, sys, tempfile, time\n"
        "os.chdir(tempfile.gettempdir())\n"
        f"child_code = {child_code!r}\n"
        "child = subprocess.Popen([sys.executable, '-c', child_code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=os.environ.copy())\n"
        f"open({str(child_pid_file)!r}, 'w', encoding='utf-8').write(str(child.pid))\n"
        "while True: time.sleep(1)\n",
        encoding="utf-8",
    )
    environment = safe_environment(tmp_path)
    log = tmp_path / "tree-smoke.log"
    child_was_alive_after_smoke = True
    with log.open("w", encoding="utf-8") as handle:
        code = _run_packaged_smoke(
            executable,
            root=tmp_path,
            python_bin=sys.executable,
            environment=environment,
            output=handle,
        )
    assert code == 0, log.read_text(encoding="utf-8")
    child_pid = int(child_pid_file.read_text(encoding="utf-8"))
    child_was_alive_after_smoke = _pid_exists(child_pid)
    try:
        assert child_was_alive_after_smoke is False
    finally:
        if child_was_alive_after_smoke:
            try:
                os.kill(child_pid, 15)
            except OSError:
                pass


def test_packaged_smoke_runs_twice_with_fresh_loopback_port(tmp_path: Path) -> None:
    body = b'{"status":"ok","service":"ARGUS","version":"test","automation_mode":"OFF","live_submit":false,"trackr_live":false}'
    first_code, first_log = _run_health_fixture(tmp_path / "first", body)
    second_code, second_log = _run_health_fixture(tmp_path / "second", body)

    assert first_code == 0, first_log
    assert second_code == 0, second_log


def _run_health_fixture(
    tmp_path: Path,
    body: bytes,
    *,
    content_type: str | None = "application/json",
    status: int = 200,
    redirect_location: str | None = None,
    expected_version: str | None = None,
) -> tuple[int, str]:
    """Run one disposable loopback health response and stop its server."""

    tmp_path.mkdir(parents=True, exist_ok=True)
    headers = ""
    if content_type is not None:
        headers += f"        self.send_header('Content-Type', {content_type!r})\n"
    if redirect_location is not None:
        headers += f"        self.send_header('Location', {redirect_location!r})\n"
    executable = tmp_path / "health-fixture.py"
    executable.write_text(
        "import http.server, os, threading\n"
        "class H(http.server.BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        f"        self.send_response({status})\n"
        f"{headers}"
        "        self.end_headers()\n"
        f"        self.wfile.write({body!r})\n"
        "        threading.Thread(target=self.server.shutdown, daemon=True).start()\n"
        "    def log_message(self, *args): pass\n"
        "http.server.ThreadingHTTPServer(('127.0.0.1', int(os.environ['ARGUS_PORT'])), H).serve_forever()\n",
        encoding="utf-8",
    )
    log = tmp_path / "health-fixture.log"
    with log.open("w", encoding="utf-8") as handle:
        code = _run_packaged_smoke(
            executable,
            root=tmp_path,
            python_bin=sys.executable,
            environment=safe_environment(tmp_path),
            output=handle,
            expected_version=expected_version,
        )
    return code, log.read_text(encoding="utf-8")


def test_packaged_smoke_accepts_application_json_charset(tmp_path: Path) -> None:
    body = b'{"status":"ok","service":"ARGUS","version":"test","automation_mode":"OFF","live_submit":false,"trackr_live":false}'
    code, log = _run_health_fixture(tmp_path, body, content_type="application/json; charset=UTF-8")
    assert code == 0, log


@pytest.mark.parametrize(
    "content_type",
    (None, "text/plain", "application/json; charset=iso-8859-1", "application/json; charset=utf-8; boundary=x"),
)
def test_packaged_smoke_rejects_non_json_or_unsafe_content_type(tmp_path: Path, content_type: str | None) -> None:
    body = b'{"status":"ok","service":"ARGUS","version":"test","automation_mode":"OFF","live_submit":false,"trackr_live":false}'
    code, log = _run_health_fixture(tmp_path, body, content_type=content_type)
    assert code != 0
    assert "health validation failed" in log
    assert "content type" in log.casefold()


def test_packaged_smoke_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    body = b'{"status":"ok","status":"ok","service":"ARGUS","version":"test","automation_mode":"OFF","live_submit":false,"trackr_live":false}'
    code, log = _run_health_fixture(tmp_path, body)
    assert code != 0
    assert "duplicate" in log.casefold()


@pytest.mark.parametrize(
    "body",
    (
        b'{"status":"ok","service":"ARGUS","version":"test","automation_mode":"OFF","live_submit":false,"trackr_live":false,"extra":0}',
        b'{"status":"ok","service":"ARGUS","version":"test","automation_mode":"OFF","live_submit":false}',
        b'["ok","ARGUS"]',
        b'{"status":"ok",',
    ),
)
def test_packaged_smoke_requires_exact_six_key_json_object(tmp_path: Path, body: bytes) -> None:
    code, log = _run_health_fixture(tmp_path, body)
    assert code != 0
    assert "health validation failed" in log


def test_packaged_smoke_rejects_unexpected_version_and_redirect(tmp_path: Path) -> None:
    body = b'{"status":"ok","service":"ARGUS","version":"test","automation_mode":"OFF","live_submit":false,"trackr_live":false}'
    code, log = _run_health_fixture(tmp_path / "version", body, expected_version="0.2.0")
    assert code != 0
    assert "version" in log.casefold()

    redirected = tmp_path / "redirect"
    redirected.mkdir()
    code, log = _run_health_fixture(redirected, body, status=302, redirect_location="http://127.0.0.1:1/healthz")
    assert code != 0
    assert "health validation failed" in log


@pytest.mark.parametrize("field", ("automation_mode", "live_submit", "trackr_live", "service", "status"))
def test_packaged_smoke_rejects_untruthful_health_json(tmp_path: Path, field: str) -> None:
    values: dict[str, object] = {
        "status": "ok",
        "service": "ARGUS",
        "version": "test",
        "automation_mode": "OFF",
        "live_submit": False,
        "trackr_live": False,
    }
    values[field] = {
        "automation_mode": "ARMED",
        "live_submit": True,
        "trackr_live": True,
        "service": "other",
        "status": "bad",
    }[field]
    payload = json.dumps(values, separators=(",", ":")).encode("utf-8")
    executable = tmp_path / "unsafe-health.py"
    executable.write_text(
        "import http.server, os\n"
        "class H(http.server.BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(" + repr(payload) + ")\n"
        "    def log_message(self, *args): pass\n"
        "http.server.ThreadingHTTPServer(('127.0.0.1', int(os.environ['ARGUS_PORT'])), H).serve_forever()\n",
        encoding="utf-8",
    )
    log = tmp_path / "unsafe-health.log"
    with log.open("w", encoding="utf-8") as handle:
        code = _run_packaged_smoke(executable, root=tmp_path, python_bin=sys.executable, environment=safe_environment(tmp_path), output=handle)
    assert code != 0
    assert "health validation failed" in log.read_text(encoding="utf-8")
