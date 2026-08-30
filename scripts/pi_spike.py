# -*- coding: utf-8 -*-
"""pi 双网关 spike 汇总跑（DS anthropic-messages + Zhipu GLM）。

经验教训（已实证）：
1. pi 0.73.0 的 anthropic 协议 api id = "anthropic-messages"（不是 "anthropic"）
2. 环境变量 ANTHROPIC_AUTH_TOKEN 会覆盖 models.json 的 apiKey —— 子进程 env 必须清洗
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SPIKE = REPO / "tmp" / "pi-spike"
NPM_CLI = None


def find_pi_cli() -> str:
    global NPM_CLI
    if NPM_CLI:
        return NPM_CLI
    import shutil
    npm = shutil.which("npm") or r"D:\software\nodejs\npm.cmd"
    root = subprocess.run([npm, "root", "-g"], capture_output=True, text=True, timeout=30).stdout.strip()
    cli = Path(root) / "@mariozechner" / "pi-coding-agent" / "dist" / "cli.js"
    assert cli.exists(), f"pi cli.js not found at {cli}"
    NPM_CLI = str(cli)
    return NPM_CLI


def read_key(env_file: str, name: str) -> str:
    for line in (REPO / "dist" / env_file).read_text(encoding="utf-8").splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    return ""


def write_models(dir_: Path, base_url: str, api: str, key: str, model: str, extra_model: dict | None = None) -> None:
    (dir_ / "sessions").mkdir(parents=True, exist_ok=True)
    m = {"id": model, "name": model, "contextWindow": 131072, "maxTokens": 16384}
    if extra_model:
        m.update(extra_model)
    payload = {"providers": {"astra": {"baseUrl": base_url, "api": api, "apiKey": key, "models": [m]}}}
    (dir_ / "models.json").write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")


def run_pi(agent_dir: Path, args: list[str], timeout: int = 180) -> list[dict]:
    """运行 pi 并返回解析后的事件流；env 清洗 ANTHROPIC_*。"""
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}
    env["PI_CODING_AGENT_DIR"] = str(agent_dir)
    env["MSYS_NO_PATHCONV"] = "1"
    cmd = ["node", find_pi_cli(),
           "--no-extensions", "--no-skills", "--no-prompt-templates", "--no-themes", "--no-context-files",
           *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env,
                          cwd=str(agent_dir), encoding="utf-8", errors="replace")
    events = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if not events:
        events.append({"type": "raw", "stderr": (proc.stderr or "")[:500], "returncode": proc.returncode})
    return events


def assistant_text(events: list[dict]) -> str:
    texts = []
    for ev in events:
        msg = ev.get("message") or {}
        if msg.get("role") != "assistant":
            continue
        for c in msg.get("content") or []:
            if c.get("type") == "text" and c.get("text"):
                texts.append(c["text"])
    return "\n".join(texts)


def last_usage(events: list[dict]) -> dict:
    for ev in reversed(events):
        msg = ev.get("message") or {}
        u = msg.get("usage")
        if u:
            return u
    return {}


def tool_calls(events: list[dict]) -> list[str]:
    calls = []
    for ev in events:
        msg = ev.get("message") or {}
        for c in msg.get("content") or []:
            if isinstance(c, dict) and c.get("type") == "toolCall":
                calls.append(c.get("toolName", "?"))
    return calls


def extract_session_id(events: list[dict]) -> str | None:
    for ev in events:
        if ev.get("type") == "session":
            return ev.get("id")
    return None


def main() -> int:
    results: list[tuple[str, str, str]] = []  # (name, ok, detail)

    def record(name, ok, detail=""):
        results.append((name, "PASS" if ok else "FAIL", detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}", flush=True)

    ds_key = read_key("hosted.env", "ANTHROPIC_AUTH_TOKEN")
    zp_key = read_key("hosted.env", "ZHIPU_API_KEY")
    ds_dir = SPIKE / "ds"
    zp_dir = SPIKE / "zhipu"
    write_models(ds_dir, "https://api.deepseek.com/anthropic", "anthropic-messages", ds_key, "deepseek-v4-flash")

    # ---- DS: pong ----
    ev = run_pi(ds_dir, ["--provider", "astra", "--model", "deepseek-v4-flash", "--mode", "json",
                         "--session-dir", str(ds_dir / "sessions"), "--no-session", "--no-tools",
                         "-p", "Reply with exactly pong."])
    txt = assistant_text(ev).strip().lower()
    record("DS pong", txt == "pong" or "pong" in txt, f"text={txt[:40]!r} usage={last_usage(ev).get('totalTokens')}")

    # ---- DS: bash 工具调用（含中文长 prompt） ----
    ev = run_pi(ds_dir, ["--provider", "astra", "--model", "deepseek-v4-flash", "--mode", "json",
                         "--session-dir", str(ds_dir / "sessions"), "--no-session",
                         "--tools", "read,write,edit,bash,grep,find,ls",
                         "-p", "用 bash 执行 echo spike-hello-星官 然后把输出原样告诉我。"])
    txt = assistant_text(ev)
    calls = tool_calls(ev)
    record("DS bash 工具+中文", "spike-hello" in txt and "bash" in calls,
           f"tools={calls} text_has_output={'spike-hello' in txt} usage={last_usage(ev).get('totalTokens')}")

    # ---- DS: 会话续接（--session 后复用） ----
    ev1 = run_pi(ds_dir, ["--provider", "astra", "--model", "deepseek-v4-flash", "--mode", "json",
                          "--session-dir", str(ds_dir / "sessions"),
                          "-p", "记住暗号：北斗七星。只回答：已记住"])
    sid = extract_session_id(ev1)
    record("DS 会话创建", sid is not None, f"session={sid and sid[:8]}…")
    if sid:
        ev2 = run_pi(ds_dir, ["--provider", "astra", "--model", "deepseek-v4-flash", "--mode", "json",
                              "--session-dir", str(ds_dir / "sessions"), "--session", sid,
                              "-p", "暗号是什么？只回答暗号本身。"])
        txt2 = assistant_text(ev2)
        record("DS 会话续接", "北斗" in txt2, f"recall={txt2.strip()[:30]!r}")

    # ---- Zhipu GLM: pong（anthropic-messages + thinking 配置） ----
    if zp_key:
        write_models(zp_dir, "https://open.bigmodel.cn/api/anthropic", "anthropic-messages", zp_key,
                     "glm-5.3",
                     extra_model={"reasoning": True, "thinkingLevelMap": {"low": "low", "medium": "medium", "high": "max", "max": "max"}})
        ev = run_pi(zp_dir, ["--provider", "astra", "--model", "glm-5.3:low", "--mode", "json",
                             "--session-dir", str(zp_dir / "sessions"), "--no-session", "--no-tools",
                             "-p", "Reply with exactly pong."], timeout=240)
        txt = assistant_text(ev).strip().lower()
        err = next((m.get("errorMessage", "") for m in reversed(ev) if isinstance(m, dict) and m.get("message", {}).get("errorMessage")), "")
        record("GLM pong (thinking=low)", "pong" in txt, f"text={txt[:40]!r} usage={last_usage(ev).get('totalTokens')} err={err[:80]}")
    else:
        record("GLM pong", False, "hosted.env 无 ZHIPU_API_KEY")

    # ---- 汇总 ----
    print("\n=== spike 汇总 ===")
    for name, ok, detail in results:
        print(f"{ok:4} | {name} | {detail}")
    return 0 if all(r[1] == "PASS" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
