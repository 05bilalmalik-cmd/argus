<#
This harness runs autonomously up to prefill/review only; CAPTCHA and the final Submit click are always left to a human; ARGUS_ENABLE_LIVE_SUBMIT and ARGUS_ENABLE_TRACKR_LIVE must never be flipped on by this harness.
#>

param(
    # Sweep interval in hours (default 4). This is the ONLY overridable input;
    # the safety env vars below are hardcoded and non-negotiable.
    [int]$SweepIntervalHours = 4
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

# Port check must happen before any other action (including env setup or launch).
# If something is already listening, exit cleanly without a second instance.
if (Test-NetConnection -ComputerName 127.0.0.1 -Port 8787 -InformationLevel Quiet -WarningAction SilentlyContinue) {
    Write-Host "ARGUS already appears to be running on port 8787. Exiting without starting a second instance." -ForegroundColor Yellow
    exit 0
}

$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    throw "ARGUS is not set up. .venv\Scripts\python.exe not found; run .\scripts\setup.ps1 first."
}

# Set for the launched process ONLY. Hardcoded, not parameters. Deliberate safety boundary.
$origAutomation = $env:ARGUS_AUTOMATION_MODE
$origLiveSubmit = $env:ARGUS_ENABLE_LIVE_SUBMIT
$origTrackr = $env:ARGUS_ENABLE_TRACKR_LIVE
$origSweep = $env:ARGUS_SWEEP_INTERVAL_HOURS
try {
    $env:ARGUS_AUTOMATION_MODE = "REVIEW_ONLY"
    $env:ARGUS_ENABLE_LIVE_SUBMIT = "false"
    $env:ARGUS_ENABLE_TRACKR_LIVE = "false"
    $env:ARGUS_SWEEP_INTERVAL_HOURS = "$SweepIntervalHours"

    # Prefer Start-Process: allows this script to print status then exit cleanly while
    # the ARGUS launcher (which stays alive for the uvicorn child) continues in background.
    # Tradeoff vs foreground (& $VenvPython launcher.py or Start-Process -Wait -NoNewWindow):
    #   background (chosen): script exits fast, status printed here, no interleaved stdout;
    #     launched python gets its own console window (or none if from scheduled task) for logs.
    #   foreground: blocks here until shutdown, mixes all output, prevents the "started" exit message.
    Start-Process -FilePath $VenvPython -ArgumentList "launcher.py" -WorkingDirectory $Root
} finally {
    # Restore to avoid polluting the caller's interactive shell environment.
    $env:ARGUS_AUTOMATION_MODE = $origAutomation
    $env:ARGUS_ENABLE_LIVE_SUBMIT = $origLiveSubmit
    $env:ARGUS_ENABLE_TRACKR_LIVE = $origTrackr
    $env:ARGUS_SWEEP_INTERVAL_HOURS = $origSweep
}

Write-Host ""
Write-Host "ARGUS autonomous launch requested with sweep interval ${SweepIntervalHours}h." -ForegroundColor Green
Write-Host "Automation mode is REVIEW_ONLY (prefill/review only; never-submit)." -ForegroundColor Green
Write-Host "CAPTCHA and the final Submit click are always left to a human." -ForegroundColor Green
