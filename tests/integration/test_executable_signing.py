from __future__ import annotations

from pathlib import Path

import pytest

from scripts.sign_release import build_signtool_command, sign_executable


def test_executable_signing_requires_certificate_thumbprint(tmp_path: Path) -> None:
    executable = tmp_path / "ARGUS.exe"
    executable.write_bytes(b"fixture")
    with pytest.raises(ValueError, match="certificate"):
        sign_executable(executable, signtool="signtool", timestamp_url="https://tsa.test")


def test_pfx_signing_fails_closed_before_putting_password_on_command_line(tmp_path: Path) -> None:
    executable = tmp_path / "ARGUS.exe"
    executable.write_bytes(b"fixture")
    certificate = tmp_path / "release.pfx"
    certificate.write_bytes(b"not-a-real-cert-for-command-construction")

    with pytest.raises(ValueError, match="PFX.*command line|certificate store"):
        build_signtool_command(
            executable,
            pfx=certificate,
            password="secret",
            signtool="signtool",
            timestamp_url="https://tsa.test",
        )


def test_thumbprint_signing_requires_timestamp_url(tmp_path: Path) -> None:
    executable = tmp_path / "ARGUS.exe"
    executable.write_bytes(b"fixture")
    with pytest.raises(ValueError, match="timestamp"):
        build_signtool_command(
            executable,
            thumbprint="ABC123",
            signtool="signtool",
            timestamp_url="",
        )
