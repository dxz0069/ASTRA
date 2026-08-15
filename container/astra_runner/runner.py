"""astra-runner —— tsecbench 靶场接入编排器（镜像 ENTRYPOINT / 本地模式通用）。

职责（SDK 五步生命周期 + 逐题 ASTRA 项目编排）：
1. 读取 BENCHMARK_TOKEN / BENCHMARK_BASE_URL，进入 SDK 上下文（自动 VPN 预检）
2. 列出题目，跳过已完成的
3. 逐题：start_challenge → 启动 ASTRA 引擎（server+dispatcher，local execution）
   → 创建项目（origin=container_addr，goal=题目描述）→ 等待完成/超时
   → 从星图 facts 中提取 flag{...} 统一提交（DuplicateSubmit 幂等跳过）
   → close_challenge（finally 释放名额）
4. 输出每题得分/flag/耗时报告（喂作品评审量化指标）

flag 提交由 runner 统一执行而非交给 agent 直连，保证幂等、防误报、可统计。
agent 只需把拿到的 flag 作为星记写回星图（见 skill 说明）。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

LOG = logging.getLogger("astra-runner")

FLAG_RE = re.compile(r"flag\{[^}\s]{3,}\}", re.IGNORECASE)
# 排除字面占位符 flag{...}（模型示例诱导）；真实 flag 内容至少 3 字符（flag{...} 占位由专门正则排除）
PLACEHOLDER_FLAG_RE = re.compile(r"^flag\{\s*\.{3}\s*\}$", re.IGNORECASE)

DEFAULT_CHALLENGE_TIMEOUT_SECONDS = 30 * 60
DEFAULT_FLAG_POLL_SECONDS = 5
# 每题最多 defer（45 分钟无果放回队尾）次数——超过则关闭放弃，防无限轮转
MAX_DEFER_PER_CHALLENGE = 2
# 按难度自适应题目超时（easy/medium/hard）
DIFFICULTY_TIMEOUTS = {"easy": 20 * 60, "medium": 30 * 60, "hard": 45 * 60}
# 引擎已完成后等待 flag 落图的窗口（不再空转一个完整超时）
DONE_FLAG_WAIT_SECONDS = 90
DEFAULT_PROJECT_TITLE_PREFIX = "astra-challenge"


@dataclass(slots=True)
class ChallengeResult:
    unique_code: str
    description: str
    started: bool = False
    flags_found: list[str] = field(default_factory=list)
    flags_correct: int = 0
    awarded: int = 0
    cumulative_score: int = 0
    elapsed_seconds: float = 0.0
    first_flag_seconds: float | None = None
    used_hint: bool = False
    facts_count: int = 0
    hints_count: int = 0
    error: str | None = None
    project_id: str | None = None  # defer 续跑：复用引擎项目保留星图进度
    defer_count: int = 0  # 已 defer 次数（达到上限则放弃该题关闭）


class TaskFinishedError(Exception):
    """跑分任务时限已到（平台 409 already finished），停止整轮。"""


class SlotBusyError(Exception):
    """平台活跃名额已满（start 409），稍后重试。"""


class BenchmarkClient(Protocol):
    """tsec_benchmark SDK 的最小接口投影（便于注入 fake 测试）。"""

    def list_challenges(self) -> list[Any]: ...
    def start_challenge(self, unique_code: str) -> Any: ...
    def get_hint(self, unique_code: str) -> Any: ...
    def submit_flag(self, unique_code: str, flag: str) -> Any: ...
    def close_challenge(self, unique_code: str) -> Any: ...


class AstraEngine(Protocol):
    """ASTRA 引擎接口投影：启动/创建项目/等待/取 facts/关闭。"""

    def start(self) -> None: ...
    def create_project(self, title: str, origin: str, goal: str) -> str: ...
    def create_hint(self, project_id: str, content: str) -> None: ...
    def wait_project(self, project_id: str, timeout_seconds: float) -> bool: ...
    def list_fact_descriptions(self, project_id: str) -> list[str]: ...
    def stop(self) -> None: ...


# SDK 业务异常按异常名识别（不重试）；其余（网络/服务错误）重试退避
KNOWN_BUSINESS_EXC_NAMES = {"InvalidState", "DuplicateSubmit", "TaskFinishedError", "SlotBusyError"}


def call_with_retry(fn, name: str, *, retries: int = 3, base_delay: float = 5.0):
    """SDK 调用重试保护：网络/服务类错误指数退避重试，业务异常（按名）直接抛出。

    连续重试仍失败 → ERROR 告警（可能 VPN 断开/平台不可达），抛出最后一个错误。
    """
    import random

    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 —— SDK 异常类型不可控，按名分流
            if type(exc).__name__ in KNOWN_BUSINESS_EXC_NAMES:
                raise
            last = exc
            if attempt < retries:
                delay = base_delay * (2**attempt) * (0.5 + random.random() * 0.5)
                LOG.warning(
                    "%s failed attempt=%s/%s error=%s（%.0fs 后重试）",
                    name, attempt + 1, retries + 1, exc, delay,
                )
                time.sleep(delay)
    LOG.error("%s unreachable after %s attempts error=%s（可能 VPN 断开或平台不可达）", name, retries + 1, last)
    assert last is not None
    raise last


def extract_flags(text: str) -> list[str]:
    """从文本中提取 flag{...}，去重保序，排除占位符（如 flag{...}）。"""
    seen: set[str] = set()
    flags: list[str] = []
    for match in FLAG_RE.findall(text):
        flag = match.strip()
        if PLACEHOLDER_FLAG_RE.match(flag):
            continue
        if flag not in seen:
            seen.add(flag)
            flags.append(flag)
    return flags


def collect_flags_from_facts(descriptions: list[str]) -> list[str]:
    flags: list[str] = []
    for description in descriptions:
        flags.extend(extract_flags(description))
    seen: set[str] = set()
    unique: list[str] = []
    for flag in flags:
        if flag not in seen:
            seen.add(flag)
            unique.append(flag)
    return unique


class ProgressStore:
    """断点续跑进度存储：{题码: started|done} 的线程安全 JSON 文件。

    每题 start 成功后标记 started，关题后标记 done；重启 runner 时用同一
    --progress-file 自动跳过已尝试的题（重构备忘候选：runner 进度文件断点续跑）。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, str] = {}

    @classmethod
    def load(cls, path: str | None) -> "ProgressStore | None":
        if not path:
            return None
        store = cls(Path(path))
        if store.path.exists():
            try:
                loaded = json.loads(store.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    store._data = {str(k): str(v) for k, v in loaded.items()}
            except (json.JSONDecodeError, OSError):
                store._data = {}  # 损坏的进度文件按空处理，不阻断
        return store

    def skipped_codes(self) -> set[str]:
        # 跳过 done / close_failed：已完整跑过或关题泄漏需人工；started 不跳过——
        # 崩溃重启后重新 start（平台幂等返回同地址）继续解题，避免放弃已启动的题目。
        return {code for code, state in self._data.items() if state in ("done", "close_failed")}

    def mark(self, code: str, state: str) -> None:
        with self._lock:
            self._data[code] = state
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_name(self.path.name + ".tmp")
                tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8")
                tmp.replace(self.path)
            except OSError:
                pass  # 进度落盘失败不阻断主流程


def window_allows_start(window_deadline: float | None, longest_single: float, now: float | None = None) -> bool:
    """任务限时窗口是否允许再开新题：剩余时间必须大于最长单题耗时。

    无窗口（None）恒允许；now 可注入（测试）。"""
    if window_deadline is None:
        return True
    return (now if now is not None else time.monotonic()) < window_deadline - longest_single


def run_benchmark(
    client: BenchmarkClient,
    engine_factory,
    *,
    challenge_timeout_seconds: float = DEFAULT_CHALLENGE_TIMEOUT_SECONDS,
    flag_poll_seconds: float = DEFAULT_FLAG_POLL_SECONDS,
    skip_completed: bool = True,
    skip_codes: set[str] | None = None,
    max_challenges: int | None = None,
    parallel: int | str = "auto",
    progress_file: str | None = None,
    task_window_seconds: float | None = None,
    auto_hint: bool = True,
    hint_after_seconds: float = 900.0,
    hint2_after_seconds: float = 1800.0,
    defer_after_seconds: float = 2700.0,
    hint_min_score: int = 0,
    prefer_easy: bool = True,
) -> list[ChallengeResult]:
    """五步生命周期主循环（并行窗口模型：同时活跃 N 题，逐题补位）。

    parallel：并发题数；"auto" 时从 2 开始探测扩容到平台名额上限（满载运行）。
    progress_file：断点续跑进度文件；提供时启动自动跳过已 done 的题
    （started 的题重启后重新 start 续跑），每题 start/关题时自动落盘。
    task_window_seconds：任务限时窗口（如六小时）；剩余时间不足以完成最长单题时
    停止开新题（活动题自然跑完），避免临期启动长题导致被迫中断。
    auto_hint / hint_after_seconds / hint2_after_seconds / hint_min_score：平台
    hint 分级策略——连续分析 hint_after_seconds（默认 900s=15 分钟）无结果取
    第一次 hint，hint2_after_seconds（默认 1800s=30 分钟）无结果取第二次；
    hint 会按比例扣该题得分，hint_min_score 可设门槛（默认 0=不限制）。
    defer_after_seconds：单题最长连续分析（默认 2700s=45 分钟）——仍无结果则
    **保留引擎项目进度**（星图/会话不删）并把该题放回队列末尾（不标记 done，
    队列轮转后再续跑），避免死磕单题拖垮整轮吞吐。
    prefer_easy：easy→medium→hard 排序开题（默认开）——先收割低分险题再攻坚，
    避免平台原始顺序把 easy 题排到窗口尾部饿死（run 9214 实测丢 3 道 easy）。
    """
    progress = ProgressStore.load(progress_file)
    task_started_at = time.monotonic()
    window_deadline = task_started_at + task_window_seconds if task_window_seconds is not None else None
    longest_single = max(DIFFICULTY_TIMEOUTS.values(), default=challenge_timeout_seconds) + DONE_FLAG_WAIT_SECONDS + 30
    # 单题最长连续分析以 defer_after_seconds 为准（默认 45 分钟），窗口收尾判断用它
    if defer_after_seconds > 0:
        longest_single = max(longest_single, defer_after_seconds + DONE_FLAG_WAIT_SECONDS + 30)
    challenges = call_with_retry(lambda: client.list_challenges(), "list_challenges")
    # 启动时清理已完成题的遗留容器（上轮异常退出可能残留并占用活跃名额）
    for ch in challenges:
        code0 = getattr(ch, "unique_code", None) or getattr(ch, "code", "")
        if code0 and getattr(ch, "is_completed", False):
            try:
                call_with_retry(lambda: client.close_challenge(code0), f"close_challenge:{code0}", retries=2)
            except Exception:  # noqa: BLE001 —— 幂等清理，失败忽略
                pass
    queue: deque = deque()
    results: dict[str, ChallengeResult] = {}
    for ch in challenges:
        code = getattr(ch, "unique_code", None) or getattr(ch, "code", "")
        description = getattr(ch, "description", "") or ""
        if not code:
            continue
        result = ChallengeResult(unique_code=code, description=description)
        if skip_completed and getattr(ch, "is_completed", False):
            LOG.info("skip completed challenge code=%s", code)
            results[code] = result
            continue
        if skip_codes and code in skip_codes:
            LOG.info("skip challenge by skip_codes code=%s", code)
            results[code] = result
            continue
        if progress is not None and code in progress.skipped_codes():
            LOG.info("skip challenge by progress file code=%s（断点续跑）", code)
            results[code] = result
            continue
        queue.append((ch, result))
        results[code] = result

    if prefer_easy and queue:
        # easy→medium→hard 开题（稳定排序：同难度保持平台原始顺序）。
        # 未知难度排在 medium 之后、hard 之前，不置于队首冒进。
        diff_rank = {"easy": 0, "medium": 1, "hard": 2}
        ordered = sorted(
            queue,
            key=lambda item: diff_rank.get(str(getattr(item[0], "difficulty", "") or "").lower(), 1.5),
        )
        queue = deque(ordered)
        LOG.info(
            "queue ordered easy-first（easy=%s medium=%s hard=%s/未知=%s）",
            sum(1 for c, _ in ordered if str(getattr(c, "difficulty", "")).lower() == "easy"),
            sum(1 for c, _ in ordered if str(getattr(c, "difficulty", "")).lower() == "medium"),
            sum(1 for c, _ in ordered if str(getattr(c, "difficulty", "")).lower() == "hard"),
            sum(1 for c, _ in ordered if str(getattr(c, "difficulty", "") or "").lower() not in diff_rank),
        )

    active: dict[str, threading.Thread] = {}
    auto_mode = parallel == "auto" or parallel is None
    slots = 2 if auto_mode else max(1, int(parallel))
    stop_event = threading.Event()
    stop_errors: list[Exception] = []
    busy_seen = threading.Event()
    probed_locked = [False]
    last_expand = [0.0]

    def _work(ch, result: ChallengeResult) -> None:
        while True:
            try:
                status = _run_single_challenge(
                    client, engine_factory, ch, result,
                    challenge_timeout_seconds, flag_poll_seconds, progress,
                    auto_hint=auto_hint, hint_after_seconds=hint_after_seconds,
                    hint2_after_seconds=hint2_after_seconds, defer_after_seconds=defer_after_seconds,
                    hint_min_score=hint_min_score,
                )
                if status == "deferred":
                    # 60 分钟无果：保留引擎项目进度，放回队列末尾（不标 done），
                    # 队列轮转后再续跑——避免死磕单题拖垮整轮吞吐
                    result.defer_count += 1
                    if result.defer_count >= MAX_DEFER_PER_CHALLENGE:
                        # 达到 defer 上限：彻底放弃——删引擎项目并标 done（防重启重跑）
                        LOG.info(
                            "challenge give up code=%s defer=%s（达到上限，删除项目放弃）",
                            result.unique_code, result.defer_count,
                        )
                        try:
                            delete_fn = getattr(engine_factory(), "delete_project", None)
                            if callable(delete_fn):
                                delete_fn(result.project_id)
                        except Exception:  # noqa: BLE001
                            pass
                        if progress is not None:
                            progress.mark(result.unique_code, "done")
                        return
                    LOG.info("challenge deferred code=%s（%s 分钟无结果，保留进度放回队尾）", result.unique_code, defer_after_seconds / 60)
                    queue.append((ch, result))
                return
            except TaskFinishedError as exc:
                stop_errors.append(exc)
                stop_event.set()
                return
            except SlotBusyError:
                busy_seen.set()
                LOG.warning("active slot busy code=%s 放回队列，等待名额释放", result.unique_code)
                time.sleep(30)
                queue.appendleft((ch, result))
                return

    def _fill() -> None:
        nonlocal slots
        # 任务限时窗口：剩余时间不足以完成最长单题 → 停止开新题（活动题自然跑完）
        if not window_allows_start(window_deadline, longest_single):
            if queue:
                LOG.info(
                    "task window nearly over（剩余不足最长单题），停止开新题（活动题自然跑完）",
                )
                queue.clear()
        while queue and len(active) < slots and not stop_event.is_set():
            ch, result = queue.popleft()
            thread = threading.Thread(target=_work, args=(ch, result), daemon=True)
            active[result.unique_code] = thread
            thread.start()
            LOG.info("并行窗口启动题目 code=%s active=%s/%s", result.unique_code, len(active), slots)
        # 自动满载探测：窗口已满、队列还有题、未遇名额满、距上次扩窗≥5s → 尝试扩容
        now = time.monotonic()
        if (
            auto_mode and not probed_locked[0] and queue
            and len(active) == slots and now - last_expand[0] >= 5.0
        ):
            slots += 1
            last_expand[0] = now
            LOG.info("并行窗口探测扩容 slots=%s（若平台拒绝将自动回退）", slots)
        # 遇到名额满：收缩一次并锁定（active 含休眠线程，不能以其为准）
        if auto_mode and busy_seen.is_set() and not probed_locked[0]:
            slots = max(2, slots - 1)
            probed_locked[0] = True
            LOG.info("并行窗口收缩并锁定 slots=%s（名额上限已探明）", slots)

    _fill()
    while active and not stop_event.is_set():
        finished = [code for code, t in list(active.items()) if not t.is_alive()]
        for code in finished:
            thread = active.pop(code)
            thread.join(timeout=5)
            LOG.info("并行窗口完成题目 code=%s active=%s/%s", code, len(active), slots)
        _fill()
        if active:
            time.sleep(2)

    if stop_errors:
        # 任务到期（409 already finished）：返回已收集的 results（含部分完成的题目），
        # 让 main 输出完整报告——修复 2026-08 实测"到期后报告崩溃 UnboundLocalError"
        LOG.warning("任务提前停止（%s 个停止错误），返回已收集结果", len(stop_errors))
        ordered = [
            results[code]
            for ch in challenges
            if (code := getattr(ch, "unique_code", None) or getattr(ch, "code", "")) in results
        ]
        return ordered

    ordered = [
        results[code]
        for ch in challenges
        if (code := getattr(ch, "unique_code", None) or getattr(ch, "code", "")) in results
    ]
    return ordered


def _run_single_challenge(
    client: BenchmarkClient,
    engine_factory,
    ch,
    result: ChallengeResult,
    challenge_timeout_seconds: float,
    flag_poll_seconds: float,
    progress: ProgressStore | None = None,
    *,
    auto_hint: bool = True,
    hint_after_seconds: float = 900.0,
    hint2_after_seconds: float = 1800.0,
    defer_after_seconds: float = 2700.0,
    hint_min_score: int = 0,
) -> str | None:
    """单题五步生命周期（并行窗口内每个线程跑一个）。

    返回状态：None=完成/超时关闭；"deferred"=45 分钟无结果保留进度放回队尾。
    """
    code = result.unique_code
    description = result.description
    started_at = time.monotonic()
    status: str | None = None  # 函数返回状态：None=完成/关闭；"deferred"=保留进度放回队尾
    difficulty = str(getattr(ch, "difficulty", "") or "").lower()
    timeout_seconds = DIFFICULTY_TIMEOUTS.get(difficulty, challenge_timeout_seconds)
    engine = engine_factory()
    try:
        engine.start()
        try:
            started = call_with_retry(lambda: client.start_challenge(code), f"start_challenge:{code}")
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ == "InvalidState" and "already finished" in str(exc):
                LOG.warning("跑分任务时限已到，停止整轮 code=%s", code)
                raise TaskFinishedError(str(exc)) from exc
            if type(exc).__name__ == "InvalidState":
                raise SlotBusyError(str(exc)) from exc
            raise
        result.started = True
        if progress is not None:
            progress.mark(code, "started")
        container_addr = getattr(started, "container_addr", None) or []
        origin = ", ".join(str(addr) for addr in container_addr) or code
        goal = _build_goal(description, ch)
        # defer 续跑：复用原引擎项目（星图/会话进度保留），否则新建
        if result.project_id:
            project_id = result.project_id
            LOG.info("challenge resumed code=%s reuse project=%s（defer 续跑）", code, project_id)
        else:
            project_id = engine.create_project(
                title=f"{DEFAULT_PROJECT_TITLE_PREFIX}-{code}",
                origin=origin,
                goal=goal,
            )
            result.project_id = project_id
        LOG.info("challenge started code=%s project=%s addr=%s", code, project_id, origin)

        # 等待引擎归航的同时，周期扫描星图 flag 并立即提交（发现即提交，不等归航）
        done = False
        project_gone = False  # 提前收尾删了引擎项目 → 跳过后续取 flag（避免 404）
        # 单题最长连续分析：defer_after_seconds（默认 45 分钟）无结果 → 保留进度放回队尾
        effective_timeout = defer_after_seconds if defer_after_seconds > 0 else timeout_seconds
        deadline = time.monotonic() + effective_timeout
        scan_round = 0
        # 卡题分级 hint：hint_after_seconds（默认 15 分钟）取第一次，
        # hint2_after_seconds（默认 30 分钟）取第二次（每题最多两次）。
        # hint 会按比例扣该题得分，hint_min_score 可设门槛（默认 0=不限制）。
        challenge_score = int(getattr(ch, "total_score", 0) or 0)
        hint_trigger_at = started_at + hint_after_seconds
        hint2_trigger_at = started_at + hint2_after_seconds if hint2_after_seconds > 0 else float("inf")
        hint_eligible = auto_hint and challenge_score >= hint_min_score
        hint_taken = 0  # 0/1/2：已取 hint 次数
        while time.monotonic() < deadline:
            try:
                flags = collect_flags_from_facts(engine.list_fact_descriptions(project_id))
                pending = [flag for flag in flags if flag not in result.flags_found]
                for flag in pending:
                    _submit_flag_safely(client, code, flag, result, started_at)
            except Exception:  # noqa: BLE001 —— 引擎 API 偶发失败不中断等待
                pass
            if hint_eligible and not result.flags_found:
                now = time.monotonic()
                if hint_taken < 1 and now >= hint_trigger_at:
                    hint_taken = 1 if _try_platform_hint(client, engine, code, project_id, result) else hint_taken
                elif hint_taken < 2 and now >= hint2_trigger_at:
                    hint_taken = 2 if _try_platform_hint(client, engine, code, project_id, result) else hint_taken
            # 每 6 轮重新拉取题目列表：平台侧已完成（如 flag 已全提交）→ 提前收尾释放名额
            scan_round += 1
            if scan_round % 6 == 0:
                try:
                    fresh = {c.unique_code: c for c in call_with_retry(lambda: client.list_challenges(), "list_challenges", retries=2)}
                    current = fresh.get(code)
                    if current is not None and getattr(current, "is_completed", False):
                        LOG.info("challenge completed on platform side code=%s 提前收尾并停引擎项目", code)
                        # 先收最后一批 flag（多 flag 题可能还有未提交的），再删引擎项目
                        try:
                            last_flags = collect_flags_from_facts(engine.list_fact_descriptions(project_id))
                            for flag in [f for f in last_flags if f not in result.flags_found]:
                                _submit_flag_safely(client, code, flag, result, started_at)
                        except Exception:  # noqa: BLE001
                            pass
                        delete_fn = getattr(engine, "delete_project", None)
                        if callable(delete_fn):
                            try:
                                delete_fn(project_id)
                            except Exception:  # noqa: BLE001
                                pass
                        done = True
                        project_gone = True
                        break
                except Exception:  # noqa: BLE001
                    pass
            if engine.wait_project(project_id, timeout_seconds=0.5):
                done = True
                break
            time.sleep(flag_poll_seconds)
        if not done:
            # 引擎未完成：最后收一次 flag
            flags = collect_flags_from_facts(engine.list_fact_descriptions(project_id))
            pending = [flag for flag in flags if flag not in result.flags_found]
            for flag in pending:
                _submit_flag_safely(client, code, flag, result, started_at)
            if result.flags_found:
                # 已部分解出（多 flag 题）：正常收尾关闭，不 defer
                LOG.info("challenge closed with partial flags code=%s flags=%s", code, result.flags_found)
                continue_flag_wait = False
            else:
                # 45 分钟无任何结果：保留引擎项目进度放回队尾（不删项目、不标 done）
                LOG.warning(
                    "challenge deferred code=%s difficulty=%s 连续分析 %ss 无结果（保留进度放回队尾）",
                    code, difficulty, effective_timeout,
                )
                project_gone = True
                continue_flag_wait = False
                status = "deferred"
                return status
        else:
            continue_flag_wait = True

        # 从星图收集 flag 并统一提交（引擎完成后再等一个短窗口）
        deadline = time.monotonic() + (DONE_FLAG_WAIT_SECONDS if continue_flag_wait and not project_gone else 0)
        while not project_gone and time.monotonic() < deadline:
            flags = collect_flags_from_facts(engine.list_fact_descriptions(project_id))
            pending = [flag for flag in flags if flag not in result.flags_found]
            if not pending:
                if flags:
                    break  # 全部已提交
                time.sleep(flag_poll_seconds)
                continue
            for flag in pending:
                _submit_flag_safely(client, code, flag, result, started_at)
            time.sleep(flag_poll_seconds)
    except TaskFinishedError:
        raise
    except SlotBusyError:
        raise
    except Exception as exc:  # noqa: BLE001 —— 单题失败不阻断整轮
        result.error = str(exc)
        LOG.exception("challenge failed code=%s error=%s", code, exc)
    finally:
        result.elapsed_seconds = time.monotonic() - started_at
        try:
            stats_fn = getattr(engine, "stats", None)
            if callable(stats_fn):
                stats = stats_fn(project_id)
                result.facts_count = int(stats.get("facts", 0) or 0)
                result.hints_count = int(stats.get("hints", 0) or 0)
        except Exception:  # noqa: BLE001 —— 统计失败不影响主流程
            pass
        # defer：保留引擎项目进度（不删），仅关平台题释放名额；progress 保持
        # started/不标 done，队列轮转后重新 start 直接续跑同项目
        deferred = status == "deferred"
        if result.started:
            closed = False
            for attempt in range(3):
                try:
                    call_with_retry(lambda: client.close_challenge(code), f"close_challenge:{code}", retries=2)
                    closed = True
                    break
                except Exception:  # noqa: BLE001
                    LOG.warning("close_challenge failed code=%s attempt=%s/3（重试释放名额）", code, attempt + 1)
                    time.sleep(5)
            if not closed:
                LOG.error("close_challenge exhausted retries code=%s（平台活跃名额可能泄漏，需人工关闭）", code)
            if progress is not None and not deferred:
                # 关题失败不标 done：close_failed 保留在进度文件里便于事后排查/补关
                progress.mark(code, "done" if closed else "close_failed")
        engine.stop()


def _build_goal(description: str, ch: Any) -> str:
    flag_count = getattr(ch, "flag_count", None)
    goal = f"在靶场地址上完成题目并获取全部 flag，目标描述：{description}"
    if flag_count:
        goal += f"（共 {flag_count} 个 flag）"
    goal += "。拿到 flag 后必须以星记形式写回星图，星记描述中必须包含完整 flag{...} 字符串。"
    return goal


def _try_platform_hint(
    client: BenchmarkClient,
    engine: Any,
    code: str,
    project_id: str,
    result: ChallengeResult,
) -> bool:
    """卡题半程时获取平台 hint 并注入 ASTRA 项目。成功返回 True（每题只取一次）。

    hint 会按平台比例扣该题得分，但"部分分"优于"0 分"；注入后星探在下一轮
    定航时通过项目 hints 读取（ASTRA 的指引原语）。
    """
    try:
        hint_res = call_with_retry(lambda: client.get_hint(code), f"get_hint:{code}", retries=2)
    except Exception as exc:  # noqa: BLE001 —— hint 获取失败不阻塞等待
        LOG.warning("platform hint failed code=%s error=%s（继续探索）", code, exc)
        return True  # 已尝试，避免每轮重试
    hint_text = getattr(hint_res, "hint", None) or getattr(hint_res, "content", None) or ""
    if not hint_text:
        LOG.info("platform hint empty code=%s", code)
        return True
    try:
        engine.create_hint(project_id, f"[平台提示] {hint_text}")
        result.used_hint = True
        LOG.info("platform hint injected code=%s hint=%s", code, hint_text[:120])
    except Exception as exc:  # noqa: BLE001
        LOG.warning("platform hint inject failed code=%s error=%s", code, exc)
    return True


def _submit_flag_safely(
    client: BenchmarkClient,
    code: str,
    flag: str,
    result: ChallengeResult,
    started_at: float | None = None,
) -> None:
    try:
        res = call_with_retry(lambda: client.submit_flag(code, flag), f"submit_flag:{code}", retries=2)
    except Exception as exc:  # noqa: BLE001 —— 按异常名识别 SDK 的 DuplicateSubmit（幂等）
        if type(exc).__name__ == "DuplicateSubmit":
            LOG.info("flag duplicate skip code=%s flag=%s", code, flag)
            result.flags_found.append(flag)
            return
        LOG.warning("submit_flag failed code=%s flag=%s error=%s", code, flag, exc)
        return
    result.flags_found.append(flag)
    correct = bool(getattr(res, "correct", False))
    if correct:
        result.flags_correct += 1
        if result.first_flag_seconds is None and started_at is not None:
            result.first_flag_seconds = round(time.monotonic() - started_at, 1)
    awarded = int(getattr(res, "awarded", 0) or 0)
    cumulative = int(getattr(res, "cumulative_score", 0) or 0)
    if awarded:
        result.awarded += awarded
    if cumulative:
        result.cumulative_score = cumulative
    LOG.info(
        "flag submitted code=%s flag=%s correct=%s awarded=%s progress=%s/%s",
        code,
        flag,
        correct,
        awarded,
        getattr(res, "correct_flag_count", None),
        getattr(res, "total_flag_count", None),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="astra-runner: tsecbench 靶场接入编排器")
    parser.add_argument("--token", default=None, help="BENCHMARK_TOKEN（默认读环境变量）")
    parser.add_argument("--base-url", default=None, help="BENCHMARK_BASE_URL（默认读环境变量）")
    parser.add_argument("--challenge-timeout", type=int, default=DEFAULT_CHALLENGE_TIMEOUT_SECONDS)
    parser.add_argument("--skip-codes", default=None, help="逗号分隔的跳过题目码（断点续跑，手动版）")
    parser.add_argument("--progress-file", default=None, help="断点续跑进度文件路径：自动记录已尝试题码，重启时自动跳过（推荐替代 --skip-codes）")
    parser.add_argument("--parallel", default="auto", help="并行活跃题数；auto=自动探测平台名额上限满载运行（默认）")
    parser.add_argument(
        "--task-window-minutes",
        type=int,
        default=int(os.environ.get("ASTRA_TASK_WINDOW_MINUTES", "360")),
        help="任务限时窗口（分钟，默认 360=六小时；托管模式无 CLI 参数，可用环境变量 "
        "ASTRA_TASK_WINDOW_MINUTES 覆盖——如百度 24 小时窗口设 1380）；"
        "剩余时间不足最长单题时停止开新题",
    )
    parser.add_argument("--no-auto-hint", action="store_true", help="禁用卡题自动获取平台 hint（hint 会按比例扣该题得分）")
    parser.add_argument("--no-prefer-easy", action="store_true", help="禁用 easy→medium→hard 开题排序（恢复平台原始顺序）")
    parser.add_argument("--hint-after-seconds", type=float, default=900.0, help="第一次 hint 触发时间（默认 900s=15 分钟无解即取）")
    parser.add_argument("--hint2-after-seconds", type=float, default=1800.0, help="第二次 hint 触发时间（默认 1800s=30 分钟无解即取）")
    parser.add_argument("--defer-after-seconds", type=float, default=2700.0, help="单题最长连续分析（默认 2700s=45 分钟无果保留进度放回队尾）")
    parser.add_argument("--hint-min-score", type=int, default=0, help="自动 hint 的最低题分值（默认 0=不限制）")
    parser.add_argument("--once", action="store_true", help="跑一轮后退出（默认循环直到任务结束）")
    parser.add_argument("--engine", default="local", choices=["local"], help="ASTRA 引擎模式（当前仅 local）")
    parser.add_argument("--watchdog", action="store_true", help="同时拉起模型健康 watchdog（403/配额秒级告警，2026-08 实测教训）")
    parser.add_argument("--check", action="store_true", help="环境自检后退出（平台 API 连通 / dsh CLI / DSH patch / 引擎可启动）")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    token = args.token or _env("BENCHMARK_TOKEN") or _env("TSEC_TOKEN")
    base_url = args.base_url or _env("BENCHMARK_BASE_URL") or _env("TSEC_BASE_URL")
    if not token or not base_url:
        LOG.error("缺少 BENCHMARK_TOKEN / BENCHMARK_BASE_URL（或 TSEC_TOKEN / TSEC_BASE_URL）")
        return 2

    if args.check:
        return _run_environment_check(token, base_url)

    try:
        from tsec_benchmark import TSecBenchmark  # 进入上下文自动 VPN 预检
    except ImportError:
        LOG.error("缺少 tsec-benchmark SDK：pip install tsec-benchmark")
        return 2

    from astra_runner_engine import LocalAstraEngine, shutdown_daemon  # 引擎实现（镜像内置）

    watchdog_proc: subprocess.Popen | None = None
    if args.watchdog:
        try:
            wd = Path(__file__).with_name("model_watchdog.py")
            watchdog_proc = subprocess.Popen(
                [sys.executable, str(wd)],
                env={k: v for k, v in os.environ.items()},
            )
            LOG.info("model watchdog started pid=%s（403/配额告警文件 %s）", watchdog_proc.pid, os.environ.get("TEMP", "."))
        except Exception as exc:  # noqa: BLE001 —— watchdog 拉起失败不阻塞跑分
            LOG.warning("model watchdog start failed error=%s（继续跑分，需人工监控）", exc)

    results: list[ChallengeResult] = []
    try:
        with TSecBenchmark(base_url=base_url, token=token) as client:
            skip_codes = {c.strip() for c in args.skip_codes.split(",")} if args.skip_codes else None
            results = run_benchmark(
                client,
                lambda: LocalAstraEngine(),
                challenge_timeout_seconds=args.challenge_timeout,
                skip_codes=skip_codes,
                parallel=args.parallel,
                progress_file=args.progress_file,
                task_window_seconds=args.task_window_minutes * 60 if args.task_window_minutes else None,
                auto_hint=not args.no_auto_hint,
                hint_after_seconds=args.hint_after_seconds,
                hint2_after_seconds=args.hint2_after_seconds,
                defer_after_seconds=args.defer_after_seconds,
                hint_min_score=args.hint_min_score,
                prefer_easy=not args.no_prefer_easy,
            )
    except TaskFinishedError:
        LOG.info("任务时限结束，输出已完成的报告")
    finally:
        shutdown_daemon()
        if watchdog_proc is not None:
            try:
                watchdog_proc.terminate()
            except Exception:  # noqa: BLE001
                pass

    total_awarded = sum(r.awarded for r in results)
    total_flags = sum(r.flags_correct for r in results)
    usage = collect_dsh_usage()
    LOG.info("=== astra-runner 报告 ===")
    report: dict[str, Any] = {
        "challenges": [
            {
                "code": r.unique_code,
                "started": r.started,
                "flags_found": len(r.flags_found),
                "flags_correct": r.flags_correct,
                "awarded": r.awarded,
                "elapsed_seconds": round(r.elapsed_seconds, 1),
                "first_flag_seconds": r.first_flag_seconds,
                "used_hint": r.used_hint,
                "facts_count": r.facts_count,
                "hints_count": r.hints_count,
                "error": r.error,
            }
            for r in results
        ],
        "total_awarded": total_awarded,
        "total_flags_correct": total_flags,
    }
    if usage:
        report["total_tokens"] = {
            "input": usage["inputTokens"],
            "output": usage["outputTokens"],
            "cache_read": usage["cacheReadTokens"],
            "cache_write": usage["cacheWriteTokens"],
            "reasoning": usage["reasoningTokens"],
        }
    LOG.info(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def _env(name: str) -> str | None:
    import os

    return os.environ.get(name) or None


def _run_environment_check(token: str, base_url: str) -> int:
    """接 token 后的第一步自检：平台 API 连通 / 模型 CLI / DSH 组件 / 引擎可启动。

    全部通过返回 0；任一项失败返回 1（并给出修复提示）。
    """
    import os
    import shutil
    import tempfile

    LOG.info("=== 环境自检 ===")
    ok = True

    # 1. 平台 API 连通（带 token 调 list_challenges，不含 VPN 内目标访问）
    try:
        import requests

        resp = requests.get(f"{base_url}/openapi/v1/challenges", headers={"BENCHMARK_TOKEN": token}, timeout=15)
        if resp.status_code == 200:
            LOG.info("[PASS] 平台 API 连通 status=200 challenges=%s", len(resp.json() or []))
        else:
            LOG.error("[FAIL] 平台 API 返回 status=%s body=%s", resp.status_code, resp.text[:200])
            ok = False
    except Exception as exc:  # noqa: BLE001
        LOG.error("[FAIL] 平台 API 不可达 error=%s（检查 BENCHMARK_BASE_URL / 网络 / VPN）", exc)
        ok = False

    # 2. 模型 CLI / dsh 组件（默认 dsh——与引擎 ASTRA_WORKER_TYPE 新默认一致）
    if os.environ.get("ASTRA_WORKER_TYPE", "dsh") == "dsh":
        dsh = shutil.which("dsh")
        if dsh:
            LOG.info("[PASS] dsh CLI 位于 %s", dsh)
        else:
            LOG.error("[FAIL] 未找到 dsh CLI（npm install -g @deepseek-ai/dsh，见 container/dsh/README.md）")
            ok = False
        from astra_runner_engine import AstraDaemon

        patch = AstraDaemon._resolve_dsh_patch()
        if os.path.exists(patch):
            LOG.info("[PASS] DSH patch 存在 %s", patch)
        else:
            LOG.error("[FAIL] DSH patch 不存在 %s", patch)
            ok = False
        # 舰队预告：双 key = DS+GLM 混合 4 worker；单 key = 单 worker（渲染时再严格校验）
        if os.environ.get("DEEPSEEK_API_KEY") and os.environ.get("ZHIPU_API_KEY"):
            if os.environ.get("ASTRA_MIX_PROVIDERS", "auto") not in ("0", "false"):
                LOG.info("[PASS] 模型舰队 = dsh 混合（deepseek-main + glm-main + glm-reason + deepseek-fallback）")
            else:
                LOG.info("[PASS] 模型舰队 = dsh 单 worker（ASTRA_MIX_PROVIDERS 已关闭混合）")
        elif os.environ.get("DEEPSEEK_API_KEY"):
            LOG.info("[PASS] 模型舰队 = dsh 单 worker（deepseek；注入 ZHIPU_API_KEY 可启用混合舰队）")
        elif os.environ.get("ZHIPU_API_KEY"):
            LOG.warning("[注意] 仅 ZHIPU_API_KEY：dsh 模式要求 DEEPSEEK_API_KEY（混合或单 DS），当前配置渲染会失败")
        else:
            LOG.error("[FAIL] 缺少 DEEPSEEK_API_KEY（dsh 模式必填；混合舰队另需 ZHIPU_API_KEY）")
            ok = False
    else:
        claude = shutil.which("claude")
        LOG.info("[%s] claude CLI 位于 %s", "PASS" if claude else "FAIL", claude or "未找到")
        if not claude:
            ok = False
        LOG.warning("[注意] ASTRA_WORKER_TYPE 已显式指定为 claudecode——默认/推荐路径是 dsh（run 9214 曾因回落 claudecode 退步），确认这是有意为之")

    # 3. 引擎可启动（server + dispatcher 拉起，含 worker env 校验）
    try:
        from astra_runner_engine import LocalAstraEngine

        engine = LocalAstraEngine()
        engine.start()
        LOG.info("[PASS] ASTRA 引擎启动成功（server + dispatcher）")
    except Exception as exc:  # noqa: BLE001
        LOG.error("[FAIL] ASTRA 引擎启动失败 error=%s", exc)
        ok = False
    finally:
        try:
            from astra_runner_engine import shutdown_daemon

            shutdown_daemon()
        except Exception:  # noqa: BLE001
            pass

    LOG.info("=== 自检%s ===", "通过，可以开始跑分" if ok else "未通过，请修复后重试")
    return 0 if ok else 1


def collect_dsh_usage() -> dict[str, int]:
    """汇总 $ASTRA_DSH_HOME/usage/astra-usage.jsonl 的 token 用量（dsh runner 逐任务写入）。

    字段与 DSH TokenUsage 对齐（input/output/cacheRead/cacheWrite/reasoning，单位 token）；
    billed input = input + cacheRead + cacheWrite（三者互斥）。无记录时返回空 dict。
    """
    import os
    import tempfile

    dsh_home = os.environ.get("ASTRA_DSH_HOME") or str(Path(tempfile.gettempdir()) / "astra-dsh" / "deepseek-main")
    usage_file = Path(dsh_home) / "usage" / "astra-usage.jsonl"
    if not usage_file.is_file():
        return {}
    total = {
        "inputTokens": 0,
        "outputTokens": 0,
        "cacheReadTokens": 0,
        "cacheWriteTokens": 0,
        "reasoningTokens": 0,
    }
    for line in usage_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        for key in total:
            value = record.get(key)
            if isinstance(value, (int, float)):
                total[key] += int(value)
    return total


if __name__ == "__main__":
    sys.exit(main())
