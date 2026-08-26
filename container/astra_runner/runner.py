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
import tempfile
import threading
import time
from collections import deque
from datetime import datetime, timezone
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
# V2-1③：单题 hint 成本上限（每次扣该题 10%，2 次=20%）
MAX_HINTS_PER_CHALLENGE = 2
# V2-2：尾段窗口（剩余<2h）优先重攻近失题
NEAR_MISS_LATE_WINDOW_SECONDS = 2 * 3600
# V2-7：饥饿回灌门槛——队列空且剩余>15min 时无视 defer 上限重拉已弃题
STARVATION_MIN_REMAINING_SECONDS = 15 * 60
# V2-7：期望预算下限（KB 复解题 15 分钟地板）
EXPECTED_BUDGET_FLOOR_SECONDS = 15 * 60
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
    wrong_count: int = 0  # V2-2：错交次数（近失题——回队插队首+尾段优先重攻）
    hint_texts: list[str] = field(default_factory=list)  # V2-1④：已购 hint 文本（defer 续跑复用，禁止重购）
    kb_seconds: float | None = None  # V2-5/V2-7：知识库历史首解耗时（期望预算依据）
    kb_entry_text: str | None = None  # V2-6：知识库思路条目（开局注入参考 fact）
    kb_approach_draft: str | None = None  # V2-6：末段星记浓缩（解出后沉淀知识库用）
    kb_neighbor_texts: list[str] = field(default_factory=list)  # V4：同题型邻居经验（举一反三注入）
    transient_count: int = 0  # 自愈①：连续网络瞬断计数（超上限判死退出，防误匹配死循环）
    busy_count: int = 0  # 自愈④：槽位 busy 连击计数（指数退避用；漏定义曾致 SlotBusy 线程炸死）
    kb_deadend_texts: list[str] = field(default_factory=list)  # V5：同题型避坑提示（失败经验库注入）


class TaskFinishedError(Exception):
    """跑分任务时限已到（平台 409 already finished），停止整轮。"""


class SlotBusyError(Exception):
    """平台活跃名额已满（start 409），稍后重试。"""


class TransientNetError(Exception):
    """网络瞬断（平台/LLM 不可达）——题不死，_work 等待后原地重进。"""


# 自愈①：网络瞬断特征——只认明确的传输层故障词（request to/getaddrinfo/连接类）。
# 刻意不含裸 "timed out/timeout"：引擎本地超时与测试超时会误匹配成断网死循环
# （实测挂死整套回归）；真断网时平台报错必含 "request to <url>" 或 unreachable。
_NET_SIGNATURES = (
    "request to", "unreachable", "getaddrinfo", "connect error", "connectionerror",
    "max retries", "temporary failure", "connection reset",
    "connection refused", "eof occurred", "remoteendclosed", "broken pipe",
)


def _is_transient_network_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(sig in text for sig in _NET_SIGNATURES)


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
    def create_fact(self, project_id: str, description: str) -> None: ...
    def wait_project(self, project_id: str, timeout_seconds: float) -> bool: ...
    def list_fact_descriptions(self, project_id: str) -> list[str]: ...
    def stop_project(self, project_id: str) -> None: ...
    def reactivate_project(self, project_id: str) -> None: ...
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


def collect_flags_from_facts(descriptions: list[str], exclude_texts: list[str] | None = None) -> list[str]:
    """从星记描述收集 flag；exclude_texts（题面/goal 原文）中出现的 flag 串一律剔除。

    a-05 实例：题面示例 flag 格式串被 bootstrap 抄进星记 → 被当发现提交 → 错交两次，
    且 flags_found 非空触发"部分解出正常收尾"把整题白白关掉。题面里出现过的 flag
    不可能是答案（答案只存在于靶机实测输出）。
    """
    banned: set[str] = set()
    for text in exclude_texts or []:
        banned.update(extract_flags(text))
    flags: list[str] = []
    for description in descriptions:
        flags.extend(f for f in extract_flags(description) if f not in banned)
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
    hint2_after_seconds: float = 1500.0,
    defer_after_seconds: float = 2700.0,
    hint_min_score: int = 0,
    prefer_easy: bool = True,
    order_codes: list[str] | None = None,
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

    # V2-6/V2-5/V4：知识库挂载（期望预算 + 思路条目 + 同题型邻居），在排序前统一附加到 result
    knowledge = _load_runtime_knowledge()
    if knowledge:
        attached = _attach_knowledge(queue, knowledge)
        LOG.info("knowledge base loaded entries=%s attached=%s file=%s", len(knowledge), attached, KNOWLEDGE_FILE)

    def _code_of(item) -> str:
        return str(getattr(item[0], "unique_code", None) or getattr(item[0], "code", "") or "").lower()

    # V2-5：显式做题顺序——列表内的题按给定顺序置顶，未列的按 prefer_easy 规则续队。
    # 失配码（题集变化/题码漂移）告警并忽略，绝不阻断。
    if order_codes:
        rank = {c.strip().lower(): i for i, c in enumerate(order_codes) if c.strip()}
        head = [item for item in queue if _code_of(item) in rank]
        rest_items = [item for item in queue if _code_of(item) not in rank]
        head.sort(key=lambda item: rank[_code_of(item)])
        if prefer_easy and rest_items:
            # 未列题目仍按 easy-first 续队（V2-5 规格：显式序只管置顶，不动续队规则）
            _dr = {"easy": 0, "medium": 1, "hard": 2}
            rest_items.sort(
                key=lambda item: _dr.get(str(getattr(item[0], "difficulty", "") or "").lower(), 1.5)
            )
        matched = { _code_of(item) for item in head }
        unmatched = [c for c in rank if c not in matched]
        if unmatched:
            LOG.warning("order_codes unmatched/ignored codes=%s（题集可能已变化）", unmatched)
        LOG.info("queue ordered by explicit list matched=%s rest=%s", len(head), len(rest_items))
        queue = deque(head + rest_items)

    if prefer_easy and queue and not order_codes:
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
            except TransientNetError as exc:
                # 自愈①：网络瞬断——题不死，退避等待后原地重进（断网 50 分钟类
                # 事故只损失等待时间，不再丢题丢线程）。连续上限 15 次（含误匹配
                # 兜底：普通超时类异常不会无限循环，累计约 30 分钟后判死退出）
                result.transient_count += 1
                if result.transient_count > 15:
                    LOG.error("network transient 超上限 code=%s（误匹配或长故障）——按普通失败处理",
                              result.unique_code)
                    result.error = str(exc)
                    return
                wait = min(120.0, 30.0 * max(1, result.defer_count))
                LOG.warning(
                    "network transient code=%s %s/15 error=%.80s（%.0fs 后重进）",
                    result.unique_code, result.transient_count, str(exc), wait,
                )
                time.sleep(wait)
                continue
            except TaskFinishedError as exc:
                stop_errors.append(exc)
                stop_event.set()
                return
            except SlotBusyError:
                # 自愈④：busy 指数退避——首次插队首快速重试，反复撞满则指数等待+
                # 回队尾，杜绝"十几个线程每 30 秒集体空转刷 409"的自旋风暴
                busy_seen.set()
                result.busy_count += 1
                wait = min(30 * (2 ** min(result.busy_count - 1, 3)), 240)
                LOG.warning("active slot busy code=%s 第 %s 次（%.0fs 后%s）",
                            result.unique_code, result.busy_count, wait,
                            "插队首" if result.busy_count == 1 else "回队尾")
                time.sleep(wait)
                if result.busy_count == 1:
                    queue.appendleft((ch, result))
                else:
                    queue.append((ch, result))
                return
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
                    # V5 失败经验库：删项目前抢救末段星记——死路与打法同样是资产
                    if result.project_id and not result.kb_approach_draft:
                        try:
                            list_fn = getattr(engine_factory(), "list_fact_descriptions", None)
                            if callable(list_fn):
                                _dd = list_fn(result.project_id)
                                if _dd:
                                    result.kb_approach_draft = "；".join(_dd[-3:])[:800]
                        except Exception:  # noqa: BLE001
                            pass
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
                # 僵尸项目修复（R5 实测）：defer 即停项目——平台容器已关，继续调度
                # 只会攻击死靶机并占用 max_running_projects 预算饿死新题；
                # 服务端 stop 清 worker 租约+reason，scheduler 停止派发并取消在途任务，
                # 星图数据保留，回队复用时 reactivate 恢复。
                try:
                    stop_fn = getattr(engine_factory(), "stop_project", None)
                    if callable(stop_fn) and result.project_id:
                        stop_fn(result.project_id)
                except Exception:  # noqa: BLE001
                    pass
                # V2-2：近失题（错交过 flag=差一步）回队插队首，普通题仍走队尾
                if result.wrong_count > 0:
                    LOG.info("near-miss requeue to head code=%s wrong=%s", result.unique_code, result.wrong_count)
                    queue.appendleft((ch, result))
                else:
                    queue.append((ch, result))
                return
            # 正常完成（解出/超时关闭）：线程收尾退出——while True 仅为 TransientNetError
            # 原地重进服务，正常路径必须显式 return（曾缺失导致正常题无限重进，回归挂死）
            return

    # V2-7：预算放不下的题停泊区（时间只减不增，停泊即终局；饥饿回灌走 results 池）
    parked: list = []

    def _remaining_seconds() -> float:
        if window_deadline is None:
            return float("inf")
        return window_deadline - time.monotonic()

    challenges_by_code = {
        str(getattr(c, "unique_code", None) or getattr(c, "code", "")): c for c in challenges
    }

    def _pick_candidate():
        """V2-2：尾段（剩余<2h）优先选近失题（错交过=差一步），否则队首。"""
        if not queue:
            return None
        items = list(queue)
        idx = 0
        late_window = window_deadline is not None and _remaining_seconds() < NEAR_MISS_LATE_WINDOW_SECONDS
        if late_window:
            for i, item in enumerate(items):
                if item[1].wrong_count > 0:
                    idx = i
                    break
        picked = items.pop(idx)
        queue.clear()
        queue.extend(items)
        return picked

    def _starvation_refill() -> bool:
        """V2-7：队列空且剩余>门槛 → 无视 defer 上限按 EV 重拉已弃题（满窗口利用）。

        EV 序：近失（wrong>0）> 有星图/hint 积累 > 分值高；且只拉期望预算放得下的题。
        """
        remaining = _remaining_seconds()
        if window_deadline is None:
            return False  # 无窗口模式维持 defer 上限语义，防无限重拉
        if remaining <= STARVATION_MIN_REMAINING_SECONDS:
            return False

        def _fits(r: ChallengeResult) -> bool:
            ch_x = challenges_by_code.get(r.unique_code)
            diff = str(getattr(ch_x, "difficulty", "") or "").lower()
            return _expected_budget_seconds(r, diff, challenge_timeout_seconds) <= remaining

        candidates = [
            r
            for code, r in results.items()
            if r.started and r.flags_correct == 0 and code not in active and _fits(r)
        ]
        candidates.sort(
            key=lambda r: (
                0 if r.wrong_count > 0 else 1,
                0 if (r.hints_count or 0) > 0 or (r.facts_count or 0) >= 3 else 1,
                -int(getattr(challenges_by_code.get(r.unique_code), "total_score", 0) or 0),
            )
        )
        if not candidates:
            return False
        target = candidates[0]
        target.defer_count = 0  # 饥饿回灌无视 defer 上限
        queue.append((challenges_by_code[target.unique_code], target))
        LOG.warning(
            "starvation requeue code=%s wrong=%s facts=%s hints=%s（队列空，重拉已弃题续用窗口）",
            target.unique_code, target.wrong_count, target.facts_count, target.hints_count,
        )
        return True

    def _fill() -> None:
        nonlocal slots
        while queue and len(active) < slots and not stop_event.is_set():
            ch, result = _pick_candidate()
            difficulty = str(getattr(ch, "difficulty", "") or "").lower()
            budget = _expected_budget_seconds(result, difficulty, challenge_timeout_seconds)
            remaining = _remaining_seconds()
            if remaining < budget:
                # V2-7：该题期望预算放不下 → 停泊，继续尝试队列里更小的题
                LOG.info(
                    "park challenge code=%s budget=%.0fs remaining=%.0fs（放不下，试下一题）",
                    result.unique_code, budget, remaining,
                )
                parked.append((ch, result))
                continue
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

    # 对账扫描（R5 修复清单 1b，兜底所有泄漏路径的孤儿项目）：
    # 引擎侧 active 但不在当前运行窗口、且创建超过宽限期（60s）的项目 → stopped。
    reconcile_interval = float(os.environ.get("ASTRA_RECONCILE_INTERVAL", "300"))
    reconcile_grace = float(os.environ.get("ASTRA_RECONCILE_GRACE", "60"))
    last_reconcile = 0.0

    def _reconcile_orphans() -> None:
        nonlocal last_reconcile
        if reconcile_interval <= 0:
            return
        now_mono = time.monotonic()
        if now_mono - last_reconcile < reconcile_interval:
            return
        last_reconcile = now_mono
        try:
            engine = engine_factory()
            list_fn = getattr(engine, "list_active_projects", None)
            if not callable(list_fn):
                return
            window_ids = {
                results[code].project_id
                for code in active
                if code in results and results[code].project_id
            }
            now_wall = datetime.now(timezone.utc).timestamp()
            for proj in list_fn():
                pid = proj.get("id")
                if not pid or pid in window_ids:
                    continue
                try:
                    created_ts = datetime.strptime(
                        proj.get("created_at", ""), "%Y-%m-%dT%H:%M:%SZ"
                    ).replace(tzinfo=timezone.utc).timestamp()
                except ValueError:
                    created_ts = 0.0
                if now_wall - created_ts < reconcile_grace:
                    continue
                LOG.warning(
                    "reconcile: orphan active project stopped project=%s", pid,
                )
                stop_fn = getattr(engine, "stop_project", None)
                if callable(stop_fn):
                    stop_fn(pid)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("reconcile scan failed error=%s", exc)

    while not stop_event.is_set():
        finished = [code for code, t in list(active.items()) if not t.is_alive()]
        for code in finished:
            thread = active.pop(code)
            thread.join(timeout=5)
            LOG.info("并行窗口完成题目 code=%s active=%s/%s", code, len(active), slots)
            r = results.get(code)
            # V2-6：解出后自动沉淀思路（双层脱敏 → 运行时知识库，赛后人工合并回仓库文件）
            if r is not None and r.flags_correct > 0 and getattr(r, "kb_approach_draft", None):
                try:
                    _append_knowledge_entry(r, [r.kb_approach_draft])
                except Exception:  # noqa: BLE001
                    pass
            # V3：经验复利统计——注入过历史思路的题记命中/未命中
            if r is not None:
                try:
                    _record_memory_stats(r)
                except Exception:  # noqa: BLE001
                    pass
            # V5：失败经验库——未解出的题把走过的死路沉淀成负记忆
            if r is not None and r.flags_correct == 0 and getattr(r, "kb_approach_draft", None):
                try:
                    _append_deadend_entry(r)
                except Exception:  # noqa: BLE001
                    pass
            # V4：赛中实时记忆复用——本轮解出的题立即成为未开题者的参考（越打越强）
            try:
                fresh_kb = _load_runtime_knowledge()
                fresh_attached = _attach_knowledge(queue, fresh_kb)
                if fresh_attached:
                    LOG.info("live memory reload：新增 %s 条思路挂到未开题队列", fresh_attached)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("live memory reload failed error=%s（继续）", exc)
        _fill()
        _reconcile_orphans()
        # 自愈②：close_failed 清道夫——每 10 分钟补试泄漏容器的关闭，成功转 done
        #（断网期 close 失败的题不再永久占坑）
        if progress is not None and time.monotonic() - _close_reap_ts[0] > 600:
            _close_reap_ts[0] = time.monotonic()
            _reap_failed_closes(client, progress)
        # 自愈③：停摆看门狗——有活跃题但 worker 会话 12 分钟无写入 → 进程级自重启
        #（progress 断点续跑，engine 换血自愈；托管无人值守的生命线）
        if active and os.environ.get("ASTRA_SELF_HEAL", "1") != "0":
            if _watchdog_stalled() or _progress_pulse_stalled(engine_factory, active, results):
                _self_heal_restart()
        if active:
            time.sleep(2)
            continue
        # V2-7：无在跑题——队列非空则等下一轮；队列空做饥饿回灌；仍无事可做才收工
        if queue:
            time.sleep(2)
            continue
        if _starvation_refill():
            time.sleep(2)
            continue
        LOG.info(
            "无可开题（窗口剩余 %.0fs 且无期望预算可容之题），自然收尾", _remaining_seconds(),
        )
        break

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
    # V2-6 修复：finally 段（星记采集）引用这两个变量——start 阶段即抛 TaskFinishedError
    # 时它们尚未赋值，UnboundLocalError 会掩盖原异常导致 409 全停信号失效。前置初始化。
    project_id: str | None = None
    project_gone = False
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
        # defer 续跑：复用原引擎项目（星图/会话进度保留），否则新建。
        # reactivate 失败（终态/不存在）→ 放弃复用新建项目——丢星图远好于
        # 对着 completed 星图无限空转（resume 死锁，实测一次咬死三题一小时）
        reuse_ok = False
        if result.project_id:
            project_id = result.project_id
            LOG.info("challenge resumed code=%s reuse project=%s（defer 续跑）", code, project_id)
            # 僵尸项目修复配套：defer 时项目已置 stopped，复用前置回 active 恢复调度
            try:
                reactivate_fn = getattr(engine, "reactivate_project", None)
                if callable(reactivate_fn):
                    reuse_ok = bool(reactivate_fn(project_id))
                else:
                    reuse_ok = True
            except Exception:  # noqa: BLE001
                reuse_ok = False
            if not reuse_ok:
                LOG.warning("resume 复用失败 code=%s project=%s（终态/不存在）→ 新建项目重打", code, project_id)
                project_id = None
                result.project_id = None
        if project_id:
            # V2-1④：defer 续跑复用已购 hint（平台按次扣分，重购=白烧分）
            if result.hint_texts:
                for cached in result.hint_texts:
                    try:
                        engine.create_hint(project_id, f"[平台提示·续跑复用] {cached}")
                    except Exception:  # noqa: BLE001
                        pass
                LOG.info("cached hints re-injected code=%s count=%s（不重购）", code, len(result.hint_texts))
        else:
            project_id = engine.create_project(
                title=f"{DEFAULT_PROJECT_TITLE_PREFIX}-{code}",
                origin=origin,
                goal=goal,
            )
            # V2-6：新题注入知识库思路参考（仅新项目；defer 复用项目已注入过）
            if result.kb_entry_text:
                try:
                    create_fact_fn = getattr(engine, "create_fact", None)
                    if callable(create_fact_fn):
                        create_fact_fn(
                            project_id,
                            "[历史思路参考·知识库] 方向参考非答案——当前实例可能已变化，"
                            "所有步骤必须实测验证；行为与参考不符时立即放弃参考回到自主探索；"
                            "不得据此猜测/构造 flag 值。"
                            + _memory_reinforcement_text(code)
                            + "历史攻击链：" + result.kb_entry_text,
                        )
                        LOG.info("knowledge base entry injected code=%s", code)
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("kb inject failed code=%s error=%s（继续）", code, exc)
            # V4：同题型邻居经验注入（举一反三）——无精确条目的题也能吃到同类打法参考
            if result.kb_neighbor_texts:
                try:
                    create_fact_fn = getattr(engine, "create_fact", None)
                    if callable(create_fact_fn):
                        create_fact_fn(
                            project_id,
                            "[同题型经验·举一反三] 以下为知识库中同题型（按实战战绩加权）历史打法，"
                            "仅作方向启发：当前题目与它们不同，禁止照搬步骤，"
                            "每一步仍须针对当前实例验证。"
                            + "\n".join(result.kb_neighbor_texts),
                        )
                        LOG.info("neighbor experience injected code=%s n=%s", code, len(result.kb_neighbor_texts))
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("neighbor inject failed code=%s error=%s（继续）", code, exc)
            # V5：失败经验库注入——同题型死路避坑提示（负记忆）
            if result.kb_deadend_texts:
                try:
                    create_fact_fn = getattr(engine, "create_fact", None)
                    if callable(create_fact_fn):
                        create_fact_fn(
                            project_id,
                            "[同题型避坑提示·失败经验库] 以下为同题型历史死路（含本轮实时沉淀），"
                            "开局即知前车之鉴、避免重蹈："
                            + "\n".join(result.kb_deadend_texts),
                        )
                        LOG.info("deadend warnings injected code=%s n=%s", code, len(result.kb_deadend_texts))
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("deadend inject failed code=%s error=%s（继续）", code, exc)
            # V7 Constellation：同网段侦察共享卡注入（多目标场景免重复扫描）
            if project_id and origin:
                try:
                    shared = _constellation_text(origin)
                    if shared:
                        create_fact_fn = getattr(engine, "create_fact", None)
                        if callable(create_fact_fn):
                            create_fact_fn(project_id, shared)
                            LOG.info("constellation recon card injected code=%s", code)
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("constellation inject failed code=%s error=%s（继续）", code, exc)
            result.project_id = project_id
        LOG.info("challenge started code=%s project=%s addr=%s", code, project_id, origin)

        # 等待引擎归航的同时，周期扫描星图 flag 并立即提交（发现即提交，不等归航）
        done = False
        project_gone = False  # 提前收尾删了引擎项目 → 跳过后续取 flag（避免 404）
        # 单题最长连续分析：defer_after_seconds（默认 45 分钟）无结果 → 保留进度放回队尾
        # defer 梯子保持 V2-5 原精调逻辑（首攻缩短/第二发恢复完整梯子）——
        # TDI 只驱动期望预算与 hint 时机，不碰 defer（实测会破坏首攻梯子的时序契约）
        effective_timeout = defer_after_seconds if defer_after_seconds > 0 else timeout_seconds
        # V2-5 首攻限时（KB 题）：有历史首解耗时的题，首攻 2×（≥15min 地板）仍无果
        # → 大概率实例已变化，提前 defer 回队（最坏损失 45min→15-20min）；
        # 第二发起恢复完整 defer 梯子（KB 已证伪，按未知题对待）。
        if (
            result.defer_count == 0
            and result.kb_seconds is not None
            and defer_after_seconds > 0
        ):
            effective_timeout = min(
                defer_after_seconds,
                max(2 * result.kb_seconds, EXPECTED_BUDGET_FLOOR_SECONDS),
            )
            # 短首攻配套：hint 阶梯按有效窗口比例缩放（40%/70%），否则 15min 窗口里
            # hint1(15min) 永远赶不上 defer。正常 45min 窗口下 min() 不改变默认值。
            # TDI：困境信号强的题更早买 hint（省无谓消耗），无信号不变
            tdi_div = 1.0 + _task_difficulty_signal(result)
            hint_after_seconds = min(hint_after_seconds / tdi_div, effective_timeout * 0.4)
            hint2_after_seconds = min(hint2_after_seconds / tdi_div, effective_timeout * 0.7)
        deadline = time.monotonic() + effective_timeout
        scan_round = 0
        # 卡题分级 hint：hint_after_seconds（默认 15 分钟）取第一次，
        # hint2_after_seconds（默认 30 分钟）取第二次（每题最多两次）。
        # hint 会按比例扣该题得分，hint_min_score 可设门槛（默认 0=不限制）。
        challenge_score = int(getattr(ch, "total_score", 0) or 0)
        hint_trigger_at = started_at + hint_after_seconds
        hint2_trigger_at = started_at + hint2_after_seconds if hint2_after_seconds > 0 else float("inf")
        hint_eligible = auto_hint and challenge_score >= hint_min_score
        # V2-1④：hint 次数缓存感知——defer 前已购的 hint 不重购
        hint_taken = min(len(result.hint_texts), MAX_HINTS_PER_CHALLENGE)
        facts_at_hint1: int | None = None  # V2-1①：hint1 时的星记数（hint2 前对比是否产生新攻击面）
        while time.monotonic() < deadline:
            fact_count: int | None = None
            try:
                fact_descs = engine.list_fact_descriptions(project_id)
                fact_count = len(fact_descs)
                flags = collect_flags_from_facts(fact_descs, [result.description])
                pending = [flag for flag in flags if flag not in result.flags_found]
                for flag in pending:
                    _submit_flag_safely(client, code, flag, result, started_at)
            except Exception:  # noqa: BLE001 —— 引擎 API 偶发失败不中断等待
                pass
            if hint_eligible and not result.flags_found:
                now = time.monotonic()
                if hint_taken < 1 and now >= hint_trigger_at:
                    if _try_platform_hint(client, engine, code, project_id, result):
                        hint_taken = 1
                        facts_at_hint1 = fact_count
                elif hint_taken < 2 and now >= hint2_trigger_at:
                    # V2-1①：hint1 注入后星图零新增（无新攻击面）→ hint2 大概率无效，跳过止损。
                    # 注意：consolidate 压缩会让 fact 数变小（那是进展不是停滞）——只跳过精确相等
                    if facts_at_hint1 is not None and fact_count is not None and fact_count == facts_at_hint1:
                        hint_taken = 2
                        LOG.info(
                            "hint2 skipped code=%s（hint1 后星图无新增 fact，止损不购）", code,
                        )
                    else:
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
                            last_descs = engine.list_fact_descriptions(project_id)
                            last_flags = collect_flags_from_facts(last_descs, [result.description])
                            for flag in [f for f in last_flags if f not in result.flags_found]:
                                _submit_flag_safely(client, code, flag, result, started_at)
                            if last_descs:
                                # V2-6：删项目前留末段星记（解出后沉淀知识库）
                                result.kb_approach_draft = "；".join(last_descs[-3:])[:800]
                                # V7 Constellation：网络级侦察结论跨项目共享
                                try:
                                    _record_constellation(origin, _extract_recon_facts(last_descs))
                                except Exception:  # noqa: BLE001
                                    pass
                        except Exception:  # noqa: BLE001
                            pass
                        # R5 修复（关题竞态 404 级联）：先 stop 让 scheduler 取消在途
                        # 任务并停止派发，等一个调度周期再删，避免在途 reason 写结果撞 404
                        stop_fn = getattr(engine, "stop_project", None)
                        if callable(stop_fn):
                            try:
                                stop_fn(project_id)
                                time.sleep(4)  # 一个调度周期让取消生效
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
            flags = collect_flags_from_facts(engine.list_fact_descriptions(project_id), [goal])
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
            flags = collect_flags_from_facts(engine.list_fact_descriptions(project_id), [goal])
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
        if _is_transient_network_error(exc):
            # 自愈①：网络瞬断不杀题——_work 循环等待后原地重进（start 幂等、
            # project_id 复用星图），断网 50 分钟类事故不再丢题
            raise TransientNetError(str(exc)) from exc
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
        # V2-6：末段星记浓缩（项目仍在时兜底采集；完成路径删项目前已采）
        if project_id and not result.kb_approach_draft and not project_gone:
            try:
                _descs = engine.list_fact_descriptions(project_id)
                if _descs:
                    result.kb_approach_draft = "；".join(_descs[-3:])[:800]
                    try:
                        _record_constellation(origin, _extract_recon_facts(_descs))
                    except Exception:  # noqa: BLE001
                        pass
            except Exception:  # noqa: BLE001
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


# ---------------- V2-5/V2-6/V2-7：知识库 + 期望预算 ----------------

# 赛中沉淀文件（模块常量：测试 monkeypatch 指向临时目录，避免污染真实 /tmp 沉淀）
KNOWLEDGE_APPEND_FILE = Path(tempfile.gettempdir()) / "astra-knowledge-append.json"
DEADENDS_APPEND_FILE = Path(tempfile.gettempdir()) / "astra-deadends-append.json"

KNOWLEDGE_FILE = (
    Path(os.environ.get("ASTRA_KNOWLEDGE_FILE"))
    if os.environ.get("ASTRA_KNOWLEDGE_FILE")
    else Path(__file__).resolve().parent.parent / "knowledge" / "challenge-approaches.md"
)
_KB_ENTRY_RE = re.compile(r"^## (.+?)（([a-z0-9-]+)）\s*$", re.MULTILINE)
_KB_META_RE = re.compile(r"首解耗时：(\d+)min")
_KB_HINT_RE = re.compile(r"^- 思路\d+：(.+)$", re.MULTILINE)
# V2-6 双层脱敏：① flag 值；② secret 语境附近的 ≥12 位随机串（flag 组件同罪）
_FLAG_VALUE_RE = re.compile(r"(flag|FLAG)\{[^}\s]{3,}\}")
_SECRET_CTX_RE = re.compile(
    r"(?i)(secret|flag|key|token|password|泄漏|密码|密钥)[^\n]{0,100}?([0-9a-fA-F]{12,})"
)


def _sanitize_kb_text(text: str) -> str:
    text = _FLAG_VALUE_RE.sub(lambda m: f"{m.group(1)}{{...已脱敏...}}", text)
    text = _SECRET_CTX_RE.sub(
        lambda m: m.group(0).replace(m.group(2), "[REDACTED]"), text
    )
    return text


def _kb_disabled() -> bool:
    """托管合规开关：ASTRA_KB_DISABLED=1 时屏蔽一切预置知识（历史题解/死路/星座卡）。

    平台规则"Agent 不可内置针对题目的历史答题记忆或解题方法"——托管跑分时置 1，
    记忆机制完整保留（赛中实时沉淀/热加载/战绩统计照常运行），只是不带预置答案入场。
    """
    return os.environ.get("ASTRA_KB_DISABLED", "") == "1"


def _load_knowledge_base() -> dict[str, dict]:
    """解析已解题思路知识库：{code: {name, seconds, approach}}。文件缺失/损坏返回空。"""
    if _kb_disabled():
        return {}
    try:
        raw = KNOWLEDGE_FILE.read_text(encoding="utf-8")
    except OSError:
        return {}
    kb: dict[str, dict] = {}
    for i, m in enumerate(_KB_ENTRY_RE.finditer(raw)):
        name, code = m.group(1), m.group(2)
        chunk_end = len(raw)
        for m2 in list(_KB_ENTRY_RE.finditer(raw))[i + 1:]:
            chunk_end = m2.start()
            break
        chunk = raw[m.end():chunk_end]
        meta = _KB_META_RE.search(chunk)
        approaches = _KB_HINT_RE.findall(chunk)
        kb[code.lower()] = {
            "name": name,
            "seconds": float(meta.group(1)) * 60 if meta else None,
            "approach": _sanitize_kb_text("；".join(a.strip() for a in approaches))[:800],
        }
    return kb


def _task_difficulty_signal(result: ChallengeResult) -> float:
    """V7 TDI（任务难度指数·增量信号）：来自实测困境的 0~0.4 增量，驱动三机制自适应。

    借鉴 PentestGPT v2 的 TDA 四维思想，用我们已有的实测数据合成：
    错交次数 / defer 次数 / 知识库同题未命中率。无任何信号时返回 0——
    期望预算、defer 窗口、hint 时机三处的默认行为与现状完全一致。
    """
    signal = 0.0
    signal += min(result.wrong_count * 0.1, 0.15)   # 近失（错交）→ 略难
    signal += min(result.defer_count * 0.1, 0.15)   # 反复 defer → 攻坚题
    stats = _load_memory_stats().get(result.unique_code)
    if stats:
        hits, misses = int(stats.get("hits", 0)), int(stats.get("misses", 0))
        if hits + misses >= 2 and misses > hits:
            signal += 0.1  # 历史注入未命中居多 → 该参考不灵，按更难处理
    return round(min(signal, 0.4), 3)


def _expected_budget_seconds(
    result: ChallengeResult,
    difficulty: str,
    challenge_timeout_seconds: float,
) -> float:
    """V2-7：单题期望预算（秒）——开题的剩余时间判据。

    有 KB 历史首解耗时 → max(2×耗时, 15min)；近失题 → 20min；无参考 → 按难度超时。
    """
    adaptive = 1.0 + _task_difficulty_signal(result)  # TDI：困境信号越多预算越宽
    if result.kb_seconds is not None:  # 0min 首解也走地板预算（bctf-40 实例），falsy 判空会错放到 45min
        return max(2 * result.kb_seconds, EXPECTED_BUDGET_FLOOR_SECONDS) * adaptive + DONE_FLAG_WAIT_SECONDS + 30
    if result.wrong_count > 0:
        return 20 * 60 * adaptive + DONE_FLAG_WAIT_SECONDS + 30
    base = DIFFICULTY_TIMEOUTS.get(difficulty, challenge_timeout_seconds)
    return base * adaptive + DONE_FLAG_WAIT_SECONDS + 30


def _parse_order_codes(cli_value: str | None, env_value: str | None) -> list[str] | None:
    """V2-5：合并 CLI 与环境变量的显式顺序参数；都为空返回 None。

    独立成函数是测试需要——曾出过 CLI 值按字符迭代的解析 bug（or 短路绕过了 split）。
    """
    raw = cli_value or env_value
    if not raw:
        return None
    return [c for c in (x.strip() for x in raw.split(",")) if c]


def _append_knowledge_entry(result: ChallengeResult, fact_descriptions: list[str]) -> None:
    """V2-6：解出后自动沉淀思路到运行时知识库（progress 同目录，赛后人工合并回仓库文件）。

    攻击链取该题末段星记（含 completion fact，是攻击链的浓缩叙述）；双层脱敏后写入。
    """
    try:
        # 托管镜像 /opt/knowledge 为 root 只读——沉淀文件写到临时目录（赛后人工取回合并）
        import tempfile as _tempfile

        out = KNOWLEDGE_APPEND_FILE
        entry = {
            result.unique_code: {
                "name": result.unique_code,
                "first_flag_seconds": result.first_flag_seconds,
                "elapsed_seconds": round(result.elapsed_seconds),
                "awarded": result.awarded,
                "approach": _sanitize_kb_text("；".join(fact_descriptions[-3:]))[:800],
            }
        }
        existing: dict = {}
        try:
            existing = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        existing.update(entry)
        out.write_text(json.dumps(existing, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass  # 沉淀失败不影响跑分


# ---------------- V3 经验复利：知识条目命中统计（回忆强化 + 遗忘权重的数据源） ----------------

MEMORY_STATS_FILE = (
    Path(os.environ.get("ASTRA_MEMORY_STATS_FILE"))
    if os.environ.get("ASTRA_MEMORY_STATS_FILE")
    else KNOWLEDGE_FILE.parent / "memory-stats.json"
)


def _load_memory_stats() -> dict:
    try:
        return json.loads(MEMORY_STATS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _record_memory_stats(result: ChallengeResult) -> None:
    """V3：单题收尾时记录知识条目命中情况——注入了历史思路且解出=命中，注入未解出=未命中。

    stats 结构：{code: {name, hits, misses, last_used}}。注入时附上战绩（回忆强化），
    高频有效打法自动浮上来，长期无效条目自然沉底（艾宾浩斯式遗忘）。
    """
    if not getattr(result, "kb_entry_text", None):
        return
    try:
        stats = _load_memory_stats()
        entry = stats.setdefault(
            result.unique_code,
            {"name": result.unique_code, "hits": 0, "misses": 0, "last_used": ""},
        )
        solved = result.flags_correct > 0
        entry["hits" if solved else "misses"] += 1
        entry["last_used"] = datetime.now().isoformat(timespec="seconds")
        MEMORY_STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
        MEMORY_STATS_FILE.write_text(
            json.dumps(stats, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except (OSError, ValueError):
        pass  # 统计失败不影响跑分


def _memory_reinforcement_text(code: str) -> str:
    """V3：注入历史思路时附上该条目的实战战绩（命中 n/m）——让 agent 知道参考可信度。"""
    entry = _load_memory_stats().get(code)
    if not entry or not (entry.get("hits") or entry.get("misses")):
        return ""
    hits, misses = int(entry.get("hits", 0)), int(entry.get("misses", 0))
    return f"（该思路历史战绩：{hits} 次命中/{misses} 次未命中）"


# ---------------- V4 举一反三：题型分类 + 同题型邻居经验注入 + 赛中实时复用 ----------------

# 关键词→题型映射（顺序即优先级：先匹配更specific的类别）
_CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("cloud", ("云", "oss", "cos", "s3", "桶", "bucket", "iam", "ak/sk", "元数据", "k8s", "容器逃逸", "docker", "redis", "kafka")),
    ("mobile", ("apk", "android", "dex", "ios", "ipa", "app.asar", "electron", "逆向app", "安卓")),
    ("crypto", ("密码", "加密", "cipher", "rsa", "aes", "椭圆", "ecc", "哈希", "hash", "随机数", "prng", "签名")),
    ("blockchain", ("合约", "solidity", "eth", "区块链", "web3", "bet", "withdraw", "chainid")),
    ("pwn", ("溢出", "pwn", "堆", "栈", "rop", "shellcode", "格式化字符串", "uaf", "glibc", "seccomp")),
    ("reverse", ("反汇编", "逆向", "ida", "ghidra", "upx", "脱壳", "vm保护", "混淆还原")),
    ("web", ("sql", "注入", "xss", "ssrf", "rce", "上传", "webshell", "jwt", "反序列化", "xxe", "csrf", "ssti", "逻辑", "admin", "登录", "越权")),
]
_CATEGORY_NAMES = {code: name for code, name in [
    ("cloud", "云安全"), ("mobile", "移动安全"), ("crypto", "密码学"),
    ("blockchain", "区块链"), ("pwn", "二进制利用"), ("reverse", "逆向工程"), ("web", "Web安全"),
]}


def _categorize(*texts: str) -> str | None:
    """V4：按关键词给题/条目归类，无命中返回 None（misc 不参与邻居注入）。"""
    blob = " ".join(t for t in texts if t).lower()
    if not blob:
        return None
    for category, keywords in _CATEGORY_RULES:
        if any(kw in blob for kw in keywords):
            return category
    return None


def _pick_neighbor_entries(
    knowledge: dict[str, dict],
    code: str,
    description: str,
    limit: int = 2,
) -> list[str]:
    """V4：同题型邻居经验选择——优先级：同题型 + 命中战绩 > 同题型 + 高分值。

    只给"知识库无精确条目"的题补充邻居参考（举一反三），每条截 400 字防预算膨胀。
    """
    my_category = _categorize(description, code)
    if my_category is None:
        return []
    stats = _load_memory_stats()

    def _score(entry_code: str, entry: dict) -> tuple[int, int]:
        st = stats.get(entry_code, {})
        hits, misses = int(st.get("hits", 0)), int(st.get("misses", 0))
        return (hits - misses, int(entry.get("awarded") or 0))

    candidates = [
        (c, e) for c, e in knowledge.items()
        if c != code.lower() and _categorize(e.get("name", ""), e.get("approach") or "") == my_category
    ]
    candidates.sort(key=lambda kv: _score(kv[0], kv[1]), reverse=True)
    label = _CATEGORY_NAMES.get(my_category, my_category)
    out: list[str] = []
    for c, e in candidates[:limit]:
        approach = (e.get("approach") or "").strip()[:400]
        if not approach:
            continue
        reinforcement = _memory_reinforcement_text(c)
        out.append(f"[{label}·{e.get('name', c)}]{reinforcement} {approach}")
    return out


def _load_runtime_knowledge() -> dict[str, dict]:
    """V4：仓库知识库 + 本轮赛中沉淀（/tmp JSON）实时合并——"同一场比赛越打越强"的来源。"""
    knowledge = _load_knowledge_base()
    try:
        import tempfile as _tempfile

        pending_path = KNOWLEDGE_APPEND_FILE
        pending = json.loads(pending_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return knowledge
    for code, data in pending.items():
        if code.lower() in knowledge:
            continue  # 仓库条目优先（已人工审核），赛中沉淀只补缺
        knowledge[code.lower()] = {
            "name": data.get("name") or code,
            "seconds": data.get("first_flag_seconds") or data.get("elapsed_seconds"),
            "approach": data.get("approach"),
        }
    return knowledge


# ---------------- V7 Constellation 跨项目侦察共享层 ----------------
# CTFExplorer 多目标范式：真实渗透中同网段的侦察结论应当跨目标复用。
# 解出/收尾题时把"网络级事实"（主机/端口/服务指纹，绝不含 flag/凭据值）摘出存
# 共享卡；新题开局若 origin 与已探测网段同 /24，注入共享卡省去重复侦察。

_RECON_FACT_RE = re.compile(
    r"(?i)(?:\d{1,3}\.){3}\d{1,3}|端口|port\s*\d+|nginx|apache|iis|tomcat|mysql|redis|ssh|ftp|服务指纹|指纹"
)


def _extract_recon_facts(fact_descriptions: list[str]) -> list[str]:
    out = []
    for d in fact_descriptions:
        d = (d or "").strip()
        if _RECON_FACT_RE.search(d) and not re.search(r"(?i)flag\{|password|凭据|token|密钥", d):
            out.append(d[:160])
    return out[:6]


def _constellation_path() -> Path:
    return KNOWLEDGE_FILE.parent / "constellation.json"


def _load_constellation() -> dict:
    if _kb_disabled():
        return {}
    try:
        return json.loads(_constellation_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _record_constellation(origin: str, recon_facts: list[str]) -> None:
    """origin 形如 http://10.0.x.y:port ——按 /24 网段聚合侦察卡。"""
    if not recon_facts:
        return
    m = re.search(r"((?:\d{1,3}\.){3})\d{1,3}", origin or "")
    if not m:
        return
    subnet = m.group(1).rstrip(".")
    try:
        data = _load_constellation()
        card = data.setdefault(subnet, {"facts": [], "updated": ""})
        for f in recon_facts:
            if f not in card["facts"]:
                card["facts"].append(f)
        card["facts"] = card["facts"][-12:]  # 每网段最多 12 条，滚动窗口
        card["updated"] = datetime.now().isoformat(timespec="seconds")
        _constellation_path().parent.mkdir(parents=True, exist_ok=True)
        _constellation_path().write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except (OSError, ValueError):
        pass


def _constellation_text(origin: str) -> str:
    """新题开局的同网段共享卡文本；无同网段数据返回空串。"""
    m = re.search(r"((?:\d{1,3}\.){3})\d{1,3}", origin or "")
    if not m:
        return ""
    card = _load_constellation().get(m.group(1).rstrip("."))
    if not card or not card.get("facts"):
        return ""
    facts = "\n".join(f"- {f}" for f in card["facts"][-6:])
    return (
        "[同网段侦察共享·Constellation] 以下为同 /24 网段历史项目的网络级侦察结论"
        f"（更新于 {card.get('updated', '?')}，仅含主机/端口/服务指纹，不含凭据）："
        "可直接复用免重复扫描，但服务可能已重置，使用前实测确认。\n" + facts
    )


def _attach_knowledge(queue: list, knowledge: dict) -> int:
    """V4：把知识库条目/邻居经验挂到队列中未开题的 result 上（初始挂载与赛中热加载共用）。"""
    attached = 0
    deadends = _load_deadends()
    for ch_item, res in queue:
        if res.started:
            continue  # 已开题不回填——注入 fact 只在项目创建时做
        code = res.unique_code.lower()
        entry = knowledge.get(code)
        if entry:
            res.kb_seconds = entry["seconds"]
            if entry["approach"] and not res.kb_entry_text:
                res.kb_entry_text = entry["approach"]
                attached += 1
        if not res.kb_entry_text and not res.kb_neighbor_texts:
            res.kb_neighbor_texts = _pick_neighbor_entries(knowledge, code, res.description)
        if not res.kb_deadend_texts:
            res.kb_deadend_texts = _pick_deadend_warnings(deadends, code, res.description)
    return attached


# ---------------- V5 失败经验库：死路沉淀 + 同题型避坑注入 ----------------

DEADENDS_FILE = (
    Path(os.environ.get("ASTRA_DEADENDS_FILE"))
    if os.environ.get("ASTRA_DEADENDS_FILE")
    else KNOWLEDGE_FILE.parent / "dead-ends.md"
)


def _append_deadend_entry(result: ChallengeResult) -> None:
    """V5：未解出题收尾时沉淀死路（末段星记=走过的弯路浓缩），双层脱敏后写 /tmp JSON。

    别人只记住成功，我们把失败变成资产：死路记忆按题型注入后来的题（避坑提示）。
    """
    draft = getattr(result, "kb_approach_draft", None)
    if not draft:
        return
    try:
        import tempfile as _tempfile

        out = DEADENDS_APPEND_FILE
        reason = "defer-giveup" if result.defer_count > 0 else ("wrong-submits" if result.wrong_count > 0 else "unsolved")
        entry = {
            result.unique_code: {
                "name": result.unique_code,
                "elapsed_seconds": round(result.elapsed_seconds),
                "reason": reason,
                "deadend": _sanitize_kb_text(draft)[:400],
            }
        }
        existing: dict = {}
        try:
            existing = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        existing.update(entry)
        out.write_text(json.dumps(existing, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass  # 沉淀失败不影响跑分


def _load_deadends() -> dict[str, dict]:
    """解析死路库（仓库 dead-ends.md + 本轮 /tmp 沉淀实时合并）。格式与知识库一致。"""
    if _kb_disabled():
        return {}
    entries: dict[str, dict] = {}
    try:
        raw = DEADENDS_FILE.read_text(encoding="utf-8")
    except OSError:
        raw = ""
    for i, m in enumerate(_KB_ENTRY_RE.finditer(raw)):
        name, code = m.group(1), m.group(2)
        chunk_end = len(raw)
        for m2 in list(_KB_ENTRY_RE.finditer(raw))[i + 1:]:
            chunk_end = m2.start()
            break
        chunk = raw[m.end():chunk_end]
        approach = _KB_HINT_RE.search(chunk)
        entries[code.lower()] = {
            "name": name,
            "approach": _sanitize_kb_text(approach.group(1)) if approach else None,
        }
    # 本轮赛中死路热合并
    try:
        import tempfile as _tempfile

        pending = json.loads(
            DEADENDS_APPEND_FILE.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        pending = {}
    for code, data in pending.items():
        if code.lower() not in entries and data.get("deadend"):
            entries[code.lower()] = {"name": data.get("name") or code, "approach": data["deadend"]}
    return entries


def _pick_deadend_warnings(
    deadends: dict[str, dict],
    code: str,
    description: str,
    limit: int = 2,
) -> list[str]:
    """V5：同题型死路避坑提示（含赛中实时沉淀的死路），每条截 300 字。"""
    my_category = _categorize(description, code)
    if my_category is None:
        return []
    label = _CATEGORY_NAMES.get(my_category, my_category)
    out: list[str] = []
    for c, e in deadends.items():
        if c == code.lower():
            continue
        text = (e.get("approach") or "").strip()
        if not text or _categorize(e.get("name", ""), text) != my_category:
            continue
        out.append(f"[{label}·{e.get('name', c)} 前车之鉴] {text[:300]}")
        if len(out) >= limit:
            break
    return out


# ---------------- 自愈组件：close 清道夫 + 停摆看门狗（托管无人值守的生命线） ----------------

_close_reap_ts = [0.0]
_WATCHDOG_STALL_SECONDS = 720  # 12 分钟无 worker 写入判停摆
# 看门狗基线：进程启动后必须先见过至少一次新鲜会话才有资格判停摆——
# 否则新进程（测试/重启）会拿历史陈旧会话误判，触发 os.execv 把自己炸了（实测）
_watchdog_seen_fresh = [False]


def _reap_failed_closes(client: BenchmarkClient, progress: Any) -> None:
    """自愈②：补试历史 close_failed 题的容器关闭，成功转 done（释放平台名额记忆）。"""
    try:
        for code, state in list(progress._data.items()):
            if state != "close_failed":
                continue
            closed = _close_challenge_quiet(client, code)
            if closed:
                progress.mark(code, "done")
                LOG.info("self-heal: 补关泄漏容器成功 code=%s（转 done）", code)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("close reaper failed error=%s（下轮再试）", exc)


def _close_challenge_quiet(client: BenchmarkClient, code: str) -> bool:
    try:
        closed = client.close_challenge(code)
        return bool(getattr(closed, "closed", True))
    except Exception:  # noqa: BLE001
        return False


def _watchdog_stalled() -> bool:
    """自愈③a：全停检测——活跃题存在，但全部 dsh worker 会话文件超时无写入。

    会话目录按 worker 隔离（$TMP/astra-dsh/<worker>/sessions），任一文件新鲜即视为
    系统在工作；全部陈旧且持续超过阈值才触发（避免把"长命令执行中"误判为停摆）。
    """
    import glob as _glob
    import tempfile as _tempfile

    try:
        newest = 0.0
        for f in _glob.glob(str(Path(_tempfile.gettempdir()) / "astra-dsh" / "*" / "sessions" / "*" / "*" / "*.zstd")):
            newest = max(newest, os.path.getmtime(f))
        if newest <= 0:
            return False  # 找不到会话（异常布局）不误杀
        fresh = (time.time() - newest) <= _WATCHDOG_STALL_SECONDS
        if fresh:
            _watchdog_seen_fresh[0] = True
            return False
        return _watchdog_seen_fresh[0]  # 从未见过新鲜会话：不判停摆（防历史残留误杀）
    except OSError:
        return False


# 自愈③b：半死检测——worker 在说话但星图零产出（b-02 冻结一小时事故的形态）。
# 每 120s 对活跃题的星图 facts 总量拍照，连续 15 分钟（可调）零增长即判半死。
_pulse_ts = [0.0]
_pulse_facts = [-1]
_pulse_stall_since = [0.0]


def _progress_pulse_stalled(engine_factory: Any, active: dict, results: dict) -> bool:
    try:
        now = time.monotonic()
        if now - _pulse_ts[0] < 120:
            return False  # 采样间隔未到
        _pulse_ts[0] = now
        sample = -1
        engine = engine_factory()
        stats_fn = getattr(engine, "stats", None)
        if callable(stats_fn):
            sample = 0
            for code in list(active):
                r = results.get(code)
                if r is not None and r.project_id:
                    try:
                        sample += int(stats_fn(r.project_id).get("facts", 0) or 0)
                    except Exception:  # noqa: BLE001
                        pass
        if sample < 0:
            return False  # 引擎不可查询时不误杀
        if sample > _pulse_facts[0]:
            _pulse_facts[0] = sample
            _pulse_stall_since[0] = now
            return False
        # 零增长：累计停滞时长
        if not _pulse_stall_since[0]:
            _pulse_stall_since[0] = now
            return False
        stalled = now - _pulse_stall_since[0]
        if stalled > float(os.environ.get("ASTRA_STALL_SECONDS", "900")):
            LOG.error("watchdog: 活跃题存在但星图 %.0f 分钟零增长（facts=%s）——判定半死", stalled / 60, sample)
            _pulse_stall_since[0] = now  # 重置，配合重启预算防风暴
            return True
        return False
    except Exception:  # noqa: BLE001
        return False


def _self_heal_restart() -> None:
    """自愈③：进程级自重启（os.execv 原位替换）——progress 断点续跑，引擎换血。

    重启预算：4 小时窗口内最多 3 次（状态文件计数），防故障风暴；耗尽则只告警不重启。
    ASTRA_SELF_HEAL=0 可整体关闭。
    """
    budget = Path(os.environ.get("ASTRA_SELF_HEAL_BUDGET_FILE", "/tmp/astra-selfheal-count.json"))
    if sys.platform == "win32":
        import tempfile as _tempfile

        budget = Path(_tempfile.gettempdir()) / "astra-selfheal-count.json"
    now = time.time()
    try:
        data = json.loads(budget.read_text(encoding="utf-8"))
        data["ts"] = [t for t in data.get("ts", []) if now - t < 4 * 3600]
    except (OSError, json.JSONDecodeError):
        data = {"ts": []}
    if len(data["ts"]) >= 3:
        LOG.error("watchdog: 引擎停摆但自重启预算耗尽（4h 内 3 次）——人工介入！")
        return
    data["ts"].append(now)
    try:
        budget.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass
    LOG.critical("watchdog: worker 会话 %.0f 分钟无写入，判定引擎停摆——自重启（第 %s 次）",
                 _WATCHDOG_STALL_SECONDS / 60, len(data["ts"]))
    os.execv(sys.executable, [sys.executable, *sys.argv])


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
        result.hint_texts.append(hint_text)  # V2-1④：缓存已购 hint（defer 续跑复用，禁止重购）
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
    flag = (flag or "").strip()  # V2-3：提交前清洗空白
    if not flag:
        return

    def _submit_once(value: str):
        try:
            return call_with_retry(lambda: client.submit_flag(code, value), f"submit_flag:{code}", retries=2)
        except Exception as exc:  # noqa: BLE001 —— 按异常名识别 SDK 的 DuplicateSubmit（幂等）
            if type(exc).__name__ == "DuplicateSubmit":
                LOG.info("flag duplicate skip code=%s flag=%s", code, value)
                result.flags_found.append(value)
                return "dup"
            LOG.warning("submit_flag failed code=%s flag=%s error=%s", code, value, exc)
            return None

    def _record(value: str, res: Any) -> bool:
        """登记一次提交结果；返回是否正确。"""
        result.flags_found.append(value)
        correct = bool(getattr(res, "correct", False))
        awarded = int(getattr(res, "awarded", 0) or 0)
        cumulative = int(getattr(res, "cumulative_score", 0) or 0)
        if correct:
            result.flags_correct += 1
            if result.first_flag_seconds is None and started_at is not None:
                result.first_flag_seconds = round(time.monotonic() - started_at, 1)
        else:
            result.wrong_count += 1  # V2-2：近失信号（回队插队首 + 尾段优先重攻）
        if awarded:
            result.awarded += awarded
        if cumulative:
            result.cumulative_score = cumulative
        LOG.info(
            "flag submitted code=%s flag=%s correct=%s awarded=%s progress=%s/%s",
            code, value, correct, awarded,
            getattr(res, "correct_flag_count", None), getattr(res, "total_flag_count", None),
        )
        return correct

    res = _submit_once(flag)
    if res is None or res == "dup":
        return
    if _record(flag, res):
        return
    # V2-3：原样提交判错 → 自动尝试大小写变体（平台大小写敏感时的兜底；最多 2 次）
    for variant in (flag.lower(), flag.upper()):
        if variant == flag:
            continue
        res2 = _submit_once(variant)
        if res2 is None or res2 == "dup":
            return
        if _record(variant, res2):
            return


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
    parser.add_argument(
        "--order-codes",
        default=None,
        help="V2-5 显式做题顺序（逗号分隔题码）：列表内按给定顺序置顶，未列题目按 easy-first 续队；"
        "失配码告警忽略不阻断。托管模式用环境变量 ASTRA_ORDER_CODES。",
    )
    parser.add_argument("--hint-after-seconds", type=float, default=900.0, help="第一次 hint 触发时间（默认 900s=15 分钟无解即取）")
    parser.add_argument("--hint2-after-seconds", type=float, default=1500.0, help="第二次 hint 触发时间（默认 1500s=25 分钟无解即取；V2-1 给取后利用留足时间）")
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
                order_codes=_parse_order_codes(args.order_codes, os.environ.get("ASTRA_ORDER_CODES")),
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
