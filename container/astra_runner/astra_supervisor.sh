#!/usr/bin/env bash
# ASTRA 引擎监督器：server + dispatcher 崩溃/僵死自动重启
# 用法：ASTRA_DISPATCH_CONFIG=... bash astra_supervisor.sh
set -u
cd "$(dirname "$0")"

LOG_DIR="${TEMP:-/tmp}"
SERVER_PORT="${ASTRA_SERVER_PORT:-8011}"
SERVER_URL="http://127.0.0.1:${SERVER_PORT}"
DB_PATH="${ASTRA_DB_PATH:-$LOG_DIR/astra-engine.db}"
CONFIG="${ASTRA_DISPATCH_CONFIG:-/tmp/dispatch_ext.yaml}"
# 项目根：默认取本脚本上级的上级（container/astra_runner → 仓库根）；可用 ASTRA_PROJECT_DIR 覆盖
PROJECT_DIR="${ASTRA_PROJECT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
STALE_LOG_SECONDS="${ASTRA_STALE_LOG_SECONDS:-900}"   # dispatcher 日志 15 分钟无更新视为僵死

SERVER_PID=0
DISPATCH_PID=0
DISPATCH_LOG=""
fail_count=0

# 文件系统审计：旧日志清理（保留最近 10 个 server/dispatch 日志）
cleanup_logs() {
  local dir="$1"
  for prefix in astra-server astra-dispatch; do
    ls -t "$dir"/${prefix}-*.log 2>/dev/null | tail -n +11 | while read -r f; do
      rm -f "$f"
    done
  done
}

log(){ echo "[supervisor] $(date '+%H:%M:%S') $*"; }

# 按命令行杀 ASTRA 引擎进程（Windows 下 kill PID 可能留下 uv 链孤儿）
kill_engine(){
  powershell -Command "Get-CimInstance Win32_Process | Where-Object { \$_.CommandLine -match 'astra serve --port $SERVER_PORT' -or \$_.CommandLine -match 'astra dispatch --config' } | ForEach-Object { Stop-Process -Id \$_.ProcessId -Force -ErrorAction SilentlyContinue }" 2>/dev/null
}

start_server(){
  local ts; ts=$(date +%Y%m%d-%H%M%S)
  cleanup_logs "$LOG_DIR"
  local out="$LOG_DIR/astra-server-$ts.log"
  log "starting server port=$SERVER_PORT db=$DB_PATH log=$out"
  ( cd "$PROJECT_DIR/astra" && uv run astra serve --port "$SERVER_PORT" --db-path "$DB_PATH" --no-access-log > "$out" 2>&1 ) &
  SERVER_PID=$!
}

start_dispatcher(){
  local ts; ts=$(date +%Y%m%d-%H%M%S)
  DISPATCH_LOG="$LOG_DIR/astra-dispatch-$ts.log"
  log "starting dispatcher config=$CONFIG log=$DISPATCH_LOG"
  ( cd "$PROJECT_DIR/astra" && uv run astra dispatch --config "$CONFIG" > "$DISPATCH_LOG" 2>&1 ) &
  DISPATCH_PID=$!
}

server_alive(){ curl -s --max-time 3 "$SERVER_URL/projects" -o /dev/null; }
proc_alive(){ kill -0 "$1" 2>/dev/null; }
wait_server(){
  for _ in $(seq 1 30); do server_alive && return 0; sleep 1; done
  return 1
}

log "=== ASTRA 引擎监督器启动 port=$SERVER_PORT ==="
kill_engine
sleep 2
start_server
if ! wait_server; then
  log "server 30 秒未就绪，继续尝试（引擎监督保持运行）"
fi
start_dispatcher

while true; do
  sleep 20

  # 1) server 探活（HTTP 层，防僵死）
  if ! server_alive; then
    fail_count=$((fail_count+1))
    if [ $fail_count -ge 3 ]; then
      log "server 探活连续失败（$fail_count 次），重启引擎"
      kill_engine
      sleep 2
      start_server
      wait_server || log "server 重启后仍未就绪"
      start_dispatcher
      log "引擎已重启 server_pid=$SERVER_PID dispatcher_pid=$DISPATCH_PID"
      fail_count=0
    else
      log "server 探活失败（$fail_count/3）"
    fi
  else
    fail_count=0
    # 2) dispatcher 进程检测
    if ! proc_alive "$DISPATCH_PID"; then
      log "dispatcher 进程退出（pid=$DISPATCH_PID），自动重启"
      start_dispatcher
      log "dispatcher 已重启 pid=$DISPATCH_PID"
      continue
    fi
    # 3) dispatcher 僵死检测（日志长时间无更新）
    if [ -n "$DISPATCH_LOG" ] && [ -f "$DISPATCH_LOG" ]; then
      mtime=$(date -r "$DISPATCH_LOG" +%s 2>/dev/null || echo 0)
      now=$(date +%s)
      age=$(( now - mtime ))
      if [ "$age" -gt "$STALE_LOG_SECONDS" ]; then
        log "dispatcher 日志 ${age}s 无更新（>${STALE_LOG_SECONDS}s），疑似僵死，重启"
        kill_engine
        sleep 2
        start_server
        wait_server || log "server 重启后仍未就绪"
        start_dispatcher
        log "引擎已重启（僵死恢复）"
      fi
    fi
  fi
done
