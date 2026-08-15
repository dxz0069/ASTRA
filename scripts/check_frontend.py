#!/usr/bin/env python3
"""前端静态资源自检：index.html 引用完整性 + server 契约验证。

用途（新前端接入 / 清理旧文件时运行，venv python 执行）：
- 校验 GET / 与 index.html 引用的全部 /static/* 资源返回 200；
- 列出"未被 index.html 引用"的 static 文件（旧前端清理候选，避免误删仍被引用的）。

用法：  python scripts/check_frontend.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from astra.server.app import STATIC_DIR, app


def main() -> int:
    index = STATIC_DIR / "index.html"
    if not index.exists():
        print(f"[FAIL] {index} 不存在（前端未就位）")
        return 1
    html = index.read_text(encoding="utf-8")
    refs = sorted(set(re.findall(r'(?:src|href)="(/static/[^"]+)"', html)))
    print(f"[OK] index.html 引用 {len(refs)} 个静态资源")

    missing = [ref for ref in refs if not (STATIC_DIR / ref.removeprefix("/static/")).exists()]
    for ref in refs:
        exists = (STATIC_DIR / ref.removeprefix("/static/")).exists()
        print(f"  {'OK     ' if exists else 'MISSING'} {ref}")
    if missing:
        print(f"[FAIL] {len(missing)} 个引用文件缺失: {missing}")
        return 1

    # server 契约：GET / 与全部 /static/* 引用
    ok = True
    with TestClient(app) as client:
        status = client.get("/").status_code
        print(f"[{'OK' if status == 200 else 'FAIL'}] GET / -> {status}")
        ok = ok and status == 200
        for ref in refs:
            status = client.get(ref).status_code
            if status != 200:
                print(f"  [FAIL] {ref} -> {status}")
                ok = False
    if not ok:
        return 1

    # 未被 index.html 引用的 static 文件（清理候选）
    # 间接引用也计入：static 下所有 css/js 文本里的 '/static/...'（如 fonts.css 的 url()、
    # app.js 的动态加载），避免误删被间接引用的资源。
    referenced = {ref.removeprefix("/static/") for ref in refs}
    for p in STATIC_DIR.rglob("*.css"):
        referenced |= {
            m.removeprefix("/static/")
            for m in re.findall(r"/static/[^'\")\s]+", p.read_text(encoding="utf-8", errors="replace"))
        }
    for p in STATIC_DIR.rglob("*.js"):
        referenced |= {
            m.removeprefix("/static/")
            for m in re.findall(r"/static/[^'\")\s]+", p.read_text(encoding="utf-8", errors="replace"))
        }
    unused = []
    for p in STATIC_DIR.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(STATIC_DIR).as_posix()
        if rel == "index.html" or rel in referenced:
            continue
        unused.append(rel)
    unused.sort()
    if unused:
        print(f"\n[INFO] 未被 index.html 引用的文件（清理候选，需确认后删除）: {len(unused)}")
        for name in unused:
            print(f"  {name}")
    else:
        print("\n[INFO] 无未引用文件（无需清理）")
    print("前端检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
