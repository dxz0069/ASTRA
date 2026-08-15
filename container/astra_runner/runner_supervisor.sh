#!/usr/bin/env bash
# ASTRA runner 监督器：runner 异常退出后自动重启（保证窗口持续管理）。
# - rc=0（正常完成/任务到期 TaskFinishedError）→ 退出循环，不再重启
# - 连续 3 次失败（可能 VPN 断开/平台不可达）→ 退避等待 300s，避免疯狂重启
cd "$(dirname "$0")"
fail_count=0
# 项目根：默认取本脚本上级的上级（container/astra_runner → 仓库根）；可用 ASTRA_PROJECT_DIR 覆盖
PROJECT_ROOT="${ASTRA_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
while true; do
  echo "[supervisor] $(date '+%H:%M:%S') starting runner..."
  ASTRA_EXTERNAL_ENGINE=1 BENCHMARK_TOKEN="${BENCHMARK_TOKEN}" BENCHMARK_BASE_URL="${BENCHMARK_BASE_URL}" \
  ASTRA_SERVER_URL="${ASTRA_SERVER_URL:-http://127.0.0.1:8011}" \
  uv run --project "$PROJECT_ROOT/astra" python runner.py --challenge-timeout 1800 "$@" 2>&1
  rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "[supervisor] runner finished normally (rc=0, 任务完成或到期), exiting."
    break
  fi
  fail_count=$((fail_count + 1))
  if [ "$fail_count" -ge 3 ]; then
    echo "[supervisor] runner failed $fail_count times consecutively; waiting 300s (可能 VPN 断开/平台不可达)..."
    sleep 300
    fail_count=0
  else
    echo "[supervisor] runner exited rc=$rc, restarting in 20s..."
    sleep 20
  fi
done
