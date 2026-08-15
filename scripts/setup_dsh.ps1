# ============================================================
# setup_dsh.ps1 - ASTRA x DeepSeek Harness local env one-shot setup
#
# Purpose (run before receiving benchmark token/URL):
#   1. Check/install @deepseek-ai/dsh (npm global)
#   2. Copy container/dsh/astra-headless-runner.js into the dsh package lib/
#      (required for the bare-specifier --patch load, see container/dsh/README.md)
#   3. Verify astra-headless.patch.yml exists (DSH_PATCH target)
#   4. Smoke-boot `dsh --profile headless --help` and print run templates
#
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\setup_dsh.ps1
#         -NoInstall   skip npm install (validate only)
# ============================================================

param(
    [switch]$NoInstall
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)   # scripts/ -> repo root
$patch = Join-Path $repo "container\dsh\astra-headless.patch.yml"
$runner = Join-Path $repo "container\dsh\astra-headless-runner.js"
$failed = $false

Write-Host "=== ASTRA x DSH environment setup ===" -ForegroundColor Cyan
Write-Host "repo: $repo"

# 1. dsh CLI
$dsh = Get-Command dsh -ErrorAction SilentlyContinue
if ($dsh) {
    Write-Host "[OK] dsh CLI: $($dsh.Source)" -ForegroundColor Green
} else {
    if ($NoInstall) {
        Write-Host "[FAIL] dsh CLI not found (skipped install with -NoInstall)" -ForegroundColor Red
        $failed = $true
    } else {
        Write-Host "[..] installing @deepseek-ai/dsh ..." -ForegroundColor Yellow
        npm install -g "@deepseek-ai/dsh@0.1.0-rc.6"
        Write-Host "[OK] dsh installed" -ForegroundColor Green
    }
}

# 2. copy runner into the dsh package lib/
$npmRoot = npm root -g 2>$null
$dshLib = Join-Path $npmRoot "@deepseek-ai\dsh\lib"
$target = Join-Path $dshLib "astra-headless-runner.js"
$needsCopy = (-not (Test-Path $target)) -or ((Get-Item $target).LastWriteTime -lt (Get-Item $runner).LastWriteTime)
if ($needsCopy) {
    New-Item -ItemType Directory -Force -Path $dshLib | Out-Null
    Copy-Item $runner $target -Force
    Write-Host "[OK] runner copied to $target" -ForegroundColor Green
} else {
    Write-Host "[OK] runner in place: $target" -ForegroundColor Green
}

# 3. patch file
if (Test-Path $patch) {
    Write-Host "[OK] DSH patch: $patch" -ForegroundColor Green
} else {
    Write-Host "[FAIL] DSH patch missing: $patch" -ForegroundColor Red
    $failed = $true
}

# 4. smoke boot (no model call)
Write-Host "`n=== dsh headless smoke boot ===" -ForegroundColor Cyan
$env:DSH_TELEMETRY_DISABLED = "1"
$env:DSH_HOME = Join-Path $env:TEMP "astra-dsh-selfcheck"
$out = dsh --profile headless --patch $patch --help 2>&1 | Out-String
if ($LASTEXITCODE -eq 0) {
    Write-Host "[OK] headless boots (--help ok)" -ForegroundColor Green
} else {
    Write-Host "[FAIL] headless boot failed: $out" -ForegroundColor Red
    $failed = $true
}

Write-Host "`n=== done ===" -ForegroundColor Cyan
if ($failed) {
    Write-Host "Setup incomplete; fix the [FAIL] items above." -ForegroundColor Red
    exit 1
}
Write-Host "Ready. After receiving token/URL and connecting the VPN:" -ForegroundColor Green
$banner = @"

  # 1) self check (platform API / engine boot)
  python container/astra_runner/runner.py --check

  # 2) run (Kimi anthropic example; 6h window + progress file + auto hint)
  ASTRA_WORKER_TYPE=dsh DSH_PROVIDER=anthropic DSH_MODEL=k3 `
  ANTHROPIC_AUTH_TOKEN=sk-xxx ANTHROPIC_BASE_URL=https://api.kimi.com/coding/ `
  python container/astra_runner/runner.py --progress-file %TEMP%\astra-progress.json
"@
Write-Host $banner -ForegroundColor White
exit 0
