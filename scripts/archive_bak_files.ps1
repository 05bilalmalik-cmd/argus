# _bak_archive/ MUST stay at the repo root, outside app/, scripts/, tests/,
# docs/, and packaging/ — those are the privacy scan's source_roots
# (tests/unit/test_privacy_scan.py), and it scans a .bak copy exactly like a
# source file, re-reporting the original's findings under the backup's
# filename and turning a green scan red. Do not "tidy" this archive into any
# scanned directory.
$ErrorActionPreference = 'Stop'
$root = (Get-Location).Path

$trackedFile = '.git_ls_files.txt'
if (-not (Test-Path $trackedFile)) {
    Write-Error "Missing tracked files list: $trackedFile (generate with: git ls-files | Out-File -Encoding utf8 .git_ls_files.txt)"
    exit 1
}
$tracked = @(Get-Content $trackedFile | ForEach-Object { $_.Trim() -replace '\\','/' } | Where-Object { $_ })

$dateStr = Get-Date -Format 'yyyyMMdd'
$archiveBase = Join-Path (Join-Path $root '_bak_archive') $dateStr
if (-not (Test-Path $archiveBase)) {
    New-Item -ItemType Directory -Path $archiveBase -Force | Out-Null
}
$manifestPath = Join-Path $archiveBase 'manifest.csv'

$files = @(Get-ChildItem -Path . -Recurse -File -Force -ErrorAction SilentlyContinue | Where-Object {
    $p = $_.FullName
    $relWin = $p.Substring($root.Length + 1)
    $rel = ($relWin -replace '\\','/')
    if ($rel -match '^\.git/|^\.venv/|/__pycache__/|/node_modules/|^_bak_archive/') { return $false }
    $name = $_.Name.ToLowerInvariant()
    $matchesBak = ($name -like '*.bak') -or ($name -like '*.bak-*') -or ($name -match '\.phase.*\.bak')
    if (-not $matchesBak) { return $false }
    if ($tracked -contains $rel) { return $false }
    return $true
})

Write-Host "Found $($files.Count) untracked backup-style files (after excludes and git-tracked cross-check)."

$moved = 0
$totalBytes = [long]0
$manifestRows = @()

foreach ($f in $files) {
    $relWin = $f.FullName.Substring($root.Length + 1)
    $rel = ($relWin -replace '\\','/')
    $parentRelWin = Split-Path -Path $relWin -Parent
    $targetDir = if ($parentRelWin) { Join-Path $archiveBase $parentRelWin } else { $archiveBase }
    if (-not (Test-Path $targetDir)) {
        New-Item -ItemType Directory -Path $targetDir -Force | Out-Null | Out-Null
    }
    $targetPath = Join-Path $targetDir $f.Name
    $targetRelWin = $targetPath.Substring($root.Length + 1)
    $targetRel = ($targetRelWin -replace '\\','/')

    $size = [long]$f.Length
    $lw = $f.LastWriteTime.ToString('yyyy-MM-ddTHH:mm:ssZ')

    Move-Item -LiteralPath $f.FullName -Destination $targetPath

    $manifestRows += [pscustomobject]@{
        original_path = $rel
        new_path = $targetRel
        size_bytes = $size
        last_write_time = $lw
    }
    $moved++
    $totalBytes += $size
}

if ($manifestRows.Count -gt 0) {
    $manifestRows | Export-Csv -Path $manifestPath -NoTypeInformation -Encoding UTF8
}

Write-Host "ARCHIVE COMPLETE: moved $moved files, total $totalBytes bytes."
Write-Host "Manifest written to: $manifestPath"
exit 0
