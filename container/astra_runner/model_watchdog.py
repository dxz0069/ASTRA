# -*- coding: utf-8 -*-
"""模型健康看门狗：检测模型 API 403/配额耗尽，及时告警避免空转。

2026-08-14 第二轮实测教训：Kimi K3 配额 18:12 耗尽后 worker 静默 403 空转
近 1 小时无人发现（b/c 系列全白跑）。本守护每 60s 探活模型端点，连续
FAIL_THRESHOLD 次失败 → 写告警文件 + 打印醒目日志；同时扫描最近 CC 会话
尾部是否全是配额/权限错误（双保险）。

2026-08-28：随 dsh 移除改探 anthropic 兼容端点（claudecode 舰队唯一通道）。

用法：BENCHMARK_TOKEN 不需要；需注入与 runner 相同的模型 env
（ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL / ANTHROPIC_MODEL）。
"""
import glob
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

ALERT_FILE = os.path.join(os.environ.get("TEMP", "."), "astra-model-alert.txt")
CHECK_INTERVAL = 60
FAIL_THRESHOLD = 3  # 连续失败次数（R5：端点抖动 2-5 分钟自愈，2 次误报）

_ERROR_MARK = re.compile(r"usage limit|permission_error|quota|403|insufficient", re.IGNORECASE)


def _probe_anthropic(url: str, headers: dict[str, str], model: str) -> tuple[bool, str]:
    body = json.dumps({
        "model": model,
        "max_tokens": 8,
        "messages": [{"role": "user", "content": "ping"}],
    }).encode()
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", "replace")
            if _ERROR_MARK.search(raw):
                return False, f"HTTP {resp.status} quota/limit: {raw[:200]}"
            return True, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        return False, f"HTTP {exc.code}: {detail}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def probe_model() -> tuple[bool, str]:
    """探活 anthropic 兼容端点（claudecode 舰队唯一模型通道）。"""
    token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
    if not token:
        return False, "缺少 ANTHROPIC_AUTH_TOKEN"
    base = os.environ.get("ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic").rstrip("/")
    model = os.environ.get("ANTHROPIC_MODEL", "deepseek-v4-flash")
    return _probe_anthropic(
        f"{base}/v1/messages",
        {"x-api-key": token, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        model,
    )


def scan_recent_sessions_403() -> int:
    """扫描最近 5 个 CC 会话 jsonl 尾部是否全是配额/权限错误；返回命中数。

    CC 会话在 <astra-claude>/<worker>/projects/**/*.jsonl（纯 jsonl，无压缩）。
    修复备注：dsh 时代此函数的计数器从未初始化（NameError 被 except 吞掉，
    计数恒 0 的死代码），CC 版重写时一并修正。
    """
    root = os.environ.get("ASTRA_CLAUDE_HOME") or os.path.join(os.environ.get("TEMP", "."), "astra-claude")
    sessions = glob.glob(os.path.join(root, "*", "projects", "**", "*.jsonl"), recursive=True)
    sessions.sort(key=os.path.getmtime, reverse=True)
    count = 0
    for path in sessions[:5]:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
            tail = " ".join(lines[-3:])
            if _ERROR_MARK.search(tail):
                count += 1
        except OSError:
            continue
    return count


def main() -> int:
    # R5 修复：启动即清上轮残留告警文件（07:23 的旧告警差点误判新一轮）
    try:
        if os.path.exists(ALERT_FILE):
            os.remove(ALERT_FILE)
            print("stale alert file removed", flush=True)
    except OSError:
        pass
    fails = 0
    last_alert = 0.0
    while True:
        ok, detail = probe_model()
        sess403 = scan_recent_sessions_403()
        now = time.strftime("%H:%M:%S")
        if ok and sess403 == 0:
            fails = 0
        else:
            fails += 1
            # 探针超时单独记日志（端点抖动常见且短时自愈）；真实会话错误才立即升级
            if not ok and sess403 == 0:
                print(f"[{now}] probe-only failure（端点抖动，观察中）", flush=True)
            msg = f"[{now}] 模型异常 probe={'OK' if ok else 'FAIL ' + detail} 最近会话错误数={sess403} fails={fails}/{FAIL_THRESHOLD}"
            print(msg, flush=True)
            if fails >= FAIL_THRESHOLD and time.monotonic() - last_alert > 300:
                last_alert = time.monotonic()
                alert = f"{now} 模型配额/健康异常！请立即检查：probe={detail} 会话错误={sess403}\n"
                try:
                    with open(ALERT_FILE, "w", encoding="utf-8") as fh:
                        fh.write(alert)
                except OSError:
                    pass
                print(f"!!! {alert}", flush=True)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
