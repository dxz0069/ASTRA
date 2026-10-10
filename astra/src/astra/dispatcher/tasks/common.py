from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from astra.dispatcher.config import DispatchConfig, WorkerConfig
from astra.dispatcher.protocol.client import ASTRAClient
from astra.dispatcher.runtime.cancellation import TaskCancellation
from astra.dispatcher.runtime.containers import ContainerManager
from astra.dispatcher.runtime.heartbeat import HeartbeatLease
from astra.dispatcher.runtime.process import ProcessResult
from astra.server.models import Fact, ProjectDetail

HEALTHCHECK_COMMUNICATE_GRACE_SECONDS = 10
PROCESS_COMMUNICATE_GRACE_SECONDS = 15
LOG_PREVIEW_LIMIT = 1200
GRAPH_SNAPSHOT_ROOT = "/tmp/astra-prompts"
# P1-4：图快照目录最长保留时长——超过即视为陈旧可清理（任务超时远小于该值）
GRAPH_SNAPSHOT_MAX_AGE_SECONDS = 2 * 3600
# P1-4：陈旧快照清理节流间隔——不必每次派任务都全量扫描目录
_GRAPH_SNAPSHOT_CLEANUP_INTERVAL_SECONDS = 600
_last_snapshot_cleanup_at = [0.0]
LOG = logging.getLogger(__name__)
_phase_usage_write_lock = threading.Lock()

# 瞬时模型错误重试（信任缺失-数据网络类控制）：托管模式模型流量必须走平台网关
# （http + .tsecbench.gw），SSE 流被网关/代理中途截断（"incomplete SSE response"；
# pi-ai 旧版措辞 "Anthropic stream ended before message_stop"）会让整个步骤作废。
# pi 自身对断流零重试（pi-ai 0.73 providers/anthropic.js 直接 throw 实证），只能在
# 派发层兜底重跑。依据：log/session-589821/589846 两轮 glm-5.3 会话均在上下文压缩后
# 断流报错。ASTRA_MODEL_RETRY_MAX 可调（0=关闭），托管排障时免重建镜像。
TRANSIENT_MODEL_ERROR_RE = re.compile(
    r"(incomplete\s+sse|stream ended before message_stop|fetch failed|econnreset|"
    r"etimedout|econnrefused|socket hang up|overloaded|rate.?limit|"
    r"\b429\b|\b502\b|\b503\b|\b529\b)",
    re.IGNORECASE,
)
TRANSIENT_RETRY_DELAYS_SECONDS: tuple[float, ...] = (5.0, 15.0)
TRANSIENT_RETRY_MAX_DEFAULT = 2


def _transient_retry_max() -> int:
    try:
        return max(0, int(os.environ.get("ASTRA_MODEL_RETRY_MAX", str(TRANSIENT_RETRY_MAX_DEFAULT))))
    except ValueError:
        return TRANSIENT_RETRY_MAX_DEFAULT


def is_transient_model_failure(result: ProcessResult) -> bool:
    """非零退出 + stderr / stdout 错误事件命中传输层瞬时错误标记。

    超时/取消/正常退出不算（各有专属处理路径）；stdout 是 pi 的 json 事件流，
    只解析事件的 error 字段——模型正文里出现 "429"/"overloaded" 等字样不算传输错误。
    """
    if result.cancelled or result.timed_out or result.returncode == 0:
        return False
    if TRANSIENT_MODEL_ERROR_RE.search(result.stderr or ""):
        return True
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        error = event.get("error")
        text = (
            error
            if isinstance(error, str)
            else (error.get("message") if isinstance(error, dict) else None)
        )
        if isinstance(text, str) and TRANSIENT_MODEL_ERROR_RE.search(text):
            return True
    return False


FAILURE_HINT_PREFIX = "[失败学习] "

# 天枢去重：新发现与既有天枢描述的 Jaccard 词集合相似度达到该阈值视为重复（防重复侦察）。
# token 粒度：ASCII 词 + 连续 CJK 段（中文描述按短语段匹配，兼顾中英文混排）。
FACT_SIMILARITY_THRESHOLD = 0.6


def _fact_tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-zA-Z0-9_./:\-{}]+|[\u4e00-\u9fff]+", text.lower()))


def find_duplicate_fact(project: ProjectDetail, description: str, *, exclude_ids: tuple[str, ...] = ("origin", "goal")) -> Fact | None:
    """在既有天枢中找与 description 高度相似的（防重复侦察/重复写回）。

    - 描述含 flag{...} 的发现不去重（flag 必须完整写回，可能多个 flag 同题）；
    - origin/goal 不参与比较；
    - 相似度按 Jaccard 词集合计算，≥ FACT_SIMILARITY_THRESHOLD 视为重复。
    """
    if re.search(r"flag\{", description, re.IGNORECASE):
        return None
    target = _fact_tokens(description)
    if not target:
        return None
    for fact in project.facts:
        if fact.id in exclude_ids:
            continue
        other = _fact_tokens(fact.description)
        if not other:
            continue
        union = target | other
        if not union:
            continue
        if len(target & other) / len(union) >= FACT_SIMILARITY_THRESHOLD:
            return fact
    return None


def record_failure_hint(
    client: ASTRAClient,
    project_id: str,
    source: str,
    summary: str,
    *,
    prefix: str = FAILURE_HINT_PREFIX,
) -> bool:
    """把失败教训/审查否决原因写为指引（hint），作为后续轮次的风险提示。

    对齐 Linghun 的失败学习原则：教训只作风险提示进入上下文，
    绝不充当完成证据（hint 不会进入 facts 校验路径）。

    熔断反馈环：内容完全相同的 hint 已存在时跳过写入（审查否决 → hint 增长 →
    再触发定航 → 同样的提案再被否决 → 再写 hint 的环路会让 hints 表无界增长）。
    返回 True 表示 hint 已就位（写入成功或已存在）；False 表示写入失败——
    调用方若依赖该 hint 触发后续定航，应返回 failed 避免 checkpoint 静默停摆。
    """
    hint = f"{prefix}{source}：{summary}"
    try:
        project = client.get_project(project_id)
        if any(h.content == hint for h in project.hints):
            LOG.info("failure hint deduped project=%s source=%s", project_id, source)
            return True
        response = client.create_hint(project_id, hint, creator="astra.learning")
        if response.status_code >= 400:
            LOG.warning(
                "failure hint write failed project=%s status=%s",
                project_id,
                response.status_code,
            )
            return False
        return True
    except Exception as exc:  # noqa: BLE001 —— 教训记录失败不影响主流程
        LOG.debug("failure hint write error project=%s error=%s", project_id, exc)
        return False


@dataclass(slots=True)
class HealthcheckRun:
    result: ProcessResult
    duration_ms: int


@dataclass(slots=True)
class ConcludeWriteResult:
    status: str
    fact_id: str | None = None


def preview(text: str, limit: int = LOG_PREVIEW_LIMIT) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "..."


def did_timeout(result: ProcessResult) -> bool:
    return not result.cancelled and (result.timed_out or result.returncode in (124, 137))


def worker_completion_failure(
    driver,
    worker: WorkerConfig,
    result: ProcessResult,
    *,
    require_tool: bool = False,
) -> str | None:
    """Apply deterministic output checks before a worker result can change project state."""
    stdout_truncated = bool(getattr(result, "stdout_truncated", False))
    if stdout_truncated:
        return "stdout was truncated by the process output limit"
    if worker.type != "pi" or worker.env.get("PI_OFFLINE_MODEL_POLICY", "disabled") == "disabled":
        return None
    check = getattr(driver, "completion_failure", None)
    if not callable(check):
        return "offline Pi driver does not expose completion evidence"
    return check(result.stdout, stdout_truncated=stdout_truncated, require_tool=require_tool)


def worker_has_tool_evidence(driver, worker: WorkerConfig, result: ProcessResult) -> bool:
    """Allow partial-output recovery only after a completed successful tool call."""
    if worker.type != "pi" or worker.env.get("PI_OFFLINE_MODEL_POLICY", "disabled") == "disabled":
        return True
    if bool(getattr(result, "stdout_truncated", False)):
        return False
    inspect = getattr(driver, "completion_evidence", None)
    if not callable(inspect):
        return False
    evidence = inspect(result.stdout)
    return evidence.successful_tool_calls > 0 and evidence.malformed_tool_events == 0


def cancel_reason(result: ProcessResult, cancellation: TaskCancellation | None = None) -> str | None:
    if result.cancelled:
        return result.cancel_reason or "cancelled"
    if cancellation is not None:
        return cancellation.reason
    return None


def communicate_timeout(timeout_seconds: int, grace_seconds: int = PROCESS_COMMUNICATE_GRACE_SECONDS) -> int:
    return timeout_seconds + grace_seconds


def task_healthcheck_enabled(config: DispatchConfig) -> bool:
    return config.runtime.worker_healthcheck == "startup_and_task"


def _cleanup_stale_graph_snapshots() -> None:
    """P1-4：清理宿主侧超过 2 小时的旧图快照目录，防止 /tmp/astra-prompts 无限膨胀。

    每次派任务都写一份 <phase>-<uuid>/graph.yaml 且从不清理；本地执行模式下
    快照落在宿主临时目录（POSIX: $TMPDIR/astra-prompts，Windows: C:\\tmp\\
    astra-prompts——与 LocalContainerManager._to_host_path 的 /tmp 映射约定一致），
    跨项目累积成磁盘泄漏；docker 模式容器随项目整体回收，不受此影响。
    目录 mtime 即创建时间（写 graph.yaml 后不再变动），任务最长超时远小于
    2 小时，按此判龄不会删到在用快照。清理失败静默忽略——不影响派发主流程。
    """
    now = time.time()
    if now - _last_snapshot_cleanup_at[0] < _GRAPH_SNAPSHOT_CLEANUP_INTERVAL_SECONDS:
        return
    _last_snapshot_cleanup_at[0] = now
    try:
        if sys.platform == "win32":
            root = Path("C:/tmp") / "astra-prompts"
        else:
            root = Path(tempfile.gettempdir()) / "astra-prompts"
        if not root.is_dir():
            return
        for entry in root.iterdir():
            try:
                if entry.is_dir() and now - entry.stat().st_mtime > GRAPH_SNAPSHOT_MAX_AGE_SECONDS:
                    shutil.rmtree(entry, ignore_errors=True)
            except OSError:
                continue  # 单个目录清理失败不影响其余
    except OSError:
        pass


_GRAPH_READ_LINE_BYTES = 40 * 1024  # Pi read truncates a single line above 50 KiB.
_GRAPH_WRAP_COLUMN = 8192


class _ReadableGraphDumper(yaml.SafeDumper):
    """Allow a long quoted scalar to wrap even when it contains no spaces."""

    def write_double_quoted(self, value: str, split: bool = True) -> None:
        if len(value) <= _GRAPH_WRAP_COLUMN:
            super().write_double_quoted(value, split=split)
            return

        self.write_indicator('"', True)
        for char in value:
            if char == " ":
                # YAML normally folds spaces at a line break; keep them exact.
                encoded = "\\x20"
            elif char in self.ESCAPE_REPLACEMENTS:
                encoded = "\\" + self.ESCAPE_REPLACEMENTS[char]
            elif not (" " <= char <= "~" or (self.allow_unicode and (
                "\u00a0" <= char <= "\ud7ff" or "\ue000" <= char <= "\ufffd"
            ))):
                ordinal = ord(char)
                encoded = (f"\\x{ordinal:02X}" if ordinal <= 0xFF else
                           f"\\u{ordinal:04X}" if ordinal <= 0xFFFF else
                           f"\\U{ordinal:08X}")
            else:
                encoded = char
            if self.column + len(encoded) > _GRAPH_WRAP_COLUMN:
                continuation = "\\"
                self.stream.write(continuation.encode(self.encoding) if self.encoding else continuation)
                self.column += 1
                self.write_indent()
                self.whitespace = False
                self.indention = False
            self.stream.write(encoded.encode(self.encoding) if self.encoding else encoded)
            self.column += len(encoded)
        self.write_indicator('"', False)


def _represent_graph_string(dumper: _ReadableGraphDumper, value: str):
    style = '"' if len(value) > _GRAPH_WRAP_COLUMN else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_ReadableGraphDumper.add_representer(str, _represent_graph_string)


def _wrap_long_graph_lines(graph_yaml: str) -> str:
    """Preserve YAML values while making every line readable by Pi's read tool."""
    if all(len(line.encode("utf-8")) <= _GRAPH_READ_LINE_BYTES for line in graph_yaml.splitlines()):
        return graph_yaml
    try:
        graph = yaml.safe_load(graph_yaml)
        wrapped = yaml.dump(
            graph, Dumper=_ReadableGraphDumper, allow_unicode=True,
            default_flow_style=False, sort_keys=False,
        )
        if yaml.safe_load(wrapped) == graph and all(
            len(line.encode("utf-8")) <= _GRAPH_READ_LINE_BYTES for line in wrapped.splitlines()
        ):
            return wrapped
    except (yaml.YAMLError, UnicodeError):
        pass
    # Invalid input cannot be transformed losslessly. Keep the original bytes.
    LOG.warning("graph snapshot contains an unreadable long line that could not be wrapped")
    return graph_yaml


def write_graph_snapshot_reference(
    container_manager: ContainerManager,
    container_name: str,
    graph_yaml: str,
    *,
    phase: str,
) -> str:
    # P1-4：写入新快照前顺手清理陈旧快照目录（节流，见函数内说明）
    _cleanup_stale_graph_snapshots()
    path = f"{GRAPH_SNAPSHOT_ROOT}/{phase}-{uuid.uuid4().hex[:12]}/graph.yaml"
    container_manager.write_text_file(container_name, path, _wrap_long_graph_lines(graph_yaml))
    return (
        "The graph YAML snapshot is stored in this file inside the current container:\n\n"
        f"{path}\n\n"
        "Before using the graph, read the entire file and treat its contents as the YAML snapshot "
        "for this Graph section."
    )


def run_healthcheck(
    container_manager: ContainerManager,
    container_name: str,
    worker: WorkerConfig,
    command: list[str],
    *,
    timeout_seconds: int,
    lease: HeartbeatLease | None = None,
    cancellation: TaskCancellation | None = None,
) -> HealthcheckRun:
    process = container_manager.build_exec_process(
        container_name,
        dict(worker.env),
        command,
        timeout_seconds=timeout_seconds,
    )
    process.start()
    if lease is not None:
        lease.attach_process(process)
    if cancellation is not None:
        cancellation.attach_process(process)
    started = time.perf_counter()
    try:
        result = process.communicate(timeout=communicate_timeout(timeout_seconds, HEALTHCHECK_COMMUNICATE_GRACE_SECONDS))
    finally:
        if lease is not None:
            lease.attach_process(None)
        if cancellation is not None:
            cancellation.attach_process(None)
    duration_ms = int((time.perf_counter() - started) * 1000)
    return HealthcheckRun(result=result, duration_ms=duration_ms)


def run_worker_process(
    container_manager: ContainerManager,
    container_name: str,
    worker: WorkerConfig,
    argv: list[str],
    *,
    phase: str,
    timeout_seconds: int,
    lease: HeartbeatLease | None = None,
    cancellation: TaskCancellation | None = None,
    project_id: str | None = None,
    step_id: str | None = None,
) -> ProcessResult:
    LOG.info(
        "starting container exec container=%s worker=%s phase=%s timeout=%ss",
        container_name,
        worker.name,
        phase,
        timeout_seconds,
    )
    process = container_manager.build_exec_process(
        container_name,
        dict(worker.env),
        argv,
        timeout_seconds=timeout_seconds,
    )
    started = time.perf_counter()
    result: ProcessResult | None = None
    try:
        process.start()
        if lease is not None:
            lease.attach_process(process)
        if cancellation is not None:
            cancellation.attach_process(process)
        result = process.communicate(timeout=communicate_timeout(timeout_seconds))
        return result
    finally:
        if worker.type == "pi":
            _log_phase_usage(
                worker.name,
                phase,
                result.stdout if result is not None else "",
                project_id=project_id,
                step_id=step_id,
                duration_ms=int((time.perf_counter() - started) * 1000),
                result=result,
            )
        if lease is not None:
            lease.attach_process(None)
        if cancellation is not None:
            cancellation.attach_process(None)


def run_worker_process_with_retry(
    container_manager: ContainerManager,
    container_name: str,
    worker: WorkerConfig,
    argv: list[str],
    *,
    phase: str,
    timeout_seconds: int,
    lease: HeartbeatLease | None = None,
    cancellation: TaskCancellation | None = None,
    runner=None,
    project_id: str | None = None,
    step_id: str | None = None,
) -> ProcessResult:
    """run_worker_process + 瞬时模型错误退避重试（网关断流/5xx 不杀步骤）。

    重试用同一 argv 重跑（pi 新会话）——已做的工具调用进度会重来，但比整步作废
    再等调度器冷却重派便宜得多；次数上限 ASTRA_MODEL_RETRY_MAX（默认 2，0=关闭）。
    退避睡眠可被取消信号打断。runner 可注入替代执行入口（任务模块传自己命名空间
    的 run_worker_process，保持既有测试打桩点有效）。
    """
    run = runner if runner is not None else run_worker_process
    identifiers = {}
    if project_id is not None:
        identifiers["project_id"] = project_id
    if step_id is not None:
        identifiers["step_id"] = step_id
    result = run(
        container_manager,
        container_name,
        worker,
        argv,
        phase=phase,
        timeout_seconds=timeout_seconds,
        lease=lease,
        cancellation=cancellation,
        **identifiers,
    )
    max_retries = _transient_retry_max()
    for attempt in range(max_retries):
        if not is_transient_model_failure(result):
            break
        if cancellation is not None and cancellation.is_cancelled:
            break
        delay = TRANSIENT_RETRY_DELAYS_SECONDS[
            min(attempt, len(TRANSIENT_RETRY_DELAYS_SECONDS) - 1)
        ]
        LOG.warning(
            "transient model failure, retrying worker=%s phase=%s attempt=%s/%s backoff=%.0fs code=%s detail=%s",
            worker.name,
            phase,
            attempt + 1,
            max_retries,
            delay,
            result.returncode,
            preview(result.stderr or result.stdout, 200),
        )
        time.sleep(delay)
        if cancellation is not None and cancellation.is_cancelled:
            break
        result = run(
            container_manager,
            container_name,
            worker,
            argv,
            phase=phase,
            timeout_seconds=timeout_seconds,
            lease=lease,
            cancellation=cancellation,
            **identifiers,
        )
    return result


def _usage_number(usage: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, bool):
            return None
        if isinstance(value, int) and value >= 0:
            return value
        if isinstance(value, str) and value.isdecimal():
            return int(value)
        if value is not None:
            return None
    return None


def _parse_pi_usage(stdout: str) -> dict[str, int | bool]:
    """Sum assistant responses from Pi events without counting replayed snapshots.

    Pi may emit the same assistant message in both ``message_end`` and
    ``turn_end``. A turn_end without a matching message_end is counted as a
    fallback. ``agent_end`` can contain session history, so it is ignored.
    """
    totals: dict[str, int | bool] = {
        "assistant_turns": 0,
        "usage_turns": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_read_turns": 0,
    }
    pending_usage: list[dict[str, Any] | None] = []
    cache_keys = ("cacheRead", "cache_read", "cacheReadInputTokens", "cache_read_input_tokens")

    def usage_quality(usage: dict[str, Any] | None) -> int:
        if usage is None:
            return 0
        return sum((
            _usage_number(usage, "input", "inputTokens", "input_tokens") is not None,
            _usage_number(usage, "output", "outputTokens", "output_tokens") is not None,
            _usage_number(usage, *cache_keys) is not None,
        ))

    def add(usage: dict[str, Any] | None) -> None:
        totals["assistant_turns"] += 1
        if usage is None:
            return
        input_tokens = _usage_number(usage, "input", "inputTokens", "input_tokens")
        output_tokens = _usage_number(usage, "output", "outputTokens", "output_tokens")
        if input_tokens is None or output_tokens is None:
            return
        cache_read_tokens = _usage_number(usage, *cache_keys)
        totals["usage_turns"] += 1
        totals["input_tokens"] += input_tokens
        totals["output_tokens"] += output_tokens
        if cache_read_tokens is not None:
            totals["cache_read_turns"] += 1
            totals["cache_read_tokens"] += cache_read_tokens

    def flush() -> None:
        for usage in pending_usage:
            add(usage)
        pending_usage.clear()

    # Pi JSON mode frames events with LF; U+2028/U+2029 may occur in text.
    for line in stdout.split("\n"):
        if not line.lstrip().startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        if event_type not in ("message_end", "turn_end"):
            continue
        message = event.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            usage = None
        if event_type == "message_end":
            pending_usage.append(usage)
        else:
            if pending_usage:
                # turn_end repeats the last assistant message. Prefer its usage
                # when message_end was emitted before usage was populated.
                if usage_quality(usage) > usage_quality(pending_usage[-1]):
                    pending_usage[-1] = usage
                flush()
            else:
                add(usage)
    flush()
    totals["usage_complete"] = (
        totals["assistant_turns"] > 0
        and totals["usage_turns"] == totals["assistant_turns"]
    )
    totals["cache_read_complete"] = (
        totals["assistant_turns"] > 0
        and totals["cache_read_turns"] == totals["assistant_turns"]
    )
    return totals


def _log_phase_usage(
    worker_name: str,
    phase: str,
    stdout: str,
    *,
    project_id: str | None = None,
    step_id: str | None = None,
    duration_ms: int | None = None,
    result: ProcessResult | None = None,
) -> None:
    """Emit metadata-only accounting; JSONL is an explicit opt-in."""
    try:
        usage = _parse_pi_usage(stdout)
        if result is None:
            outcome = "error"
        elif result.cancelled:
            outcome = "cancelled"
        elif did_timeout(result):
            outcome = "timed_out"
        elif result.returncode == 0:
            outcome = "completed"
        else:
            outcome = "failed"
        record = {
            "record_type": "pi_phase_usage",
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "run_id": uuid.uuid4().hex,
            "project_id": project_id,
            "step_id": step_id,
            "worker": worker_name,
            "phase": phase,
            "duration_ms": duration_ms,
            "outcome": outcome,
            "returncode": result.returncode if result is not None else None,
            **usage,
        }
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        LOG.info("pi phase usage %s", encoded)
        jsonl_path = os.environ.get("ASTRA_PHASE_USAGE_JSONL")
        if jsonl_path:
            # One append call per record, serialized across dispatcher threads.
            # No stdout, stderr, prompt, command, or tool output enters the record.
            payload = (encoded + "\n").encode("utf-8")
            with _phase_usage_write_lock:
                fd = os.open(jsonl_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    if os.write(fd, payload) != len(payload):
                        raise OSError("short JSONL write")
                finally:
                    os.close(fd)
    except (OSError, ValueError, TypeError) as exc:
        # Accounting must not turn a completed worker execution into a failure.
        LOG.warning("pi phase usage accounting failed: %s", type(exc).__name__)


def project_allows_conclude_fallback(client: ASTRAClient, project_id: str, *, worker_name: str, step_id: str) -> bool:
    project = client.get_project(project_id)
    if project.project.status == "active":
        return True
    LOG.info(
        "skip conclude fallback because project is no longer active project=%s step=%s worker=%s status=%s",
        project_id,
        step_id,
        worker_name,
        project.project.status,
    )
    return False


def best_effort_release_decide(client: ASTRAClient, project_id: str, worker_name: str, lease_token: str | None = None) -> None:
    response = client.release_decide(project_id, worker_name, lease_token)
    if not response.ok and response.status_code not in (403, 409):
        LOG.warning(
            "decide release failed project=%s worker=%s status=%s",
            project_id,
            worker_name,
            response.status_code,
        )
    elif response.ok:
        LOG.info("released decide project=%s worker=%s", project_id, worker_name)
    else:
        LOG.info(
            "decide release skipped project=%s worker=%s status=%s",
            project_id,
            worker_name,
            response.status_code,
        )


def write_conclude_result(
    client: ASTRAClient,
    project_id: str,
    step_id: str,
    worker_name: str,
    description: str,
    *,
    source: str,
    phase_ms: int,
    total_ms: int | None = None,
    kind: str = "regular",
    finding: str | None = None,
    finding_high_value: bool = False,
    reuse_fact_id: str | None = None,
    verification_status: str | None = None,
    verification_summary: str | None = None,
) -> str:
    return write_conclude_result_with_fact_id(
        client,
        project_id,
        step_id,
        worker_name,
        description,
        source=source,
        phase_ms=phase_ms,
        total_ms=total_ms,
        kind=kind,
        finding=finding,
        finding_high_value=finding_high_value,
        reuse_fact_id=reuse_fact_id,
        verification_status=verification_status,
        verification_summary=verification_summary,
    ).status


def write_conclude_result_with_fact_id(
    client: ASTRAClient,
    project_id: str,
    step_id: str,
    worker_name: str,
    description: str,
    *,
    source: str,
    phase_ms: int,
    total_ms: int | None = None,
    kind: str = "regular",
    finding: str | None = None,
    finding_high_value: bool = False,
    reuse_fact_id: str | None = None,
    verification_status: str | None = None,
    verification_summary: str | None = None,
) -> ConcludeWriteResult:
    conclude_kwargs: dict[str, Any] = {}
    if finding_high_value:
        conclude_kwargs["finding_high_value"] = True
    if reuse_fact_id is not None:
        conclude_kwargs["reuse_fact_id"] = reuse_fact_id
    if verification_status is not None:
        conclude_kwargs["verification_status"] = verification_status
    if verification_summary is not None:
        conclude_kwargs["verification_summary"] = verification_summary
    response = client.conclude(
        project_id,
        step_id,
        worker_name,
        description,
        kind=kind,
        finding=finding,
        **conclude_kwargs,
    )
    if response.ok:
        fact_id: str | None = None
        if isinstance(response.data, dict):
            fact = response.data.get("fact")
            if isinstance(fact, dict):
                candidate = fact.get("id")
                if isinstance(candidate, str) and candidate:
                    fact_id = candidate
        if total_ms is None:
            LOG.info(
                "step concluded project=%s step=%s worker=%s source=%s phase_ms=%s",
                project_id,
                step_id,
                worker_name,
                source,
                phase_ms,
            )
        else:
            LOG.info(
                "step concluded project=%s step=%s worker=%s source=%s phase_ms=%s total_ms=%s",
                project_id,
                step_id,
                worker_name,
                source,
                phase_ms,
                total_ms,
            )
        return ConcludeWriteResult(status="success", fact_id=fact_id)
    if response.status_code in (403, 404):
        LOG.info(
            "project became inactive during conclude project=%s step=%s worker=%s",
            project_id,
            step_id,
            worker_name,
        )
    else:
        LOG.warning(
            "conclude write failed project=%s step=%s worker=%s status=%s body=%s",
            project_id,
            step_id,
            worker_name,
            response.status_code,
            response.text,
        )
    best_effort_release(client, project_id, step_id, worker_name)
    return ConcludeWriteResult(status="failed", fact_id=None)


def best_effort_release(client: ASTRAClient, project_id: str, step_id: str, worker_name: str) -> None:
    response = client.release(project_id, step_id, worker_name)
    if not response.ok and response.status_code not in (403, 409):
        LOG.warning(
            "release failed project=%s step=%s worker=%s status=%s",
            project_id,
            step_id,
            worker_name,
            response.status_code,
        )
    elif response.ok:
        LOG.info("released step project=%s step=%s worker=%s", project_id, step_id, worker_name)
    else:
        LOG.info(
            "release skipped project=%s step=%s worker=%s status=%s",
            project_id,
            step_id,
            worker_name,
            response.status_code,
        )
