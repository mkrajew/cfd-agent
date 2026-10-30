"""A chat backend that replays a fixed script, for tests and offline dry runs.

It implements `ChatBackend` like `LLMClient`, records every call, and for calls with a
`response_schema` builds the strict schema and validates the scripted `content` exactly as
`LLMClient` does, so tests exercise the same schema, parsing and error path.
"""

import copy
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from cfdagent.llm.client import (
    ChatResult,
    EmptyChoicesError,
    LLMError,
    LLMTransportError,
    MalformedReplyError,
    SchemaError,
    StructuredOutputError,
    ToolCall,
    TruncatedOutputError,
    Usage,
    parse_structured,
    retryable_status,
    strict_schema,
)
from cfdagent.llm.config import ModelConfig

_ERROR_CLASSES = (
    "LLMError, EmptyChoicesError, MalformedReplyError, LLMTransportError, SchemaError, "
    "StructuredOutputError or TruncatedOutputError"
)


@dataclass(frozen=True)
class RecordedCall:
    messages: list[dict]  # a deep copy taken at call time
    model: ModelConfig
    tools: list[dict] | None  # a deep copy taken at call time
    response_schema: type[BaseModel] | None


def _build_usage(raw: Any) -> Usage | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("usage must be an object or null")
    try:
        return Usage(
            prompt_tokens=raw["prompt_tokens"],
            completion_tokens=raw["completion_tokens"],
            reasoning_tokens=raw.get("reasoning_tokens"),
        )
    except KeyError as exc:
        raise ValueError(f"usage lacks the key {exc.args[0]!r}") from exc


def _build_error(step: dict) -> Exception:
    name = step["raise"]
    message = step.get("message", "scripted error")
    raw_text = step.get("raw_text", "")
    usage = _build_usage(step.get("usage"))
    if name == "LLMError":
        return LLMError(message, usage=usage)
    if name == "EmptyChoicesError":
        return EmptyChoicesError(message, usage=usage)
    if name == "MalformedReplyError":
        return MalformedReplyError(message, usage=usage)
    if name == "LLMTransportError":
        status = step.get("status_code")
        retryable = step.get("retryable")
        return LLMTransportError(
            message,
            status_code=status,
            retryable=retryable_status(status) if retryable is None else bool(retryable),
            usage=usage,
        )
    if name == "SchemaError":
        return SchemaError(message)
    if name == "StructuredOutputError":
        return StructuredOutputError(message, raw_text, usage=usage)
    if name == "TruncatedOutputError":
        return TruncatedOutputError(raw_text, step.get("finish_reason") or "length", usage=usage)
    raise ValueError(f"unknown error class {name!r}; use {_ERROR_CLASSES}")


def _build_result(step: dict) -> ChatResult:
    calls = step.get("tool_calls") or []
    if not isinstance(calls, list):
        raise ValueError("tool_calls must be a list")
    try:
        tool_calls = [
            ToolCall(id=c["id"], name=c["name"], arguments=c.get("arguments", {})) for c in calls
        ]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"a tool call needs the keys id, name, arguments ({exc!r})") from exc
    return ChatResult(
        content=step.get("content"),
        tool_calls=tool_calls,
        usage=_build_usage(step.get("usage")),
        parsed=None,
        raw={},
        finish_reason=step.get("finish_reason"),
    )


class ScriptedBackend:
    """Replays `steps` in order: a `ChatResult` is returned, an `Exception` is raised.

    Calling it after the script is exhausted raises `AssertionError`. Every call, including that
    one, is recorded in `calls`. A `response_schema` that the real client would reject
    (`SchemaError`) is rejected here too, before a step is consumed.
    """

    def __init__(self, steps: list[ChatResult | Exception]):
        self._steps = list(steps)
        self._next = 0
        self.calls: list[RecordedCall] = []

    @classmethod
    def from_json(cls, path: str | Path) -> "ScriptedBackend":
        """Load a script: a JSON list of steps.

        A reply step is `{"content": str|null, "tool_calls": [{"id", "name", "arguments": {...}}],
        "usage": {"prompt_tokens", "completion_tokens", "reasoning_tokens"?}|null,
        "finish_reason": str|null}` (every key optional). An error step is
        `{"raise": "<error class name>", "message": "...", "raw_text": "...", "usage": {...}|null,
        "status_code": int, "retryable": bool}` (`raw_text` for structured and truncated errors,
        `status_code` and `retryable` for `LLMTransportError`; `retryable` defaults from the
        status). A malformed step raises `ValueError` naming the step number.
        """
        data: Any = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("a scripted-backend file must hold a JSON list of steps")
        steps: list[ChatResult | Exception] = []
        for index, step in enumerate(data):
            if not isinstance(step, dict):
                raise ValueError(f"script step {index} is not an object")
            try:
                steps.append(_build_error(step) if "raise" in step else _build_result(step))
            except ValueError as exc:
                raise ValueError(f"script step {index}: {exc}") from exc
        return cls(steps)

    def chat(
        self,
        messages: list[dict],
        model: ModelConfig,
        *,
        tools: list[dict] | None = None,
        response_schema: type[BaseModel] | None = None,
    ) -> ChatResult:
        self.calls.append(
            RecordedCall(copy.deepcopy(messages), model, copy.deepcopy(tools), response_schema)
        )
        if response_schema is not None:
            strict_schema(response_schema.model_json_schema())  # raises SchemaError like the client
        if self._next >= len(self._steps):
            raise AssertionError(
                f"scripted backend exhausted: call {len(self.calls)} but only "
                f"{len(self._steps)} steps were scripted"
            )
        step = self._steps[self._next]
        self._next += 1
        if isinstance(step, Exception):
            raise step
        if response_schema is not None:
            parsed = parse_structured(response_schema, step.content, step.finish_reason, step.usage)
            return replace(step, parsed=parsed)
        return step
