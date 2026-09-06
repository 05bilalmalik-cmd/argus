$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    throw "ARGUS is not set up. Run .\scripts\setup.ps1 first."
}
& $VenvPython -m app.cli serve --open
