"""A chat backend that replays a fixed script, for tests and offline dry runs.

It implements `ChatBackend` like `LLMClient`, records every call, and for calls with a
`response_schema` validates the scripted `content` exactly as `LLMClient` does, so tests exercise
the same parsing and error path.
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
    SchemaError,
    StructuredOutputError,
    ToolCall,
    TruncatedOutputError,
    Usage,
    parse_structured,
)
from cfdagent.llm.config import ModelConfig


@dataclass(frozen=True)
class RecordedCall:
    messages: list[dict]  # a deep copy taken at call time
    model: ModelConfig
    tools: list[dict] | None
    response_schema: type[BaseModel] | None


def _build_error(step: dict) -> Exception:
    name = step["raise"]
    message = step.get("message", "scripted error")
    raw_text = step.get("raw_text", "")
    if name == "LLMError":
        return LLMError(message)
    if name == "EmptyChoicesError":
        return EmptyChoicesError(message)
    if name == "SchemaError":
        return SchemaError(message)
    if name == "StructuredOutputError":
        return StructuredOutputError(message, raw_text)
    if name == "TruncatedOutputError":
        return TruncatedOutputError(raw_text, step.get("finish_reason") or "length")
    raise ValueError(
        f"unknown error class {name!r} in script; use LLMError, EmptyChoicesError, SchemaError, "
        "StructuredOutputError or TruncatedOutputError"
    )


def _build_result(step: dict) -> ChatResult:
    usage = step.get("usage")
    return ChatResult(
        content=step.get("content"),
        tool_calls=[
            ToolCall(id=c["id"], name=c["name"], arguments=c.get("arguments", {}))
            for c in step.get("tool_calls") or []
        ],
        usage=None
        if usage is None
        else Usage(
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            reasoning_tokens=usage.get("reasoning_tokens"),
        ),
        parsed=None,
        raw={},
        finish_reason=step.get("finish_reason"),
    )


class ScriptedBackend:
    """Replays `steps` in order: a `ChatResult` is returned, an `Exception` is raised.

    Calling it after the script is exhausted raises `AssertionError`. Every call, including that
    one, is recorded in `calls`.
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
        `{"raise": "<LLM error class name>", "message": "...", "raw_text": "..."}`.
        """
        data: Any = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("a scripted-backend file must hold a JSON list of steps")
        steps: list[ChatResult | Exception] = []
        for index, step in enumerate(data):
            if not isinstance(step, dict):
                raise ValueError(f"script step {index} is not an object")
            steps.append(_build_error(step) if "raise" in step else _build_result(step))
        return cls(steps)

    def chat(
        self,
        messages: list[dict],
        model: ModelConfig,
        *,
        tools: list[dict] | None = None,
        response_schema: type[BaseModel] | None = None,
    ) -> ChatResult:
        self.calls.append(RecordedCall(copy.deepcopy(messages), model, tools, response_schema))
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
