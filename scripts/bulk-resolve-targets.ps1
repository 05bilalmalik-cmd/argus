<#
Bulk re-resolution of stuck opportunity targets (CLASSIFY-ONLY, DRY-RUN BY DEFAULT).

SAFETY CONTRACT (non-negotiable):
- This script only CLASSIFIES targets via POST /api/opportunities/{id}/resolve-target.
- It never fills or submits a form, and the final submit always stays with a human.
- DEFAULT IS DRY RUN: without -Live it only prints what it WOULD do and changes nothing.
- Live mode only re-runs target classification; it performs no application submission.

Usage:
  Dry run (default, changes nothing):
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\bulk-resolve-targets.ps1 -Limit 5
  Live run (re-classifies at most $Limit rows, ~2s apart to be polite to employer sites):
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\bulk-resolve-targets.ps1 -Limit 25 -Live
#>

param(
    [int]$Limit = 25,
    [string]$TargetStatus = 'UNRESOLVED',
    [switch]$Live
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$Port = 8787
$DbPath = Join-Path $env:LOCALAPPDATA "ARGUS\argus.db"
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"

# Refuse to run at all if the server is not listening (exit 1).
$PortUp = Test-NetConnection -ComputerName 127.0.0.1 -Port $Port -InformationLevel Quiet -WarningAction SilentlyContinue
if (-not $PortUp) {
    Write-Host ("[ERROR] Server is not listening on 127.0.0.1:" + $Port + ". Refusing to run; start the server first.") -ForegroundColor Red
    exit 1
}

if ($Live) {
    Write-Host "MODE: LIVE - re-classifying up to $Limit row(s) with target_status='$TargetStatus'." -ForegroundColor Yellow
    Write-Host "Classify-only: no form is filled or submitted; final submit stays with a human." -ForegroundColor Yellow
} else {
    Write-Host "MODE: DRY RUN - printing what WOULD happen. Nothing will be changed." -ForegroundColor Cyan
    Write-Host "Add -Live to actually re-classify targets (classify-only, never submits)." -ForegroundColor Cyan
}

# Select candidate opportunity ids READ-ONLY via the venv python + sqlite3 mode=ro URI.
# This script never opens the DB writable.
if (-not (Test-Path $VenvPython)) {
    Write-Host ("[ERROR] venv python not found: " + $VenvPython) -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $DbPath)) {
    Write-Host ("[ERROR] DB not found: " + $DbPath) -ForegroundColor Red
    exit 1
}

$PyCode = @'
import sqlite3, json, sys
status = sys.argv[1]
limit = int(sys.argv[2])
db_path = sys.argv[3]
con = sqlite3.connect('file:' + db_path + '?mode=ro', uri=True)
cur = con.cursor()
rows = cur.execute(
    'SELECT id, employer, role_title, target_status, application_window_status FROM opportunities '
    'WHERE target_status = ? AND application_window_status = ? AND user_status IN (?, ?) '
    'ORDER BY updated_at ASC LIMIT ?',
    (status, 'OPEN', 'NOT_APPLIED', 'INTERESTED', limit),
).fetchall()
con.close()
for r in rows:
    print(json.dumps({'id': r[0], 'employer': r[1], 'role': r[2], 'target_status': r[3], 'window': r[4]}))
'@

$Lines = @(& $VenvPython -c $PyCode $TargetStatus $Limit $DbPath | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] read-only DB query failed (exit $LASTEXITCODE). Abandoning run; nothing was changed." -ForegroundColor Red
    exit 1
}
$CandidatesJson = ($Lines | Out-String)
if ([string]::IsNullOrWhiteSpace($CandidatesJson)) {
    Write-Host ("No opportunities found with target_status='" + $TargetStatus + "'. Nothing to do.") -ForegroundColor Green
    exit 0
}
$Candidates = @($Lines | ForEach-Object { $_ | ConvertFrom-Json })
if ($Candidates.Count -eq 0) {
    Write-Host ("No opportunities found with target_status='" + $TargetStatus + "'. Nothing to do.") -ForegroundColor Green
    exit 0
}

Write-Host ("Selected " + $Candidates.Count + " candidate(s) with target_status='" + $TargetStatus + "' AND application_window_status='OPEN' AND user_status in (NOT_APPLIED, INTERESTED) (limit $Limit).") -ForegroundColor Cyan
Write-Host ""

$Attempted = 0
$Improved = 0
$Unchanged = 0
$Errored = 0
$SkippedWindow = 0
$ConsecutiveErrors = 0
$Aborted = $false

for ($i = 0; $i -lt $Candidates.Count; $i++) {
    $Row = $Candidates[$i]
    $Id = $Row.id
    $Employer = $Row.employer
    $Role = $Row.role
    $Before = $Row.target_status

    $HttpStatus = $null
    $After = $Before

    if ($Live) {
        $Attempted++
        try {
            $Uri = "http://127.0.0.1:" + $Port + "/api/opportunities/" + $Id + "/resolve-target"
            $resp = Invoke-WebRequest -Method Post -Uri $Uri -ContentType "application/json" -Body '{}' -UseBasicParsing -TimeoutSec 180
            $HttpStatus = [int]$resp.StatusCode
            $Body = $resp.Content | ConvertFrom-Json
            if ($null -ne $Body.target_status) {
                $After = [string]$Body.target_status
            }
            $ConsecutiveErrors = 0
            if ((($After -eq 'APPLICATION_FORM') -or ($After -eq 'APPLICATION_ENTRY')) -and (($Before -ne 'APPLICATION_FORM') -and ($Before -ne 'APPLICATION_ENTRY'))) {
                $Improved++
            } else {
                $Unchanged++
            }
        } catch {
            if (($null -ne $_.Exception) -and ($null -ne $_.Exception.Response)) {
                try {
                    $HttpStatus = [int]$_.Exception.Response.StatusCode
                } catch {
                    $HttpStatus = "ERROR"
                }
            } else {
                $HttpStatus = "ERROR"
            }
            # An HTTP 409 "window is not OPEN" is a SKIP (ineligible row), not an
            # error: the server correctly refused a CLOSED/NOT_YET_OPEN/UNKNOWN
            # row that slipped through (e.g. window changed after selection).
            # Count it separately and do NOT let it trip the abort guard.
            $RespBody = ""
            try {
                $RespStream = $_.Exception.Response.GetResponseStream()
                if ($null -ne $RespStream) {
                    $Reader = New-Object System.IO.StreamReader($RespStream)
                    $RespBody = $Reader.ReadToEnd()
                }
            } catch {
                $RespBody = ""
            }
            $ExcMsg = ""
            try { $ExcMsg = [string]$_.Exception.Message } catch { $ExcMsg = "" }
            # Invoke-WebRequest surfaces the JSON error body in
            # $_.ErrorDetails.Message (Exception.Message is only "(409)
            # Conflict" and the Response stream is often already consumed),
            # so check all three sources for the window-not-open phrase.
            $ErrDetails = ""
            try { $ErrDetails = [string]$_.ErrorDetails.Message } catch { $ErrDetails = "" }
            $IsWindowSkip = (($HttpStatus -eq 409) -and ((($RespBody -match "application window is not OPEN") -or ($ExcMsg -match "application window is not OPEN") -or ($ErrDetails -match "application window is not OPEN"))))
            $After = $Before
            if ($IsWindowSkip) {
                $SkippedWindow++
                $ConsecutiveErrors = 0
                $HttpStatus = "409-SKIP-window-not-open"
                Write-Host ("  [skip] window not OPEN (ineligible row, not an error): " + $ExcMsg) -ForegroundColor Gray
            } else {
                $Errored++
                $ConsecutiveErrors++
                Write-Host ("  [warn] row failed: " + $_.Exception.Message) -ForegroundColor Yellow
            }
        }
    } else {
        $HttpStatus = "DRY-RUN (no request sent)"
        $After = $Before
    }

    Write-Host ("id=" + $Id + " | employer=" + $Employer + " | role=" + $Role + " | before=" + $Before + " | http=" + $HttpStatus + " | after=" + $After) -ForegroundColor White

    if ($ConsecutiveErrors -gt 5) {
        $Aborted = $true
        Write-Host "" -ForegroundColor White
        Write-Host ("ABORTED: more than 5 consecutive rows errored (" + $ConsecutiveErrors + " in a row). Something is systematically wrong, so stopping instead of hammering employer sites.") -ForegroundColor Red
        break
    }

    # Be polite to employer sites: live re-resolution makes real HTTP requests
    # to real job boards, so pause briefly between rows (not after the last one).
    # Dry runs send no requests, so no pause is needed there.
    if ($Live -and ($i -lt ($Candidates.Count - 1))) {
        Start-Sleep -Seconds 2
    }
}

Write-Host ""
Write-Host "SUMMARY: attempted=$Attempted improved=$Improved unchanged=$Unchanged errored=$Errored skipped_window=$SkippedWindow" -ForegroundColor Cyan
Write-Host "(improved = moved to APPLICATION_FORM/APPLICATION_ENTRY)" -ForegroundColor Cyan
if ($Aborted) {
    Write-Host "Run was ABORTED early by the consecutive-error guard." -ForegroundColor Red
    exit 1
}
if (-not $Live) {
    Write-Host "DRY RUN complete: no rows were touched and nothing was changed." -ForegroundColor Green
} else {
    Write-Host "LIVE run complete: classify-only, no form was filled or submitted." -ForegroundColor Green
}
