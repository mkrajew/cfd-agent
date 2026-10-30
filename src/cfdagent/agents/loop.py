"""The gated stage loop: one agent, tools, a deterministic gate, bounded attempts.

How a stage runs (design 6.2, 6.5)
----------------------------------
The conversation starts with the stage's system prompt and opening message. The model works with
its tools and calls ``check_stage`` when it believes the work is complete; that runs the stage's
gate on the current attempt directory and counts as one *attempt*. A passing gate ends the stage
(the loop then promotes the attempt). A failing gate returns its errors to the model as the tool
result and a fresh attempt directory is prepared. If the model replies without any tool call and
no gate has passed, the loop runs the gate itself, which also counts as an attempt, and sends the
errors back as a user message.

A stage ends, with a reason, when: the gate passes; ``max_attempts_per_stage`` gates have failed;
``max_turns_per_attempt`` model turns pass without a check; the run's wall-clock budget is used;
the cost limit is exceeded; or the model service fails for good. A ``SchemaError`` is a
programming error and propagates, as does any exception from the gate itself.

Model errors
------------
A reply that arrives but is unusable (cut off, no choices, malformed, failing a schema) costs one
*turn* and is reported to the model; it is not an attempt. A transport error that is worth
retrying is retried twice with a growing pause (the injectable ``ctx.sleep``) and does not count
as a turn; after that, or at once for a permanent error, the stage fails. Every reply's usage is
billed to ``ctx.cost`` (an unknown usage is estimated from text length), also for unusable
replies.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from cfdagent.agents.stage import Stage, StageContext
from cfdagent.agents.tools import (
    NoArgs,
    Tool,
    ToolRegistry,
    builtin_tools,
    truncate_utf8,
)
from cfdagent.errors import AgentInputError, ToolFailure, as_gate
from cfdagent.llm.client import (
    ChatResult,
    LLMError,
    LLMTransportError,
    ToolCall,
    TruncatedOutputError,
    Usage,
    assistant_message,
)
from cfdagent.llm.cost import CostLimitExceeded
from cfdagent.record import AttemptRecord, StageRecord, redact

CONTEXT_CHAR_LIMIT = 200_000  # estimated size of the messages above which old tool output goes
KEEP_LAST_MESSAGES = 6  # never elided
ELIDED = "[output elided to save context]"
CHECK_ERROR_BYTES = 4096  # gate errors returned to the model
TRANSPORT_RETRIES = 2
BACKOFF_S = (2.0, 8.0)  # pause before the first and the second retry
CHECK_TOOL = "check_stage"
CUT_OFF_ADVICE = (
    "Reason briefly, then call tools with SMALLER arguments: split the work into several "
    "smaller files; do not put everything into one huge file."
)
MAX_REASON_CHARS = 1500
MAX_ECHOED_ERROR_CHARS = 600


@dataclass(frozen=True)
class StageOutcome:
    """Result of ``run_stage``: ``attempts`` is the number of gate runs, ``errors`` the errors of
    the last failed gate (empty on success) and ``reason`` why a failed stage ended."""

    ok: bool
    attempts: int
    errors: list[str] = field(default_factory=list)
    reason: str | None = None


def message_chars(message: dict) -> int:
    """Rough size of one chat message: its text plus the text of its tool-call arguments."""
    total = len(message.get("content") or "")
    for call in message.get("tool_calls") or []:
        total += len(call.get("function", {}).get("arguments") or "")
    return total


def _elide_old_outputs(messages: list[dict]) -> None:
    """Replace the content of the oldest tool results, in place, until the estimated size of the
    conversation is under ``CONTEXT_CHAR_LIMIT``. The newest ``KEEP_LAST_MESSAGES`` messages and
    everything that is not a tool result are kept."""
    total = sum(message_chars(m) for m in messages)
    if total <= CONTEXT_CHAR_LIMIT:
        return
    for message in messages[:-KEEP_LAST_MESSAGES]:
        content = message.get("content")
        if (
            message.get("role") == "tool"
            and isinstance(content, str)
            and len(content) > len(ELIDED)
        ):
            total -= len(content) - len(ELIDED)
            message["content"] = ELIDED
            if total <= CONTEXT_CHAR_LIMIT:
                return


@contextmanager
def _internal(what: str) -> Iterator[None]:
    """Run bookkeeping that is not the model's business.

    ``RunDirError`` and friends are ``AgentInputError``s, which a tool call would show to the
    model as text. Here they mean the run's own files misbehave, so they become ``ToolFailure``
    and propagate to the orchestrator.
    """
    try:
        yield
    except AgentInputError as exc:
        raise ToolFailure(f"{what} failed: {exc}") from exc


def _short(text: object, limit: int = MAX_ECHOED_ERROR_CHARS) -> str:
    text = str(text)
    return text if len(text) <= limit else text[:limit] + "..."


class _StageRun:
    """The state of one ``run_stage`` call."""

    def __init__(self, stage: Stage, ctx: StageContext) -> None:
        self.stage = stage
        self.ctx = ctx
        self.limits = ctx.config.limits
        self.model = ctx.config.models[stage.agent]
        self.messages: list[dict] = []
        self.registry = ToolRegistry()
        self.attempts = 0  # gate runs so far
        self.turns = 0  # model turns since the last gate run
        self.last_errors: list[str] = []
        self.passed = False
        self.exhausted = False
        self.abort: str | None = None  # why the stage must stop, if it must
        self.attempt_dir = Path()
        self.attempt_started = 0.0
        self.logged = 0  # messages already written to the transcript
        self.record = StageRecord(name=stage.name, ok=False)
        ctx.record.stages.append(self.record)

    # ------------------------------------------------------------------ attempts and tools

    def _relative(self, path: Path) -> str:
        return self.ctx.run.relative(path) + "/"

    def _start_attempt(self) -> None:
        number = self.attempts + 1
        with _internal("preparing the attempt directory"):
            self.attempt_dir = self.ctx.run.attempt_dir(self.stage.name, number)
        self.attempt_started = self.ctx.clock()
        self.turns = 0
        registry = ToolRegistry()
        for tool in builtin_tools(self.ctx.run, self.stage.scope):
            registry.add(tool)
        registry.add(
            Tool(
                CHECK_TOOL,
                "Run the automatic checks on the current attempt directory. Call it only when "
                "the work is complete: every call uses one of a few attempts. Returns the list "
                "of problems to fix, or confirms that the stage passed.",
                NoArgs,
                lambda _args: self._check(),
            )
        )
        with _internal("building the stage tools"):
            extra = self.stage.extra_tools(self.ctx, self.attempt_dir)
        for tool in extra:
            registry.add(tool)
        self.registry = registry

    def _attempt_note(self) -> str:
        return (
            f"This is attempt {self.attempts + 1} of {self.limits.max_attempts_per_stage}. "
            f"Work in the attempt directory {self._relative(self.attempt_dir)} (paths are "
            "relative to the run directory). When the work is complete, call check_stage."
        )

    def _time_left(self) -> float:
        return self.ctx.remaining_s()

    def _wall_clock_reason(self) -> str:
        return (
            f"wall-clock limit of {self.limits.wall_clock_s:.0f} s reached during stage "
            f"{self.stage.name!r}"
        )

    def _check(self) -> str:
        """Run the gate once; the text returned is the tool result (or the user message)."""
        if self._time_left() <= 0:
            self.abort = self._wall_clock_reason()
            return "The run's time budget is used up; the stage stops here."
        # An AgentInputError from the gate is the agent's work being wrong: a failed attempt.
        result = as_gate(lambda: self.stage.execute_and_gate(self.ctx, self.attempt_dir))
        seconds = max(0.0, self.ctx.clock() - self.attempt_started)
        self.attempts += 1
        errors = [] if result.ok else [str(e) for e in result.errors]
        if not result.ok and not errors:
            errors = ["the checks failed without a message"]
        self.record.attempts.append(
            AttemptRecord(n=self.attempts, ok=result.ok, errors=errors, seconds=seconds)
        )
        if result.ok:
            self.passed = True
            return "The checks passed. The stage is complete."
        self.last_errors = errors
        listing = truncate_utf8(
            "\n".join(f"- {e}" for e in errors), CHECK_ERROR_BYTES, "\n[errors truncated]"
        )
        limit = self.limits.max_attempts_per_stage
        text = f"The checks failed (attempt {self.attempts} of {limit}):\n{listing}"
        if self.attempts >= limit:
            self.exhausted = True
            self._progress()
            return text + "\nNo attempts are left."
        previous = self._relative(self.attempt_dir)
        self._start_attempt()
        self._progress()
        return (
            f"{text}\n\nA fresh attempt directory {self._relative(self.attempt_dir)} is ready "
            f"(attempt {self.attempts + 1} of {self.limits.max_attempts_per_stage}). Write the "
            f"corrected files there; the files of the previous attempt stay readable in "
            f"{previous}. Call check_stage again when done."
        )

    def _progress(self) -> None:
        if self.ctx.on_progress is not None:
            with _internal("saving the run record"):
                self.ctx.on_progress(self.stage.name)

    # ------------------------------------------------------------------ the model

    def _prompt_chars(self) -> int:
        schemas = json.dumps(self.registry.schemas())
        return sum(message_chars(m) for m in self.messages) + len(schemas)

    def _bill(self, usage: Usage | None, prompt_chars: int, completion_chars: int) -> None:
        try:
            self.ctx.cost.add(usage, prompt_chars, completion_chars)
        except CostLimitExceeded as exc:
            self.abort = f"cost limit: {exc}"

    def _log(self, sent: int, result: ChatResult | None, error: str | None = None) -> None:
        self.ctx.transcript.append(
            self.stage.name,
            self.stage.agent,
            self.attempts + 1,
            self.model.deployment,
            self.messages[self.logged : sent],
            result,
            error=error,
        )
        self.logged = sent

    def _ask(self) -> ChatResult | None:
        """One model turn. Returns the reply, or ``None`` when there is none to act on: the
        problem was reported to the model as a user message, or ``self.abort`` is set."""
        _elide_old_outputs(self.messages)
        retries = 0
        while True:
            if self._time_left() <= 0:
                self.abort = self._wall_clock_reason()
                return None
            sent = len(self.messages)
            prompt_chars = self._prompt_chars()
            try:
                result = self.ctx.backend.chat(
                    self.messages, self.model, tools=self.registry.schemas()
                )
            except LLMTransportError as exc:
                self._log(sent, None, error=f"{type(exc).__name__}: {exc}")
                if exc.retryable and retries < TRANSPORT_RETRIES:
                    self.ctx.sleep(BACKOFF_S[retries])
                    retries += 1
                    continue
                status = f"HTTP {exc.status_code}" if exc.status_code else "no HTTP status"
                again = f" after {retries} retries" if retries else ""
                self.abort = f"the model service failed{again} ({status}): {redact(str(exc), 300)}"
                return None
            except LLMError as exc:
                self.turns += 1
                raw = getattr(exc, "raw_text", "") or ""
                self._bill(exc.usage, prompt_chars, len(raw))
                self._log(sent, None, error=f"{type(exc).__name__}: {exc}")
                if self.abort is None:
                    self.messages.append({"role": "user", "content": self._unusable_text(exc)})
                return None
            self.turns += 1
            completion = len(result.content or "") + sum(
                len(json.dumps(call.arguments)) for call in result.tool_calls
            )
            self._bill(result.usage, prompt_chars, completion)
            self._log(sent, result)
            if self.abort is not None:
                return None
            if result.finish_reason == "length" or (
                result.finish_reason == "content_filter" and not result.tool_calls
            ):
                # Not usable: a tool call cut off by the length limit may be incomplete in
                # meaning even when its JSON parses, and an empty reply has nothing to act on.
                # The reply is not echoed into the history, so no tool call is left unanswered.
                self.messages.append({"role": "user", "content": self._discarded_text(result)})
                return None
            self.messages.append(assistant_message(result))
            self.logged = len(self.messages)  # the echo is already in the transcript as the reply
            return result

    @staticmethod
    def _discarded_text(result: ChatResult) -> str:
        if result.finish_reason == "length":
            return (
                "Your last reply was cut off at the output limit and was discarded, so none of "
                "it was done. " + CUT_OFF_ADVICE
            )
        return (
            "Your last reply was withheld by the content filter and was discarded. Reply "
            "again with a shorter, plainer answer or a tool call."
        )

    @staticmethod
    def _unusable_text(exc: LLMError) -> str:
        if isinstance(exc, TruncatedOutputError):
            return (
                "Your last reply could not be used: it was cut off at the output limit. "
                + CUT_OFF_ADVICE
            )
        return (
            f"Your last reply could not be used ({type(exc).__name__}: {_short(exc)}). Reply "
            "again with a valid tool call or a final answer."
        )

    # ------------------------------------------------------------------ the loop

    def run(self) -> StageOutcome:
        self._start_attempt()
        opening = f"{self.stage.opening_message(self.ctx)}\n\n{self._attempt_note()}"
        self.messages = [
            {"role": "system", "content": self.stage.system_prompt()},
            {"role": "user", "content": opening},
        ]
        while True:
            if self.turns >= self.limits.max_turns_per_attempt:
                return self._fail(
                    f"stage {self.stage.name!r}: {self.turns} model turns passed without a "
                    "passing check (max_turns_per_attempt)"
                )
            result = self._ask()
            if self.abort is not None:
                return self._fail(self.abort)
            if result is None:
                continue
            if not result.tool_calls:
                text = self._check()
                if self.passed or self.exhausted or self.abort is not None:
                    break
                self.messages.append(
                    {
                        "role": "user",
                        "content": "You replied without calling a tool, so the checks were run "
                        f"for you.\n\n{text}",
                    }
                )
                continue
            ran_gate = False  # at most one gate run per reply
            for call in result.tool_calls:
                before = self.attempts
                output = self._answer(call, ran_gate)
                ran_gate = ran_gate or self.attempts != before
                self.messages.append({"role": "tool", "tool_call_id": call.id, "content": output})
            if self.passed or self.exhausted or self.abort is not None:
                break
        if self.abort is not None:
            return self._fail(self.abort)
        if self.passed:
            with _internal("promoting the accepted attempt"):
                self.stage.promote(self.ctx, self.attempt_dir)
            self.record.ok = True
            self._progress()
            return StageOutcome(ok=True, attempts=self.attempts, errors=[], reason=None)
        shown = _short("; ".join(self.last_errors), MAX_REASON_CHARS - 200)
        return self._fail(
            f"stage {self.stage.name!r} failed after {self.attempts} attempts; last errors: {shown}"
        )

    def _answer(self, call: ToolCall, ran_gate: bool) -> str:
        """The tool result for one call of a reply. A call that cannot run is still answered."""
        if self.abort is not None:  # only the wall-clock limit can stop a stage while tools run
            return "not run: wall-clock limit reached"
        if self.passed or self.exhausted:
            return "not run: the stage has already ended"
        if self._time_left() <= 0:
            self.abort = self._wall_clock_reason()
            return "not run: wall-clock limit reached"
        if call.name == CHECK_TOOL and ran_gate:
            return (
                "not run: the checks already ran in this reply; work in "
                f"{self._relative(self.attempt_dir)}"
            )
        return self.registry.dispatch(call)

    def _fail(self, reason: str) -> StageOutcome:
        self.record.ok = False
        return StageOutcome(
            ok=False,
            attempts=self.attempts,
            errors=list(self.last_errors),
            reason=redact(reason, MAX_REASON_CHARS),
        )


def run_stage(stage: Stage, ctx: StageContext) -> StageOutcome:
    """Run ``stage`` to its end; see the module docstring."""
    return _StageRun(stage, ctx).run()
