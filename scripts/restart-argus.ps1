<#
Gracefully restart ARGUS so a stale process picks up new code.
Verifies the port owner is this repo's python before stopping anything.
#>

param(
    [int]$SweepIntervalHours = 4
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    throw "ARGUS is not set up. .venv\Scripts\python.exe not found; run .\scripts\setup.ps1 first."
}

# Find the process listening on 8787, if any.
$conn = Get-NetTCPConnection -LocalPort 8787 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1

if ($null -ne $conn) {
    $TargetPid = $conn.OwningProcess
    Write-Host ("Port 8787 is currently owned by PID " + $TargetPid + ". Verifying ownership...") -ForegroundColor Yellow

    $proc = Get-Process -Id $TargetPid -ErrorAction SilentlyContinue
    if ($null -eq $proc) {
        Write-Host ("WARNING: PID " + $TargetPid + " no longer exists. Treating port as free.") -ForegroundColor Yellow
        $conn = $null
    } else {
        $VenvDir = Join-Path $Root ".venv"
        $procPath = $null
        try {
            $procPath = $proc.Path
        } catch {
            $procPath = $null
        }

        $pathOk = $false
        if (-not [string]::IsNullOrEmpty($procPath)) {
            try {
                $fullProcPath = [System.IO.Path]::GetFullPath($procPath)
                $fullVenvDir = [System.IO.Path]::GetFullPath($VenvDir)
                if ($fullProcPath.StartsWith($fullVenvDir, [System.StringComparison]::OrdinalIgnoreCase)) {
                    $pathOk = $true
                }
            } catch {
                $pathOk = $false
            }
        }

        $cmdLine = $null
        try {
            $cim = Get-CimInstance Win32_Process -Filter ("ProcessId = " + $TargetPid) -ErrorAction SilentlyContinue
            if ($null -ne $cim) {
                $cmdLine = $cim.CommandLine
            }
        } catch {
            $cmdLine = $null
        }

        # Normalise slashes so forward-slash and back-slash forms compare equal.
        # Use IndexOf with OrdinalIgnoreCase (no regex, no wildcard pitfalls); CommandLine may be $null.
        $rootInCmd = $false
        $launcherAndRoot = $false
        if (-not [string]::IsNullOrEmpty($cmdLine)) {
            $normCmd = ($cmdLine -replace '/', '\')
            $normRoot = ($Root -replace '/', '\')
            if ($normCmd.IndexOf($normRoot, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) {
                $rootInCmd = $true
            }
            if (($normCmd.IndexOf("launcher.py", [System.StringComparison]::OrdinalIgnoreCase) -ge 0) -and $rootInCmd) {
                $launcherAndRoot = $true
            }
        }

        $cmdOk = ($rootInCmd -or $launcherAndRoot)

        $nameOk = ($proc.ProcessName -eq "python" -or $proc.ProcessName -eq "python.exe" -or $proc.ProcessName -eq "pythonw" -or $proc.ProcessName -eq "pythonw.exe")

        if (-not ($pathOk -or $cmdOk)) {
            Write-Host ("WARNING: PID " + $TargetPid + " does NOT look like this repo's python. Refusing to kill an unrelated process.") -ForegroundColor Red
            Write-Host ("  ProcessName: " + $proc.ProcessName) -ForegroundColor Red
            Write-Host ("  Path: " + $procPath) -ForegroundColor Red
            try {
                $cimDbg = Get-CimInstance Win32_Process -Filter ("ProcessId = " + $TargetPid) -ErrorAction SilentlyContinue
                if ($null -ne $cimDbg) {
                    Write-Host ("  CommandLine: " + $cimDbg.CommandLine) -ForegroundColor Red
                }
            } catch {
            }
            Write-Host ("  Expected Path under: " + $VenvDir + " or CommandLine containing repo root (" + $Root + ") or CommandLine containing launcher.py with repo root") -ForegroundColor Red
            exit 1
        }

        if (-not $nameOk) {
            Write-Host ("WARNING: PID " + $TargetPid + " matched repo path/commandline but ProcessName is '" + $proc.ProcessName + "'. Refusing to stop it.") -ForegroundColor Red
            exit 1
        }

        Write-Host ("Verified PID " + $TargetPid + " (" + $procPath + ") is this repo's python. Stopping it...") -ForegroundColor Yellow
        Stop-Process -Id $TargetPid -ErrorAction Stop

        # Wait for the port to free (poll up to ~20s).
        $freed = $false
        for ($i = 0; $i -lt 20; $i++) {
            Start-Sleep -Seconds 1
            if (-not (Test-NetConnection -ComputerName 127.0.0.1 -Port 8787 -InformationLevel Quiet -WarningAction SilentlyContinue)) {
                $freed = $true
                break
            }
        }
        if (-not $freed) {
            if (Test-NetConnection -ComputerName 127.0.0.1 -Port 8787 -InformationLevel Quiet -WarningAction SilentlyContinue) {
                Write-Host "Timed out waiting for port 8787 to free after stopping PID $TargetPid." -ForegroundColor Red
                exit 1
            }
        }
        Write-Host "Previous ARGUS process stopped; port 8787 is free." -ForegroundColor Green
    }
} else {
    Write-Host "Nothing is listening on port 8787. Starting ARGUS fresh." -ForegroundColor Yellow
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
Write-Host "ARGUS restart requested with sweep interval ${SweepIntervalHours}h." -ForegroundColor Green
Write-Host "Automation mode is REVIEW_ONLY (prefill/review only; never-submit)." -ForegroundColor Green
Write-Host "CAPTCHA and the final Submit click are always left to a human." -ForegroundColor Green
