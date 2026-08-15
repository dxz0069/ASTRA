# 参赛提交物打包脚本：git archive 只导出 git 跟踪的文件，
# 自动排除 .gitignore 覆盖的敏感物（dispatch.astra.yaml 的模型 key、*.ovpn、tmp/、*.db、*.log）。
# 用法：powershell -ExecutionPolicy Bypass -File scripts\make_submission.ps1
# 产物：dist/astra-submission-<日期>.zip（源码仓库提交件）
# 注意：比赛方按 container/Dockerfile 自行构建镜像，无需提交 agent.tar.gz。

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (git status --porcelain | Select-String -NotMatch "^\?\?") {
    Write-Warning "存在未提交改动，打包前请先 git commit（否则 zip 不含最新代码）"
    git status --short | Select-Object -First 10
}

$dist = Join-Path $root "dist"
New-Item -ItemType Directory -Force -Path $dist | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmm"
$out = Join-Path $dist "astra-submission-$stamp.zip"

git archive --format=zip --output=$out HEAD
Write-Host "已生成：$out"
Write-Host "大小：$([math]::Round((Get-Item $out).Length / 1MB, 1)) MB"

# 敏感物自检（zip 内不应出现任何已知敏感文件）
$sensitive = @("dispatch.astra.yaml", ".ovpn", "astra-progress")
$found = & {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [System.IO.Compression.ZipFile]::OpenRead($out)
    try {
        $zip.Entries | Where-Object {
            $n = $_.FullName
            ($sensitive | Where-Object { $n -like "*$_*" }).Count -gt 0 -and $n -notlike "*example*"
        } | ForEach-Object { $_.FullName }
    } finally { $zip.Dispose() }
}
if ($found) {
    Write-Error "敏感文件泄漏：$found"
} else {
    Write-Host "[PASS] 敏感物自检通过（无 key 配置 / ovpn / 进度文件）"
}
