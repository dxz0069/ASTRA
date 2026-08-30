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
    monkeypatch.setattr(R, "KNOWLEDGE_APPEND_FILE", tmp_path / "astra-knowledge-append.json")
    monkeypatch.setattr(R, "DEADENDS_APPEND_FILE", tmp_path / "astra-deadends-append.json")
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


def test_attach_knowledge_proven_dead_exact_entry_not_injected(tmp_path, monkeypatch):
    """战绩淘汰：0 命中且 ≥6 未命中的精确条目不再注入（f1-04 型 0/24 负资产）。"""
    _isolate(tmp_path, monkeypatch)
    kb = R._load_knowledge_base()
    assert "old-web1" in kb
    (tmp_path / "stats.json").write_text(
        json.dumps({"old-web1": {"name": "老注入题", "hits": 0, "misses": 24}}), encoding="utf-8"
    )
    fresh = _result("old-web1", "sql 注入题")
    R._attach_knowledge([(FakeChallenge("old-web1"), fresh)], kb)
    assert not fresh.kb_entry_text
    # 命中过的条目（hits>0）不受淘汰影响
    (tmp_path / "stats.json").write_text(
        json.dumps({"old-web1": {"name": "老注入题", "hits": 1, "misses": 8}}), encoding="utf-8"
    )
    fresh2 = _result("old-web1", "sql 注入题")
    R._attach_knowledge([(FakeChallenge("old-web1"), fresh2)], kb)
    assert fresh2.kb_entry_text and "sqlmap" in fresh2.kb_entry_text


def test_pick_neighbor_entries_skips_negative_weight(tmp_path, monkeypatch):
    """负权重邻居（未命中多于命中）不打扰——参考被实战证伪还注入=负资产。"""
    _isolate(tmp_path, monkeypatch)
    (tmp_path / "stats.json").write_text(
        json.dumps({"old-web1": {"name": "老注入题", "hits": 0, "misses": 5}}), encoding="utf-8"
    )
    kb = R._load_knowledge_base()
    assert R._pick_neighbor_entries(kb, "new-web", "登录后台 sql 注入") == []
    # 从未用过的邻居（weight=1.0）仍可注入
    (tmp_path / "stats.json").write_text("{}", encoding="utf-8")
    nb = R._pick_neighbor_entries(kb, "new-web", "登录后台 sql 注入")
    assert nb and "sqlmap" in nb[0]


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


# ---------------- 审计13轮：赛中沉淀并发写 + 假旗过滤精度 ----------------

def test_sediment_writers_concurrent_no_lost_update(tmp_path, monkeypatch):
    """3-4 槽并发收尾：战绩/死路/知识沉淀读改写加锁后并行写不丢条目、不撕裂 JSON。"""
    import threading

    _isolate(tmp_path, monkeypatch)
    results = [
        _result(f"cc-{i:02d}", "并发题", flags_correct=1, elapsed_seconds=10.0)
        for i in range(12)
    ]
    for res in results:
        res.kb_approach_draft = f"打法 {res.unique_code}"
        res.kb_entry_text = "参考思路"  # 使 _record_memory_stats 记账

    def worker(res):
        R._record_memory_stats(res)
        R._append_deadend_entry(res)
        R._append_knowledge_entry(res, ["天枢A", "天枢B"])

    threads = [threading.Thread(target=worker, args=(r,)) for r in results]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    stats = json.loads((tmp_path / "stats.json").read_text(encoding="utf-8"))
    deadends = json.loads((tmp_path / "astra-deadends-append.json").read_text(encoding="utf-8"))
    knowledge = json.loads((tmp_path / "astra-knowledge-append.json").read_text(encoding="utf-8"))
    assert set(stats) == {r.unique_code for r in results}, "战绩并发丢更新"
    assert set(deadends) == {r.unique_code for r in results}, "死路并发丢更新"
    assert set(knowledge) == {r.unique_code for r in results}, "知识并发丢更新"


def test_junk_flag_filter_strict_mask_marker(tmp_path):
    """掩码标记严格型：纯标记内容=假旗；真旗含标记子串不误杀（丢真旗代价=整题）。"""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "container" / "astra_runner"))
    from tsecbench_adapter import extract_flags as ef

    # 假旗：变异占位符（r7 实例）与纯标记形态全灭
    assert ef("星记描述中必须包含完整 flag{...已脱敏...} 字符串") == []
    assert ef("flag{redacted...}") == []
    assert ef("flag{[masked]}") == []
    assert ef("flag{***}") == []
    # 真旗：内容含标记子串但有实质字符 → 必须提取
    assert ef("输出 flag{redacted_a3f9c2}") == ["flag{redacted_a3f9c2}"]
    assert ef("输出 flag{masked_key_9917}") == ["flag{masked_key_9917}"]


def test_record_memory_stats_hit_and_miss(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    hit = _result("old-web1", flags_correct=2, kb_entry_text="思路", elapsed_seconds=60.0)
    miss = _result("new-x", flags_correct=0, kb_entry_text="思路", elapsed_seconds=60.0)
    R._record_memory_stats(hit)
    R._record_memory_stats(miss)
    stats = json.loads((tmp_path / "stats.json").read_text(encoding="utf-8"))
    assert stats["old-web1"]["hits"] == 1 and stats["new-x"]["misses"] == 1
    assert "1 次命中" in R._memory_reinforcement_text("old-web1")


def test_hosted_kb_disabled_gates_all_preloaded_memory(tmp_path, monkeypatch):
    """托管合规：ASTRA_KB_DISABLED=1 屏蔽预置知识/死路/星座，赛内实时沉淀不受影响。"""
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("ASTRA_KB_DISABLED", "1")
    assert R._load_knowledge_base() == {}
    assert R._load_deadends() == {}
    assert R._load_constellation() == {}
    # 赛中实时沉淀仍工作（写入 /tmp 的 pending 照常被运行时知识合并读取）
    import json as _json

    (tmp_path / "astra-knowledge-append.json").write_text(
        _json.dumps({"live-1": {"name": "赛中题", "approach": "赛中打出的思路"}}), encoding="utf-8"
    )
    kb = R._load_runtime_knowledge()
    assert "live-1" in kb


# ---------------- 遗忘曲线：战绩 × 时间衰减 ----------------

def test_recency_decay_boundaries():
    from datetime import datetime, timedelta

    assert R._recency_decay("") == 1.0
    assert R._recency_decay("garbage") == 1.0
    assert R._recency_decay(datetime.now().isoformat()) == 1.0
    # 半衰期 30 天：30 天前 ≈ 0.5，90 天前 ≈ 0.125
    assert 0.45 <= R._recency_decay((datetime.now() - timedelta(days=30)).isoformat()) <= 0.55
    assert 0.10 <= R._recency_decay((datetime.now() - timedelta(days=90)).isoformat()) <= 0.15


def test_neighbor_picking_prefers_recent_over_stale_same_record(tmp_path, monkeypatch):
    """遗忘曲线：同战绩条目，近期实战验证的排在前，久未使用的自然沉底。"""
    from datetime import datetime, timedelta

    _isolate(tmp_path, monkeypatch)
    (tmp_path / "kb.md").write_text(
        "# KB\n\n"
        "## 老注入题（old-web1）\n- 分值/难度：300 / medium ｜ 首解耗时：5min ｜ 来源：[x]\n"
        "- 思路1：登录页 sql 注入，sqlmap --tamper 绕 WAF 拿数据\n\n"
        "## 另一注入题（old-web2）\n- 分值/难度：300 / medium ｜ 首解耗时：5min ｜ 来源：[x]\n"
        "- 思路1：宽字节注入绕过转义拿数据\n",
        encoding="utf-8",
    )
    now = datetime.now()
    (tmp_path / "stats.json").write_text(
        json.dumps({
            "old-web1": {"name": "老注入题", "hits": 3, "misses": 1,
                         "last_used": (now - timedelta(days=1)).isoformat(timespec="seconds")},
            "old-web2": {"name": "另一注入题", "hits": 3, "misses": 1,
                         "last_used": (now - timedelta(days=120)).isoformat(timespec="seconds")},
        }),
        encoding="utf-8",
    )
    kb = R._load_knowledge_base()
    nb = R._pick_neighbor_entries(kb, "new-web", "某站登录后台 sql 注入点", limit=2)
    assert len(nb) == 2
    assert "老注入题" in nb[0]  # 近期验证（1 天前）满权重在前
    assert "宽字节" in nb[1]  # 久未使用（120 天前）衰减沉底


# ---------------- 沉淀卫生：注入记忆不进知识库 ----------------

def test_sediment_filter_strips_injected_memory_lines():
    """开局注入的记忆 fact（参考文本）不得混入沉淀——防知识库复读自己的注入。"""
    descs = [
        "目标：拿下 web 服务 flag",
        "[历史思路参考·知识库]（该思路历史战绩：3 次命中/0 次未命中）登录页 sql 注入",
        "[同题型经验·举一反三][Web安全·老题] 宽字节注入",
        "[同题型避坑提示·失败经验库][前车之鉴] sqlmap 直跑被 WAF 全拦",
        "[同网段侦察共享·Constellation] 10.0.0.0/24 开放 80/22",
        "实测：union 注入拿回 admin 密码哈希",
    ]
    kept = R._sediment_fact_filter(descs)
    assert kept == ["目标：拿下 web 服务 flag", "实测：union 注入拿回 admin 密码哈希"]


def test_append_knowledge_entry_excludes_injected_facts(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    res = _result("solved-01", flags_correct=1, elapsed_seconds=300.0)
    R._append_knowledge_entry(
        res,
        [
            "[历史思路参考·知识库] 登录页 sql 注入打法",
            "实测 union 注入拿回数据",
            "flag 提交成功",
        ],
    )
    data = json.loads((tmp_path / "astra-knowledge-append.json").read_text(encoding="utf-8"))
    approach = data["solved-01"]["approach"]
    assert "历史思路参考" not in approach
    assert "union 注入" in approach and "flag 提交成功" in approach
