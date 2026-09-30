<#
This harness runs autonomously up to prefill/review only; CAPTCHA and the final Submit click are always left to a human; ARGUS_ENABLE_LIVE_SUBMIT and ARGUS_ENABLE_TRACKR_LIVE must never be flipped on by this harness.
#>

param(
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$StartScript = Join-Path $Root "scripts\start-argus-autonomous.ps1"
$TaskName = "ARGUS-Autopilot"

if ($Uninstall) {
    Write-Host "Unregistering Windows scheduled task '$TaskName'..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Uninstall complete (task removed if present)." -ForegroundColor Green
    exit 0
}

if (-not (Test-Path $StartScript)) {
    throw "Start script not found: $StartScript"
}

if (-not (Get-Command Register-ScheduledTask -ErrorAction SilentlyContinue)) {
    throw "Register-ScheduledTask cmdlet not available. This requires the ScheduledTasks module (built into Windows 10/11 PowerShell)."
}

# Register-ScheduledTask is used (instead of schtasks.exe) for robustness, error handling,
# and native PowerShell object model on Windows 11.
$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$StartScript`"" `
    -WorkingDirectory $Root

$trigger = New-ScheduledTaskTrigger -AtLogOn

# Restart-on-failure policy: 3 retries with 2-minute backoff between attempts.
# No execution time limit; allow on batteries for laptops; do not stop on logoff.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -DontStopOnIdleEnd `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 2) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0)

# Run in the current user's context at logon (interactive). This matches the
# design where ARGUS UI/automation is user-session, not a system service.
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive

Write-Host "Registering scheduled task '$TaskName' (runs at logon, restart-on-failure)..."
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Force | Out-Null

Write-Host "Task '$TaskName' registered successfully." -ForegroundColor Green
Write-Host "It will run scripts/start-argus-autonomous.ps1 (defaults: 4h sweep, REVIEW_ONLY never-submit)." -ForegroundColor Green
Write-Host "Uninstall with: .\scripts\install-argus-autopilot-task.ps1 -Uninstall" -ForegroundColor Yellow
