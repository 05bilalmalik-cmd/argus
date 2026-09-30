<#
Preflight health check for ARGUS (READ-ONLY - changes nothing).
Checks venv python, launcher.py, port 8787, and /api/review-queue route.
Exits 0 if all pass, 1 otherwise.
#>

param()

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$FailCount = 0

$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
$Launcher = Join-Path $Root "launcher.py"

# 1. venv python exists
if (Test-Path $VenvPython) {
    Write-Host "[PASS] .venv\Scripts\python.exe exists: $VenvPython" -ForegroundColor Green
} else {
    Write-Host "[FAIL] .venv\Scripts\python.exe MISSING: $VenvPython" -ForegroundColor Red
    $FailCount++
}

# 2. launcher.py exists
if (Test-Path $Launcher) {
    Write-Host "[PASS] launcher.py exists: $Launcher" -ForegroundColor Green
} else {
    Write-Host "[FAIL] launcher.py MISSING: $Launcher" -ForegroundColor Red
    $FailCount++
}

# 3. port 8787 listening
$PortUp = Test-NetConnection -ComputerName 127.0.0.1 -Port 8787 -InformationLevel Quiet -WarningAction SilentlyContinue
if ($PortUp) {
    Write-Host "[PASS] port 8787 is listening on 127.0.0.1" -ForegroundColor Green
} else {
    Write-Host "[FAIL] port 8787 is NOT listening on 127.0.0.1" -ForegroundColor Red
    $FailCount++
}

# 4. if listening: check openapi.json for /api/review-queue route
if ($PortUp) {
    try {
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:8787/openapi.json" -UseBasicParsing -TimeoutSec 5
        $body = $resp.Content
        if ($body -match "/api/review-queue") {
            Write-Host "[PASS] running server exposes route /api/review-queue" -ForegroundColor Green
        } else {
            Write-Host "[FAIL] STALE server: port 8787 is up but route /api/review-queue is MISSING - process predates recent code changes and needs a restart." -ForegroundColor Red
            $FailCount++
        }
    } catch {
        Write-Host ("[FAIL] HTTP GET http://127.0.0.1:8787/openapi.json failed: " + $_.Exception.Message) -ForegroundColor Red
        $FailCount++
    }
} else {
    Write-Host "[SKIP] openapi.json check skipped (port 8787 not listening)" -ForegroundColor Yellow
}

# 5. current-shell env values (informational only)
foreach ($name in @("ARGUS_AUTOMATION_MODE", "ARGUS_ENABLE_LIVE_SUBMIT", "ARGUS_ENABLE_TRACKR_LIVE", "ARGUS_SWEEP_INTERVAL_HOURS")) {
    $val = [System.Environment]::GetEnvironmentVariable($name)
    if ([string]::IsNullOrEmpty($val)) {
        Write-Host ("[INFO] " + $name + "=(unset)") -ForegroundColor Yellow
    } else {
        Write-Host ("[INFO] " + $name + "=" + $val) -ForegroundColor Yellow
    }
}

Write-Host ""
if ($FailCount -eq 0) {
    Write-Host "PREFLIGHT PASS: all checks passed." -ForegroundColor Green
    exit 0
} else {
    Write-Host ("PREFLIGHT FAIL: " + $FailCount + " check(s) failed.") -ForegroundColor Red
    exit 1
}
