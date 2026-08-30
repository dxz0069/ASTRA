from __future__ import annotations

from typing import Any

from astra.dispatcher.output_parser import extract_json_object


def parse_json_output(stdout: str) -> dict[str, Any]:
    try:
        return extract_json_object(stdout)
    except ValueError as exc:
        # 降级可观测性：解析失败附带原始输出前 500 字，定位模型格式漂移
        snippet = stdout.strip()[:500]
        raise ValueError(f"{exc}; raw_output[:500]={snippet}") from exc


# 单轮 decide 的图操作封顶（防幻觉/恶意输出倾倒；正常轮次远低于此）
MAX_CLOSE_STEPS_PER_DECIDE = 20
MAX_SUBGOALS_PER_DECIDE = 5


def _coerce_accepted(value: Any) -> bool | None:
    """模型偶发把 accepted 写成字符串（"true"/"True"/"yes"）——统一收敛为布尔。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes", "1"):
            return True
        if lowered in ("false", "no", "0"):
            return False
    if value == 1:
        return True
    if value == 0:
        return False
    return None


def _unwrap_wrapped_payload(payload: dict[str, Any]) -> tuple[bool | None, dict[str, Any] | None]:
    accepted = _coerce_accepted(payload.get("accepted"))
    if accepted is False:
        return False, None
    if accepted is True:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError("data must be an object")
        return True, data
    return None, None


def _is_dict(value: Any) -> bool:
    return isinstance(value, dict)


def _looks_like_decide_data(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    keys = set(payload)
    if keys == {"complete"}:
        complete = payload["complete"]
        return isinstance(complete, dict) and "from" in complete and "description" in complete
    return bool(keys & {"steps", "close_steps", "subgoals", "drop_subgoals"})


def _looks_like_bootstrap_execute_data(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict) or set(payload) != {"fact", "complete"}:
        return False
    return _is_dict(payload.get("fact")) and _is_dict(payload.get("complete"))


def _looks_like_bootstrap_conclude_data(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    keys = set(payload)
    if keys not in ({"fact"}, {"fact", "complete"}):
        return False
    return _is_dict(payload.get("fact"))


def _looks_like_execute_data(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    return bool(set(payload) & {"description", "finding"})


def validate_decide_payload(
    payload: dict[str, Any], open_steps_empty: bool, max_steps: int,
) -> tuple[str, dict[str, Any] | list[dict[str, Any]] | None]:
    """Decide 输出契约。

    返回 (kind, data)：
    - ("rejected", None)
    - ("complete", {"from": [...], "description": "..."})
    - ("ops", {"steps": [...], "close_steps": [...], "subgoals": [...], "drop_subgoals": [...]})
      （steps/close_steps/subgoals/drop_subgoals 各字段可选；全空且 open_steps 为空 → 报错，
       因为无未决步骤时必须有所动作）
    - ("noop", None)
    """
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        if not _looks_like_decide_data(payload):
            raise ValueError("accepted must be true or false")
        data = payload
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")
    complete = data.get("complete")
    steps = data.get("steps")
    # backward compat: accept singular "step" key from LLMs
    if steps is None:
        singular = data.get("step")
        if isinstance(singular, dict):
            steps = [singular]
    if complete is not None:
        if not isinstance(complete, dict) or "from" not in complete or "description" not in complete:
            raise ValueError("invalid complete payload")
        return "complete", complete

    close_steps = data.get("close_steps")
    subgoals = data.get("subgoals")
    drop_subgoals = data.get("drop_subgoals")

    if steps is not None:
        if not isinstance(steps, list):
            raise ValueError("steps must be an array")
        for i, step in enumerate(steps):
            if not isinstance(step, dict) or "from" not in step or "description" not in step:
                raise ValueError(f"invalid step at index {i}")
        steps = steps[:max_steps]
    if close_steps is not None:
        if not isinstance(close_steps, list):
            raise ValueError("close_steps must be an array")
        for i, item in enumerate(close_steps):
            if isinstance(item, str):
                close_steps[i] = {"id": item, "reason": ""}
            elif not isinstance(item, dict) or not item.get("id"):
                raise ValueError(f"invalid close_steps at index {i}")
        close_steps = close_steps[:MAX_CLOSE_STEPS_PER_DECIDE]
    if subgoals is not None:
        if not isinstance(subgoals, list):
            raise ValueError("subgoals must be an array")
        subgoals = [str(s) for s in subgoals if str(s).strip()][:MAX_SUBGOALS_PER_DECIDE]
    if drop_subgoals is not None:
        if not isinstance(drop_subgoals, list):
            raise ValueError("drop_subgoals must be an array")
        drop_subgoals = [str(s) for s in drop_subgoals if str(s).strip()][:MAX_SUBGOALS_PER_DECIDE]

    has_ops = bool(steps or close_steps or subgoals or drop_subgoals)
    if not has_ops:
        if open_steps_empty:
            raise ValueError("steps is required when open_steps is empty")
        return "noop", None
    return "ops", {
        "steps": steps or [],
        "close_steps": close_steps or [],
        "subgoals": subgoals or [],
        "drop_subgoals": drop_subgoals or [],
    }


def validate_bootstrap_execute_payload(payload: dict[str, Any]) -> tuple[str, dict[str, str] | None]:
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        if not _looks_like_bootstrap_execute_data(payload):
            raise ValueError("accepted must be true or false")
        data = payload
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")

    fact = data.get("fact")
    if not isinstance(fact, dict):
        raise ValueError("fact is required")
    fact_description = fact.get("description")
    if not isinstance(fact_description, str) or not fact_description.strip():
        raise ValueError("fact.description is required")

    result = {"fact_description": fact_description.strip()}
    complete = data.get("complete")
    if complete is None:
        raise ValueError("complete is required")
    if not isinstance(complete, dict):
        raise ValueError("complete must be an object")
    complete_description = complete.get("description")
    if not isinstance(complete_description, str) or not complete_description.strip():
        raise ValueError("complete.description is required")
    result["complete_description"] = complete_description.strip()
    return "complete", result


def validate_bootstrap_stream(stdout: str) -> tuple[list[str], str | None]:
    """增量输出解析：逐行 JSON（每行一条事实，末行可带 complete），超时不丢中间产物。

    返回 (facts: list[str], complete: str | None)。兼容旧的单对象格式。
    """
    facts: list[str] = []
    complete: str | None = None
    raw_lines = [ln for ln in stdout.splitlines() if ln.strip()]
    if not raw_lines:
        return facts, complete

    def _consume(payload: dict[str, Any]) -> None:
        nonlocal complete
        # 宽松校验：fact 必填、complete 可选（增量行无 complete）
        accepted, data = _unwrap_wrapped_payload(payload)
        if accepted is not True or not isinstance(data, dict):
            return
        fact = data.get("fact")
        fd = fact.get("description") if isinstance(fact, dict) else None
        if not isinstance(fd, str) or not fd.strip():
            return
        facts.append(fd.strip())
        comp = data.get("complete")
        cd = comp.get("description") if isinstance(comp, dict) else None
        if isinstance(cd, str) and cd.strip():
            complete = cd.strip()

    # 增量优先：逐行解析（每行一个 JSON 对象）
    for line in raw_lines:
        try:
            payload = extract_json_object(line)
        except ValueError:
            continue
        try:
            _consume(payload)
        except ValueError:
            continue
    if facts:
        return facts, complete

    # 无增量行 → 整段兜底（旧单对象格式：可能带 markdown 围栏/前缀/跨行 pretty）
    try:
        payload = extract_json_object(stdout)
        _consume(payload)
    except ValueError:
        pass
    return facts, complete


def validate_bootstrap_conclude_payload(payload: dict[str, Any]) -> tuple[str, str | None]:
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        if not _looks_like_bootstrap_conclude_data(payload):
            raise ValueError("accepted must be true or false")
        data = payload
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")
    extra_keys = set(data) - {"fact", "complete"}
    if extra_keys:
        raise ValueError("unexpected keys in conclude payload")
    fact = data.get("fact")
    if not isinstance(fact, dict):
        raise ValueError("fact is required")
    fact_description = fact.get("description")
    if not isinstance(fact_description, str) or not fact_description.strip():
        raise ValueError("fact.description is required")
    return "fact", fact_description.strip()


def validate_execute_payload(payload: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    """Execute 输出契约：description 必填，finding 可选（沿途发现）。"""
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        if not _looks_like_execute_data(payload):
            raise ValueError("accepted must be true or false")
        data = payload
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")
    description = data.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("description is required")
    finding = data.get("finding")
    finding_description: str | None = None
    if isinstance(finding, dict):
        fd = finding.get("description")
        if isinstance(fd, str) and fd.strip():
            finding_description = fd.strip()
    elif isinstance(finding, str) and finding.strip():
        finding_description = finding.strip()
    return "fact", {"description": description.strip(), "finding": finding_description}


# 质询理由封顶（防长篇倾倒；正常反驳一句话说清缺什么验证）
MAX_CHALLENGE_REASON_CHARS = 2000


def validate_challenge_payload(payload: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    """质询星探输出契约：对抗审查待审结论，输出 uphold/refute 判定。

    返回 (kind, data)：
    - ("rejected", None) —— 拒答（按 fail-open 处理）
    - ("uphold", None) —— 结论经对抗审查站得住，维持原判
    - ("refute", {"reason": "..."}) —— 反驳成立（结论不应收束/入图），reason 必填
    """
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        if not isinstance(payload, dict) or "verdict" not in payload:
            raise ValueError("accepted must be true or false")
        data = payload
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")
    verdict = data.get("verdict")
    if isinstance(verdict, str):
        verdict = verdict.strip().lower()
    if verdict in ("uphold", "upheld", "sustain", "confirmed"):
        return "uphold", None
    if verdict in ("refute", "refuted", "reject", "rebut"):
        reason = data.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason is required when verdict is refute")
        return "refute", {"reason": reason.strip()[:MAX_CHALLENGE_REASON_CHARS]}
    raise ValueError("verdict must be uphold or refute")
