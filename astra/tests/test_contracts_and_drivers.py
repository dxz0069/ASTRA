from __future__ import annotations

import json

import pytest

from astra.dispatcher.contracts import (
    parse_json_output,
    validate_decide_payload,
    validate_execute_payload,
)
from astra.dispatcher.runtime.process import ManagedProcess
from astra.dispatcher.workers.adapters.pi import PiDriver


def test_parse_json_output_extracts_object_from_markdown_noise() -> None:
    assert parse_json_output('result:\n```json\n{"accepted": true, "data": {}}\n```') == {
        "accepted": True,
        "data": {},
    }


def test_decide_payload_limits_number_of_steps() -> None:
    kind, data = validate_decide_payload(
        {
            "accepted": True,
            "data": {
                "steps": [
                    {"from": ["f001"], "description": "one", "expect": "e1"},
                    {"from": ["f001"], "description": "two"},
                ]
            },
        },
        open_steps_empty=True,
        max_steps=1,
    )

    assert kind == "ops"
    assert isinstance(data, dict)
    assert data["steps"] == [{"from": ["f001"], "description": "one", "expect": "e1"}]


def test_decide_payload_supports_close_and_subgoals() -> None:
    kind, data = validate_decide_payload(
        {
            "accepted": True,
            "data": {
                "close_steps": [{"id": "s001", "decide": "exhausted"}],
                "subgoals": ["get a foothold"],
            },
        },
        open_steps_empty=False,
        max_steps=3,
    )
    assert kind == "ops"
    assert data["close_steps"] == [{"id": "s001", "decide": "exhausted"}]
    assert data["subgoals"] == ["get a foothold"]


def test_decide_payload_requires_steps_when_none_are_open() -> None:
    with pytest.raises(ValueError, match="steps is required"):
        validate_decide_payload(
            {"accepted": True, "data": {}},
            open_steps_empty=True,
            max_steps=3,
        )


def test_execute_payload_extracts_optional_finding() -> None:
    kind, data = validate_execute_payload(
        {"accepted": True, "data": {"description": "found", "finding": {"description": "SQLi at /login"}}}
    )
    assert kind == "fact"
    assert data["description"] == "found"
    assert data["finding"] == "SQLi at /login"


def test_execute_payload_rejects_planning_text() -> None:
    with pytest.raises(ValueError):
        validate_execute_payload(parse_json_output("Need inspect files and keep working."))


def test_pi_driver_extracts_session_and_last_assistant_text() -> None:
    driver = PiDriver()
    stdout = "\n".join(
        [
            json.dumps({"type": "session", "id": "session-123"}),
            json.dumps(
                {
                    "type": "turn_end",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": '{"accepted":true,"data":{}}'}],
                    },
                }
            ),
        ]
    )

    assert driver.extract_session(None, stdout, "") == "session-123"
    assert driver.extract_response_text(stdout, "") == '{"accepted":true,"data":{}}'


def test_close_stream_closes_response_even_when_stream_close_fails() -> None:
    class Response:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class Stream:
        def __init__(self) -> None:
            self._response = Response()

        def close(self) -> None:
            raise ValueError("already closed")

    stream = Stream()
    ManagedProcess._close_stream(stream)

    assert stream._response.closed
