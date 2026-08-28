"""V7 满血版优化测试：状态卡片 / 跨模型评审 / 主题聚簇压缩 / TDI / Constellation。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "container"))

from astra.dispatcher.config import DispatchConfig, WorkerConfig  # noqa: E402
from astra.dispatcher.context import build_focus_open_intents  # noqa: E402
from astra.dispatcher.tasks.common import review_graph_summary  # noqa: E402
from astra.dispatcher.tasks.consolidate import pick_stale_facts  # noqa: E402
from astra.dispatcher.tasks.reason import _resolve_review_worker  # noqa: E402
from astra.server.models import Fact, Intent, ProjectDetail, ProjectMeta  # noqa: E402
from conftest import make_config  # noqa: E402


def _project(facts: list[Fact], intents: list[Intent] | None = None) -> ProjectDetail:
    meta = ProjectMeta(id="p7", title="t", status="active", bootstrap_enabled=True, created_at="2026-01-01T00:00:00Z")
    return ProjectDetail(
        project=meta,
        facts=[Fact(id="goal", description="get flag"), Fact(id="origin", description="http://10.0.0.5:80"), *facts],
        intents=intents or [],
        hints=[],
    )


# ---------------- 状态卡片 ----------------

def test_state_card_structured_extractions():
    project = _project([
        Fact(id="f1", description="10.0.0.5:8080 运行 tomcat 服务"),
        Fact(id="f2", description="admin password found: 见配置文件", confidence="high"),
        Fact(id="f3", description="无关发现"),
    ])
    card = review_graph_summary(project)
    assert "战况卡" in card
    assert "10.0.0.5" in card and "8080" in card
    assert "凭据/会话" in card and "f2" in card


def test_state_card_topic_index_with_dispatch_count():
    project = _project(
        [Fact(id="f1", description="10.0.0.5:80 nginx")],
        intents=[Intent(
            id="i1", from_=["f1"], to=None, description="打注入", creator="w", worker="claudecode",
            dispatch_count=3, last_heartbeat_at="2026-01-01T00:10:00Z", created_at="2026-01-01T00:00:00Z",
        )],
    )
    card = review_graph_summary(project)
    assert "未决航向主题索引" in card and "打注入" in card
    rendered = build_focus_open_intents(project, 8)
    assert rendered[0]["dispatch_count"] == 3  # UCB 投入数据进入 reason 上下文


def test_state_card_fallback_when_no_structure():
    project = _project([Fact(id="f1", description="纯文本发现无 ip 无端口")])
    card = review_graph_summary(project)
    assert "- Facts:" in card and "纯文本发现" in card  # 回退逐条摘要


# ---------------- 跨模型评审异构 ----------------

_TYPE_ENV = {
    "mock": {},
    "claudecode": {"ANTHROPIC_MODEL": "m", "ANTHROPIC_BASE_URL": "http://x", "ANTHROPIC_AUTH_TOKEN": "x"},
    "pi": {"PI_MODEL": "m", "PI_BASE_URL": "http://x", "PI_API_KEY": "x", "PI_PROVIDER_API": "openai-completions"},
}


def _worker(name: str, wtype: str, priority: int) -> WorkerConfig:
    env = dict(_TYPE_ENV[wtype])
    return WorkerConfig(
        name=name, type=wtype, task_types=["reason"], max_running=2, priority=priority, env=env,
    )


def test_review_prefers_heterogeneous_reviewer():
    config = make_config()
    proposer = _worker("cc-main", "claudecode", 1)
    # 舰队里有不同 type 的可评审 worker（mock 审查可用；pi 显式不支持审查）
    config.workers = [proposer, _worker("mock-rev", "mock", 2)]
    reviewer, _ = _resolve_review_worker(config, proposer)
    assert reviewer.type != proposer.type


def test_review_falls_back_to_self_when_only_homogeneous():
    config = make_config()
    proposer = _worker("solo", "claudecode", 1)
    config.workers = [proposer]
    reviewer, _ = _resolve_review_worker(config, proposer)
    assert reviewer is proposer


# ---------------- 主题聚簇压缩 ----------------

def test_pick_stale_facts_clusters_by_topic():
    facts = [
        Fact(id=f"f{i}", description=d) for i, d in enumerate([
            "10.0.0.1:22 ssh open", "10.0.0.1:80 nginx", "3306 mysql weak",  # 网络服务簇
            "注入点发现 sql", "ssrf 可利用",  # 漏洞簇
            "杂项记录",  # 其他
        ])
    ]
    project = _project(facts)
    batch = pick_stale_facts(project, 3)
    ids = [b["id"] for b in batch]
    # 最大簇（网络服务×3）整批入选
    assert set(ids) == {"f0", "f1", "f2"}


def test_pick_stale_facts_respects_references_and_kinds():
    project = _project(
        [Fact(id="f1", description="端口 80"), Fact(id="f2", description="注入", kind="summary")],
        intents=[Intent(id="i1", from_=["f1"], to="f1", description="x", creator="w", created_at="t")],
    )
    assert pick_stale_facts(project, 5) == []  # 引用中的/摘要类不压缩


# ---------------- TDI 难度信号 ----------------

def test_task_difficulty_signal_zero_without_evidence(tmp_path, monkeypatch):
    import astra_runner.runner as R
    monkeypatch.setattr(R, "MEMORY_STATS_FILE", tmp_path / "stats.json")
    res = R.ChallengeResult(unique_code="c1", description="d")
    assert R._task_difficulty_signal(res) == 0.0
    res.wrong_count = 3
    res.defer_count = 4
    assert 0.25 < R._task_difficulty_signal(res) <= 0.4  # 有困境信号即升，封顶 0.4


# ---------------- Constellation 侦察共享 ----------------

def test_constellation_roundtrip_same_subnet(tmp_path, monkeypatch):
    import astra_runner.runner as R
    monkeypatch.setattr(R, "KNOWLEDGE_FILE", tmp_path / "kb.md")
    (tmp_path / "kb.md").write_text("# KB", encoding="utf-8")
    R._record_constellation("http://10.1.2.3:80", ["10.1.2.3:80 nginx 1.18 指纹", "10.1.2.3:22 ssh open"])
    text = R._constellation_text("http://10.1.2.99:8080")
    assert "Constellation" in text and "nginx" in text
    assert R._constellation_text("http://10.9.9.9:80") == ""  # 异网段不注入


def test_constellation_filters_secrets():
    import astra_runner.runner as R
    facts = R._extract_recon_facts(["10.0.0.1:80 nginx", "flag{abc}", "password=123456", "8080 tomcat"])
    assert facts == ["10.0.0.1:80 nginx", "8080 tomcat"]  # flag/凭据绝入共享卡


# ---------------- dispatch_count 迁移与递增 ----------------

def test_intent_dispatch_count_column_and_claim_increment(tmp_path):
    from astra.server import db as adb
    adb._db_path = None
    adb.configure(tmp_path / "d.db")
    with adb.get_conn() as conn:
        conn.execute(
            "INSERT INTO projects VALUES ('p1','t','active','2026-01-01',1,NULL,NULL,NULL,NULL,NULL)"
        )
        conn.execute(
            "INSERT INTO intents (id,project_id,description,creator,created_at) "
            "VALUES ('i1','p1','d','w','2026-01-01')"
        )
        conn.execute(
            "INSERT INTO facts (id,project_id,description) VALUES ('f1','p1','x')"
        )
        conn.execute(
            "INSERT INTO intent_sources (intent_id,project_id,fact_id) VALUES ('i1','p1','f1')"
        )
        # 首次认领（worker 从 NULL→w1）计一次派发
        conn.execute(
            "UPDATE intents SET worker='w1', last_heartbeat_at='t', dispatch_count=dispatch_count+1 "
            "WHERE id='i1' AND project_id='p1'"
        )
        # 同 worker 心跳续租不计数
        prev = conn.execute("SELECT worker FROM intents WHERE id='i1'").fetchone()
        inc = 1 if prev["worker"] != "w1" else 0
        conn.execute(
            "UPDATE intents SET last_heartbeat_at='t2', dispatch_count=dispatch_count+? WHERE id='i1'",
            (inc,),
        )
        count = conn.execute("SELECT dispatch_count FROM intents WHERE id='i1'").fetchone()["dispatch_count"]
        assert count == 1
    adb._db_path = None
