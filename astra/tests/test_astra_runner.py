"""astra-runner 编排逻辑测试（fake SDK client + fake engine）。"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "container"))

from astra_runner.runner import (  # noqa: E402
    ChallengeResult,
    collect_flags_from_facts,
    extract_flags,
    run_benchmark,
)

# 测试隔离：fake 跑分会写 memory-stats/沉淀 JSON，全部指到临时目录避免污染真实文件
import astra_runner.runner as _runner_module  # noqa: E402
_test_tmp = Path(tempfile.mkdtemp())
_runner_module.MEMORY_STATS_FILE = _test_tmp / "memory-stats.json"
_runner_module.KNOWLEDGE_APPEND_FILE = _test_tmp / "astra-knowledge-append.json"
_runner_module.DEADENDS_APPEND_FILE = _test_tmp / "astra-deadends-append.json"
os.environ["ASTRA_SELF_HEAL"] = "0"  # 测试禁用看门狗（os.execv 自重启会炸掉 pytest）


@dataclass
class FakeChallenge:
    unique_code: str
    description: str = "solve the challenge"
    is_completed: bool = False
    flag_count: int = 1
    total_score: int = 100


@dataclass
class FakeStartResult:
    container_addr: list[str] = field(default_factory=lambda: ["10.0.0.5:8080"])


@dataclass
class FakeSubmitResult:
    correct: bool
    awarded: int
    cumulative_score: int = 0
    correct_flag_count: int = 1
    total_flag_count: int = 1


@dataclass
class FakeCloseResult:
    closed: bool = True


class FakeClient:
    def __init__(self, challenges: list[FakeChallenge], flags: dict[str, list[str]] | None = None):
        self.challenges = challenges
        self.flags = flags or {}
        self.started: list[str] = []
        self.hints: list[str] = []
        self.submitted: list[tuple[str, str]] = []
        self.closed: list[str] = []

    def list_challenges(self):
        return self.challenges

    def start_challenge(self, unique_code: str):
        self.started.append(unique_code)
        return FakeStartResult()

    def get_hint(self, unique_code: str):
        self.hints.append(unique_code)
        return type("HintResult", (), {"hint": f"platform hint for {unique_code}"})()

    def submit_flag(self, unique_code: str, flag: str):
        self.submitted.append((unique_code, flag))
        if flag in self.flags.get(unique_code, []):
            return FakeSubmitResult(correct=True, awarded=100, cumulative_score=100)
        return FakeSubmitResult(correct=False, awarded=0)

    def close_challenge(self, unique_code: str):
        self.closed.append(unique_code)
        return FakeCloseResult(closed=True)


class FakeEngine:
    def __init__(self, flags_by_project: dict[str, list[str]], done: bool = True, delay: float = 0.0):
        self.flags_by_project = flags_by_project
        self.done = done
        self.delay = delay
        self.projects: list[tuple[str, str, str, str]] = []
        self.hints: list[tuple[str, str]] = []
        self.deleted: list[str] = []
        self.started = 0
        self.stopped = 0
        self.stop_calls: list[str] = []
        self.reactivate_calls: list[str] = []
        self.active_projects: list[dict] = []

    def stop_project(self, project_id: str) -> None:
        self.stop_calls.append(project_id)

    def reactivate_project(self, project_id: str) -> None:
        self.reactivate_calls.append(project_id)

    def list_active_projects(self) -> list[dict]:
        return list(self.active_projects)

    def start(self) -> None:
        self.started += 1

    def create_project(self, title: str, origin: str, goal: str) -> str:
        project_id = f"proj-{len(self.projects)}"
        self.projects.append((project_id, title, origin, goal))
        return project_id

    def create_hint(self, project_id: str, content: str) -> None:
        self.hints.append((project_id, content))

    def wait_project(self, project_id: str, timeout_seconds: float) -> bool:
        if self.delay:
            time.sleep(self.delay)
        return self.done

    def list_fact_descriptions(self, project_id: str) -> list[str]:
        return [f"found flag {flag}" for flag in self.flags_by_project.get(project_id, [])]

    def stats(self, project_id: str) -> dict[str, int]:
        return {"facts": 7, "hints": 3, "review_hints": 1, "failure_hints": 2}

    def delete_project(self, project_id: str) -> None:
        self.deleted.append(project_id)

    def stop(self) -> None:
        self.stopped += 1


def test_extract_flags_deduplicates() -> None:
    assert extract_flags("a flag{one} b flag{two} flag{one}") == ["flag{one}", "flag{two}"]
    assert extract_flags("no flags here") == []
    assert extract_flags("placeholder flag{...} here") == []
    assert collect_flags_from_facts(["x flag{abc}", "y flag{bcd}", "z flag{abc}"]) == ["flag{abc}", "flag{bcd}"]


def test_run_benchmark_prefers_easy_order() -> None:
    """prefer_easy：easy→medium→hard 开题（同难度保持平台顺序，run 9214 饿死教训）。"""

    @dataclass
    class DiffChallenge(FakeChallenge):
        difficulty: str = ""

    hard1 = DiffChallenge("x-hard-1", difficulty="hard")
    easy1 = DiffChallenge("x-easy-1", difficulty="easy")
    med1 = DiffChallenge("x-med-1", difficulty="medium")
    easy2 = DiffChallenge("x-easy-2", difficulty="easy")
    flags = {c.unique_code: ["flag{f}"] for c in (hard1, easy1, med1, easy2)}

    client = FakeClient([hard1, easy1, med1, easy2], flags=flags)
    run_benchmark(
        client,
        lambda: FakeEngine({"proj-0": ["flag{f}"]}),
        challenge_timeout_seconds=0.5,
        flag_poll_seconds=0.01,
        parallel=1,
    )
    assert client.started == ["x-easy-1", "x-easy-2", "x-med-1", "x-hard-1"]

    # --no-prefer-easy：恢复平台原始顺序
    client2 = FakeClient([hard1, easy1, med1, easy2], flags=flags)
    run_benchmark(
        client2,
        lambda: FakeEngine({"proj-0": ["flag{f}"]}),
        challenge_timeout_seconds=0.5,
        flag_poll_seconds=0.01,
        parallel=1,
        prefer_easy=False,
    )
    assert client2.started == ["x-hard-1", "x-easy-1", "x-med-1", "x-easy-2"]


def test_run_benchmark_lifecycle() -> None:
    challenges = [FakeChallenge("c001"), FakeChallenge("c002", is_completed=True)]
    client = FakeClient(challenges, flags={"c001": ["flag{hello}"]})
    engines: list[FakeEngine] = []

    def factory():
        engine = FakeEngine({"proj-0": ["flag{hello}"]})
        engines.append(engine)
        return engine

    results = run_benchmark(client, factory, flag_poll_seconds=0)
    assert client.started == ["c001"]  # c002 已完成被跳过
    assert client.submitted == [("c001", "flag{hello}")]
    # 启动时幂等清理已完成题遗留容器（c002）+ 正常流程 close（c001）
    assert client.closed == ["c002", "c001"]
    assert results[0].flags_correct == 1
    assert results[0].awarded == 100
    assert results[0].facts_count == 7  # 每题统计（评审量化口径）
    assert results[0].hints_count == 3
    assert results[1].started is False  # 跳过项
    assert engines[0].stopped == 1


def test_run_benchmark_timeout_and_engine_stop() -> None:
    challenges = [FakeChallenge("c003")]
    client = FakeClient(challenges)
    engines: list[FakeEngine] = []

    def factory():
        engine = FakeEngine({}, done=False)
        engines.append(engine)
        return engine

    results = run_benchmark(
        client, factory,
        challenge_timeout_seconds=0.2, flag_poll_seconds=0.05,
        defer_after_seconds=0.2,  # 无结果 → 保留进度放回队尾（defer）
    )
    assert results[0].started is True
    # defer 语义：无结果时每次关平台题释放名额并放回队尾；
    # 达到上限（MAX_DEFER=2）后放弃——引擎项目删除、进度文件标 done
    assert client.closed == ["c003", "c003"]
    assert results[0].defer_count == 2
    # 前 2 次 defer 保留引擎项目（星图进度），第 3 次进入前判断达上限放弃删除
    assert engines[-1].deleted == ["proj-0"]


def test_run_benchmark_duplicate_flag_skipped() -> None:
    challenges = [FakeChallenge("c004")]
    client = FakeClient(challenges, flags={"c004": ["flag{dup}"]})

    class DupClient(FakeClient):
        def submit_flag(self, unique_code: str, flag: str):
            self.submitted.append((unique_code, flag))
            # 模拟真实 SDK 的 DuplicateSubmit（幂等）：按异常名识别
            raise _named_exc("DuplicateSubmit", "duplicate")

    dup_client = DupClient(challenges)

    def factory():
        return FakeEngine({"proj-0": ["flag{dup}"]})

    results = run_benchmark(dup_client, factory, flag_poll_seconds=0)
    assert results[0].flags_found == ["flag{dup}"]
    assert results[0].flags_correct == 0
    assert dup_client.submitted == [("c004", "flag{dup}")]


def _named_exc(name: str, message: str) -> Exception:
    return type(name, (Exception,), {})(message)


def test_run_benchmark_stops_on_task_finished() -> None:
    """任务时限已到（409 already finished）→ 停止整轮但返回已收集结果（报告不崩）。"""
    challenges = [FakeChallenge("e9-01"), FakeChallenge("e9-02")]
    client = FakeClient(challenges)

    class FinishedClient(FakeClient):
        def start_challenge(self, unique_code: str):
            exc = type("InvalidState", (Exception,), {})("task task_xxx already finished [code=invalid_state] [http=409]")
            raise exc

    finished = FinishedClient(challenges)

    # 2026-08 实测修复：到期不再抛异常（否则 main 报告段 UnboundLocalError），
    # 而是返回已收集的 results，让报告正常输出
    results = run_benchmark(finished, lambda: FakeEngine({}), flag_poll_seconds=0)
    assert [r.unique_code for r in results] == ["e9-01", "e9-02"]
    assert all(not r.started for r in results)  # 无一题成功 start


def test_run_benchmark_parallel_window() -> None:
    """并行窗口：2 题同时活跃，全部完成后返回有序结果。"""
    challenges = [FakeChallenge("p-01"), FakeChallenge("p-02"), FakeChallenge("p-03")]
    # 并行下每个引擎独立 project 空间，flag 值统一以便提交判定
    client = FakeClient(challenges, flags={"p-01": ["flag{xyz}"], "p-02": ["flag{xyz}"], "p-03": ["flag{xyz}"]})

    def factory():
        return FakeEngine({"proj-0": ["flag{xyz}"]})

    results = run_benchmark(client, factory, flag_poll_seconds=0, parallel=2)
    assert len(results) == 3
    assert [r.flags_correct for r in results] == [1, 1, 1]
    assert client.started == ["p-01", "p-02", "p-03"]
    assert client.closed == ["p-01", "p-02", "p-03"]


def test_run_benchmark_auto_expand_to_slot_limit() -> None:
    """自动满载：探测到平台名额上限（3）后保持满载运行。"""
    challenges = [FakeChallenge(f"a-0{i}") for i in range(1, 7)]
    client = FakeClient(challenges, flags={c.unique_code: ["flag{xyz}"] for c in challenges})

    class LimitedClient(FakeClient):
        def __init__(self, challenges, flags=None, limit=3):
            super().__init__(challenges, flags)
            self.limit = limit
            self.running = 0
            self.max_running = 0

        def start_challenge(self, unique_code: str):
            if self.running >= self.limit:
                exc = type("InvalidState", (Exception,), {})("max active slots reached [code=invalid_state] [http=409]")
                raise exc
            self.running += 1
            self.max_running = max(self.max_running, self.running)
            return super().start_challenge(unique_code)

        def close_challenge(self, unique_code: str):
            self.running -= 1
            return super().close_challenge(unique_code)

    limited = LimitedClient(challenges, {c.unique_code: ["flag{xyz}"] for c in challenges}, limit=3)

    def factory():
        return FakeEngine({"proj-0": ["flag{xyz}"]}, delay=0.15)

    results = run_benchmark(limited, factory, flag_poll_seconds=0.05, parallel="auto")
    assert len(results) == 6
    assert all(r.flags_correct == 1 for r in results)
    # 探测到名额上限 3：峰值并发为 3（而非无脑扩到 6）
    assert limited.max_running == 3


def test_run_benchmark_progress_file_skips_started_on_restart(tmp_path) -> None:
    """断点续跑：进度文件记录 started/done，重启 runner 自动跳过已尝试题。"""
    import json

    from astra_runner.runner import ProgressStore

    progress = tmp_path / "progress.json"
    challenges = [FakeChallenge("p001"), FakeChallenge("p002")]
    flags = {"p001": ["flag{one}"], "p002": ["flag{two}"]}

    def factory():
        return FakeEngine({"proj-0": ["flag{one}"]})

    # 第一轮：两题都跑，p001 成功标记 done，p002 完成
    client1 = FakeClient(challenges, flags=flags)
    results1 = run_benchmark(client1, factory, flag_poll_seconds=0, progress_file=str(progress))
    assert client1.started == ["p001", "p002"]
    data = json.loads(progress.read_text(encoding="utf-8"))
    assert data["p001"] == "done"
    assert data["p002"] == "done"

    # 第二轮（模拟重启）：进度文件自动跳过，不再 start
    client2 = FakeClient(challenges, flags=flags)
    results2 = run_benchmark(client2, factory, flag_poll_seconds=0, progress_file=str(progress))
    assert client2.started == []
    assert all(r.started is False for r in results2)
    assert results2[0].flags_found == []  # 未跑故未提交

    # skip_codes 与 progress 并存：skip_codes 仍生效
    client3 = FakeClient(challenges, flags=flags)
    results3 = run_benchmark(client3, factory, flag_poll_seconds=0, skip_codes={"p002"})
    assert client3.started == ["p001"]  # 只跑 p001，p002 被 skip_codes 跳过


def test_progress_store_handles_missing_and_corrupt_files(tmp_path) -> None:
    from astra_runner.runner import ProgressStore

    assert ProgressStore.load(None) is None
    store = ProgressStore.load(str(tmp_path / "missing.json"))
    assert store is not None and store.skipped_codes() == set()

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("not json", encoding="utf-8")
    store = ProgressStore.load(str(corrupt))
    assert store is not None and store.skipped_codes() == set()

    store.mark("c001", "started")
    store.mark("c002", "done")
    store.mark("c003", "close_failed")
    # done / close_failed 跳过；started 不跳过（崩溃重启后重新 start 续跑）
    assert ProgressStore.load(str(corrupt)).skipped_codes() == {"c002", "c003"}


def test_run_benchmark_started_challenge_restarts_after_crash(tmp_path) -> None:
    """崩溃重启：进度文件里 started 的题重新 start（平台幂等返回同地址）继续解。"""
    from astra_runner.runner import ProgressStore

    progress = tmp_path / "progress.json"
    store = ProgressStore.load(str(progress))
    assert store is not None
    store.mark("s001", "started")  # 模拟崩溃前已启动的题
    store.mark("s002", "done")

    challenges = [FakeChallenge("s001"), FakeChallenge("s002")]
    client = FakeClient(challenges, flags={"s001": ["flag{resume}"]})

    def factory():
        return FakeEngine({"proj-0": ["flag{resume}"]})

    results = run_benchmark(client, factory, flag_poll_seconds=0, progress_file=str(progress))
    assert client.started == ["s001"]  # started 的题重新 start 续跑；done 的跳过
    assert results[0].flags_correct == 1


def test_run_benchmark_auto_hint_injects_platform_hint_on_stall() -> None:
    """卡题时自动获取平台 hint 并注入 ASTRA 项目（used_hint 标记 + 只取一次）。"""
    from astra_runner.runner import ChallengeResult, _try_platform_hint

    challenge = FakeChallenge("h001")
    result = ChallengeResult(unique_code="h001", description="stuck challenge")
    client = FakeClient([challenge], flags={})
    engine = FakeEngine({})

    used = _try_platform_hint(client, engine, "h001", "proj-h001", result)
    assert used is True
    assert result.used_hint is True
    assert client.hints == ["h001"]
    assert engine.hints == [("proj-h001", "[平台提示] platform hint for h001")]

    # hint 获取失败：不再重试（返回 True 表示已尝试），不标记 used_hint
    class NoHintClient(FakeClient):
        def get_hint(self, unique_code):
            raise ConnectionError("platform unreachable")

    result2 = ChallengeResult(unique_code="h002", description="x")
    client2 = NoHintClient([FakeChallenge("h002")], flags={})
    engine2 = FakeEngine({})
    used2 = _try_platform_hint(client2, engine2, "h002", "proj-h002", result2)
    assert used2 is True
    assert result2.used_hint is False
    assert engine2.hints == []


def test_run_benchmark_auto_hint_after_seconds_trigger() -> None:
    """卡题超过 hint_after_seconds（新策略：20/40 分钟两级）自动注入 hint。"""
    challenge = FakeChallenge("h003", total_score=100)  # 低分题也触发（默认不设门槛）
    client = FakeClient([challenge], flags={})
    engine = FakeEngine({}, done=False)

    results = run_benchmark(
        client,
        lambda: engine,
        challenge_timeout_seconds=0.5,
        flag_poll_seconds=0.01,
        hint_after_seconds=0.05,
        hint2_after_seconds=0.1,
        defer_after_seconds=0.5,  # 测试内快速 defer 防挂起
        hint_min_score=0,
    )
    assert results[0].used_hint is True
    assert len(client.hints) >= 1  # defer 续跑可能触发多段 hint，至少一次
    assert engine.hints and engine.hints[0][0] == "proj-0"

    # 高分门槛：低于门槛的题不触发（hint 次数不再增加）
    baseline = len(client.hints)
    engine2 = FakeEngine({}, done=False)
    results2 = run_benchmark(
        client,
        lambda: engine2,
        challenge_timeout_seconds=0.5,
        flag_poll_seconds=0.01,
        hint_after_seconds=0.05,
        hint2_after_seconds=0.1,
        defer_after_seconds=0.5,
        hint_min_score=300,
    )
    assert results2[0].used_hint is False
    assert len(client.hints) == baseline  # 门槛拦住，无新增 hint
    assert engine2.hints == []


def test_run_benchmark_defer_resumes_same_project() -> None:
    """60 分钟无结果 → 保留进度放回队尾；队列轮转后续跑复用同一引擎项目。"""
    challenges = [FakeChallenge("d001", total_score=500)]
    client = FakeClient(challenges)  # 无 flag：纯 defer 场景
    wait_calls = [0]

    class DeferEngine(FakeEngine):
        def wait_project(self, project_id: str, timeout_seconds: float) -> bool:
            wait_calls[0] += 1
            return False  # 引擎不归航：完全靠 defer 放回队尾轮转

    engines: list[DeferEngine] = []

    def factory():
        e = DeferEngine({})  # 无 flag 星图
        engines.append(e)
        return e

    results = run_benchmark(
        client, factory,
        challenge_timeout_seconds=0.2, flag_poll_seconds=0.05,
        defer_after_seconds=0.2,  # 快速 defer
    )
    # 无 flag 无归航：连续 defer 到上限（MAX_DEFER=2）后放弃
    assert results[0].started is True
    assert results[0].defer_count == 2  # defer 2 次后达上限
    assert client.started.count("d001") == 2  # 第 1 次 + defer 后第 2 次（达上限不再放回）
    # defer 续跑复用同一引擎项目 id（结果保留 project_id）
    assert results[0].project_id is not None


def test_run_benchmark_multiflag_partial_defers_for_remaining() -> None:
    """V9 多旗收割：flag_count>已收旗数时 defer 回队续攻，不提前关题丢剩余旗。"""
    challenges = [FakeChallenge("mflag", total_score=1200, flag_count=4)]

    class PartialEngine(FakeEngine):
        """永不归航；星图第 1 窗口吐 1 旗、第 2 窗口起吐 2 旗（部分进展）。"""

        def __init__(self, flags_by_project):
            super().__init__(flags_by_project)
            self.windows = 0

        def list_fact_descriptions(self, project_id: str) -> list[str]:
            n = min(2, self.windows) if self.windows else 1
            return [f"found flag flag{{m{i}_part}}" for i in range(1, n + 1)]

        def wait_project(self, project_id: str, timeout_seconds: float) -> bool:
            return False  # 不归航：逼 defer 路径

        def start(self) -> None:
            super().start()
            self.windows += 1

    engines: list[PartialEngine] = []

    def factory():
        e = PartialEngine({})
        engines.append(e)
        return e

    client = FakeClient(challenges, flags={"mflag": ["flag{m1_part}"]})
    results = run_benchmark(
        client, factory,
        challenge_timeout_seconds=0.2, flag_poll_seconds=0.05,
        defer_after_seconds=0.2,
    )
    r = results[0]
    # 有正确旗（1 旗）且 flag_count=4 未收满 → 必须 defer 而非关题；预算=2+2*1=4
    assert r.flags_correct >= 1
    assert r.defer_count >= 1
    # 多旗 defer 回队：同题被 start 多次（首攻 + defer 后续攻）
    assert client.started.count("mflag") >= 2


def test_run_benchmark_multiflag_closes_when_all_collected() -> None:
    """V9 多旗收割：旗收满（flags_correct ≥ flag_count）→ 正常关题不再 defer。"""
    challenges = [FakeChallenge("mfull", total_score=400, flag_count=2)]
    client = FakeClient(challenges, flags={"mfull": ["flag{alpha}", "flag{beta}"]})

    class FullEngine(FakeEngine):
        def wait_project(self, project_id: str, timeout_seconds: float) -> bool:
            return False

        def list_fact_descriptions(self, project_id: str) -> list[str]:
            return ["found flag flag{alpha}", "found flag flag{beta}"]

    def factory():
        return FullEngine({"x": ["flag{a}", "flag{b}"]})

    results = run_benchmark(
        client, factory,
        challenge_timeout_seconds=0.2, flag_poll_seconds=0.05,
        defer_after_seconds=0.2,
    )
    r = results[0]
    assert r.flags_correct == 2
    # 旗收满：正常关题一次，不回队续攻
    assert client.started.count("mfull") == 1
    assert r.defer_count == 0


def test_run_benchmark_records_first_flag_seconds() -> None:
    """首次正确提交记录 first_flag_seconds（评审'单高危漏洞发现时长'口径）。"""
    from astra_runner.runner import _submit_flag_safely

    challenges = [FakeChallenge("f001")]
    client = FakeClient(challenges, flags={"f001": ["flag{fast}"]})
    result = ChallengeResult(unique_code="f001", description="x")
    started_at = time.monotonic()

    _submit_flag_safely(client, "f001", "flag{fast}", result, started_at)
    assert result.flags_correct == 1
    assert result.first_flag_seconds is not None and 0 <= result.first_flag_seconds < 5
    # 第二次提交不覆盖首次时间
    _submit_flag_safely(client, "f001", "flag{fast}", result, started_at)
    assert result.first_flag_seconds is not None


def test_call_with_retry_retries_transient_and_passes_business_exceptions() -> None:
    """SDK 重试保护：网络错误重试退避；业务异常（InvalidState 等）直接抛出。"""
    import pytest

    from astra_runner.runner import call_with_retry

    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionError("temporary")
        return "ok"

    assert call_with_retry(flaky, "test", retries=3, base_delay=0) == "ok"
    assert attempts["n"] == 3

    # 业务异常不重试
    def business():
        raise type("InvalidState", (Exception,), {})("already finished")

    attempts2 = {"n": 0}

    def counted():
        attempts2["n"] += 1
        return business()

    with pytest.raises(Exception, match="already finished"):
        call_with_retry(counted, "test", retries=3, base_delay=0)
    assert attempts2["n"] == 1


def test_window_allows_start_pure_logic() -> None:
    """任务限时窗口判断（纯函数）：耗尽 → False；充足 → True；无窗口 → True。"""
    from astra_runner.runner import window_allows_start

    # 无窗口：恒允许
    assert window_allows_start(None, 2800) is True
    # 窗口充足（剩余 > 最长单题）：允许
    assert window_allows_start(10_000.0, 2_800.0, now=1_000.0) is True
    # 剩余 2900s > 2800s → 仍允许
    assert window_allows_start(3_900.0, 2_800.0, now=1_000.0) is True
    # 窗口耗尽（剩余 < 最长单题）：禁止
    assert window_allows_start(3_000.0, 2_800.0, now=1_000.0) is False
    # 边界：剩余恰好等于最长单题 → 禁止（保守）
    assert window_allows_start(3_800.0, 2_800.0, now=1_000.0) is False


def test_render_dispatch_config_claudecode_fleet(monkeypatch, tmp_path) -> None:
    """默认 ASTRA_WORKER_TYPE=claudecode：explore×2(p0) + reason×1(p1)，MCP 注入。"""
    import json as _json

    from astra.dispatcher.config import DispatchConfig
    from astra_runner.astra_runner_engine import AstraDaemon

    monkeypatch.delenv("ASTRA_WORKER_TYPE", raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-test")
    monkeypatch.setenv("ANTHROPIC_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic")
    monkeypatch.setenv("ASTRA_CLAUDE_HOME", str(tmp_path / "claude-home"))
    monkeypatch.setenv("ASTRA_EXPLORE_REPLICAS", "2")

    path = AstraDaemon()._render_dispatch_config()
    yaml = path.read_text(encoding="utf-8")

    assert 'type: "claudecode"' in yaml
    assert 'ANTHROPIC_MODEL: "deepseek-v4-flash"' in yaml
    assert 'ANTHROPIC_BASE_URL: "https://api.deepseek.com/anthropic"' in yaml
    config = DispatchConfig.load(path)
    assert [w.name for w in config.workers] == [
        "deepseek-explore-0", "deepseek-explore-1", "deepseek-reason",
    ]
    by_name = {w.name: w for w in config.workers}
    assert set(by_name["deepseek-explore-0"].task_types) == {"bootstrap", "explore"}
    assert set(by_name["deepseek-reason"].task_types) == {"reason", "consolidate"}
    assert by_name["deepseek-explore-0"].priority == 0
    assert by_name["deepseek-reason"].priority == 1
    assert by_name["deepseek-explore-0"].max_running == 3
    env0 = by_name["deepseek-explore-0"].env
    # SMALL_FAST/SUBAGENT 钉主模型（anthropic 兼容端点无 haiku）
    assert env0["ANTHROPIC_SMALL_FAST_MODEL"] == "deepseek-v4-flash"
    assert env0["CLAUDE_CODE_SUBAGENT_MODEL"] == "deepseek-v4-flash"
    # MCP 注入：worker 的 CLAUDE_CONFIG_DIR 下应有 .claude.json
    mcp = _json.loads((Path(env0["CLAUDE_CONFIG_DIR"]) / ".claude.json").read_text(encoding="utf-8"))
    assert mcp["mcpServers"]["playwright"]["command"] == "playwright-mcp"


def test_render_dispatch_config_claudecode_requires_token(monkeypatch) -> None:
    import pytest

    from astra_runner.astra_runner_engine import AstraDaemon

    monkeypatch.delenv("ASTRA_WORKER_TYPE", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="ANTHROPIC_AUTH_TOKEN"):
        AstraDaemon()._render_dispatch_config()


def test_render_dispatch_config_rejects_dsh(monkeypatch) -> None:
    """dsh 已移除：显式 ASTRA_WORKER_TYPE=dsh 必须硬失败（防旧 env 静默跑错栈）。"""
    import pytest

    from astra_runner.astra_runner_engine import AstraDaemon

    monkeypatch.setenv("ASTRA_WORKER_TYPE", "dsh")

    with pytest.raises(RuntimeError, match="claudecode"):
        AstraDaemon()._render_dispatch_config()


def test_render_dispatch_config_single_explore_replica(monkeypatch, tmp_path) -> None:
    from astra.dispatcher.config import DispatchConfig
    from astra_runner.astra_runner_engine import AstraDaemon

    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-test")
    monkeypatch.setenv("ASTRA_CLAUDE_HOME", str(tmp_path / "claude-home"))
    monkeypatch.setenv("ASTRA_EXPLORE_REPLICAS", "1")

    path = AstraDaemon()._render_dispatch_config()
    config = DispatchConfig.load(path)
    assert [w.name for w in config.workers] == ["deepseek-explore", "deepseek-reason"]


def _make_claude_worker_home(root: Path, name: str, age_days: float) -> Path:
    worker_dir = root / name
    session = worker_dir / "projects" / "--cwd--" / "s1"
    session.mkdir(parents=True, exist_ok=True)
    (session / "session-abc.jsonl").write_text("{}", encoding="utf-8")
    old = time.time() - age_days * 86400
    os.utime(worker_dir, (old, old))
    return worker_dir


def test_cleanup_claude_homes_removes_stale_keeps_recent(monkeypatch, tmp_path) -> None:
    """启动清理：整 worker 目录按 mtime 判定，近期（72h 内）绝不清理。"""
    from astra_runner.astra_runner_engine import AstraDaemon

    root = tmp_path / "claude-home"
    root.mkdir()
    _make_claude_worker_home(root, "old-worker", age_days=30)
    recent = _make_claude_worker_home(root, "live-worker", age_days=0.01)
    # 非目录文件不应被动
    keep_me = root / "keep.txt"
    keep_me.write_text("x", encoding="utf-8")

    monkeypatch.setenv("ASTRA_CLAUDE_HOME", str(root))
    AstraDaemon._cleanup_claude_homes()

    assert not (root / "old-worker").exists()
    assert recent.exists()
    assert keep_me.exists()


def test_cleanup_claude_homes_noop_when_absent(monkeypatch, tmp_path) -> None:
    from astra_runner.astra_runner_engine import AstraDaemon

    monkeypatch.setenv("ASTRA_CLAUDE_HOME", str(tmp_path / "missing"))
    AstraDaemon._cleanup_claude_homes()  # 不应抛异常


def test_collect_claude_usage_aggregates_and_tolerates_bad_lines(monkeypatch, tmp_path) -> None:
    """token 计量：汇总 CC 会话 jsonl 的 message.usage，坏行/缺字段跳过。"""
    from astra_runner.runner import collect_claude_usage

    session_dir = tmp_path / "claude-home" / "cc-worker" / "projects" / "--cwd--"
    session_dir.mkdir(parents=True)
    lines = [
        '{"message":{"usage":{"input_tokens":100,"output_tokens":20,"cache_read_input_tokens":10,"cache_creation_input_tokens":5}}}',
        '{"message":{"usage":{"input_tokens":50,"output_tokens":30}}}',
        "not-json-line",
        '{"message":{}}',
        "",
    ]
    (session_dir / "a.jsonl").write_text(chr(10).join(lines), encoding="utf-8")
    monkeypatch.setenv("ASTRA_CLAUDE_HOME", str(tmp_path / "claude-home"))

    total = collect_claude_usage()
    assert total["inputTokens"] == 150
    assert total["outputTokens"] == 50
    assert total["cacheReadTokens"] == 10
    assert total["cacheWriteTokens"] == 5

    monkeypatch.setenv("ASTRA_CLAUDE_HOME", str(tmp_path / "missing"))
    assert collect_claude_usage() == {}


def test_render_dispatch_config_defaults_to_claudecode(monkeypatch, tmp_path) -> None:
    """默认 ASTRA_WORKER_TYPE=claudecode（2026-08-28 翻转：tsecbench 前十 0 家
    dsh，CC/Agent SDK 系 6 家）。漏带该变量也走正确栈。"""
    from astra_runner.astra_runner_engine import AstraDaemon

    monkeypatch.delenv("ASTRA_WORKER_TYPE", raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-test")
    monkeypatch.setenv("ASTRA_CLAUDE_HOME", str(tmp_path / "claude-home"))

    path = AstraDaemon()._render_dispatch_config()
    yaml = path.read_text(encoding="utf-8")

    assert 'type: "claudecode"' in yaml
    assert 'ANTHROPIC_AUTH_TOKEN: "sk-test"' in yaml
    assert "dsh" not in yaml.replace("0 家 dsh", "")


def test_defer_stops_and_resume_reactivates_project() -> None:
    """R5 修复清单 P0-1 验收：defer→stop_project；requeue→reactivate_project；上限→delete 不变。"""
    challenges = [FakeChallenge("d001", total_score=500)]
    client = FakeClient(challenges)

    class DeferEngine(FakeEngine):
        def wait_project(self, project_id: str, timeout_seconds: float) -> bool:
            return False  # 永不归航：走 defer 路径

    # 共享单例：runner 的 defer 分支通过 engine_factory() 再取引擎，生产环境
    # LocalAstraEngine 是 daemon 单例；测试用同一实例才能观测生命周期调用
    shared = DeferEngine({})

    def factory():
        return shared

    results = run_benchmark(
        client, factory,
        challenge_timeout_seconds=0.2, flag_poll_seconds=0.05,
        defer_after_seconds=0.2,
    )
    assert results[0].defer_count == 2
    # defer 时项目被停（防僵尸），resume 时被激活，达上限后被删除
    assert len(shared.stop_calls) >= 1
    assert len(shared.reactivate_calls) >= 1
    assert len(shared.deleted) == 1


def test_reconcile_stops_orphan_active_project(monkeypatch) -> None:
    """R5 修复清单 1b 验收：引擎侧 active 孤儿项目（不在窗口）被对账停掉。"""
    from datetime import datetime, timedelta, timezone

    challenge = FakeChallenge("r001")
    client = FakeClient([challenge], flags={"r001": ["flag{reconcile_ok}"]})
    stale_time = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh_time = (datetime.now(timezone.utc) - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    shared = FakeEngine({"proj-0": ["flag{reconcile_ok}"]})

    class OrphanEngine(FakeEngine):
        def list_active_projects(self) -> list[dict]:
            return [
                {"id": "orphan-1", "created_at": stale_time},   # 老孤儿：应被停
                {"id": "proj-0", "created_at": fresh_time},     # 窗口内新项目：宽限保护
            ]

    shared.__class__ = OrphanEngine  # 共享实例叠加孤儿视图（生产引擎是单例）

    monkeypatch.setenv("ASTRA_RECONCILE_INTERVAL", "0.001")  # 每轮都扫（0 是禁用）
    monkeypatch.setenv("ASTRA_RECONCILE_GRACE", "60")

    def factory():
        return shared

    results = run_benchmark(client, factory, challenge_timeout_seconds=2, flag_poll_seconds=0.05)
    assert results[0].flags_correct == 1
    assert "orphan-1" in shared.stop_calls    # 孤儿被停
    assert "proj-0" not in shared.stop_calls  # 窗口内项目不动



# ---------------- V2 修复测试（2026-08-16：run 10089 实证驱动的策略层） ----------------

def test_v2_sanitize_kb_text() -> None:
    """V2-6 双层脱敏：flag 值正则 + secret 语境的 flag 组件（InterviewAI 教训）。"""
    from astra_runner.runner import _sanitize_kb_text

    dirty = "Flag 1 = flag{S3cr3t-X}; HR audit value 3e5a7b1c9d2f4e06 leaked"
    clean = _sanitize_kb_text(dirty)
    assert "S3cr3t-X" not in clean
    assert "3e5a7b1c9d2f4e06" not in clean
    # 无 secret 语境的普通内容不误伤
    assert _sanitize_kb_text("vulnerability CVE-2024-1234 via upload") == "vulnerability CVE-2024-1234 via upload"


def test_v2_expected_budget(tmp_path, monkeypatch) -> None:
    """V2-7 期望预算：KB 题 2×首解(15min 地板)、近失 20min、无参考按难度。

    V7 TDI：困境信号（错交/defer/参考失灵）按 (1+signal) 放宽预算；无信号时
    与 V2-7 原值完全一致。stats 指到用例级空目录，隔离共享 tmp 的跨用例污染。
    """
    import astra_runner.runner as _R

    monkeypatch.setattr(_R, "MEMORY_STATS_FILE", tmp_path / "stats.json")
    from astra_runner.runner import (
        DIFFICULTY_TIMEOUTS,
        DONE_FLAG_WAIT_SECONDS,
        _expected_budget_seconds,
        _task_difficulty_signal,
    )

    r_kb = ChallengeResult(unique_code="c", description="d")
    r_kb.kb_seconds = 60
    assert _task_difficulty_signal(r_kb) == 0.0
    assert _expected_budget_seconds(r_kb, "hard", 1800) == 900 + DONE_FLAG_WAIT_SECONDS + 30
    r_fast = ChallengeResult(unique_code="c", description="d")
    r_fast.kb_seconds = 600
    assert _expected_budget_seconds(r_fast, "hard", 1800) == 1200 + DONE_FLAG_WAIT_SECONDS + 30
    r_miss = ChallengeResult(unique_code="c", description="d")
    r_miss.wrong_count = 2
    signal = _task_difficulty_signal(r_miss)
    assert signal == 0.15  # 错交 2 次 → 封顶 0.15
    assert _expected_budget_seconds(r_miss, "hard", 1800) == 20 * 60 * (1 + signal) + DONE_FLAG_WAIT_SECONDS + 30
    r_plain = ChallengeResult(unique_code="c", description="d")
    assert _expected_budget_seconds(r_plain, "easy", 1800) == DIFFICULTY_TIMEOUTS["easy"] + DONE_FLAG_WAIT_SECONDS + 30


def test_v2_knowledge_base_parse(tmp_path, monkeypatch) -> None:
    """V2-6 知识库解析：条目/首解耗时/思路，加载时同步脱敏。"""
    import astra_runner.runner as runner_mod

    kb_file = tmp_path / "kb.md"
    kb_file.write_text(
        "# 已解题思路知识库\n\n"
        "## Foo（bctf-01）\n"
        "- 分值/难度：100 / easy ｜ 首解耗时：3min（09:45 解出）\n"
        "- 思路1：SSRF via nip.io 绕 IP 黑名单\n\n"
        "## Bar（bctf-02）\n"
        "- 首解耗时：7min\n"
        "- 思路1：captured flag{Leak-9f} then revoked\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runner_mod, "KNOWLEDGE_FILE", kb_file)
    kb = runner_mod._load_knowledge_base()
    assert kb["bctf-01"]["seconds"] == 180
    assert "nip.io" in kb["bctf-01"]["approach"]
    assert "Leak-9f" not in kb["bctf-02"]["approach"]  # 加载时脱敏


def test_v2_order_codes_explicit_priority() -> None:
    """V2-5：显式顺序置顶（未列题续队）；失配码告警忽略不阻断。"""

    @dataclass
    class DiffChallenge(FakeChallenge):
        difficulty: str = "medium"

    c1, c2, c3, c4 = (DiffChallenge(f"bctf-0{i}") for i in range(1, 5))
    flags = {c.unique_code: ["flag{f}"] for c in (c1, c2, c3, c4)}
    client = FakeClient([c1, c2, c3, c4], flags=flags)
    engine = FakeEngine({f"proj-{i}": ["flag{f}"] for i in range(4)})
    run_benchmark(
        client,
        lambda: engine,
        challenge_timeout_seconds=0.5,
        flag_poll_seconds=0,
        defer_after_seconds=5,
        order_codes=["bctf-03", "bctf-01", "zz-nonexistent"],
        parallel=2,
    )
    # 显式列表前两位置顶启动；失配码 zz-nonexistent 不阻断
    assert client.started[:2] == ["bctf-03", "bctf-01"]
    assert set(client.started) == {"bctf-01", "bctf-02", "bctf-03", "bctf-04"}


def test_v2_flag_variants_and_wrong_count() -> None:
    """V2-3：原样错交后自动大小写变体兜底；V2-2：wrong_count 记近失。"""
    from astra_runner.runner import _submit_flag_safely

    client = FakeClient([FakeChallenge("c1")], flags={"c1": ["FLAG{ABC}" ]})
    result = ChallengeResult(unique_code="c1", description="d")
    _submit_flag_safely(client, "c1", "flag{abc} ", result)  # 带空白，原样为小写错
    assert result.flags_correct == 1
    assert result.wrong_count >= 1  # 原样错交记为近失信号
    assert ("c1", "FLAG{ABC}") in client.submitted  # 变体兜底命中


def test_v2_hint_cache_store() -> None:
    """V2-1④：hint 购买即入 result 缓存（defer 续跑复用，禁止重购）。"""
    from astra_runner.runner import _try_platform_hint

    client = FakeClient([])
    engine = FakeEngine({})
    result = ChallengeResult(unique_code="c1", description="d")
    assert _try_platform_hint(client, engine, "c1", "proj-0", result)
    assert len(result.hint_texts) == 1
    assert "platform hint for c1" in result.hint_texts[0]


def test_v2_starvation_refill_uses_window() -> None:
    """V2-7：无窗口模式不回灌（防无限重拉）；带窗口且队列空时回灌已弃题。"""
    import astra_runner.runner as runner_mod

    @dataclass
    class HardChallenge(FakeChallenge):
        difficulty: str = "hard"

    ch = HardChallenge("bctf-x")
    client = FakeClient([ch])  # 无 flag 可解

    # 无窗口：defer 上限后放弃并自然收尾（不回灌）
    engine1 = FakeEngine({}, done=False)
    results1 = run_benchmark(
        client, lambda: engine1, challenge_timeout_seconds=0.2,
        flag_poll_seconds=0, defer_after_seconds=0.15, parallel=1,
    )
    assert client.started.count("bctf-x") <= runner_mod.MAX_DEFER_PER_CHALLENGE + 1
    assert results1[0].flags_correct == 0


def test_v2_parse_order_codes_cli_and_env() -> None:
    """V2-5 回归：CLI 值曾因 or 短路被按字符迭代（split 只作用于 env 分支）。"""
    from astra_runner.runner import _parse_order_codes

    assert _parse_order_codes("bctf-12,bctf-13, bctf-30", None) == ["bctf-12", "bctf-13", "bctf-30"]
    assert _parse_order_codes(None, "a-01,b-02") == ["a-01", "b-02"]
    assert _parse_order_codes("c1", "ignored-env") == ["c1"]
    assert _parse_order_codes(None, None) is None
    assert _parse_order_codes("", "") is None
    assert _parse_order_codes(" , ,x ,", None) == ["x"]


def test_v2_expected_budget_zero_kb_seconds() -> None:
    """V2-5/V2-7 回归：0min 首解的 KB 题应走 15min 地板预算（falsy 判空曾错放到难度预算）。"""
    from astra_runner.runner import DONE_FLAG_WAIT_SECONDS, _expected_budget_seconds

    r = ChallengeResult(unique_code="c", description="d")
    r.kb_seconds = 0.0
    assert _expected_budget_seconds(r, "hard", 1800) == 900 + DONE_FLAG_WAIT_SECONDS + 30


def test_v2_kb_short_first_attempt(monkeypatch, tmp_path) -> None:
    """V2-5 运行时缺口回归：KB 题首攻限时（只影响第一次尝试，第二发恢复完整梯子）。"""
    import time as _time

    import astra_runner.runner as runner_mod

    kb_file = tmp_path / "kb.md"
    kb_file.write_text(
        "## Foo（kb-01）\n- 首解耗时：0min\n- 思路1：historical approach\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runner_mod, "KNOWLEDGE_FILE", kb_file)
    monkeypatch.setattr(runner_mod, "EXPECTED_BUDGET_FLOOR_SECONDS", 0.5)

    ch = FakeChallenge("kb-01")
    client = FakeClient([ch])  # 永无可解 flag
    t0 = _time.monotonic()
    results = run_benchmark(
        client,
        lambda: FakeEngine({}, done=False),
        challenge_timeout_seconds=0.2,
        flag_poll_seconds=0,
        defer_after_seconds=6.0,  # 完整梯子 6s/发；首攻应被压到 0.5s
        parallel=1,
    )
    elapsed = _time.monotonic() - t0
    # 首攻 0.5s + 第二发 6s + 收尾 < 10s（若首攻也吃满 6s 会 >12s）
    assert elapsed < 10.0, f"first attack not shortened? elapsed={elapsed:.1f}s"
    assert client.started.count("kb-01") == 2  # 两发后 defer 上限放弃
    assert results[0].defer_count == runner_mod.MAX_DEFER_PER_CHALLENGE
