# -*- coding: utf-8 -*-
"""模型健康看门狗：检测模型 API 403/配额耗尽，及时告警避免空转。

2026-08-14 第二轮实测教训：Kimi K3 配额 18:12 耗尽后 worker 静默 403 空转
近 1 小时无人发现（b/c 系列全白跑）。本守护每 60s 探活**所有已配置**的模型
端点（2026-08-15 混合舰队：DEEPSEEK_API_KEY 与 ZHIPU_API_KEY 各探各的），
连续 2 次失败 → 写告警文件 + 打印醒目日志；同时扫描全部 DSH worker 的最近
会话尾部是否全是 403 错误（双保险）。

用法：BENCHMARK_TOKEN 不需要；需注入与 runner 相同的模型 env（
DEEPSEEK_API_KEY / ZHIPU_API_KEY，或单通道 ANTHROPIC_AUTH_TOKEN 等）。
"""
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error

ALERT_FILE = os.path.join(os.environ.get("TEMP", "."), "astra-model-alert.txt")
CHECK_INTERVAL = 60
FAIL_THRESHOLD = 2  # 连续失败次数


def _probe_chat(url: str, headers: dict[str, str], model: str) -> tuple[bool, str]:
    body = json.dumps({
        "model": model,
        "max_tokens": 8,
        "messages": [{"role": "user", "content": "ping"}],
    }).encode()
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", "replace")
            if "error" in raw.lower() and ("quota" in raw.lower() or "limit" in raw.lower() or "403" in raw):
                return False, f"HTTP {resp.status} quota/limit: {raw[:200]}"
            return True, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        return False, f"HTTP {exc.code}: {detail}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def probe_model() -> tuple[bool, str]:
    """探测所有已配置的模型端点（混合舰队逐一探活）；返回 (ok, detail)。

    任一通道失败即整体告警——混合模式下单通道挂掉会拖慢对应 worker，
    值得第一时间发现（另一通道仍可继续出分）。
    """
    probes: list[tuple[str, bool, str]] = []
    provider = os.environ.get("DSH_PROVIDER", "")
    if os.environ.get("DEEPSEEK_API_KEY"):
        base = os.environ.get("DEEPSEEK_BASE_URL", "").rstrip("/") or "https://api.deepseek.com"
        model = "deepseek-v4-flash" if provider not in ("deepseek", "") else os.environ.get("DSH_MODEL", "deepseek-v4-flash")
        ok, detail = _probe_chat(
            f"{base}/chat/completions",
            {"Authorization": f"Bearer {os.environ['DEEPSEEK_API_KEY']}", "Content-Type": "application/json"},
            model,
        )
        probes.append(("deepseek", ok, detail))
    if os.environ.get("ZHIPU_API_KEY"):
        base = os.environ.get("ZHIPU_BASE_URL", "").rstrip("/") or "https://open.bigmodel.cn/api/coding/paas/v4"
        model = os.environ.get("ZHIPU_MODEL", "glm-5.3")
        ok, detail = _probe_chat(
            f"{base}/chat/completions",
            {"Authorization": f"Bearer {os.environ['ZHIPU_API_KEY']}", "Content-Type": "application/json"},
            model,
        )
        probes.append(("zhipu", ok, detail))
    if not probes and provider == "anthropic":
        token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
        base = os.environ.get("ANTHROPIC_BASE_URL", "").rstrip("/")
        model = os.environ.get("DSH_MODEL", "k3")
        if not token:
            return False, "缺少 ANTHROPIC_AUTH_TOKEN"
        ok, detail = _probe_chat(
            f"{base}/v1/messages",
            {"x-api-key": token, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            model,
        )
        probes.append(("anthropic", ok, detail))
    if not probes:
        return False, "未配置任何模型通道（DEEPSEEK_API_KEY / ZHIPU_API_KEY / ANTHROPIC_AUTH_TOKEN）"
    failed = [f"{name}: {detail}" for name, ok, detail in probes if not ok]
    if failed:
        return False, "; ".join(failed)
    return True, "; ".join(f"{name} {detail}" for name, ok, detail in probes)


def scan_recent_sessions_403() -> int:
    """扫描所有 DSH worker 最近 5 个会话文件尾部是否全是 403；返回 403 数量。

    混合舰队下 DSH_HOME 为 <root>/<worker-name>（deepseek-main / glm-main /
    glm-reason / deepseek-fallback），全部纳入扫描。
    """
    import glob
    import subprocess
    root = os.environ.get("ASTRA_DSH_HOME") or os.path.join(os.environ.get("TEMP", "."), "astra-dsh")
    zstd = os.environ.get("ZSTD_EXE", "")
    sessions = glob.glob(os.path.join(root, "*", "sessions", "*", "*", "session.jsonl.zstd"))
    sessions.sort(key=os.path.getmtime, reverse=True)
    count403 = 0
    for path in sessions[:5]:
        tmp = path + ".probe"
        try:
            if zstd:
                subprocess.run([zstd, "-d", "-f", path, "-o", tmp], capture_output=True, timeout=15)
            else:
                # 尝试 python zstandard；没有则跳过
                import importlib
                importlib.import_module("zstandard")
                subprocess.run(
                    [sys.executable, "-c",
                     f"import zstandard;open(r'{tmp}','wb').write(zstandard.ZstdDecompressor().decompress(open(r'{path}','rb').read()))"],
                    capture_output=True, timeout=30,
                )
            if not os.path.exists(tmp):
                continue
            with open(tmp, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
            tail = " ".join(lines[-2:])
            if re.search(r"usage limit|permission_error|already.*quota|403", tail):
                count403 += 1
            checked += 1
        except Exception:  # noqa: BLE001
            continue
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    return count403


def main() -> int:
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
            msg = f"[{now}] 模型异常 probe={'OK' if ok else 'FAIL ' + detail} 最近会话403数={sess403} fails={fails}/{FAIL_THRESHOLD}"
            print(msg, flush=True)
            if fails >= FAIL_THRESHOLD and time.monotonic() - last_alert > 300:
                last_alert = time.monotonic()
                alert = f"{now} 模型配额/健康异常！请立即检查：probe={detail} 会话403={sess403}\n"
                try:
                    with open(ALERT_FILE, "w", encoding="utf-8") as fh:
                        fh.write(alert)
                except OSError:
                    pass
                print(f"!!! {alert}", flush=True)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
