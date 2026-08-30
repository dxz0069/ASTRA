"""品牌词回归锁：产品面不得出现竞品（上游 Cairn 系）原创词汇。

背景（2026-08-30 清零）：FGS / Fact-Goal-Step 是榜首闭源方案文章首创缩写，
Less is More 是其哲学口号，submit_fact 是其机制命名，cairn/l3yx 是上游与作者字样
——ASTRA 是独立产品，产品面一律用自有表述（星图/星图架构/星图导航/自证入图）。

扫描范围（产品面）：astra/src、astra/tests、container/astra_runner、
container/AGENTS.md、README.md、note/。
豁免：docs/ 下的竞品调研文档（引用其公众号原文的研究记录，含文件名，
如"榜首闭源架构解读与FGS提炼.md"——note 中指向这些文件的路径引用同步豁免）；
FGSM（对抗样本学术术语 Fast Gradient Sign Method）。

判定方法存档：仓库首提交 dc24d270 为上游英文原版（Intent 系、无星辰词），
fork 后自创词（天枢/斗柄/北辰/客星/星图/摇光/玉衡/角宿）与通用英文
（Fact/Step/Goal/Finding/Decide/Execute）均不属竞品词。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

SCAN_DIRS = [
    "astra/src",
    "astra/tests",
    "container/astra_runner",
]
SCAN_FILES = [
    "container/AGENTS.md",
    "README.md",
]
SCAN_FILE_SUFFIXES = (".py", ".md")

# 竞品原创词 → 本仓替代表述（出现即失败；FGSM 由负向断言豁免）
BANNED_PATTERNS = [
    (re.compile(r"FGS(?!M)"), "FGS/Fact-Goal-Step（榜首方案首创缩写）→ 星图/星图架构"),
    (re.compile(r"Fact[-–]Goal[-–]Step", re.IGNORECASE), "Fact-Goal-Step 全称 → 事实—目标—步骤因果图"),
    (re.compile(r"Less\s+Is\s+More", re.IGNORECASE), "Less is More（竞品哲学口号）→ 删除或用自有表述"),
    (re.compile(r"submit_fact", re.IGNORECASE), "submit_fact（竞品机制名）→ 自证入图/自证写回"),
    (re.compile(r"cairn", re.IGNORECASE), "cairn 字样（上游产品名）→ ASTRA"),
    (re.compile(r"l3yx", re.IGNORECASE), "l3yx 字样（上游作者）→ 中性出处表述"),
]

# 调研文档文件名（note/docs 中指向它们的路径引用豁免）
RESEARCH_DOC_NAMES = (
    "榜首闭源架构解读与FGS提炼.md",
    "整改计划-FGS路线.md",
)

# 事实性引用豁免（精确串）：AGPL 血统声明（上游仓库路径）与本地磁盘目录名
FACTUAL_CITATIONS = (
    "oritera/Cairn",  # 上游仓库路径（fork 出处与 license 声明需要）
    "Cairn-main",     # 用户本地磁盘目录名（运维手册中的真实路径）
)


def _iter_product_files():
    self_path = Path(__file__).resolve()
    for rel_dir in SCAN_DIRS:
        for path in (REPO_ROOT / rel_dir).rglob("*"):
            if path.suffix in SCAN_FILE_SUFFIXES and "__pycache__" not in path.parts:
                if path.resolve() != self_path:  # 本测试自身含禁词样例，自豁免
                    yield path
    for rel in SCAN_FILES:
        path = REPO_ROOT / rel
        if path.exists():
            yield path
    for path in (REPO_ROOT / "note").rglob("*.md"):
        yield path


def test_product_surface_has_no_upstream_coined_terms() -> None:
    violations: list[str] = []
    for path in _iter_product_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for name in RESEARCH_DOC_NAMES:
            text = text.replace(name, "")  # 调研文件路径引用豁免
        for citation in FACTUAL_CITATIONS:
            text = text.replace(citation, "")  # 事实性引用豁免（血统声明/本地路径）
        for pattern, hint in BANNED_PATTERNS:
            match = pattern.search(text)
            if match:
                rel = path.relative_to(REPO_ROOT)
                violations.append(f"{rel}: 出现 {match.group(0)!r}（{hint}）")
    assert not violations, "产品面出现竞品原创词:\n" + "\n".join(violations)


def test_research_docs_are_the_only_place_upstream_terms_may_live() -> None:
    """docs/ 内出现竞品词的文件必须限于既定调研清单（防止新文档无意引入）。"""
    allowed_docs = {
        "榜首闭源架构解读与FGS提炼.md",
        "整改计划-FGS路线.md",
        "榜首差距分析.md",
        "与上游对比.md",
        "两届TCH决赛方案AI评审拆解.md",
        "Tsecbench前十名日志机制拆解.md",
        "Cairn_Y文章解读与架构提炼.md",
        "调研-2026安全与Agent记忆前沿.md",
        "优化路线图-架构对照论文.md",
        "论文框架.md",
        "答辩路演方案.md",
        "答辩一页纸-痛点特色差异化.md",
        "演示视频脚本.md",
        "本地调优作战记录.md",
        "R9-claudecode首轮诊断.md",
        "10089未解题审计.md",
    }
    pattern = re.compile(r"FGS(?!M)|cairn", re.IGNORECASE)
    offenders = []
    for path in (REPO_ROOT / "docs").glob("*.md"):
        try:
            text = path.name + path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for name in RESEARCH_DOC_NAMES:
            text = text.replace(name, "")
        for citation in FACTUAL_CITATIONS:
            text = text.replace(citation, "")
        if pattern.search(text) and path.name not in allowed_docs:
            offenders.append(path.name)
    assert not offenders, f"docs/ 新文档出现竞品词且不在调研豁免清单: {offenders}"
