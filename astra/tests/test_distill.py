"""赛后自动蒸馏测试：三件套草稿产出 / 无数据跳过 / env 门控 / runner 接入。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "container"))

from astra.distill import auto_distill, distill_corrections  # noqa: E402
import astra_runner.runner as R  # noqa: E402


def _prepare(tmp_path: Path):
    (tmp_path / "kb.md").write_text(
        "# KB\n\n## 老注入题（old-web1）\n- 分值/难度：300 / medium\n- 思路1：sql 注入\n", encoding="utf-8"
    )
    (tmp_path / "dd.md").write_text(
        "# 死路\n\n## 老注入坑（old-web1）\n- 原因：unsolved\n- 思路1：WAF 全拦\n", encoding="utf-8"
    )
    (tmp_path / "stats.json").write_text(
        json.dumps({"old-web1": {"name": "老注入题", "hits": 1, "misses": 5}}), encoding="utf-8"
    )
    (tmp_path / "pending.json").write_text(
        json.dumps({"new-1": {"name": "新题", "elapsed_seconds": 300,
                              "approach": "sqlmap 注入拿数据，nmap 扫端口"}}), encoding="utf-8"
    )
    (tmp_path / "dd-pending.json").write_text(
        json.dumps({"bad-1": {"name": "失败题", "deadend": "msf 利用失败"}}), encoding="utf-8"
    )


def test_auto_distill_produces_three_drafts(tmp_path, monkeypatch):
    monkeypatch.delenv("ASTRA_LLM_API_KEY", raising=False)  # 规则模式
    _prepare(tmp_path)
    out = auto_distill(
        pending_file=tmp_path / "pending.json",
        dd_pending_file=tmp_path / "dd-pending.json",
        stats_file=tmp_path / "stats.json",
        kb_file=tmp_path / "kb.md",
        deadends_file=tmp_path / "dd.md",
        out_root=tmp_path / "drafts",
    )
    assert out is not None
    names = {p.name for p in out.iterdir()}
    assert names == {"new-entries.md", "corrections.md", "skill-drafts.md"}
    new_entries = (out / "new-entries.md").read_text(encoding="utf-8")
    assert "## 新题（new-1）" in new_entries and "sqlmap" in new_entries
    corrections = (out / "corrections.md").read_text(encoding="utf-8")
    assert "降权" in corrections  # 未命中(5) > 命中(1)
    skills = (out / "skill-drafts.md").read_text(encoding="utf-8")
    assert "playbook" in skills and "Web安全" in skills


def test_auto_distill_skips_when_no_data(tmp_path):
    (tmp_path / "kb.md").write_text("# KB\n", encoding="utf-8")
    assert auto_distill(
        pending_file=tmp_path / "none1.json",
        dd_pending_file=tmp_path / "none2.json",
        stats_file=tmp_path / "none3.json",
        kb_file=tmp_path / "kb.md",
        deadends_file=tmp_path / "dd.md",
        out_root=tmp_path / "drafts",
    ) is None
    assert not (tmp_path / "drafts").exists()


def test_distill_corrections_flags_kb_deadend_overlap():
    kb = "# KB\n\n## 同题（x-1）\n- 思路1：打法\n"
    dd = "# 死路\n\n## 同题坑（x-1）\n- 原因：unsolved\n"
    out = distill_corrections({}, kb, dd)
    assert "同时出现在知识库" in out


def test_runner_auto_distill_env_gate_and_output_dir(tmp_path, monkeypatch):
    """runner 收尾接入：env 开→草稿落在 progress 同目录；env 关→零副作用。"""
    monkeypatch.delenv("ASTRA_LLM_API_KEY", raising=False)
    _prepare(tmp_path)
    monkeypatch.setattr(R, "KNOWLEDGE_FILE", tmp_path / "kb.md")
    monkeypatch.setattr(R, "DEADENDS_FILE", tmp_path / "dd.md")
    monkeypatch.setattr(R, "MEMORY_STATS_FILE", tmp_path / "stats.json")
    monkeypatch.setattr(R, "KNOWLEDGE_APPEND_FILE", tmp_path / "pending.json")
    monkeypatch.setattr(R, "DEADENDS_APPEND_FILE", tmp_path / "dd-pending.json")

    monkeypatch.setenv("ASTRA_AUTO_DISTILL", "0")
    R._auto_distill(str(tmp_path / "progress.json"))
    assert not (tmp_path / "review-drafts").exists()

    monkeypatch.setenv("ASTRA_AUTO_DISTILL", "1")
    R._auto_distill(str(tmp_path / "progress.json"))
    out_dirs = list((tmp_path / "review-drafts").iterdir())
    assert len(out_dirs) == 1
    assert {p.name for p in out_dirs[0].iterdir()} == {"new-entries.md", "corrections.md", "skill-drafts.md"}
