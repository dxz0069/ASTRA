#!/usr/bin/env python3
"""本地跑分一站式态势板：平台分数 + runner 日志要点 + 星图活性 + worker 会话活性。

用法：python tools/run_status.py [--json]
审计24轮：对齐 v0.2 pi 栈现实——星图查 steps 表（旧 intents 表仅作老库回退）、
runner 日志取 dist/local-run*.log 最新一份（旧版只读 local-run.log 永远落空）、
worker 活性扫 astra-pi（pi 栈）与 astra-dsh（遗留栈）两处会话目录。
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _benchmark_token() -> str:
    token = os.environ.get("BENCHMARK_TOKEN", "")
    if token:
        return token
    # 审计24轮：env 优先，回退按 mtime 取最新（local-fgs-run.env 是现行轮次文件）
    candidates = sorted(
        (ROOT / "dist").glob("local*.env"), key=lambda p: p.stat().st_mtime, reverse=True
    ) if (ROOT / "dist").is_dir() else []
    for env_file in candidates:
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("BENCHMARK_TOKEN="):
                return line.split("=", 1)[1].strip()
    return ""


def _platform() -> dict:
    token = _benchmark_token()
    if not token:
        return {"error": "BENCHMARK_TOKEN 未配置（env 或 dist/local*.env）"}
    out = subprocess.run(
        ["curl", "-s", "-m", "15", "-H", f"BENCHMARK_TOKEN: {token}",
         "https://tsecbench.zc.tencent.com/openapi/v1/challenges"],
        capture_output=True, text=True, encoding="utf-8",
    )
    try:
        chs = json.loads(out.stdout)
    except json.JSONDecodeError:
        return {"error": f"平台响应非 JSON（curl exit={out.returncode}，可能任务已收卷/断网）"}
    if not isinstance(chs, list):
        detail = chs.get("detail") or chs.get("error") if isinstance(chs, dict) else str(chs)[:120]
        return {"error": f"平台返回非题集（任务可能已收卷）：{detail}"}
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


def _runner_log() -> dict:
    logs = sorted(
        (ROOT / "dist").glob("local-run*.log"), key=lambda p: p.stat().st_mtime, reverse=True
    ) if (ROOT / "dist").is_dir() else []
    if not logs:
        return {"exists": False}
    log = logs[0]  # 最新一轮（旧版只读 local-run.log，现行轮次文件名带轮号后缀）
    text = log.read_text(encoding="utf-8", errors="replace")
    correct = re.findall(r"flag submitted code=(\S+) .*correct=True awarded=(\d+)", text)
    wrong = re.findall(r"flag submitted code=(\S+) .*correct=False", text)
    hints = re.findall(r"hint.*?code=(\S+)", text)
    gives = re.findall(r"challenge give up code=(\S+)", text)
    defers = re.findall(r"challenge deferred code=(\S+)", text)
    started = re.findall(r"challenge started code=(\S+)", text)
    return {
        "exists": True, "file": log.name, "started": len(started),
        "correct": correct[-5:], "wrong_count": len(wrong),
        "hints": len(set(hints)), "gives_up": len(set(gives)), "defers": len(defers),
    }


def _star_chart() -> dict:
    dbs = sorted(glob.glob(str(Path(tempfile.gettempdir()) / "astra-runner-*.db")), key=os.path.getmtime)
    if not dbs:
        return {}
    latest = dbs[-1]
    # 只读打开（活库 WAL 并发安全；也避免误抓 pytest 临时库时加锁——仍以 mtime 最新为准）
    conn = sqlite3.connect(f"file:{latest}?mode=ro", uri=True)
    try:
        facts = conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        table = "steps" if _table_exists(conn, "steps") else "intents"  # v0.2 改名回退
        steps = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        per_worker = conn.execute(
            f"SELECT worker, COUNT(*) FROM {table} WHERE worker IS NOT NULL GROUP BY worker"
        ).fetchall()
    finally:
        conn.close()
    return {"facts": facts, "steps": steps, "per_worker": per_worker, "db": Path(latest).name}


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _worker_liveness() -> dict:
    """pi 栈（astra-pi/<worker>/）与遗留 dsh 栈（astra-dsh/<worker>/sessions）都扫。"""
    tmp = Path(tempfile.gettempdir())
    out: dict[str, str] = {}
    now = time.time()
    for home_root in ("astra-pi", "astra-dsh"):
        for home in glob.glob(str(tmp / home_root / "*")):
            files = [
                os.path.join(root, f)
                for root, _dirs, fs in os.walk(home)
                for f in fs
                if not f.endswith(("auth.json", "models.json"))
            ]
            if not files:
                continue
            latest = max(files, key=os.path.getmtime)
            out[f"{home_root}/{Path(home).name}"] = f"{int(now - os.path.getmtime(latest))}s ago"
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    board = {"platform": _platform(), "runner": _runner_log(), "chart": _star_chart(), "workers": _worker_liveness()}
    if args.json:
        board["platform"].pop("challenges", None)
        print(json.dumps(board, ensure_ascii=False, indent=1))
        return 0
    p, r, c, w = board["platform"], board["runner"], board["chart"], board["workers"]
    if "error" in p:
        print(f"▶ 平台: {p['error']}")
    else:
        print(f"▶ 解出 {p['solved']}/{p['total']} ｜ 得分 {p['score']}/{p['max_score']} ｜ 在跑 {p['running']} ｜ 部分 {p['partial']}")
    if r.get("exists"):
        print(f"▶ runner[{r['file']}]: 开题 {r['started']} ｜ 错交 {r['wrong_count']} ｜ hint {r['hints']} 题 ｜ defer {r['defers']} ｜ 放弃 {r['gives_up']} ｜ 最近正确: {r['correct']}")
    else:
        print("▶ runner: 无 dist/local-run*.log")
    if c:
        print(f"▶ 星图: facts {c['facts']} ｜ steps {c['steps']} ｜ 认领 {c['per_worker']}")
    print(f"▶ worker 活性: {w}")
    stale = [k for k, v in w.items() if int(v.split("s")[0]) > 900]
    if stale:
        print(f"⚠ 停滞 worker: {stale}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
