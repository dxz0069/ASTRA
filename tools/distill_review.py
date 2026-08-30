#!/usr/bin/env python3
"""赛后记忆蒸馏流水线 CLI（核心逻辑在 astra.distill 模块，本脚本是仓库路径薄壳）。

产出（docs/review-drafts/<日期>/）三件套草稿：new-entries / corrections / skill-drafts。
用法：python tools/distill_review.py [--out docs/review-drafts]
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "astra" / "src"))  # 未 pip 安装时兜底可跑

from astra.distill import auto_distill  # noqa: E402

KB_FILE = ROOT / "container" / "knowledge" / "challenge-approaches.md"
DEADENDS_FILE = ROOT / "container" / "knowledge" / "dead-ends.md"
STATS_FILE = ROOT / "container" / "knowledge" / "memory-stats.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "review-drafts")
    args = parser.parse_args()

    tmp = Path(tempfile.gettempdir())
    out = auto_distill(
        pending_file=tmp / "astra-knowledge-append.json",
        dd_pending_file=tmp / "astra-deadends-append.json",
        stats_file=STATS_FILE,
        kb_file=KB_FILE,
        deadends_file=DEADENDS_FILE,
        out_root=args.out,
    )
    if out is None:
        print("[skip] 无沉淀且无战绩数据")
        return 0
    print(f"[done] 三件套草稿已生成 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
