#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PYTHON_BIN="${PYTHON_BIN:-python3}"

"$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info < (3, 11):
    raise SystemExit("ARGUS requires Python 3.11 or newer")
print(f"Using Python {sys.version.split()[0]}")
PY

if [[ ! -d .venv ]]; then
  "$PYTHON_BIN" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev,bridge]"
python -m playwright install chromium
python -m app.cli init

cat <<'EOF'

ARGUS setup complete.
Start it with:  ./scripts/start.sh
Demo data:      ./.venv/bin/python -m app.cli seed
Capture token:  ./.venv/bin/python -m app.cli token
EOF
