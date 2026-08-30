# -*- coding: utf-8 -*-
"""本地全链路性能测试：真实模型（DS flash + GLM-5.3 经 pi）× 本地合成靶机 × FGS 引擎。

不依赖 tsecbench 平台（token 已 finished 阻塞）——测的是 v0.2 栈本身的端到端行为：
pi worker 循环 / 玉衡定航-摇光巡猎节奏 / 天枢产出速度 / token 消耗。
"""
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "container" / "astra_runner"))

FLAG = "flag{perf-local-a9f3e2}"
WINDOW_SECONDS = int(os.environ.get("PERF_WINDOW_SECONDS", "900"))


class TargetHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/secret":
            body = f"the key is {FLAG}\n".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(body)
        else:
            body = b"welcome to perf-local target. links: /secret\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *args):
        pass


def main() -> int:
    # 1) 合成靶机
    server = HTTPServer(("127.0.0.1", 18765), TargetHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print("[perf] target up at http://127.0.0.1:18765/", flush=True)

    # 2) 引擎（env 已由外层注入 PI_*；清洗污染）
    for k in list(os.environ):
        if k.startswith("ANTHROPIC_"):
            os.environ.pop(k)
    from astra_runner_engine import LocalAstraEngine, AstraDaemon

    engine = LocalAstraEngine()
    t0 = time.monotonic()
    engine.start()
    print(f"[perf] engine up in {time.monotonic()-t0:.1f}s", flush=True)

    # 3) 建项目（起点=靶机，北辰=拿旗）
    pid = engine.create_project(
        title="perf-local-fgs-001",
        origin="http://127.0.0.1:18765/ （本地合成靶机，HTTP 服务）",
        goal="在靶机上找到 flag（形如 flag{...}）并写回星图。靶机是个简单 HTTP 站点，先探测页面与路径。",
    )
    print(f"[perf] project={pid}", flush=True)

    # 4) 观测窗口
    import requests
    facts_n = steps_n = 0
    first_fact_at = None
    flag_seen_at = None
    headers = {}
    tok = os.environ.get("ASTRA_AUTH_TOKEN", "")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    base = "http://127.0.0.1:8000"
    deadline = time.monotonic() + WINDOW_SECONDS
    last_status = ""
    while time.monotonic() < deadline:
        time.sleep(20)
        try:
            r = requests.get(f"{base}/projects/{pid}", headers=headers, timeout=10).json()
            facts = [f["description"] for f in r.get("facts", [])]
            steps = r.get("steps", [])
            facts_n, steps_n = len(facts), len(steps)
            if first_fact_at is None and facts_n > 2:
                first_fact_at = time.monotonic() - t0
            if flag_seen_at is None and any("flag{" in f for f in facts):
                flag_seen_at = time.monotonic() - t0
            status = f"status={r['project']['status']} facts={facts_n} steps={steps_n} findings={len(r.get('findings', []))}"
            if status != last_status:
                print(f"[perf +{time.monotonic()-t0:6.0f}s] {status}", flush=True)
                last_status = status
            if r["project"]["status"] in ("completed", "stopped"):
                break
        except Exception as exc:  # noqa: BLE001
            print(f"[perf] poll error: {exc}", flush=True)

    # 5) 汇总
    elapsed = time.monotonic() - t0
    try:
        r = requests.get(f"{base}/projects/{pid}", headers=headers, timeout=10).json()
        facts = [f["description"] for f in r.get("facts", [])]
        steps = r.get("steps", [])
        facts_n, steps_n = len(facts), len(steps)
        flag_hit = any(FLAG in f or "flag{" in f for f in facts)
    except Exception:  # noqa: BLE001
        flag_hit = False
    print("\n=== 性能汇总 ===")
    print(f"窗口: {elapsed:.0f}s | 项目状态观测完成")
    print(f"天枢(事实): {facts_n} | 斗柄(步骤): {steps_n}")
    print(f"首个天枢产出: {first_fact_at and f'{first_fact_at:.0f}s' or '未产出'}")
    print(f"flag 写回: {flag_hit and (flag_seen_at and f'{flag_seen_at:.0f}s' or 'yes') or '未命中'}")
    if facts:
        print("天枢样例:")
        for f in facts[-8:]:
            print("  -", f[:110])
    # token 用量（pi 会话汇总，宽容扫描）
    sys.path.insert(0, str(REPO / "container" / "astra_runner"))
    from runner import collect_worker_usage

    usage = collect_worker_usage()
    print(f"pi 会话 token: {usage or '(会话目录无数据)'}")
    AstraDaemon.instance().shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
