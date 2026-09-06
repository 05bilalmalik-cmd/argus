$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Python = if ($env:PYTHON_BIN) { $env:PYTHON_BIN } else { "python" }

& $Python -c "import sys; assert sys.version_info >= (3,11), 'ARGUS requires Python 3.11 or newer'; print('Using Python', sys.version.split()[0])"
if (-not (Test-Path ".venv")) {
    & $Python -m venv .venv
}
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -e ".[dev]"
& $VenvPython -m playwright install chromium
& $VenvPython -m app.cli init

Write-Host ""
Write-Host "ARGUS setup complete." -ForegroundColor Green
Write-Host "Start it with:  .\scripts\start.ps1"
Write-Host "Demo data:      .\.venv\Scripts\python.exe -m app.cli seed"
Write-Host "Capture token:  .\.venv\Scripts\python.exe -m app.cli token"
