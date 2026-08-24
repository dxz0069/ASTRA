"""星尘记忆 V4/V5 机制测试：题型分片注入、赛中热加载、失败经验库。

环境隔离：monkeypatch runner 模块的 KNOWLEDGE_FILE/MEMORY_STATS_FILE/DEADENDS_FILE
常量与 tempfile.gettempdir()，不读真实仓库知识库、不碰真实 /tmp 赛中沉淀文件。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "container"))

import astra_runner.runner as R  # noqa: E402
from astra_runner.runner import ChallengeResult  # noqa: E402


def _isolate(tmp_path: Path, monkeypatch):
    (tmp_path / "kb.md").write_text(
        "# KB\n\n## 老注入题（old-web1）\n- 分值/难度：300 / medium ｜ 首解耗时：5min ｜ 来源：[x]\n"
        "- 思路1：登录页 sql 注入，sqlmap --tamper 绕 WAF 拿数据\n\n"
        "## 老云题（old-cloud1）\n- 分值/难度：400 / hard ｜ 首解耗时：10min ｜ 来源：[x]\n"
        "- 思路1：SSRF 打云元数据拿临时 AKSK 操作 oss 桶\n",
        encoding="utf-8",
    )
    (tmp_path / "stats.json").write_text("{}", encoding="utf-8")
    (tmp_path / "dead-ends.md").write_text(
        "# 死路\n\n## 老注入坑（dd-web1）\n- 原因：unsolved ｜ 耗时：40min ｜ 来源：[x]\n"
        "- 思路1：sqlmap 直跑被 WAF 全拦，浪费 30 分钟\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(R, "KNOWLEDGE_FILE", tmp_path / "kb.md")
    monkeypatch.setattr(R, "MEMORY_STATS_FILE", tmp_path / "stats.json")
    monkeypatch.setattr(R, "DEADENDS_FILE", tmp_path / "dead-ends.md")
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))


@dataclass
class FakeChallenge:
    unique_code: str
    description: str = "solve the challenge"
    is_completed: bool = False
    flag_count: int = 1
    total_score: int = 100


def _result(code: str, description: str = "", **kw) -> ChallengeResult:
    base = dict(
        unique_code=code, description=description, started=False,
        kb_neighbor_texts=[], kb_deadend_texts=[],
    )
    base.update(kw)
    return ChallengeResult(**base)


# ---------------- V4：题型分类与邻居注入 ----------------

def test_categorize_priority_and_miss():
    assert R._categorize("云桶利用题", "oss bucket ak/sk") == "cloud"
    assert R._categorize("登录注入", "sql 注入 union") == "web"
    assert R._categorize("apk 逆向") == "mobile"
    assert R._categorize("rsa 公钥分解") == "crypto"
    assert R._categorize("完全无关的题目描述 xyz") is None


def test_pick_neighbor_entries_only_same_category_with_reinforcement(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    (tmp_path / "stats.json").write_text(
        json.dumps({"old-web1": {"name": "老注入题", "hits": 3, "misses": 0}}), encoding="utf-8"
    )
    kb = R._load_knowledge_base()
    nb = R._pick_neighbor_entries(kb, "new-web", "某站登录后台 sql 注入点")
    assert len(nb) == 1 and "sqlmap" in nb[0] and "3 次命中" in nb[0]
    # 密码学题无同类邻居 → 不注入
    assert R._pick_neighbor_entries(kb, "new-crypto", "rsa 公钥分解") == []


# ---------------- V4：赛中实时热加载 ----------------

def test_load_runtime_knowledge_merges_pending_without_overwrite(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    (tmp_path / "astra-knowledge-append.json").write_text(
        json.dumps({"live-99": {"name": "赛中解题", "first_flag_seconds": 120,
                                 "approach": "云元数据 169.254 拿角色凭据"}}),
        encoding="utf-8",
    )
    kb = R._load_runtime_knowledge()
    assert "live-99" in kb and "old-web1" in kb
    # 仓库条目优先：赛中沉淀同码不覆盖仓库思路
    (tmp_path / "astra-knowledge-append.json").write_text(
        json.dumps({"old-web1": {"name": "老注入题", "approach": "赛中覆盖版"}}), encoding="utf-8"
    )
    kb2 = R._load_runtime_knowledge()
    assert "sqlmap" in kb2["old-web1"]["approach"]


def test_attach_knowledge_skips_started_and_fills_neighbors_deadends(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    kb = R._load_knowledge_base()
    started = _result("new-web", "sql 注入题", started=True)
    fresh = _result("new-web", "sql 注入题")
    queue = [(FakeChallenge("new-web"), started), (FakeChallenge("new-web"), fresh)]
    R._attach_knowledge(queue, kb)
    assert not started.kb_neighbor_texts and not started.kb_deadend_texts  # 已开题不回填
    assert fresh.kb_neighbor_texts and "sqlmap" in fresh.kb_neighbor_texts[0]
    assert fresh.kb_deadend_texts and "WAF" in fresh.kb_deadend_texts[0]
    # 二次挂载幂等：已有邻居/避坑不重复
    before = list(fresh.kb_neighbor_texts)
    R._attach_knowledge(queue, kb)
    assert fresh.kb_neighbor_texts == before


# ---------------- V5：失败经验库 ----------------

def test_append_deadend_entry_sanitizes_and_classifies_reason(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    res = _result(
        "failed-01", flags_correct=0, defer_count=2,
        kb_approach_draft="sqlmap 直跑被拦，改 tamper 失败 flag{secret123abc456def}",
        elapsed_seconds=2700.0,
    )
    R._append_deadend_entry(res)
    data = json.loads((tmp_path / "astra-deadends-append.json").read_text(encoding="utf-8"))
    assert data["failed-01"]["reason"] == "defer-giveup"
    assert "已脱敏" in data["failed-01"]["deadend"]


def test_load_deadends_merges_runtime_and_pick_filters_category(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    (tmp_path / "astra-deadends-append.json").write_text(
        json.dumps({"failed-crypto": {"name": "crypto坑", "deadend": "费马分解超时"}}), encoding="utf-8"
    )
    dd = R._load_deadends()
    assert set(dd) == {"dd-web1", "failed-crypto"}
    warns = R._pick_deadend_warnings(dd, "new-web", "sql 注入点")
    assert len(warns) == 1 and "WAF" in warns[0] and "前车之鉴" in warns[0]
    assert R._pick_deadend_warnings(dd, "new-crypto", "rsa") == []  # 同类无死路不注入


def test_record_memory_stats_hit_and_miss(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    hit = _result("old-web1", flags_correct=2, kb_entry_text="思路", elapsed_seconds=60.0)
    miss = _result("new-x", flags_correct=0, kb_entry_text="思路", elapsed_seconds=60.0)
    R._record_memory_stats(hit)
    R._record_memory_stats(miss)
    stats = json.loads((tmp_path / "stats.json").read_text(encoding="utf-8"))
    assert stats["old-web1"]["hits"] == 1 and stats["new-x"]["misses"] == 1
    assert "1 次命中" in R._memory_reinforcement_text("old-web1")
