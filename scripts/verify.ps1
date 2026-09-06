$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Python = if (Test-Path ".venv\Scripts\python.exe") { (Resolve-Path ".venv\Scripts\python.exe").Path } else { "python" }

# The shared verifier owns the complete fail-fast gate, safe environment,
# timestamped raw logs, and machine-readable evidence manifest.  Forwarding
# arguments keeps --dry-run and --packaged-smoke available on Windows.
& $Python "scripts\verify.py" --root $Root @args
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
