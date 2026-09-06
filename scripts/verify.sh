#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -n "${PYTHON_BIN:-}" ]]; then
    PYTHON="${PYTHON_BIN}"
elif [[ -x "$ROOT/.venv/bin/python" ]]; then
    PYTHON="$ROOT/.venv/bin/python"
else
    PYTHON="python3"
fi

# Keep orchestration identical to PowerShell: the Python verifier discovers
# every E2E file, enforces offline environment semantics, writes timestamped
# raw logs, and propagates the first non-zero exit code.
exec "$PYTHON" "$ROOT/scripts/verify.py" --root "$ROOT" "$@"
