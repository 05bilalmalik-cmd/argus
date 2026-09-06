from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_verification_scripts_delegate_to_offline_fail_fast_gate() -> None:
    shell = (ROOT / "scripts/verify.sh").read_text(encoding="utf-8")
    powershell = (ROOT / "scripts/verify.ps1").read_text(encoding="utf-8")

    assert "scripts/verify.py" in shell
    assert "scripts\\verify.py" in powershell
    assert 'exec "$PYTHON"' in shell
    assert "$LASTEXITCODE" in powershell
    assert '"$@"' in shell
    assert "@args" in powershell
