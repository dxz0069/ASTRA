from __future__ import annotations

from typing import Any

from astra.dispatcher.output_parser import extract_json_object


def parse_json_output(stdout: str) -> dict[str, Any]:
    try:
        return extract_json_object(stdout)
    except ValueError as exc:
        # 降级可观测性（R5 实测）：解析失败附带原始输出前 500 字，定位模型格式漂移
        snippet = stdout.strip()[:500]
        raise ValueError(f"{exc}; raw_output[:500]={snippet}") from exc


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


def _looks_like_reason_data(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    keys = set(payload)
    if keys == {"complete"}:
        complete = payload["complete"]
        return isinstance(complete, dict) and "from" in complete and "description" in complete
    if keys == {"intents"}:
        return isinstance(payload["intents"], list)
    if keys == {"intent"}:
        intent = payload["intent"]
        return isinstance(intent, dict) and "from" in intent and "description" in intent
    return False


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


def _looks_like_explore_data(payload: dict[str, Any]) -> bool:
    return isinstance(payload, dict) and set(payload) == {"description"}


def validate_reason_payload(
    payload: dict[str, Any], open_intents_empty: bool, max_intents: int,
) -> tuple[str, dict[str, Any] | list[dict[str, Any]] | None]:
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        if not _looks_like_reason_data(payload):
            raise ValueError("accepted must be true or false")
        data = payload
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")
    complete = data.get("complete")
    intents = data.get("intents")
    # backward compat: accept singular "intent" key from LLMs
    if intents is None:
        singular = data.get("intent")
        if isinstance(singular, dict):
            intents = [singular]
    if complete is not None:
        if intents is not None:
            raise ValueError("complete and intents cannot coexist")
        if not isinstance(complete, dict) or "from" not in complete or "description" not in complete:
            raise ValueError("invalid complete payload")
        return "complete", complete
    if intents is not None:
        if not isinstance(intents, list):
            raise ValueError("intents must be an array")
        for i, intent in enumerate(intents):
            if not isinstance(intent, dict) or "from" not in intent or "description" not in intent:
                raise ValueError(f"invalid intent at index {i}")
        if not intents and open_intents_empty:
            raise ValueError("intents must not be empty when open_intents is empty")
        intents = intents[:max_intents]
        if not intents:
            return "noop", None
        return "intents", intents
    if open_intents_empty:
        raise ValueError("intents is required when open_intents is empty")
    return "noop", None


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
    """增量输出解析：逐行 JSON（每行一条星记，末行可带 complete），超时不丢中间产物。

    返回 (facts: list[str], complete: str | None)。兼容旧的单对象格式。
    """
    from astra.dispatcher.output_parser import extract_json_object

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


def validate_explore_payload(payload: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        if not _looks_like_explore_data(payload):
            raise ValueError("accepted must be true or false")
        data = payload
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")
    description = data.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("description is required")
    confidence = data.get("confidence", "medium")
    if confidence not in ("low", "medium", "high"):
        confidence = "medium"
    evidence = data.get("evidence")
    if not isinstance(evidence, str) or not evidence.strip():
        evidence = None
    return "fact", {"description": description.strip(), "confidence": confidence, "evidence": evidence}


def validate_consolidate_payload(payload: dict[str, Any]) -> tuple[str, str | None]:
    """记忆整理：接受一条摘要星记描述。"""
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        if not _looks_like_explore_data(payload):
            raise ValueError("accepted must be true or false")
        data = payload
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")
    description = data.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("description is required")
    return "fact", description.strip()


def validate_challenge_payload(payload: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    """质询输出：{accepted, objections[], confidence}——质询结果不包装在 data 中。"""
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    if payload.get("accepted") is False:
        return "rejected", None
    if payload.get("accepted") is not True:
        raise ValueError("accepted must be true or false")
    objections = payload.get("objections", [])
    if not isinstance(objections, list):
        raise ValueError("objections must be an array")
    confidence = payload.get("confidence", "medium")
    if confidence not in ("low", "medium", "high"):
        raise ValueError("confidence must be one of low/medium/high")
    return "accepted", {"objections": [str(o) for o in objections], "confidence": confidence}


def validate_verdict_payload(payload: dict[str, Any], expected_kind: str) -> tuple[str, dict[str, Any] | None]:
    """裁决输出：accepted=true 且 data 与提案类型一致（complete dict / intents list）。"""
    accepted, data = _unwrap_wrapped_payload(payload)
    if accepted is False:
        return "rejected", None
    if accepted is None:
        raise ValueError("accepted must be true or false")
    if not isinstance(data, dict):
        raise ValueError("accepted must be true or false")
    if expected_kind == "complete":
        complete = data.get("complete")
        if not isinstance(complete, dict) or "from" not in complete or "description" not in complete:
            raise ValueError("invalid verdict complete payload")
        return "complete", complete
    intents = data.get("intents")
    if not isinstance(intents, list) or not intents:
        raise ValueError("invalid verdict intents payload")
    for intent in intents:
        if not isinstance(intent, dict) or "from" not in intent or "description" not in intent:
            raise ValueError(f"invalid verdict intent: {intent}")
    return "intents", intents
