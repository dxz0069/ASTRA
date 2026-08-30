#!/usr/bin/env python3
"""本地跑分一站式态势板：平台分数 + runner 日志要点 + 星图活性 + worker 会话活性。

用法：python tools/run_status.py [--json]
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _platform():
    token = os.environ.get("BENCHMARK_TOKEN", "")
    if not token:
        for line in (ROOT / "dist" / "local.env").read_text(encoding="utf-8").splitlines():
            if line.startswith("BENCHMARK_TOKEN="):
                token = line.split("=", 1)[1].strip()
    out = subprocess.run(
        ["curl", "-s", "-m", "15", "-H", f"BENCHMARK_TOKEN: {token}",
         "https://tsecbench.zc.tencent.com/openapi/v1/challenges"],
        capture_output=True, text=True, encoding="utf-8",
    )
    chs = json.loads(out.stdout)
    done = [c for c in chs if c["is_completed"]]
    running = [c for c in chs if c["container_status"] == "available"]
    partial = [c for c in chs if c["correct_flag_count"] > 0 and not c["is_completed"]]
    return {
        "solved": len(done), "total": len(chs),
        "score": sum(c["total_score"] for c in done),
        "max_score": sum(c["total_score"] for c in chs),
        "running": [c["unique_code"] for c in running],
        "partial": [c["unique_code"] for c in partial],
        "challenges": chs,
    }


def _runner_log():
    log = ROOT / "dist" / "local-run.log"
    if not log.exists():
        return {"exists": False}
    text = log.read_text(encoding="utf-8", errors="replace")
    correct = re.findall(r"flag submitted code=(\S+) .*correct=True awarded=(\d+)", text)
    wrong = re.findall(r"flag submitted code=(\S+) .*correct=False", text)
    hints = re.findall(r"hint.*?code=(\S+)", text)
    gives = re.findall(r"challenge give up code=(\S+)", text)
    defers = re.findall(r"challenge deferred code=(\S+)", text)
    started = re.findall(r"challenge started code=(\S+)", text)
    return {
        "exists": True, "started": len(started),
        "correct": correct[-5:], "wrong_count": len(wrong),
        "hints": len(set(hints)), "gives_up": len(set(gives)), "defers": len(defers),
    }


def _star_chart():
    dbs = sorted(glob.glob(str(Path(tempfile.gettempdir()) / "astra-runner-*.db")), key=os.path.getmtime)
    if not dbs:
        return {}
    import sqlite3
    conn = sqlite3.connect(dbs[-1])
    facts = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    intents = conn.execute("SELECT COUNT(*) FROM intents").fetchone()[0]
    per_worker = conn.execute(
        "SELECT worker, COUNT(*) FROM intents WHERE worker IS NOT NULL GROUP BY worker"
    ).fetchall()
    conn.close()
    return {"facts": facts, "intents": intents, "per_worker": per_worker, "db": Path(dbs[-1]).name}


def _worker_liveness():
    homes = glob.glob(str(Path(tempfile.gettempdir()) / "astra-dsh" / "*"))
    out = {}
    now = time.time()
    for home in homes:
        sessions = glob.glob(os.path.join(home, "sessions", "*", "*", "session.jsonl.zstd"))
        if sessions:
            latest = max(sessions, key=os.path.getmtime)
            age = int(now - os.path.getmtime(latest))
            out[Path(home).name] = f"{age}s ago"
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    board = {"platform": _platform(), "runner": _runner_log(), "chart": _star_chart(), "workers": _worker_liveness()}
    if args.json:
        board["platform"].pop("challenges")
        print(json.dumps(board, ensure_ascii=False, indent=1))
        return 0
    p, r, c, w = board["platform"], board["runner"], board["chart"], board["workers"]
    print(f"▶ 解出 {p['solved']}/{p['total']} ｜ 得分 {p['score']}/{p['max_score']} ｜ 在跑 {p['running']} ｜ 部分 {p['partial']}")
    if r.get("exists"):
        print(f"▶ runner: 开题 {r['started']} ｜ 错交 {r['wrong_count']} ｜ hint {r['hints']} 题 ｜ defer {r['defers']} ｜ 放弃 {r['gives_up']} ｜ 最近正确: {r['correct']}")
    if c:
        print(f"▶ 星图: facts {c['facts']} ｜ intents {c['intents']} ｜ 认领 {c['per_worker']}")
    print(f"▶ worker 活性: {w}")
    stale = [k for k, v in w.items() if int(v.split("s")[0]) > 900]
    if stale:
        print(f"⚠ 停滞 worker: {stale}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
