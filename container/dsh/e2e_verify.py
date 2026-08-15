#!/usr/bin/env python3
"""ASTRA × DeepSeek Harness 端到端验收脚本（需要真实模型凭据）。

验证 ASTRA 迁移的核心契约（对应 claude --session-id / -r 语义）：

  1. dsh headless 基础调用：真实模型返回结果（exit 0）
  2. 会话续接：execute 阶段让模型跑命令并记住输出 → conclude 阶段同 session
     复述（模型必须不执行命令也能回忆出前一阶段的输出）——证明 ASTRA
     bootstrap/explore 的「execute 超时 → conclude 同会话收尾」不会丢上下文
  3. 落盘检查：$DSH_HOME/sessions/ 下出现对应 session 的 JSONL 文件

用法（任选一种凭据模式，与 dispatch.yaml 的 dsh worker env 对应）：

  # deepseek 模式
  python3 container/dsh/e2e_verify.py \
      --provider deepseek --model deepseek-v4-flash \
      --api-key sk-xxx

  # anthropic 模式（Kimi / DeepSeek /anthropic 兼容端点）
  python3 container/dsh/e2e_verify.py \
      --provider anthropic --model k3 \
      --api-key sk-xxx --base-url https://api.kimi.com/coding/

前置：已安装 @deepseek-ai/dsh，且 astra-headless-runner.js 已复制进 dsh 包 lib/
（见 container/dsh/README.md）。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DSH_PATCH = REPO_ROOT / "container" / "dsh" / "astra-headless.patch.yml"


def _find_dsh() -> str:
    """定位 dsh CLI（Windows 下 node 直跑 lib/bin.js，绕过 .cmd shim）。"""
    import shutil

    resolved = shutil.which("dsh")
    if sys.platform != "win32" or not resolved:
        return resolved or "dsh"
    if resolved.lower().endswith(".cmd"):
        # npm 全局：<root>/node_modules/.bin/dsh.CMD → <root>/node_modules/@deepseek-ai/dsh/lib/bin.js
        # （npx 缓存同理：<cache>/node_modules/.bin/dsh.CMD）
        for candidate in (
            Path(resolved).resolve().parent.parent / "@deepseek-ai" / "dsh" / "lib" / "bin.js",
            Path(resolved).resolve().parent / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js",
        ):
            if candidate.exists():
                return f"node {candidate}"
    return resolved


def _run(dsh: str, dsh_home: str, env: dict[str, str], session: str | None, task: str) -> subprocess.CompletedProcess:
    argv = dsh.split() + [
        "--profile", "headless",
        "--patch", str(DSH_PATCH),
    ]
    if session:
        argv += ["--session", session]
    argv.append(task)
    run_env = {
        **os.environ,
        "DSH_HOME": dsh_home,
        "DSH_TELEMETRY_DISABLED": "1",
        "DSH_PERMISSION_MODE": "danger-full-access",
        **env,
    }
    return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", env=run_env)


def _check(name: str, ok: bool, detail: str) -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if detail:
        print(f"      {detail}")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--provider", choices=["deepseek", "anthropic"], default="deepseek")
    parser.add_argument("--model", default=None, help="模型 id（默认 deepseek-v4-flash / k3）")
    parser.add_argument("--api-key", required=True, help="DEEPSEEK_API_KEY 或 ANTHROPIC_AUTH_TOKEN")
    parser.add_argument("--base-url", default=None, help="端点 base URL（anthropic 模式必填）")
    args = parser.parse_args()

    if args.provider == "deepseek":
        model = args.model or "deepseek-v4-flash"
        env = {"DSH_PROVIDER": "deepseek", "DSH_MODEL": model, "DEEPSEEK_API_KEY": args.api_key}
        if args.base_url:
            env["DEEPSEEK_BASE_URL"] = args.base_url
    else:
        model = args.model or "k3"
        if not args.base_url:
            parser.error("anthropic 模式需要 --base-url（如 https://api.kimi.com/coding/）")
        env = {
            "DSH_PROVIDER": "anthropic",
            "DSH_MODEL": model,
            "ANTHROPIC_AUTH_TOKEN": args.api_key,
            "ANTHROPIC_BASE_URL": args.base_url,
        }

    dsh = _find_dsh()
    dsh_home = tempfile.mkdtemp(prefix="astra-dsh-e2e-")
    session_id = "session-e2e-verify-001"
    results: list[bool] = []

    print(f"dsh: {dsh}")
    print(f"provider: {args.provider}  model: {model}  DSH_HOME: {dsh_home}")
    print()

    # 1. 基础调用
    r1 = _run(dsh, dsh_home, env, None, "Reply with exactly: PONG")
    results.append(_check("基础调用（真实模型返回）", r1.returncode == 0 and "PONG" in r1.stdout, f"exit={r1.returncode} stdout={r1.stdout.strip()!r} stderr={r1.stderr.strip()[:200]!r}"))

    # 2. execute：跑命令并记住输出
    marker = "ASTRA_E2E_MARKER_42"
    r2 = _run(dsh, dsh_home, env, session_id, f"Run this exact command in the shell: echo {marker}. Then answer with the exact output of that command.")
    execute_ok = r2.returncode == 0 and marker in r2.stdout
    results.append(_check("execute（模型执行命令并返回输出）", execute_ok, f"exit={r2.returncode} stdout={r2.stdout.strip()!r}"))

    # 3. conclude：同 session 复述（关键契约：不执行命令也能回忆）
    r3 = _run(dsh, dsh_home, env, session_id, "Without running any command, what was the exact output of the command you executed in your previous turn? Answer with only that value.")
    resume_ok = "resumed session" in r3.stderr and r3.returncode == 0 and marker in r3.stdout
    results.append(_check("会话续接（conclude 复述前一阶段输出）", resume_ok, f"exit={r3.returncode} stdout={r3.stdout.strip()!r} stderr={r3.stderr.strip()[:200]!r}"))

    # 4. 落盘检查
    session_dir = Path(dsh_home) / "sessions"
    persisted = any(session_id in p.name for p in session_dir.rglob("*") if p.is_dir()) if session_dir.exists() else False
    results.append(_check("会话落盘（$DSH_HOME/sessions）", persisted, f"root={session_dir}"))

    passed = sum(results)
    print(f"\n结果: {passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
