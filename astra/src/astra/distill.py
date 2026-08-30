from __future__ import annotations

"""赛后记忆蒸馏（"睡眠巩固"）：一轮跑完 → 自动产出三件套草稿，人工只审核不搬运。

产出（<out_root>/<时间戳>/）：
  1. new-entries.md   新知识库条目草稿（来自赛中沉淀，LLM 润色或规则原样）
  2. corrections.md   旧条目修正建议（memory-stats 里未命中>命中 → 建议降权/复核；
                      死路库与知识库同题型矛盾 → 提示对勘）
  3. skill-drafts.md  值得固化为 SKILL.md 的打法草稿（按题型聚合 + 工具词频提取）

双模式：
  - 配置 ASTRA_LLM_API_KEY（OpenAI 兼容 chat 端点，可选 ASTRA_LLM_API_URL/ASTRA_LLM_MODEL）
    → LLM 蒸馏（质量高）；
  - 未配置 → 纯规则模式（零依赖离线可用，草稿质量低一档但结构完整）。

合并回知识库仍走 tools/merge_knowledge.py 人工流程——蒸馏只产草稿，人只审核。
原 tools/distill_review.py 为本模块的薄壳（保持 CLI 兼容）。
"""

import json
import os
import re
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path

KB_ENTRY_RE = re.compile(r"^## (.+?)（([a-z0-9-]+)）\s*$", re.MULTILINE)
TOOL_KEYWORDS = (
    "sqlmap", "nmap", "burp", "ffuf", "gobuster", "dirsearch", "hydra", "hashcat",
    "john", "metasploit", "msf", "nuclei", "httpx", "subfinder", "frida", "objection",
    "jadx", "ghidra", "ida", "apktool", "nc ", "反弹", "webshell", "蚁剑", "冰蝎",
)


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _llm_available() -> bool:
    return bool(os.environ.get("ASTRA_LLM_API_KEY"))


def llm_chat(prompt: str) -> str | None:
    url = os.environ.get("ASTRA_LLM_API_URL", "https://open.bigmodel.cn/api/paas/v4/chat/completions")
    model = os.environ.get("ASTRA_LLM_MODEL", "glm-4-flash")
    payload = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}]}).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {os.environ['ASTRA_LLM_API_KEY']}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]
    except Exception:  # noqa: BLE001
        return None


def distill_new_entries(pending: dict) -> str:
    """新条目草稿：LLM 把攻击链 JSON 浓缩为可注入的知识库条目；无 LLM 则规则排版。"""
    if not pending:
        return "（本轮无新增沉淀）\n"
    if _llm_available():
        result = llm_chat(
            "以下是安全评测中自动沉淀的解题攻击链 JSON（code→{name,elapsed_seconds,approach}）。"
            "请逐条改写为知识库条目：每条含 ①一句话题型判断 ②核心攻击链（3-5 步、工具/端点具体化）"
            "③关键坑点。脱敏 flag/secret。markdown 输出，每条以 '## <题名>（<code>）' 开头。"
            f"输入：{json.dumps(pending, ensure_ascii=False)}"
        )
        if result:
            return result + "\n"
    lines = []
    for code, d in pending.items():
        lines.append(f"## {d.get('name', code)}（{code}）\n- 待人工润色 ｜ 首解耗时：{round((d.get('elapsed_seconds') or 0) / 60)}min\n- 思路1：{(d.get('approach') or '')[:600]}\n")
    return "\n".join(lines)


def distill_corrections(stats: dict, kb_text: str, dd_text: str) -> str:
    """修正建议：战绩差的条目建议降权复核；死路与打法同题型对勘。

    审计16轮：题码匹配从子串 in 改为 KB 条目正则提取的精确集合——
    旧实现 "a-1" in text 会命中（a-10）/（a-12），修正建议张冠李戴。
    """
    kb_codes = {m.group(2).lower() for m in KB_ENTRY_RE.finditer(kb_text)}
    lines = []
    for code, st in stats.items():
        hits, misses = int(st.get("hits", 0)), int(st.get("misses", 0))
        if misses > hits and code.lower() in kb_codes:
            lines.append(
                f"- **{st.get('name', code)}（{code}）**：{hits} 命中/{misses} 未命中——"
                f"建议复核该条目是否过时/实例已变，考虑降权或标注适用条件"
            )
    dd_codes = {m.group(2) for m in KB_ENTRY_RE.finditer(dd_text)}
    for code in dd_codes:
        if code.lower() in kb_codes:
            lines.append(
                f"- **{code}**：同时出现在知识库（已解出）与死路库（未解出）——"
                f"不同实例分叉，建议在条目中标注适用条件而非删除"
            )
    return "\n".join(lines) + "\n" if lines else "（无修正建议——所有条目战绩健康）\n"


def distill_skills(pending: dict, dd_pending: dict) -> str:
    """技能草稿：聚合本轮攻击链的题型与工具词频，产出 SKILL.md 骨架（Voyager 式自写技能库）。"""
    corpus = [d.get("approach") or "" for d in pending.values()] + [d.get("deadend") or "" for d in dd_pending.values()]
    blob = "\n".join(corpus).lower()
    if not blob:
        return "（本轮素材不足以提炼新技能）\n"
    tools = Counter(t.strip() for t in TOOL_KEYWORDS if t in blob)
    cats = []
    for cat, kws in (
        ("Web安全", ("sql", "xss", "ssrf", "上传", "webshell", "jwt", "反序列化")),
        ("云安全", ("桶", "oss", "iam", "aksk", "元数据", "redis")),
        ("移动安全", ("apk", "dex", "android", "asar")),
        ("密码学", ("rsa", "cipher", "加密", "随机数")),
        ("二进制", ("溢出", "rop", "堆", "栈")),
    ):
        if any(k in blob for k in kws):
            cats.append(cat)
    tool_line = "、".join(f"{t}({c})" for t, c in tools.most_common()) or "无明显工具特征"
    cat_line = "、".join(cats) or "未识别"
    skill = (
        f"### 建议新技能：{cat_line} 实战组合打法\n\n"
        f"```yaml\n---\nname: {('-'.join(cats) or 'mixed').lower()}-playbook\ndescription: 本轮实战蒸馏的{cat_line}组合打法"
        f"（高频工具：{tool_line}）\n---\n```\n\n"
        "**触发条件**：（人工补充——什么题型/指纹出现时用本技能）\n\n"
        "**打法骨架**（从本轮攻击链提取，人工校验后固化）：\n"
    )
    for code, d in list(pending.items())[:5]:
        skill += f"1. {d.get('name', code)}：{(d.get('approach') or '')[:150]}…\n"
    if dd_pending:
        skill += "\n**已知死路（写进技能防重蹈）**：\n"
        for code, d in list(dd_pending.items())[:3]:
            skill += f"- {d.get('name', code)}：{(d.get('deadend') or '')[:120]}…\n"
    return skill


def _default_out_root(kb_file: Path) -> Path:
    """输出目录：env 指定 > 仓库 docs/review-drafts（从知识库位置向上找）> 知识库同目录。"""
    env_dir = os.environ.get("ASTRA_DISTILL_DIR")
    if env_dir:
        return Path(env_dir)
    for parent in kb_file.resolve().parents:
        candidate = parent / "docs" / "review-drafts"
        if candidate.is_dir():
            return candidate
    return kb_file.parent / "review-drafts"


def auto_distill(
    pending_file: Path,
    dd_pending_file: Path,
    stats_file: Path,
    kb_file: Path,
    deadends_file: Path,
    out_root: Path | None = None,
) -> Path | None:
    """跑一遍蒸馏，产出三件套草稿；无沉淀且无战绩数据时跳过（返回 None）。

    任何写失败向上抛 OSError 由调用方决定降级（runner 侧全吞）。
    """
    pending = _load_json(pending_file)
    dd_pending = _load_json(dd_pending_file)
    stats = _load_json(stats_file)
    if not pending and not dd_pending and not stats:
        return None

    kb_text = kb_file.read_text(encoding="utf-8") if kb_file.exists() else ""
    dd_text = deadends_file.read_text(encoding="utf-8") if deadends_file.exists() else ""

    mode = "LLM 蒸馏" if _llm_available() else "规则模式（配置 ASTRA_LLM_API_KEY 可升级为 LLM 蒸馏）"
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    out_dir = (out_root or _default_out_root(kb_file)) / stamp
    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "new-entries.md").write_text(
        f"# 新知识库条目草稿（{stamp}，{mode}）\n\n> 来源：赛中自动沉淀，人工审核后合并进 challenge-approaches.md\n\n"
        + distill_new_entries(pending), encoding="utf-8")
    (out_dir / "corrections.md").write_text(
        f"# 旧条目修正建议（{stamp}）\n\n" + distill_corrections(stats, kb_text, dd_text), encoding="utf-8")
    (out_dir / "skill-drafts.md").write_text(
        f"# 新技能草稿（{stamp}，{mode}）\n\n> 目标：container/.agents/skills/<name>/SKILL.md，人工校验后固化\n\n"
        + distill_skills(pending, dd_pending), encoding="utf-8")
    return out_dir
