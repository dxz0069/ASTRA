"""星尘记忆：焦点子图裁剪逻辑测试。"""

from __future__ import annotations

from astra.dispatcher.context import (
    build_focus_fact_ids,
    build_focus_hints,
    build_focus_open_intents,
)
from astra.server.models import Fact, Hint, Intent, ProjectDetail, ProjectMeta


def _project(
    facts: list[Fact],
    intents: list[Intent] | None = None,
    hints: list[Hint] | None = None,
    goal: str = "find the flag on target",
) -> ProjectDetail:
    meta = ProjectMeta(id="proj_t", title="target", status="active", bootstrap_enabled=True, created_at="2026-01-01T00:00:00Z")
    return ProjectDetail(
        project=meta,
        facts=[Fact(id="goal", description=goal), Fact(id="origin", description="http://target:8080"), *facts],
        intents=intents or [],
        hints=hints or [],
    )


def _fact(fid: str, description: str) -> Fact:
    return Fact(id=fid, description=description)


def _intent(iid: str, description: str, created_at: str) -> Intent:
    return Intent(
        id=iid,
        from_=["origin"],
        to=None,
        description=description,
        creator="worker",
        worker="w",
        created_at=created_at,
    )


def test_focus_fact_ids_returns_all_when_within_budget() -> None:
    project = _project([_fact("f1", "port 80 open"), _fact("f2", "login page found")])
    # origin 与普通星记一起保留（与旧行为一致），仅排除 goal
    assert build_focus_fact_ids(project, 60) == ["origin", "f1", "f2"]


def test_focus_fact_ids_caps_at_budget_and_keeps_graph_order() -> None:
    project = _project(
        [
            _fact("f1", "port 80 open"),
            _fact("f2", "login page found"),
            _fact("f3", "sqli on login parameter"),
            _fact("f4", "sqlmap run complete"),
            _fact("f5", "flag{} extracted from database"),
            _fact("f6", "waf fingerprint detected"),
        ]
    )
    ids = build_focus_fact_ids(project, 3)
    assert len(ids) == 3
    # 输出保持星图顺序（时间线可读）
    order = {"origin": 0, "f1": 1, "f2": 2, "f3": 3, "f4": 4, "f5": 5, "f6": 6}
    assert ids == sorted(ids, key=lambda x: order[x])


def test_focus_fact_ids_prefers_relevant_and_recent() -> None:
    project = _project(
        [
            _fact("f1", "nmap scan shows open ports 22,80"),
            _fact("f2", "http server is nginx 1.18"),
            _fact("f3", "mysql on 3306 with weak password"),
            _fact("f4", "wordpress 5.2 detected"),
        ],
        intents=[_intent("i1", "exploit sqli on login page", "2026-01-01T00:00:01Z")],
    )
    # 预算 2：与未完成航向重叠最深的 f3（sqli/login）必选
    ids = build_focus_fact_ids(project, 2)
    assert "f3" in ids
    assert len(ids) == 2


def test_focus_open_intents_keeps_newest_within_budget() -> None:
    project = _project(
        [],
        intents=[
            _intent("i1", "old direction", "2026-01-01T00:00:01Z"),
            _intent("i2", "new direction", "2026-01-01T00:00:02Z"),
            _intent("i3", "newest direction", "2026-01-01T00:00:03Z"),
        ],
    )
    focused = build_focus_open_intents(project, 2)
    assert [item["id"] for item in focused] == ["i3", "i2"]


def test_focus_hints_caps_count() -> None:
    hints = [
        Hint(id=f"h{i}", content=f"hint {i}", creator="me", created_at=f"2026-01-01T00:00:0{i}Z")
        for i in range(5)
    ]
    project = _project([], hints=hints)
    focused = build_focus_hints(project, 2)
    assert [item["id"] for item in focused] == ["h4", "h3"]


def test_budget_zero_returns_empty() -> None:
    project = _project([_fact("f1", "port 80 open"), _fact("f2", "login page found")])
    assert build_focus_fact_ids(project, 0) == []
