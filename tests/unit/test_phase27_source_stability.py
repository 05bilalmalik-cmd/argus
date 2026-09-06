from __future__ import annotations

from pathlib import Path


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_source_snapshot_detects_modified_added_and_deleted_python(tmp_path: Path) -> None:
    from tests.source_stability import diff_source_snapshots, snapshot_source_tree

    _write(tmp_path / "app" / "stable.py", "VALUE = 1\n")
    _write(tmp_path / "scripts" / "deleted.py", "VALUE = 2\n")
    _write(tmp_path / "tests" / "stable_test.py", "def test_ok(): pass\n")
    before = snapshot_source_tree(tmp_path)

    _write(tmp_path / "app" / "stable.py", "VALUE = 3\n")
    (tmp_path / "scripts" / "deleted.py").unlink()
    _write(tmp_path / "tests" / "added_test.py", "def test_new(): pass\n")
    after = snapshot_source_tree(tmp_path)

    assert diff_source_snapshots(before, after) == {
        "added": ("tests/added_test.py",),
        "deleted": ("scripts/deleted.py",),
        "modified": ("app/stable.py",),
    }


def test_source_snapshot_ignores_non_python_runtime_artifacts(tmp_path: Path) -> None:
    from tests.source_stability import diff_source_snapshots, snapshot_source_tree

    _write(tmp_path / "app" / "stable.py", "VALUE = 1\n")
    before = snapshot_source_tree(tmp_path)
    _write(tmp_path / "app" / "__pycache__" / "stable.pyc", "runtime\n")
    _write(tmp_path / "tests" / "evidence.json", "{}\n")

    assert diff_source_snapshots(before, snapshot_source_tree(tmp_path)) == {
        "added": (),
        "deleted": (),
        "modified": (),
    }
