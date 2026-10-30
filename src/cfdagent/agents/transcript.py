"""The LLM transcript: one JSON line per model call in ``llm_transcript.jsonl`` (design 6.4).

Each line holds the stage, agent, attempt and model, the messages added since the previous call,
the reply (content, tool calls, finish reason), the token usage (``null`` if the service gave
none), the cost so far and whether that cost is partly estimated, and, for a call that failed,
the error. The reply's ``raw`` wire data is never stored.

Free text is passed through the same redaction as the run record (URLs, host names, e-mail
addresses, IP addresses, tokens), and every field and every line is bounded, so a model that
echoes a large file cannot make the transcript unbounded. The file is append-only: each call
appends one line, opened without following links, so a symbolic link or a device planted in the
run directory is refused instead of written through.
"""

from __future__ import annotations

import json
import os
import stat
from typing import Any

from cfdagent.llm.client import UNPARSED_KEY, ChatResult
from cfdagent.llm.cost import CostTracker
from cfdagent.record import redact
from cfdagent.rundir import RunDir, RunDirError

TRANSCRIPT_FILE = "llm_transcript.jsonl"
MAX_LINE_BYTES = 400_000
# Per-text limits tried in turn until a line fits; the last one drops message bodies altogether.
_TEXT_LIMITS = (20_000, 2_000, 200)


def _clip(text: Any, limit: int) -> str:
    text = text if isinstance(text, str) else json.dumps(text, default=str)
    if len(text) <= limit:
        return redact(text, limit)
    return redact(text[:limit], limit) + f"...[{len(text) - limit} more characters not stored]"


def _arguments_text(arguments: Any) -> str:
    if isinstance(arguments, dict) and set(arguments) == {UNPARSED_KEY}:
        return str(arguments[UNPARSED_KEY])
    return json.dumps(arguments, default=str)


def _message(message: dict, limit: int) -> dict:
    out: dict[str, Any] = {"role": message.get("role")}
    if "tool_call_id" in message:
        out["tool_call_id"] = _clip(message["tool_call_id"], 200)
    if message.get("content") is not None:
        out["content"] = _clip(message["content"], limit)
    calls = message.get("tool_calls")
    if calls:
        out["tool_calls"] = [
            {
                "id": _clip(call.get("id"), 200),
                "name": _clip(call.get("function", {}).get("name"), 200),
                "arguments": _clip(call.get("function", {}).get("arguments"), limit),
            }
            for call in calls
        ]
    return out


class TranscriptWriter:
    """Appends one line per model call to ``llm_transcript.jsonl`` in the run directory.

    ``cost`` is the run's cost tracker; its totals at the time of the call go into the line.
    """

    def __init__(self, run: RunDir, cost: CostTracker | None = None) -> None:
        self._run = run
        self._cost = cost

    def append(
        self,
        stage: str,
        agent: str,
        attempt: int,
        model: str,
        new_messages: list[dict],
        result: ChatResult | None,
        *,
        error: str | None = None,
    ) -> None:
        """Write the line for one call. ``result`` is ``None`` and ``error`` set for a call that
        failed before a usable reply."""
        line = self._line(
            stage, agent, attempt, model, new_messages, result, error, _TEXT_LIMITS[0]
        )
        for limit in _TEXT_LIMITS[1:]:
            if len(line) <= MAX_LINE_BYTES:
                break
            line = self._line(stage, agent, attempt, model, new_messages, result, error, limit)
        if len(line) > MAX_LINE_BYTES:  # a very long conversation delta: keep the shape only
            line = self._line(stage, agent, attempt, model, [], result, error, _TEXT_LIMITS[-1])
        self._append(line)

    def _line(self, stage, agent, attempt, model, new_messages, result, error, limit) -> bytes:
        usage = None
        reply = None
        if result is not None:
            if result.usage is not None:
                usage = {
                    "prompt_tokens": result.usage.prompt_tokens,
                    "completion_tokens": result.usage.completion_tokens,
                    "reasoning_tokens": result.usage.reasoning_tokens,
                }
            reply = {
                "content": None if result.content is None else _clip(result.content, limit),
                "tool_calls": [
                    {
                        "id": _clip(call.id, 200),
                        "name": _clip(call.name, 200),
                        "arguments": _clip(_arguments_text(call.arguments), limit),
                    }
                    for call in result.tool_calls
                ],
                "finish_reason": result.finish_reason,
            }
        entry = {
            "stage": stage,
            "agent": agent,
            "attempt": attempt,
            "model": model,
            "new_messages": [_message(m, limit) for m in new_messages],
            "reply": reply,
            "error": None if error is None else _clip(error, 2000),
            "usage": usage,
            "cost_usd": None if self._cost is None else self._cost.total_usd,
            "cost_estimated": None if self._cost is None else self._cost.estimated,
        }
        return (json.dumps(entry, default=str) + "\n").encode("utf-8")

    def _append(self, data: bytes) -> None:
        path = self._run.path(TRANSCRIPT_FILE)  # refuses a symbolic link
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(path, flags, 0o644)
        except OSError as exc:
            raise RunDirError(f"cannot open {TRANSCRIPT_FILE} ({type(exc).__name__})") from None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise RunDirError(f"{TRANSCRIPT_FILE} is not a regular file")
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
        finally:
            os.close(fd)
