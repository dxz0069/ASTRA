# Submission packaging: git archive exports ONLY git-tracked files,
# auto-excluding .gitignore'd sensitive items (dispatch.astra.yaml with model keys,
# *.ovpn, tmp/, *.db, *.log). Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\make_submission.ps1
# Output: dist/astra-submission-<timestamp>.zip (source repo deliverable)
# Note: judges build the image from container/Dockerfile themselves; agent.tar.gz is NOT part of this zip.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$dirty = git status --porcelain | Where-Object { $_ -notmatch "^\?\?" }
if ($dirty) {
    Write-Warning "Uncommitted changes exist; commit first or the zip will miss latest code:"
    $dirty | Select-Object -First 10
}

$dist = Join-Path $root "dist"
New-Item -ItemType Directory -Force -Path $dist | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmm"
$out = Join-Path $dist "astra-submission-$stamp.zip"

git archive --format=zip --output=$out HEAD
if ($LASTEXITCODE -ne 0) { throw "git archive failed" }
Write-Host "Created: $out"
Write-Host ("Size: {0} MB" -f [math]::Round((Get-Item $out).Length / 1MB, 1))

# Sensitive-file self check (zip must NOT contain known sensitive items)
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::OpenRead($out)
$leaks = @()
try {
    foreach ($e in $zip.Entries) {
        $n = $e.FullName
        if ($n -like "*dispatch.astra.yaml" -or $n -like "*.ovpn" -or $n -like "*astra-progress*" -or $n -like "tmp/*") {
            if ($n -notlike "*example*") { $leaks += $n }
        }
    }
} finally { $zip.Dispose() }

if ($leaks.Count -gt 0) {
    throw "SENSITIVE LEAK: $($leaks -join ', ')"
} else {
    Write-Host "[PASS] sensitive check ok (no key config / ovpn / progress files)"
}
