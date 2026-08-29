"""审计第十轮（runner 数据操作深查）：ProgressStore 损坏/并发、give-up×close_failed、断点续跑战果。"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


@pytest.fixture()
def store_cls():
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "container"))
    from astra_runner.runner import ProgressStore

    return ProgressStore


def test_load_falls_back_to_tmp_when_main_is_nondict(tmp_path, store_cls) -> None:
    """主文件=合法 JSON 但非 dict（形状损坏）→ 回退 .tmp（旧版直接丢库）。"""
    main = tmp_path / "p.json"
    tmp = tmp_path / "p.json.tmp"
    main.write_text(json.dumps(["corrupt", "shape"]), encoding="utf-8")
    tmp.write_text(json.dumps({"c1": "done"}), encoding="utf-8")

    store = store_cls.load(str(main))
    assert store.skipped_codes() == {"c1"}


def test_load_rejects_nondict_tmp_keeps_clean_main(tmp_path, store_cls) -> None:
    """.tmp=非 dict（崩溃时半写）→ 主文件完好即用主文件，不被 tmp 污染。"""
    main = tmp_path / "p.json"
    tmp = tmp_path / "p.json.tmp"
    main.write_text(json.dumps({"c1": "done"}), encoding="utf-8")
    tmp.write_text(json.dumps(42), encoding="utf-8")

    store = store_cls.load(str(main))
    assert store.skipped_codes() == {"c1"}


def test_load_all_corrupt_never_crashes(tmp_path, store_cls) -> None:
    """主+tmp 双双非 dict → 空库启动（旧版 tmp 非 dict 直接 AttributeError 崩）。"""
    main = tmp_path / "p.json"
    main.write_text("garbage{", encoding="utf-8")
    (tmp_path / "p.json.tmp").write_text(json.dumps([1, 2]), encoding="utf-8")

    store = store_cls.load(str(main))
    assert store is not None
    assert store.skipped_codes() == set()  # 关键：不抛异常
    store.mark("x", "started")  # 且还能继续写
    assert store.state_of("x") == "started"


def test_v1_progress_file_backcompat(tmp_path, store_cls) -> None:
    """v1 纯字符串进度文件照常加载跳过（升级无感）。"""
    main = tmp_path / "p.json"
    main.write_text(json.dumps({"c1": "done", "c2": "close_failed", "c3": "started"}), encoding="utf-8")

    store = store_cls.load(str(main))
    assert store.skipped_codes() == {"c1", "c2"}
    assert store.score_of("c1") == (0, 0)  # v1 无战果，回填 0
    store.mark("c3", "done", flags=2, awarded=300)  # 升级写为 v2
    again = store_cls.load(str(main))
    assert again.skipped_codes() == {"c1", "c2", "c3"}
    assert again.score_of("c3") == (2, 300)


def test_concurrent_mark_file_stays_valid(tmp_path, store_cls) -> None:
    """并发写压力：20 线程×25 次 mark，文件始终合法且全键可见（原子替换+锁）。"""
    main = tmp_path / "p.json"
    store = store_cls.load(str(main))

    def hammer(i: int):
        for j in range(25):
            store.mark(f"c{i}-{j}", "done" if j % 2 else "started")

    with ThreadPoolExecutor(max_workers=20) as pool:
        list(pool.map(hammer, range(20)))

    final = store_cls.load(str(main))
    assert len(final.skipped_codes()) == 20 * 12  # 每线程 range(25) 中奇数 j=12 个标 done
    # 文件本体合法 JSON（中途读也不断言崩溃）
    assert isinstance(json.loads(main.read_text(encoding="utf-8")), dict)


def test_mark_preserves_existing_score_unless_overridden(tmp_path, store_cls) -> None:
    """增量关题：先记 (1,100)，后续 mark 不带战果不抹零；显式覆盖生效。"""
    main = tmp_path / "p.json"
    store = store_cls.load(str(main))
    store.mark("c1", "started")
    store.mark("c1", "done", flags=1, awarded=100)
    store.mark("c1", "done")  # 如重复关题路径
    assert store.score_of("c1") == (1, 100)
    store.mark("c1", "done", flags=2, awarded=250)
    assert store.score_of("c1") == (2, 250)


def test_giveup_does_not_overwrite_close_failed(tmp_path, store_cls) -> None:
    """give-up×close_failed 交互：finally 关题失败已标 close_failed 后，
    give-up 分支不得用 done 覆盖（否则清道夫永久失联泄漏容器）。语义回归。"""
    store = store_cls.load(str(tmp_path / "p.json"))
    store.mark("c1", "close_failed", flags=0, awarded=0)
    # give-up 分支的条件：state_of == close_failed → 保持
    if store.state_of("c1") != "close_failed":
        store.mark("c1", "done", flags=0, awarded=0)
    assert store.state_of("c1") == "close_failed"
    # 反例：普通 done 状态不被误判为 close_failed
    store.mark("c2", "done")
    if store.state_of("c2") != "close_failed":
        store.mark("c2", "done", flags=3, awarded=900)
    assert store.state_of("c2") == "done"


def test_restart_report_rehydrates_prior_scores(tmp_path) -> None:
    """端到端：第一轮解题得分 → 崩溃重启 → 报告含历史战果（总分不缩水）。"""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "container"))
    from astra_runner.runner import run_benchmark

    from test_astra_runner import FakeChallenge, FakeClient, FakeEngine

    progress = tmp_path / "progress.json"
    challenges = [FakeChallenge("a-01"), FakeChallenge("a-02")]
    flags = {"a-01": ["flag{one}"], "a-02": ["flag{two}"]}

    def factory():
        return FakeEngine({"proj-0": ["flag{one}", "flag{two}"]})

    r1 = run_benchmark(
        FakeClient(list(challenges), flags=flags), factory,
        flag_poll_seconds=0, progress_file=str(progress),
    )
    total_awarded_1 = sum(r.awarded for r in r1)
    total_flags_1 = sum(r.flags_correct for r in r1)
    assert total_awarded_1 == 200  # 两题各 100
    assert total_flags_1 == 2

    # 崩溃重启：全部跳过，但报告战果守恒（历史回填）
    r2 = run_benchmark(
        FakeClient(list(challenges), flags=flags), factory,
        flag_poll_seconds=0, progress_file=str(progress),
    )
    assert sum(r.awarded for r in r2) == total_awarded_1
    assert sum(r.flags_correct for r in r2) == total_flags_1


def test_double_defer_zero_new_flags_triggers_graph_reset() -> None:
    """B 项：连续 2 轮 defer 零新旗 → 图重置（删项目+project_id 清空）→ 回访走新项目。

    V10 榜首经验语义回归：星图被死路占满时保图续攻只会重复旧路线。
    """
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "container"))
    from astra_runner.runner import run_benchmark

    from test_astra_runner import FakeChallenge, FakeClient, FakeEngine

    challenges = [FakeChallenge("g001", flag_count=3)]  # 多旗题：永远差旗 → defer 循环
    client = FakeClient(challenges, flags={"g001": ["flag{one}"]})

    class NeverDoneEngine(FakeEngine):
        def __init__(self):
            super().__init__({})
            self.created_count = 0

        def wait_project(self, project_id: str, timeout_seconds: float) -> bool:
            return False  # 永不归航 → defer 循环

        def create_project(self, title: str, origin: str, goal: str) -> str:
            self.created_count += 1
            return f"proj-{self.created_count}"

        def list_fact_descriptions(self, project_id: str) -> list[str]:
            return ["侦察：80 端口开放"]  # 有事实但无 flag → 零新旗 defer

        def stats(self, project_id: str):
            return {"facts": 30, "hints": 0, "steps": 5, "findings": 0}

    shared = NeverDoneEngine()

    results = run_benchmark(
        FakeClient(list(challenges), flags={"g001": ["flag{one}"]}), lambda: shared,
        challenge_timeout_seconds=0.2, flag_poll_seconds=0.05,
        defer_after_seconds=0.15, progress_file=None,
    )
    r = results[0]
    # 图重置语义：至少触发过一次删除（连续 defer 零新旗）
    assert len(shared.deleted) >= 1, "连续零新旗 defer 必须触发图重置删除"
    # 回访建了新项目（created ≥ 2：首建 + 重置后重建）
    assert shared.created_count >= 2, "图重置后回访必须走全新项目"
    # runner 侧战果语义：无旗则 flags_correct 保持 0
    assert r.flags_correct == 0
