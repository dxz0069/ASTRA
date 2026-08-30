"""tsecbench 靶场适配器 —— 平台特定知识的唯一归口。

runner.py 是 benchmark-agnostic 的编排内核（经济性调度/自愈/进度）；本文件是它的
第一个靶场适配件：tsecbench 平台 SDK、字段形态、异常语义、flag 格式、经验教训
全部收敛于此。**换靶场 = 换本文件**（接口契约见 runner.BenchmarkClient 协议）。

经验清单（R5-R10 十轮实战沉淀，每条都有事故档案）：
- 异常按名识别（SDK 异常类型不可静态导入）：InvalidState+"already finished"=时限到全停；
  InvalidState=名额满（409 busy）；DuplicateSubmit=幂等跳过
- 网络瞬断只认传输层签名（裸 timeout 会误匹配本地超时 → 死循环，实测炸过整套回归）
- 字段宽容读取：unique_code|code；container_addr 可能缺省（地址漂移，R6 教训）
- flag{...} 格式：占位符排除；题面出现过的 flag 一律剔除（a-05 教训：示例 flag 被抄进
  天枢 → 错交两次 + 假"部分解出"关题）
- goal 文案由题面+旗数组装（多旗题显式告知旗数，收割调度的前提）
"""

from __future__ import annotations

import logging
import random
import re
import time
from typing import Any

LOG = logging.getLogger("astra-runner.tsecbench")


# ---------------- 平台异常语义 ----------------

class TaskFinishedError(Exception):
    """跑分任务时限已到（平台 409 already finished），停止整轮。"""


class SlotBusyError(Exception):
    """平台活跃名额已满（start 409），稍后重试。"""


class TransientNetError(Exception):
    """网络瞬断（平台/LLM 不可达）——题不死，等待后原地重进。"""


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


def translate_sdk_error(exc: Exception) -> None:
    """按异常名把 SDK 异常翻译成适配器语义异常；不认识的原样返回（调用方 re-raise）。"""
    name = type(exc).__name__
    if name == "InvalidState" and "already finished" in str(exc):
        raise TaskFinishedError(str(exc)) from exc
    if name == "InvalidState":
        raise SlotBusyError(str(exc)) from exc
    if _is_transient_network_error(exc):
        raise TransientNetError(str(exc)) from exc


# ---------------- SDK 调用纪律 ----------------

# SDK 业务异常按异常名识别（不重试）；其余（网络/服务错误）重试退避
KNOWN_BUSINESS_EXC_NAMES = {"InvalidState", "DuplicateSubmit", "TaskFinishedError", "SlotBusyError"}


def call_with_retry(fn, name: str, *, retries: int = 3, base_delay: float = 5.0):
    """SDK 调用重试保护：网络/服务类错误指数退避重试（瞬断抖动在这里吸收——R5 实测
    端点抖动 2-5 分钟自愈），业务异常（按名）直接抛出。

    连续重试仍失败 → ERROR 告警（可能 VPN 断开/平台不可达），抛出最后一个错误。
    注意：不做提前的瞬断翻译——重试就是为瞬断准备的；翻译发生在适配器出口与
    runner 的兜底路径。
    """
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


# ---------------- flag 格式（tsecbench 靶场旗格式） ----------------

FLAG_RE = re.compile(r"flag\{[^}\s]{3,}\}", re.IGNORECASE)
# 排除字面占位符 flag{...}（模型示例诱导）；真实 flag 内容至少 3 字符
PLACEHOLDER_FLAG_RE = re.compile(r"^flag\{\s*\.{3}\s*\}$", re.IGNORECASE)
# 掩码/占位内容标记：日志脱敏词等曾借知识库回流成"变异占位符"（flag{...已脱敏...}
# 实例：PLACEHOLDER 正则漏网 → t=0 错交）。含脱敏标记或无任何字母数字的内容必为假旗。
MASK_MARKER_RE = re.compile(r"脱敏|打码|redacted|masked", re.IGNORECASE)


def _is_junk_flag(flag: str) -> bool:
    inner = flag[flag.index("{") + 1 : flag.rindex("}")]
    if MASK_MARKER_RE.search(inner):
        return True
    return not re.search(r"[A-Za-z0-9]", inner)


def extract_flags(text: str) -> list[str]:
    """从文本中提取 flag{...}，去重保序，排除占位符（如 flag{...}）。"""
    seen: set[str] = set()
    flags: list[str] = []
    for match in FLAG_RE.findall(text):
        flag = match.strip()
        if PLACEHOLDER_FLAG_RE.match(flag) or _is_junk_flag(flag):
            continue
        if flag not in seen:
            seen.add(flag)
            flags.append(flag)
    return flags


def strip_flag_like(text: str) -> str:
    """剥离文本中的 flag{...} 串（知识库/邻居注入用——思路参考不该携带任何旗格式串）。"""
    return FLAG_RE.sub("flag{...}", text)


def collect_flags_from_facts(descriptions: list[str], exclude_texts: list[str] | None = None) -> list[str]:
    """从天枢描述收集 flag；exclude_texts（题面/goal 原文）中出现的 flag 串一律剔除。

    a-05 实例：题面示例 flag 格式串被 bootstrap 抄进天枢 → 被当发现提交 → 错交两次，
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


# ---------------- 字段宽容读取 ----------------

def challenge_code(ch: Any) -> str:
    """题码宽容读取：unique_code | code。"""
    return str(getattr(ch, "unique_code", None) or getattr(ch, "code", "") or "")


def challenge_addr(started: Any) -> Any:
    """start 结果的靶机地址宽容读取（container_addr 可能缺省——地址漂移教训）。"""
    return getattr(started, "container_addr", None) or []


def build_goal_text(description: str, ch: Any) -> str:
    """由题面+旗数组装北辰文案（多旗题显式告知旗数——收割调度的前提）。"""
    flag_count = getattr(ch, "flag_count", None)
    goal = f"在靶场地址上完成题目并获取全部 flag，目标描述：{description}"
    if flag_count:
        goal += f"（共 {flag_count} 个 flag）"
    goal += "。拿到 flag 后必须以天枢形式写回星图，天枢描述中必须包含完整 flag{...} 字符串。"
    return goal


# ---------------- 环境自检 ----------------

def check_platform(token: str, base_url: str) -> tuple[bool, str]:
    """平台 API 连通探针（带 token 拉题目全集；不含 VPN 内目标访问）。"""
    import requests

    resp = requests.get(f"{base_url}/openapi/v1/challenges", headers={"BENCHMARK_TOKEN": token}, timeout=15)
    if resp.status_code == 200:
        return True, f"status=200 challenges={len(resp.json() or [])}"
    return False, f"status={resp.status_code} body={resp.text[:200]}"


# ---------------- 适配器主体 ----------------

class TsecbenchAdapter:
    """tsecbench 靶场适配器：上下文管理器（自动 VPN 预检）+ BenchmarkClient 协议实现。

    用法（runner.main）：
        with TsecbenchAdapter(base_url=..., token=...) as client:
            run_benchmark(client, ...)
    所有 SDK 异常在出口处经 translate_sdk_error 归一为适配器语义异常。
    """

    def __init__(self, base_url: str, token: str) -> None:
        from tsec_benchmark import TSecBenchmark  # 进入上下文自动 VPN 预检

        self._sdk_factory = lambda: TSecBenchmark(base_url=base_url, token=token)
        self._sdk = None

    def __enter__(self) -> "TsecbenchAdapter":
        self._sdk = self._sdk_factory().__enter__()
        return self

    def __exit__(self, *exc_info) -> None:
        if self._sdk is not None:
            self._sdk.__exit__(*exc_info)
            self._sdk = None

    def _call(self, method: str, *args):
        assert self._sdk is not None, "TsecbenchAdapter 必须作为上下文管理器使用"
        try:
            return getattr(self._sdk, method)(*args)
        except Exception as exc:  # noqa: BLE001 —— 统一翻译出口
            translate_sdk_error(exc)
            raise

    # ---- BenchmarkClient 协议 ----
    def list_challenges(self):
        return self._call("list_challenges")

    def start_challenge(self, unique_code: str):
        return self._call("start_challenge", unique_code)

    def get_hint(self, unique_code: str):
        return self._call("get_hint", unique_code)

    def submit_flag(self, unique_code: str, flag: str):
        return self._call("submit_flag", unique_code, flag)

    def close_challenge(self, unique_code: str):
        return self._call("close_challenge", unique_code)
