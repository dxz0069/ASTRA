"""astra-runner 编排逻辑测试（fake SDK client + fake engine）。"""

from __future__ import annotations

import os
import sys
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


def test_render_dispatch_config_dsh_worker(monkeypatch) -> None:
    """ASTRA_WORKER_TYPE=dsh 生成 dsh worker 配置（DSH_* env + 权限/隔离目录）。"""
    import pytest

    from astra.dispatcher.config import DispatchConfig
    from astra_runner.astra_runner_engine import AstraDaemon

    monkeypatch.setenv("ASTRA_WORKER_TYPE", "dsh")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("DSH_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("DSH_PATCH", "/opt/astra/dsh/astra-headless.patch.yml")
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    monkeypatch.delenv("ASTRA_MIX_PROVIDERS", raising=False)

    path = AstraDaemon()._render_dispatch_config()
    yaml = path.read_text(encoding="utf-8")

    assert 'type: "dsh"' in yaml
    assert 'DSH_MODEL: "deepseek-v4-pro"' in yaml
    assert 'DEEPSEEK_API_KEY: "sk-test"' in yaml
    assert 'DSH_PERMISSION_MODE: "danger-full-access"' in yaml
    assert 'DSH_PATCH: "/opt/astra/dsh/astra-headless.patch.yml"' in yaml
    assert "DSH_HOME:" in yaml
    # 生成的配置必须通过 dispatcher 同款 schema 校验（含 prompt 资源检查）
    config = DispatchConfig.load(path)
    assert config.workers[0].type == "dsh"
    assert config.workers[0].env["DSH_MODEL"] == "deepseek-v4-pro"


def test_render_dispatch_config_dsh_requires_api_key(monkeypatch) -> None:
    import pytest

    from astra_runner.astra_runner_engine import AstraDaemon

    monkeypatch.setenv("ASTRA_WORKER_TYPE", "dsh")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    monkeypatch.delenv("ASTRA_MIX_PROVIDERS", raising=False)

    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
        AstraDaemon()._render_dispatch_config()


def test_render_dispatch_config_mixed_fleet(monkeypatch) -> None:
    """DS+GLM 双 key 齐备 → 混合舰队 4 worker（同题多路并进）。"""
    from astra.dispatcher.config import DispatchConfig
    from astra_runner.astra_runner_engine import AstraDaemon

    monkeypatch.setenv("ASTRA_WORKER_TYPE", "dsh")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-test")
    monkeypatch.setenv("ZHIPU_API_KEY", "zk-glm-test")
    monkeypatch.setenv("DSH_PATCH", "/opt/astra/dsh/astra-headless.patch.yml")

    path = AstraDaemon()._render_dispatch_config()
    config = DispatchConfig.load(path)
    assert [w.name for w in config.workers] == [
        "deepseek-main",
        "glm-main",
        "glm-reason",
        "deepseek-fallback",
    ]
    by_name = {w.name: w for w in config.workers}
    # 探索双通道同优先级（running-count 轮转 → 同题多路不同模型）
    assert by_name["deepseek-main"].priority == 0
    assert by_name["glm-main"].priority == 0
    assert set(by_name["deepseek-main"].task_types) == {"bootstrap", "explore"}
    assert set(by_name["glm-main"].task_types) == {"bootstrap", "explore"}
    # 决策走 GLM 深度档，DS 兜底
    assert by_name["glm-reason"].priority == 1
    assert by_name["glm-reason"].env["DSH_REASONING_EFFORT"] == "xhigh"
    assert set(by_name["glm-reason"].task_types) == {"reason", "consolidate"}
    assert by_name["deepseek-fallback"].priority == 3
    # 通道与凭据
    assert by_name["deepseek-main"].env["DSH_PROVIDER"] == "deepseek"
    assert by_name["glm-main"].env["DSH_PROVIDER"] == "zhipu"
    assert by_name["glm-main"].env["ZHIPU_API_KEY"] == "zk-glm-test"
    assert by_name["glm-main"].env["DSH_REASONING_EFFORT"] == "high"
    assert by_name["glm-main"].env["DSH_MODEL"] == "glm-5.3"


def test_render_dispatch_config_mixed_fleet_disabled(monkeypatch) -> None:
    """ASTRA_MIX_PROVIDERS=0 强制单 worker（历史行为）。"""
    from astra.dispatcher.config import DispatchConfig
    from astra_runner.astra_runner_engine import AstraDaemon

    monkeypatch.setenv("ASTRA_WORKER_TYPE", "dsh")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds-test")
    monkeypatch.setenv("ZHIPU_API_KEY", "zk-glm-test")
    monkeypatch.setenv("ASTRA_MIX_PROVIDERS", "0")
    monkeypatch.setenv("DSH_PATCH", "/opt/astra/dsh/astra-headless.patch.yml")

    path = AstraDaemon()._render_dispatch_config()
    config = DispatchConfig.load(path)
    assert [w.name for w in config.workers] == ["deepseek-main"]


def test_render_dispatch_config_dsh_anthropic_mode(monkeypatch) -> None:
    """DSH_PROVIDER=anthropic → ANTHROPIC_* env（Kimi 等 Anthropic 兼容端点）。"""
    import pytest

    from astra.dispatcher.config import DispatchConfig
    from astra_runner.astra_runner_engine import AstraDaemon

    monkeypatch.setenv("ASTRA_WORKER_TYPE", "dsh")
    monkeypatch.setenv("DSH_PROVIDER", "anthropic")
    monkeypatch.setenv("DSH_MODEL", "k3")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-kimi")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.kimi.com/coding/")

    path = AstraDaemon()._render_dispatch_config()
    yaml = path.read_text(encoding="utf-8")

    assert 'type: "dsh"' in yaml
    assert 'DSH_PROVIDER: "anthropic"' in yaml
    assert 'ANTHROPIC_AUTH_TOKEN: "sk-kimi"' in yaml
    assert 'ANTHROPIC_BASE_URL: "https://api.kimi.com/coding/"' in yaml
    assert "DEEPSEEK_API_KEY" not in yaml
    config = DispatchConfig.load(path)
    assert config.workers[0].env["DSH_PROVIDER"] == "anthropic"


def test_render_dispatch_config_dsh_anthropic_requires_token(monkeypatch) -> None:
    import pytest

    from astra_runner.astra_runner_engine import AstraDaemon

    monkeypatch.setenv("ASTRA_WORKER_TYPE", "dsh")
    monkeypatch.setenv("DSH_PROVIDER", "anthropic")
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="ANTHROPIC_AUTH_TOKEN"):
        AstraDaemon()._render_dispatch_config()


def _make_session_dir(root: Path, name: str, age_days: float) -> Path:
    session_dir = root / f"--cwd--" / name
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "session.jsonl.zstd").write_bytes(b"x")
    old = time.time() - age_days * 86400
    os.utime(session_dir, (old, old))
    os.utime(session_dir / "session.jsonl.zstd", (old, old))
    return session_dir


def test_cleanup_dsh_home_keeps_recent_and_removes_stale(monkeypatch, tmp_path) -> None:
    """启动清理：保留最近 keep 个会话目录，删除更旧的（只清会话、不碰其他）。"""
    from astra_runner.astra_runner_engine import AstraDaemon

    dsh_home = tmp_path / "dsh-home"
    sessions = dsh_home / "sessions"
    _make_session_dir(sessions, "session-old-1", age_days=30)
    _make_session_dir(sessions, "session-old-2", age_days=20)
    _make_session_dir(sessions, "session-new", age_days=0.01)
    # 非会话文件不应被删
    keep_me = sessions / "keep.txt"
    keep_me.write_text("x", encoding="utf-8")

    monkeypatch.setenv("ASTRA_DSH_HOME", str(dsh_home))
    AstraDaemon._cleanup_dsh_home(keep=1)

    remaining = sorted(p.name for p in sessions.rglob("*") if p.is_dir() and (p / "session.jsonl.zstd").exists())
    assert remaining == ["session-new"]
    assert keep_me.exists()


def test_cleanup_dsh_home_noop_when_absent(monkeypatch, tmp_path) -> None:
    from astra_runner.astra_runner_engine import AstraDaemon

    monkeypatch.setenv("ASTRA_DSH_HOME", str(tmp_path / "missing"))
    AstraDaemon._cleanup_dsh_home()  # 不应抛异常


def test_collect_dsh_usage_aggregates_and_tolerates_bad_lines(monkeypatch, tmp_path) -> None:
    """token 计量：汇总 $DSH_HOME/usage/astra-usage.jsonl，坏行跳过。"""
    from astra_runner.runner import collect_dsh_usage

    usage_dir = tmp_path / "dsh" / "usage"
    usage_dir.mkdir(parents=True)
    usage_file = usage_dir / "astra-usage.jsonl"
    usage_file.write_text(
        "\n".join(
            [
                '{"ts":"t1","session":"s1","inputTokens":100,"outputTokens":20,"cacheReadTokens":10,"cacheWriteTokens":5,"reasoningTokens":3}',
                '{"ts":"t2","session":"s2","inputTokens":50,"outputTokens":30}',
                "not-json-line",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ASTRA_DSH_HOME", str(tmp_path / "dsh"))

    total = collect_dsh_usage()
    assert total["inputTokens"] == 150
    assert total["outputTokens"] == 50
    assert total["cacheReadTokens"] == 10
    assert total["cacheWriteTokens"] == 5
    assert total["reasoningTokens"] == 3

    # 无文件 → 空 dict
    monkeypatch.setenv("ASTRA_DSH_HOME", str(tmp_path / "missing"))
    assert collect_dsh_usage() == {}


def test_render_dispatch_config_defaults_to_dsh(monkeypatch) -> None:
    """默认 ASTRA_WORKER_TYPE=dsh（2026-08-15 翻转：run 9214 因漏带该变量静默
    回落 claudecode 单模型导致退步，默认值改为 dsh 防再犯）。"""
    from astra_runner.astra_runner_engine import AstraDaemon

    monkeypatch.delenv("ASTRA_WORKER_TYPE", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)
    monkeypatch.delenv("ASTRA_MIX_PROVIDERS", raising=False)
    monkeypatch.setenv("DSH_PATCH", "/opt/astra/dsh/astra-headless.patch.yml")

    path = AstraDaemon()._render_dispatch_config()
    yaml = path.read_text(encoding="utf-8")

    assert 'type: "dsh"' in yaml
    assert 'DEEPSEEK_API_KEY: "sk-test"' in yaml
    assert "claudecode" not in yaml
