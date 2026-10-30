"""Unit tests for the scripted chat backend used by agent tests and the dry run."""

import json

import pytest
from pydantic import BaseModel

from cfdagent.llm.client import (
    ChatBackend,
    ChatResult,
    EmptyChoicesError,
    LLMClient,
    StructuredOutputError,
    ToolCall,
    TruncatedOutputError,
    Usage,
)
from cfdagent.llm.config import ModelConfig
from cfdagent.llm.scripted import ScriptedBackend

MODEL = ModelConfig(deployment="scripted")
MESSAGES = [{"role": "user", "content": "hi"}]


class Verdict(BaseModel):
    solver: str
    steady: bool


def reply(content: str | None = None, tool_calls=None, **kwargs) -> ChatResult:
    return ChatResult(
        content=content,
        tool_calls=tool_calls or [],
        usage=kwargs.pop("usage", Usage(1, 2, None)),
        parsed=None,
        raw={},
        finish_reason=kwargs.pop("finish_reason", "stop"),
    )


def test_scripted_backend_replays_in_order():
    first = reply("one")
    boom = EmptyChoicesError("no choices")
    third = reply(tool_calls=[ToolCall("c1", "t", {"a": 1})], finish_reason="tool_calls")
    backend = ScriptedBackend([first, boom, third])
    assert backend.chat(MESSAGES, MODEL) is first
    with pytest.raises(EmptyChoicesError):
        backend.chat(MESSAGES, MODEL)
    assert backend.chat(MESSAGES, MODEL) is third
    with pytest.raises(AssertionError, match="exhausted"):
        backend.chat(MESSAGES, MODEL)
    assert len(backend.calls) == 4  # the call that found the script empty is recorded too


def test_calls_are_recorded_and_isolated_from_later_mutation():
    backend = ScriptedBackend([reply("a"), reply("b")])
    tools = [{"type": "function", "function": {"name": "t"}}]
    messages = [{"role": "user", "content": "q"}]
    backend.chat(messages, MODEL, tools=tools)
    messages.append({"role": "assistant", "content": "a"})
    backend.chat(messages, MODEL, response_schema=None)
    first, second = backend.calls
    assert first.messages == [{"role": "user", "content": "q"}]
    assert first.tools == tools
    assert first.model is MODEL
    assert first.response_schema is None
    assert len(second.messages) == 2


def test_backend_satisfies_the_protocol():
    backend: ChatBackend = ScriptedBackend([])
    assert hasattr(backend, "chat")
    assert hasattr(LLMClient, "chat")


def test_response_schema_is_validated_like_the_real_client():
    backend = ScriptedBackend(
        [
            reply('{"solver": "icoFoam", "steady": false}'),
            reply("not json at all"),
            reply('{"solver": "icoFo', finish_reason="length"),
        ]
    )
    ok = backend.chat(MESSAGES, MODEL, response_schema=Verdict)
    assert ok.parsed == Verdict(solver="icoFoam", steady=False)
    with pytest.raises(StructuredOutputError) as info:
        backend.chat(MESSAGES, MODEL, response_schema=Verdict)
    assert info.value.raw_text == "not json at all"
    assert info.value.usage == Usage(1, 2, None)
    with pytest.raises(TruncatedOutputError):
        backend.chat(MESSAGES, MODEL, response_schema=Verdict)
    assert backend.calls[0].response_schema is Verdict


def test_from_json_builds_steps(tmp_path):
    steps = [
        {"content": "hello", "usage": {"prompt_tokens": 5, "completion_tokens": 7}},
        {
            "content": None,
            "tool_calls": [{"id": "c1", "name": "run", "arguments": {"x": 1}}],
            "usage": None,
            "finish_reason": "tool_calls",
        },
        {"content": '{"solver": "s", "steady": true}', "finish_reason": "stop"},
        {"raise": "TruncatedOutputError", "message": "cut", "raw_text": "{"},
        {"raise": "EmptyChoicesError", "message": "no choices"},
        {"raise": "StructuredOutputError", "message": "bad", "raw_text": "xx"},
    ]
    path = tmp_path / "script.json"
    path.write_text(json.dumps(steps), encoding="utf-8")
    backend = ScriptedBackend.from_json(path)

    first = backend.chat(MESSAGES, MODEL)
    assert first.content == "hello"
    assert first.usage == Usage(5, 7, None)
    assert first.finish_reason is None
    assert first.tool_calls == []

    second = backend.chat(MESSAGES, MODEL)
    assert second.content is None
    assert second.usage is None
    assert second.finish_reason == "tool_calls"
    assert second.tool_calls == [ToolCall("c1", "run", {"x": 1})]

    third = backend.chat(MESSAGES, MODEL, response_schema=Verdict)
    assert third.parsed == Verdict(solver="s", steady=True)

    with pytest.raises(TruncatedOutputError) as info:
        backend.chat(MESSAGES, MODEL)
    assert info.value.raw_text == "{"
    with pytest.raises(EmptyChoicesError, match="no choices"):
        backend.chat(MESSAGES, MODEL)
    with pytest.raises(StructuredOutputError, match="bad") as info2:
        backend.chat(MESSAGES, MODEL)
    assert info2.value.raw_text == "xx"
    with pytest.raises(AssertionError):
        backend.chat(MESSAGES, MODEL)


def test_from_json_rejects_unknown_error_class_and_bad_shape(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps([{"raise": "ValueError", "message": "x"}]), encoding="utf-8")
    with pytest.raises(ValueError, match="ValueError"):
        ScriptedBackend.from_json(path)
    path.write_text(json.dumps({"steps": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="list"):
        ScriptedBackend.from_json(path)
