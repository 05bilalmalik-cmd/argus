"""Fail-closed Windows Authenticode signing hook for an ARGUS executable.

This script is deliberately separate from archive creation. A release is
never described as signed unless SignTool successfully signs the executable
and ``signtool verify /pa`` succeeds afterwards. SignTool's PFX password
option is a command-line argument; this hook therefore refuses PFX signing
and requires a certificate-store thumbprint instead, so a private password
cannot be exposed in a process command line.
"""
from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path
from typing import Sequence


def build_signtool_command(
    executable: Path,
    *,
    pfx: Path | None = None,
    thumbprint: str | None = None,
    password: str | None = None,
    store: str = "My",
    signtool: str = "signtool",
    timestamp_url: str,
) -> list[str]:
    executable = Path(executable).expanduser().resolve()
    if not executable.is_file():
        raise FileNotFoundError(executable)
    if not timestamp_url.strip():
        raise ValueError("timestamp URL is required for executable signing")
    has_pfx = pfx is not None
    has_thumbprint = bool(thumbprint and thumbprint.strip())
    if has_pfx == has_thumbprint:
        raise ValueError(
            "provide exactly one signing certificate PFX or certificate thumbprint"
        )
    if has_pfx:
        raise ValueError(
            "PFX signing is refused because SignTool would place its password on the command line; "
            "import the certificate into the Windows certificate store and use --thumbprint"
        )
    command = [str(signtool), "sign", "/fd", "SHA256"]
    command.extend(["/sha1", "".join(thumbprint.split()), "/s", store])
    command.extend(["/tr", timestamp_url.strip(), "/td", "SHA256", "/d", "ARGUS"])
    command.append(str(executable))
    return command


def _run_checked(command: list[str], *, action: str) -> None:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise RuntimeError(f"{action} failed: SignTool is unavailable") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "no SignTool diagnostics").strip()
        raise RuntimeError(f"{action} failed: {detail}")


def sign_executable(
    executable: Path,
    *,
    pfx: Path | None = None,
    thumbprint: str | None = None,
    password_env: str = "ARGUS_SIGNING_PASSWORD",
    store: str = "My",
    signtool: str = "signtool",
    timestamp_url: str,
) -> Path:
    """Sign and verify one executable; never claim success without both steps."""

    if pfx is not None:
        raise ValueError(
            "PFX signing is refused because SignTool would place its password on the command line; "
            "import the certificate into the Windows certificate store and use --thumbprint"
        )
    if not (thumbprint and thumbprint.strip()):
        raise ValueError(
            "signing requires a certificate-store thumbprint; PFX signing is refused"
        )
    command = build_signtool_command(
        executable,
        thumbprint=thumbprint,
        store=store,
        signtool=signtool,
        timestamp_url=timestamp_url,
    )
    _run_checked(command, action="executable signing")
    executable_path = Path(executable).expanduser().resolve()
    _run_checked(
        [str(signtool), "verify", "/pa", str(executable_path)],
        action="executable signature verification",
    )
    return executable_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sign and verify ARGUS.exe with Windows SignTool"
    )
    parser.add_argument("--exe", required=True, type=Path)
    parser.add_argument("--pfx", type=Path)
    parser.add_argument("--thumbprint")
    parser.add_argument("--store", default="My")
    parser.add_argument("--signtool", default="signtool")
    parser.add_argument("--timestamp-url", required=True)
    parser.add_argument("--password-env", default="ARGUS_SIGNING_PASSWORD")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        signed = sign_executable(
            args.exe,
            pfx=args.pfx,
            thumbprint=args.thumbprint,
            password_env=args.password_env,
            store=args.store,
            signtool=args.signtool,
            timestamp_url=args.timestamp_url,
        )
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"ARGUS executable signing refused: {exc}", file=os.sys.stderr)
        return 2
    print(f"Verified Authenticode signature: {signed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
