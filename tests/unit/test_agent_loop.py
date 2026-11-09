"""Unit tests for the gated stage loop and the transcript, with a scripted model and a toy stage."""

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from cfdagent.agents import loop
from cfdagent.agents.loop import StageOutcome, run_stage
from cfdagent.agents.stage import Stage, StageContext
from cfdagent.agents.tools import FileScope, NoArgs, Tool
from cfdagent.agents.transcript import TRANSCRIPT_FILE, TranscriptWriter
from cfdagent.config import RunConfig, RunLimits
from cfdagent.errors import AgentInputError, ToolFailure
from cfdagent.gates import GateResult
from cfdagent.llm.client import (
    UNPARSED_KEY,
    ChatResult,
    EmptyChoicesError,
    LLMTransportError,
    MalformedReplyError,
    SchemaError,
    StructuredOutputError,
    ToolCall,
    TruncatedOutputError,
    Usage,
)
from cfdagent.llm.cost import PRICING, CostTracker
from cfdagent.llm.scripted import ScriptedBackend
from cfdagent.record import RunRecord
from cfdagent.rundir import RunDir, RunDirError, UnsafePath

USAGE = Usage(100, 50, None)


def reply(content=None, *calls, usage=USAGE, finish_reason=None) -> ChatResult:
    return ChatResult(
        content=content,
        tool_calls=list(calls),
        usage=usage,
        parsed=None,
        raw={"secret_wire_format": "must not be stored"},
        finish_reason=finish_reason or ("tool_calls" if calls else "stop"),
    )


def tc(name: str, call_id: str = "c1", **arguments) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments)


def check(call_id: str = "k1") -> ToolCall:
    return tc("check_stage", call_id)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class ToyStage(Stage):
    name = "toy"
    agent = "planner"
    scope = FileScope(readable=["attempts/toy-*/*"], writable=["attempts/toy-*/*"])

    def __init__(self, gates, clock: FakeClock | None = None, gate_seconds: float = 0.0):
        self.gates = list(gates)
        self.clock = clock
        self.gate_seconds = gate_seconds
        self.gate_dirs: list[Path] = []
        self.promoted: list[Path] = []
        self.extra_calls: list[Path] = []

    def system_prompt(self) -> str:
        return "SYSTEM PROMPT"

    def opening_message(self, ctx) -> str:
        return "Do the toy task."

    def extra_tools(self, ctx, attempt_dir):
        self.extra_calls.append(attempt_dir)

        def slow(_args) -> str:
            if self.clock is not None:
                self.clock.now += 200
            return "slept"

        def big(_args) -> str:
            return "B" * 3000

        return [
            Tool("big", "Return a lot of text.", NoArgs, big),
            Tool("slow", "Take a long time.", NoArgs, slow),
        ]

    def execute_and_gate(self, ctx, attempt_dir) -> GateResult:
        self.gate_dirs.append(attempt_dir)
        if self.clock is not None:
            self.clock.now += self.gate_seconds
        outcome = self.gates.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def promote(self, ctx, attempt_dir) -> None:
        self.promoted.append(attempt_dir)


def make_ctx(
    tmp_path,
    steps,
    *,
    limits: RunLimits | None = None,
    cost_limit: float = 1.0,
    clock: FakeClock | None = None,
    sleeps: list | None = None,
    progress: list | None = None,
):
    root = tmp_path / "run"
    root.mkdir(parents=True, exist_ok=True)
    run = RunDir(root)
    config = RunConfig.default()
    if limits is not None:
        config = config.model_copy(update={"limits": limits})
    cost = CostTracker(cost_limit, PRICING["gpt-oss-120b"])
    record = RunRecord(
        run_id="20261112-143005-demo",
        task_set="ad hoc",
        task_sha256="a" * 64,
        started_at=datetime(2026, 11, 12, 14, 30, 5, tzinfo=UTC),
        status="running",
        cfdagent_version="0.0.1",
    )
    backend = ScriptedBackend(steps)
    extra = {"started_at": (clock or time.monotonic)()}
    if clock is not None:
        extra["clock"] = clock
    if sleeps is not None:
        extra["sleep"] = sleeps.append
    if progress is not None:
        extra["on_progress"] = progress.append
    ctx = StageContext(
        run=run,
        config=config,
        backend=backend,
        sandbox=SimpleNamespace(),  # the toy stage never calls the sandbox
        cost=cost,
        record=record,
        transcript=TranscriptWriter(run, cost),
        **extra,
    )
    return ctx, backend


def tool_messages(call_index: int, backend) -> list[dict]:
    return [m for m in backend.calls[call_index].messages if m["role"] == "tool"]


# ------------------------------------------------------------------ stage control flow


def test_loop_passes_on_first_check(tmp_path):
    steps = [
        reply(None, tc("write_file", path="attempts/toy-1/s.py", content="x = 1\n")),
        reply(None, check()),
        reply("never requested"),
    ]
    ctx, backend = make_ctx(tmp_path, steps)
    stage = ToyStage([GateResult(True, [])])
    outcome = run_stage(stage, ctx)
    assert outcome == StageOutcome(ok=True, attempts=1, errors=[], reason=None)
    assert len(backend.calls) == 2
    assert (ctx.run.root / "attempts" / "toy-1" / "s.py").read_text(encoding="utf-8") == "x = 1\n"
    assert stage.gate_dirs == [ctx.run.root / "attempts" / "toy-1"]
    assert stage.promoted == stage.gate_dirs  # promote is called with the attempt that passed
    first = backend.calls[0]
    assert first.messages[0] == {"role": "system", "content": "SYSTEM PROMPT"}
    assert first.messages[1]["role"] == "user"
    assert first.messages[1]["content"].startswith("Do the toy task.")
    assert "attempts/toy-1" in first.messages[1]["content"]
    names = {t["function"]["name"] for t in first.tools}
    assert {"check_stage", "calculator", "read_file", "list_dir", "write_file", "big"} <= names
    assert backend.calls[0].model.deployment == "gpt-oss-120b"
    (stage_record,) = ctx.record.stages
    assert stage_record.name == "toy" and stage_record.ok
    assert [(a.n, a.ok, a.errors) for a in stage_record.attempts] == [(1, True, [])]


def test_loop_feeds_gate_errors_back_then_passes(tmp_path):
    steps = [reply(None, check("k1")), reply(None, check("k2"))]
    ctx, backend = make_ctx(tmp_path, steps)
    stage = ToyStage(
        [GateResult(False, ["solid is not valid", "no manifest"]), GateResult(True, [])]
    )
    outcome = run_stage(stage, ctx)
    assert outcome.ok and outcome.attempts == 2
    (result,) = tool_messages(1, backend)
    assert result["tool_call_id"] == "k1"
    assert "solid is not valid" in result["content"] and "no manifest" in result["content"]
    assert "attempts/toy-2" in result["content"]  # the model is told where to write now
    assert [d.name for d in stage.gate_dirs] == ["toy-1", "toy-2"]
    assert [d.name for d in stage.extra_calls] == ["toy-1", "toy-2"]  # tools rebuilt per attempt
    (stage_record,) = ctx.record.stages
    assert [(a.n, a.ok) for a in stage_record.attempts] == [(1, False), (2, True)]
    assert stage_record.attempts[0].errors == ["solid is not valid", "no manifest"]
    assert stage_record.ok


def test_gate_errors_returned_to_the_model_are_capped_at_4_kb(tmp_path):
    steps = [reply(None, check())]
    ctx, backend = make_ctx(tmp_path, steps + [reply(None, check())])
    stage = ToyStage([GateResult(False, ["é" * 5000]), GateResult(True, [])])
    run_stage(stage, ctx)
    (result,) = tool_messages(1, backend)
    assert 4000 < len(result["content"].encode("utf-8")) < 5000
    assert "\ufffd" not in result["content"] and "truncated" in result["content"]


def test_loop_stops_after_three_failed_attempts(tmp_path):
    steps = [reply(None, check(f"k{i}")) for i in range(1, 6)]
    ctx, backend = make_ctx(tmp_path, steps)
    gates = [GateResult(False, [f"failure {i}"]) for i in range(1, 6)]
    stage = ToyStage(gates)
    outcome = run_stage(stage, ctx)
    assert not outcome.ok and outcome.attempts == 3
    assert outcome.errors == ["failure 3"]
    assert "toy" in outcome.reason and "3 attempts" in outcome.reason
    assert "failure 3" in outcome.reason
    assert len(backend.calls) == 3  # no model call after the last failed attempt
    assert stage.promoted == []
    (stage_record,) = ctx.record.stages
    assert not stage_record.ok and len(stage_record.attempts) == 3


def test_attempt_limit_comes_from_the_configuration(tmp_path):
    steps = [reply(None, check(f"k{i}")) for i in range(1, 4)]
    ctx, backend = make_ctx(tmp_path, steps, limits=RunLimits(max_attempts_per_stage=1))
    outcome = run_stage(ToyStage([GateResult(False, ["bad"])]), ctx)
    assert not outcome.ok and outcome.attempts == 1 and len(backend.calls) == 1


def test_loop_runs_gate_itself_when_agent_stops_calling_tools(tmp_path):
    steps = [reply("I am done."), reply("Fixed it, really.")]
    ctx, backend = make_ctx(tmp_path, steps)
    stage = ToyStage([GateResult(False, ["output file is missing"]), GateResult(True, [])])
    outcome = run_stage(stage, ctx)
    assert outcome.ok and outcome.attempts == 2
    last = backend.calls[1].messages[-1]
    assert last["role"] == "user"
    assert "output file is missing" in last["content"]
    assert backend.calls[1].messages[-2]["role"] == "assistant"


def test_one_turn_reply_without_tools_then_exhausted_attempts_end_the_stage(tmp_path):
    steps = [reply("done 1"), reply("done 2"), reply("done 3"), reply("never")]
    ctx, backend = make_ctx(tmp_path, steps)
    stage = ToyStage([GateResult(False, [f"e{i}"]) for i in range(3)])
    outcome = run_stage(stage, ctx)
    assert not outcome.ok and outcome.attempts == 3 and len(backend.calls) == 3


@pytest.mark.parametrize("blank", [None, "", " \n "], ids=["none", "empty", "whitespace"])
def test_a_blank_reply_without_tool_calls_is_nudged_not_checked(tmp_path, blank):
    steps = [reply(blank), reply(None, check("k1"))]
    ctx, backend = make_ctx(tmp_path, steps)
    stage = ToyStage([GateResult(True, [])])
    outcome = run_stage(stage, ctx)
    assert outcome.ok and outcome.attempts == 1  # the nudge used no attempt
    assert len(stage.gate_dirs) == 1  # the gate ran once, when the model asked for it
    nudge = backend.calls[1].messages[-1]
    assert nudge["role"] == "user"
    assert "no text and no tool call" in nudge["content"] and "check_stage" in nudge["content"]
    assert "checks were run for you" not in nudge["content"]


def test_blank_replies_beyond_the_nudge_limit_fall_back_to_running_the_gate(tmp_path):
    n = loop.MAX_BLANK_NUDGES
    steps = [reply(None) for _ in range(n + 1)] + [reply(None, check("k1"))]
    ctx, backend = make_ctx(tmp_path, steps)
    stage = ToyStage([GateResult(False, ["nothing written"]), GateResult(True, [])])
    outcome = run_stage(stage, ctx)
    assert outcome.ok and outcome.attempts == 2
    # n nudges, then the next blank reply is treated as "done": the gate ran for the model
    ran_for_model = backend.calls[n + 1].messages[-1]
    assert "checks were run for you" in ran_for_model["content"]
    assert "nothing written" in ran_for_model["content"]


def test_a_tool_call_resets_the_count_of_blank_replies(tmp_path):
    n = loop.MAX_BLANK_NUDGES
    steps = [reply(None) for _ in range(n)]
    steps += [reply(None, tc("calculator", "c1", expression="1+1"))]
    steps += [reply(None) for _ in range(n)]
    steps += [reply(None, check("k1"))]
    ctx, backend = make_ctx(tmp_path, steps)
    stage = ToyStage([GateResult(True, [])])
    outcome = run_stage(stage, ctx)
    assert outcome.ok and outcome.attempts == 1 and len(stage.gate_dirs) == 1
    # A gate run before the model asked for it would end the stage early, leaving steps unused.
    assert len(backend.calls) == len(steps)


def test_the_count_of_blank_replies_starts_again_after_the_gate_ran_for_the_model(tmp_path):
    n = loop.MAX_BLANK_NUDGES
    steps = [reply(None) for _ in range(n + 1)]  # n nudges, then the gate runs for the model
    steps += [reply(None) for _ in range(n)]  # nudged again in attempt 2
    steps += [reply(None, check("k1"))]
    ctx, backend = make_ctx(tmp_path, steps)
    stage = ToyStage([GateResult(False, ["x"]), GateResult(True, [])])
    outcome = run_stage(stage, ctx)
    assert outcome.ok and outcome.attempts == 2 and len(backend.calls) == len(steps)


def test_a_reply_with_text_and_no_tool_call_still_runs_the_gate(tmp_path):
    ctx, backend = make_ctx(tmp_path, [reply("All done."), reply(None, check("k1"))])
    stage = ToyStage([GateResult(False, ["e"]), GateResult(True, [])])
    outcome = run_stage(stage, ctx)
    assert outcome.ok and outcome.attempts == 2
    assert "checks were run for you" in backend.calls[1].messages[-1]["content"]


def test_loop_stops_at_turn_limit(tmp_path):
    steps = [reply(None, tc("calculator", f"c{i}", expression="1+1")) for i in range(6)]
    ctx, backend = make_ctx(tmp_path, steps, limits=RunLimits(max_turns_per_attempt=3))
    stage = ToyStage([])
    outcome = run_stage(stage, ctx)
    assert not outcome.ok and outcome.attempts == 0
    assert len(backend.calls) == 3
    assert "3" in outcome.reason and "turns" in outcome.reason and "toy" in outcome.reason
    assert stage.gate_dirs == []


def test_nudged_blank_replies_do_not_use_up_the_turn_limit(tmp_path):
    # Without the refund the two blank replies would take 2 of the 3 turns and the check would
    # never be reached.
    steps = [reply(None), reply(None)]
    steps += [reply(None, tc("calculator", f"c{i}", expression="1+1")) for i in range(2)]
    steps += [reply(None, check("k1"))]
    ctx, backend = make_ctx(tmp_path, steps, limits=RunLimits(max_turns_per_attempt=3))
    outcome = run_stage(ToyStage([GateResult(True, [])]), ctx)
    assert outcome.ok and len(backend.calls) == 5


def test_blank_replies_between_tool_calls_never_extend_the_tool_turn_limit(tmp_path):
    steps = []
    for i in range(6):
        steps += [reply(None), reply(None, tc("calculator", f"c{i}", expression="1+1"))]
    ctx, backend = make_ctx(tmp_path, steps, limits=RunLimits(max_turns_per_attempt=3))
    outcome = run_stage(ToyStage([]), ctx)
    assert not outcome.ok and "max_turns_per_attempt" in outcome.reason
    assert len(backend.calls) == 6  # three tool turns, each preceded by a nudged blank reply


def test_a_model_that_only_replies_blank_still_ends_the_stage(tmp_path):
    n = loop.MAX_BLANK_NUDGES
    attempts = 3
    steps = [reply(None) for _ in range(attempts * (n + 1) + 5)]
    ctx, backend = make_ctx(tmp_path, steps, limits=RunLimits(max_turns_per_attempt=1))
    stage = ToyStage([GateResult(False, [f"e{i}"]) for i in range(attempts)])
    outcome = run_stage(stage, ctx)
    assert not outcome.ok and outcome.attempts == attempts
    assert len(backend.calls) == attempts * (n + 1)  # the nudge cap, not the turn limit, ends it


def test_turn_counter_restarts_with_each_attempt(tmp_path):
    steps = [
        reply(None, tc("calculator", "c1", expression="1")),
        reply(None, check("k1")),
        reply(None, tc("calculator", "c2", expression="2")),
        reply(None, check("k2")),
    ]
    ctx, backend = make_ctx(tmp_path, steps, limits=RunLimits(max_turns_per_attempt=2))
    outcome = run_stage(ToyStage([GateResult(False, ["x"]), GateResult(True, [])]), ctx)
    assert outcome.ok and len(backend.calls) == 4


@pytest.mark.parametrize(
    "error",
    [
        TruncatedOutputError("{partial", usage=Usage(10, 20, None)),
        EmptyChoicesError("no choices", usage=Usage(10, 0, None)),
        MalformedReplyError("no message", usage=None),
        StructuredOutputError("bad", "raw", usage=Usage(5, 5, None)),
    ],
    ids=lambda e: type(e).__name__,
)
def test_unusable_reply_costs_one_turn_not_one_attempt(tmp_path, error):
    steps = [error, reply(None, check())]
    ctx, backend = make_ctx(tmp_path, steps)
    outcome = run_stage(ToyStage([GateResult(True, [])]), ctx)
    assert outcome.ok and outcome.attempts == 1  # the failed reply was not a gate attempt
    last = backend.calls[1].messages[-1]
    assert last["role"] == "user" and "could not be used" in last["content"]
    assert ctx.cost.tokens_in > 0 or ctx.cost.estimated  # the failed call was billed


def test_truncated_reply_counts_against_the_turn_limit(tmp_path):
    steps = [TruncatedOutputError("{x", usage=USAGE), reply(None, tc("big", "b1"))]
    ctx, backend = make_ctx(tmp_path, steps, limits=RunLimits(max_turns_per_attempt=2))
    outcome = run_stage(ToyStage([]), ctx)
    assert not outcome.ok and len(backend.calls) == 2 and "turns" in outcome.reason
    message = backend.calls[1].messages[-1]["content"]
    assert "cut off" in message


def test_other_llm_errors_are_reported_to_the_model(tmp_path):
    from cfdagent.llm.client import LLMError

    steps = [LLMError("odd failure"), reply(None, check())]
    ctx, backend = make_ctx(tmp_path, steps)
    assert run_stage(ToyStage([GateResult(True, [])]), ctx).ok
    assert "odd failure" in backend.calls[1].messages[-1]["content"]


def test_cost_limit_stops_loop_and_is_recorded(tmp_path):
    big = Usage(1_000_000, 100_000, None)  # about 0.21 USD
    steps = [reply(None, tc("calculator", "c1", expression="1+1"), usage=big), reply("never")]
    ctx, backend = make_ctx(tmp_path, steps, cost_limit=0.1)
    stage = ToyStage([])
    outcome = run_stage(stage, ctx)
    assert not outcome.ok and "cost limit" in outcome.reason
    assert len(backend.calls) == 1
    assert ctx.cost.total_usd > 0.1  # the overrun is visible in the totals
    assert stage.gate_dirs == []
    (line,) = read_transcript(ctx)
    assert line["cost_usd"] == pytest.approx(ctx.cost.total_usd)
    assert not ctx.record.stages[0].ok


def test_cost_limit_during_a_failed_reply_also_stops(tmp_path):
    error = TruncatedOutputError("{x", usage=Usage(10_000_000, 0, None))
    ctx, backend = make_ctx(tmp_path, [error, reply("never")], cost_limit=0.5)
    outcome = run_stage(ToyStage([]), ctx)
    assert not outcome.ok and "cost limit" in outcome.reason and len(backend.calls) == 1


def test_unknown_usage_is_estimated_and_the_stage_still_passes(tmp_path):
    steps = [reply(None, check(), usage=None)]
    ctx, backend = make_ctx(tmp_path, steps)
    assert run_stage(ToyStage([GateResult(True, [])]), ctx).ok
    assert ctx.cost.estimated and ctx.cost.tokens_in > 0 and ctx.cost.total_usd > 0
    (line,) = read_transcript(ctx)
    assert line["usage"] is None and line["cost_estimated"] is True


def test_wall_clock_limit_stops_loop(tmp_path):
    clock = FakeClock()
    steps = [reply(None, check("k1")), reply(None, check("k2"))]
    ctx, backend = make_ctx(tmp_path, steps, limits=RunLimits(wall_clock_s=100), clock=clock)
    stage = ToyStage([GateResult(False, ["slow"]), GateResult(True, [])], clock, gate_seconds=150)
    outcome = run_stage(stage, ctx)
    assert not outcome.ok and "wall-clock" in outcome.reason
    assert outcome.attempts == 1 and len(backend.calls) == 1  # no model call after the deadline
    assert [a.seconds for a in ctx.record.stages[0].attempts] == [150.0]


def test_wall_clock_is_checked_before_a_gate_runs(tmp_path):
    clock = FakeClock()

    class SlowBackend(ScriptedBackend):
        def chat(self, messages, model, *, tools=None, response_schema=None):
            clock.now += 500  # the model call itself takes longer than the whole budget
            return super().chat(messages, model, tools=tools, response_schema=response_schema)

    ctx, _ = make_ctx(tmp_path, [], limits=RunLimits(wall_clock_s=100), clock=clock)
    ctx.backend = SlowBackend([reply(None, check())])
    stage = ToyStage([GateResult(True, [])])
    outcome = run_stage(stage, ctx)
    assert not outcome.ok and "wall-clock" in outcome.reason and stage.gate_dirs == []


def test_remaining_time_is_available_to_stages(tmp_path):
    clock = FakeClock()
    ctx, _ = make_ctx(tmp_path, [], limits=RunLimits(wall_clock_s=100), clock=clock)
    assert ctx.remaining_s() == pytest.approx(100.0)
    clock.now += 70
    assert ctx.remaining_s() == pytest.approx(30.0)
    assert ctx.inner_timeout(600) == pytest.approx(30.0)  # clipped to what is left
    assert ctx.inner_timeout(10) == pytest.approx(10.0)
    clock.now += 500
    assert ctx.remaining_s() == 0.0
    assert ctx.inner_timeout(600) == 1.0  # never zero or negative: sandbox calls refuse that


def test_schema_error_propagates(tmp_path):
    ctx, _ = make_ctx(tmp_path, [SchemaError("not usable")])
    with pytest.raises(SchemaError):
        run_stage(ToyStage([]), ctx)


def test_gate_internal_failures_propagate(tmp_path):
    ctx, _ = make_ctx(tmp_path, [reply(None, check())])
    with pytest.raises(ToolFailure):
        run_stage(ToyStage([ToolFailure("the tool broke")]), ctx)


def test_on_progress_is_called_after_each_attempt(tmp_path):
    progress: list[str] = []
    steps = [reply(None, check("k1")), reply(None, check("k2"))]
    ctx, _ = make_ctx(tmp_path, steps, progress=progress)
    run_stage(ToyStage([GateResult(False, ["x"]), GateResult(True, [])]), ctx)
    assert progress == ["toy", "toy"]


# ------------------------------------------------------------------ transport errors


def test_retryable_transport_error_is_retried_then_succeeds(tmp_path):
    sleeps: list[float] = []
    boom = LLMTransportError("busy", status_code=503, retryable=True)
    steps = [boom, boom, reply(None, check())]
    ctx, backend = make_ctx(
        tmp_path, steps, sleeps=sleeps, limits=RunLimits(max_turns_per_attempt=1)
    )
    assert run_stage(ToyStage([GateResult(True, [])]), ctx).ok
    assert len(backend.calls) == 3
    assert len(sleeps) == 2 and sleeps[0] < sleeps[1]  # backoff grows; nothing really slept


def test_transport_error_fails_the_stage_after_two_retries(tmp_path):
    sleeps: list[float] = []
    boom = LLMTransportError("busy", status_code=503, retryable=True)
    ctx, backend = make_ctx(tmp_path, [boom, boom, boom, reply("never")], sleeps=sleeps)
    outcome = run_stage(ToyStage([]), ctx)
    assert not outcome.ok and "503" in outcome.reason
    assert len(backend.calls) == 3 and len(sleeps) == 2


def test_non_retryable_transport_error_fails_at_once_without_secrets(tmp_path):
    sleeps: list[float] = []
    secret_url = "https://resource-name.services.ai.azure.com/openai/v1/"
    boom = LLMTransportError(
        f"HTTP 401 from the model service: denied at {secret_url}",
        status_code=401,
        retryable=False,
    )
    ctx, backend = make_ctx(tmp_path, [boom, reply("never")], sleeps=sleeps)
    outcome = run_stage(ToyStage([]), ctx)
    assert not outcome.ok and "401" in outcome.reason
    assert "azure.com" not in outcome.reason and "resource-name" not in outcome.reason
    assert len(backend.calls) == 1 and sleeps == []


# ------------------------------------------------------------------ tool handling in the loop


def test_every_tool_call_is_answered_in_order_even_unknown_ones(tmp_path):
    calls = [
        tc("calculator", "a1", expression="2*21"),
        tc("no_such_tool", "a2", x=1),
        tc("calculator", "a3", expression="__import__('os')"),
        ToolCall("a4", "calculator", {"_unparsed": '{"expression": '}),
    ]
    ctx, backend = make_ctx(tmp_path, [reply("thinking", *calls), reply(None, check())])
    assert run_stage(ToyStage([GateResult(True, [])]), ctx).ok
    messages = backend.calls[1].messages
    assistant = messages[-5]
    assert assistant["role"] == "assistant" and len(assistant["tool_calls"]) == 4
    tail = messages[-4:]
    assert [m["tool_call_id"] for m in tail] == ["a1", "a2", "a3", "a4"]
    assert all(m["role"] == "tool" for m in tail)
    assert tail[0]["content"] == "42"
    assert "unknown tool" in tail[1]["content"]
    assert tail[2]["content"].startswith("error:")
    assert "not valid JSON" in tail[3]["content"]


def test_tool_output_over_the_cap_is_cut_in_the_conversation(tmp_path):
    steps = [reply(None, tc("write_file", path="attempts/toy-1/a", content="x" * 100_000))]
    steps += [reply(None, tc("read_file", path="attempts/toy-1/a")), reply(None, check())]
    ctx, backend = make_ctx(tmp_path, steps)
    assert run_stage(ToyStage([GateResult(True, [])]), ctx).ok
    contents = [m["content"] for m in tool_messages(2, backend)]
    assert all(len(c.encode("utf-8")) <= 8000 for c in contents)
    assert "offset=" in contents[-1]


def test_calls_after_a_passing_check_in_the_same_reply_are_answered_but_not_run(tmp_path):
    steps = [
        reply(None, check("k1"), tc("write_file", "w1", path="attempts/toy-1/late", content="x"))
    ]
    ctx, backend = make_ctx(tmp_path, steps)
    stage = ToyStage([GateResult(True, [])])
    assert run_stage(stage, ctx).ok
    assert not (ctx.run.root / "attempts" / "toy-1" / "late").exists()
    assert len(backend.calls) == 1


def test_a_failed_check_in_a_reply_swaps_in_the_next_attempt_for_later_calls(tmp_path):
    steps = [
        reply(None, check("k1"), tc("write_file", "w1", path="attempts/toy-2/s.py", content="y")),
        reply(None, check("k2")),
    ]
    ctx, backend = make_ctx(tmp_path, steps)
    stage = ToyStage([GateResult(False, ["bad"]), GateResult(True, [])])
    assert run_stage(stage, ctx).ok
    assert (ctx.run.root / "attempts" / "toy-2" / "s.py").read_text(encoding="utf-8") == "y"
    assert [m["tool_call_id"] for m in tool_messages(1, backend)] == ["k1", "w1"]


def test_attempt_directories_are_fresh(tmp_path):
    steps = [
        reply(None, tc("write_file", "w1", path="attempts/toy-1/old.txt", content="old")),
        reply(None, check("k1")),
        reply(None, check("k2")),
    ]
    ctx, _ = make_ctx(tmp_path, steps)
    stage = ToyStage([GateResult(False, ["bad"]), GateResult(True, [])])
    run_stage(stage, ctx)
    assert (ctx.run.root / "attempts" / "toy-1" / "old.txt").exists()
    assert not (ctx.run.root / "attempts" / "toy-2" / "old.txt").exists()


def test_old_tool_outputs_are_elided_when_the_conversation_grows(tmp_path, monkeypatch):
    monkeypatch.setattr(loop, "CONTEXT_CHAR_LIMIT", 10_000)
    steps = [reply(None, tc("big", f"b{i}")) for i in range(8)] + [reply(None, check())]
    ctx, backend = make_ctx(tmp_path, steps)
    assert run_stage(ToyStage([GateResult(True, [])]), ctx).ok
    final = backend.calls[-1].messages
    assert final[0]["content"] == "SYSTEM PROMPT" and final[1]["content"].startswith("Do the toy")
    outputs = [m["content"] for m in final if m["role"] == "tool"]
    assert outputs[0] == "[output elided to save context]"
    assert outputs[-1] == "B" * 3000  # the newest outputs stay intact
    assert len(final) == 2 + 2 * 8
    assert [m["tool_call_id"] for m in final if m["role"] == "tool"] == [f"b{i}" for i in range(8)]


def test_a_small_conversation_is_never_elided(tmp_path):
    steps = [reply(None, tc("big", f"b{i}")) for i in range(4)] + [reply(None, check())]
    ctx, backend = make_ctx(tmp_path, steps)
    run_stage(ToyStage([GateResult(True, [])]), ctx)
    assert all(m["content"] == "B" * 3000 for m in tool_messages(4, backend))


# ------------------------------------------------------------------ transcript


def read_transcript(ctx) -> list[dict]:
    text = (ctx.run.root / TRANSCRIPT_FILE).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def test_transcript_has_one_line_per_call(tmp_path):
    boom = LLMTransportError("busy", status_code=503, retryable=True)
    steps = [
        boom,
        reply("hello", tc("calculator", "c1", expression="1+1"), usage=USAGE),
        TruncatedOutputError("{x", usage=USAGE),
        reply(None, check(), usage=None),
    ]
    ctx, backend = make_ctx(tmp_path, steps, sleeps=[])
    assert run_stage(ToyStage([GateResult(True, [])]), ctx).ok
    lines = read_transcript(ctx)
    assert len(lines) == len(backend.calls) == 4
    assert [line["error"] is not None for line in lines] == [True, False, True, False]
    for line in lines:
        assert line["stage"] == "toy" and line["agent"] == "planner"
        assert line["attempt"] == 1 and line["model"] == "gpt-oss-120b"
        assert "raw" not in line and "must not be stored" not in json.dumps(line)
    ok_line = lines[1]
    assert ok_line["reply"] == {
        "content": "hello",
        "tool_calls": [{"id": "c1", "name": "calculator", "arguments": '{"expression": "1+1"}'}],
        "finish_reason": "tool_calls",
    }
    assert ok_line["usage"] == {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "reasoning_tokens": None,
    }
    assert ok_line["cost_usd"] > 0 and ok_line["cost_estimated"] is False
    assert lines[3]["usage"] is None and lines[3]["cost_estimated"] is True
    # new messages are only those added since the previous call
    assert [m["role"] for m in lines[0]["new_messages"]] == ["system", "user"]
    assert lines[1]["new_messages"] == []
    assert [m["role"] for m in lines[2]["new_messages"]] == ["tool"]
    assert [m["role"] for m in lines[3]["new_messages"]] == ["user"]
    assert lines[2]["new_messages"][0]["tool_call_id"] == "c1"


def test_transcript_attempt_number_follows_the_gate_attempts(tmp_path):
    steps = [reply(None, check("k1")), reply(None, check("k2"))]
    ctx, _ = make_ctx(tmp_path, steps)
    run_stage(ToyStage([GateResult(False, ["x"]), GateResult(True, [])]), ctx)
    assert [line["attempt"] for line in read_transcript(ctx)] == [1, 2]


def test_transcript_redacts_and_bounds_lines(tmp_path):
    run = RunDir(tmp_path)
    writer = TranscriptWriter(run)
    url = "https://resource-name.services.ai.azure.com/openai/v1/"
    huge = "z" * 900_000
    messages = [
        {"role": "user", "content": f"endpoint {url} for you@example.org"},
        {"role": "tool", "tool_call_id": "t", "content": huge},
    ]
    writer.append(
        "toy", "planner", 1, "m", messages, reply(huge, tc("write_file", path="a", content=huge))
    )
    raw = (tmp_path / TRANSCRIPT_FILE).read_bytes()
    assert raw.endswith(b"\n") and raw.count(b"\n") == 1
    assert len(raw) < 600_000
    text = raw.decode("utf-8")
    assert "azure.com" not in text and "example.org" not in text
    line = json.loads(text)
    assert "<url>" in line["new_messages"][0]["content"]
    writer.append("toy", "planner", 1, "m", [], reply("second"))
    assert (tmp_path / TRANSCRIPT_FILE).read_bytes().count(b"\n") == 2  # appended, not replaced


def test_transcript_refuses_a_symbolic_link(tmp_path):
    target = tmp_path / "elsewhere.jsonl"
    target.write_text("keep\n", encoding="utf-8")
    run_root = tmp_path / "run"
    run_root.mkdir()
    try:
        os.symlink(target, run_root / TRANSCRIPT_FILE)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available here")
    writer = TranscriptWriter(RunDir(run_root))
    with pytest.raises(UnsafePath, match="symbolic link"):
        writer.append("toy", "planner", 1, "m", [], reply("x"))
    assert target.read_text(encoding="utf-8") == "keep\n"


# ------------------------------------------------------------------ fix round 1


def test_at_most_one_gate_run_per_model_reply(tmp_path):
    steps = [reply(None, check("k1"), check("k2"), check("k3")), reply(None, check("k4"))]
    ctx, backend = make_ctx(tmp_path, steps)
    stage = ToyStage([GateResult(False, ["bad"]), GateResult(True, [])])
    outcome = run_stage(stage, ctx)
    assert outcome.ok and outcome.attempts == 2 and len(stage.gate_dirs) == 2
    first, second, third = tool_messages(1, backend)
    assert "bad" in first["content"]
    for later in (second, third):
        assert later["content"].startswith("not run: the checks already ran in this reply")
        assert "attempts/toy-2/" in later["content"]


def test_a_check_with_unusable_arguments_does_not_use_up_the_reply_check(tmp_path):
    junk = ToolCall("k1", "check_stage", {UNPARSED_KEY: "[1"})
    steps = [reply(None, junk, check("k2"))]
    ctx, backend = make_ctx(tmp_path, steps)
    stage = ToyStage([GateResult(True, [])])
    assert run_stage(stage, ctx).ok and len(stage.gate_dirs) == 1


def test_null_arguments_are_fine_for_check_stage(tmp_path):
    null = ToolCall("k1", "check_stage", {UNPARSED_KEY: "null"})
    ctx, _ = make_ctx(tmp_path, [reply(None, null)])
    assert run_stage(ToyStage([GateResult(True, [])]), ctx).ok


def test_gate_raising_agent_input_error_is_a_failed_attempt(tmp_path):
    steps = [reply(None, check("k1")), reply(None, check("k2"))]
    ctx, backend = make_ctx(tmp_path, steps)
    stage = ToyStage([AgentInputError("the script is broken"), GateResult(True, [])])
    outcome = run_stage(stage, ctx)
    assert outcome.ok and outcome.attempts == 2
    (result,) = tool_messages(1, backend)
    assert "the script is broken" in result["content"]
    attempts = ctx.record.stages[0].attempts
    assert [(a.n, a.ok) for a in attempts] == [(1, False), (2, True)]
    assert attempts[0].errors == ["the script is broken"]


def test_gate_raising_agent_input_error_on_the_orchestrator_path(tmp_path):
    steps = [reply("done"), reply(None, check())]
    ctx, backend = make_ctx(tmp_path, steps)
    stage = ToyStage([AgentInputError("manifest is unreadable"), GateResult(True, [])])
    outcome = run_stage(stage, ctx)
    assert outcome.ok and outcome.attempts == 2
    assert "manifest is unreadable" in backend.calls[1].messages[-1]["content"]


def boom_progress(_name):
    raise RunDirError("cannot write the record")


def test_bookkeeping_errors_propagate_as_tool_failure_on_the_tool_path(tmp_path):
    ctx, backend = make_ctx(tmp_path, [reply(None, check()), reply("never")])
    ctx.on_progress = boom_progress
    with pytest.raises(ToolFailure, match="cannot write the record"):
        run_stage(ToyStage([GateResult(False, ["bad"])]), ctx)
    assert len(backend.calls) == 1  # nothing was shown to the model


def test_bookkeeping_errors_propagate_as_tool_failure_on_the_orchestrator_path(tmp_path):
    ctx, _ = make_ctx(tmp_path, [reply("done")])
    ctx.on_progress = boom_progress
    with pytest.raises(ToolFailure):
        run_stage(ToyStage([GateResult(False, ["bad"])]), ctx)


def test_bookkeeping_errors_after_a_pass_propagate_as_tool_failure(tmp_path):
    ctx, _ = make_ctx(tmp_path, [reply(None, check())])
    ctx.on_progress = boom_progress
    with pytest.raises(ToolFailure):
        run_stage(ToyStage([GateResult(True, [])]), ctx)


def test_promote_errors_propagate_as_tool_failure(tmp_path):
    class BadPromote(ToyStage):
        def promote(self, ctx, attempt_dir):
            raise AgentInputError("cannot copy")

    ctx, _ = make_ctx(tmp_path, [reply(None, check())])
    with pytest.raises(ToolFailure, match="cannot copy"):
        run_stage(BadPromote([GateResult(True, [])]), ctx)


def test_attempt_directory_errors_propagate_as_tool_failure(tmp_path, monkeypatch):
    ctx, _ = make_ctx(tmp_path, [reply(None, check())])

    def refuse(stage, attempt):
        raise RunDirError("cannot prepare the directory")

    monkeypatch.setattr(ctx.run, "attempt_dir", refuse)
    with pytest.raises(ToolFailure):
        run_stage(ToyStage([GateResult(True, [])]), ctx)


@pytest.mark.parametrize("calls", [[], [tc("calculator", "c1", expression="1+1")]])
def test_a_reply_cut_off_at_the_output_limit_is_discarded(tmp_path, calls):
    cut = reply("I will now write", *calls, finish_reason="length")
    steps = [cut, reply(None, check())]
    ctx, backend = make_ctx(tmp_path, steps)
    stage = ToyStage([GateResult(True, [])])
    outcome = run_stage(stage, ctx)
    assert outcome.ok and outcome.attempts == 1 and len(stage.gate_dirs) == 1  # no gate run for it
    history = backend.calls[1].messages
    last = history[-1]
    assert last["role"] == "user" and "cut off at the output limit" in last["content"]
    assert "SMALLER" in last["content"] and "several smaller files" in last["content"]
    assert "write large files" not in last["content"]
    assert all(m["role"] != "tool" and not m.get("tool_calls") for m in history)
    assert ctx.cost.tokens_in > 0  # the reply was billed


def test_a_cut_off_tool_call_gets_the_same_message(tmp_path):
    torn = ToolCall("w1", "write_file", {UNPARSED_KEY: '{"path": "attempts/toy-1/a", "con'})
    steps = [reply(None, torn, finish_reason="length"), reply(None, check())]
    ctx, backend = make_ctx(tmp_path, steps, limits=RunLimits(max_turns_per_attempt=2))
    assert run_stage(ToyStage([GateResult(True, [])]), ctx).ok
    assert "SMALLER" in backend.calls[1].messages[-1]["content"]


def test_a_cut_off_reply_counts_as_a_turn(tmp_path):
    steps = [reply("cut", finish_reason="length"), reply("cut", finish_reason="length")]
    ctx, backend = make_ctx(tmp_path, steps, limits=RunLimits(max_turns_per_attempt=2))
    stage = ToyStage([])
    outcome = run_stage(stage, ctx)
    assert not outcome.ok and outcome.attempts == 0 and "turns" in outcome.reason
    assert stage.gate_dirs == []


def test_a_withheld_reply_without_tool_calls_is_not_checked(tmp_path):
    steps = [reply(None, finish_reason="content_filter"), reply(None, check())]
    ctx, backend = make_ctx(tmp_path, steps)
    stage = ToyStage([GateResult(True, [])])
    assert run_stage(stage, ctx).ok and len(stage.gate_dirs) == 1
    last = backend.calls[1].messages[-1]
    assert last["role"] == "user" and "content filter" in last["content"]


def test_a_truncated_structured_error_uses_the_same_advice(tmp_path):
    steps = [TruncatedOutputError("{x", usage=USAGE), reply(None, check())]
    ctx, backend = make_ctx(tmp_path, steps)
    run_stage(ToyStage([GateResult(True, [])]), ctx)
    assert "several smaller files" in backend.calls[1].messages[-1]["content"]


def test_wall_clock_is_checked_before_each_tool_in_a_reply(tmp_path):
    clock = FakeClock()
    calls = [tc("slow", "s1"), tc("calculator", "c1", expression="1+1"), tc("slow", "s2")]
    ctx, backend = make_ctx(
        tmp_path,
        [reply(None, *calls), reply("never")],
        limits=RunLimits(wall_clock_s=100),
        clock=clock,
    )
    stage = ToyStage([GateResult(True, [])], clock)
    runner = loop._StageRun(stage, ctx)
    outcome = runner.run()
    assert not outcome.ok and "wall-clock" in outcome.reason
    assert len(backend.calls) == 1 and stage.gate_dirs == []
    answers = [m for m in runner.messages if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in answers] == ["s1", "c1", "s2"]
    assert answers[0]["content"] == "slept"
    assert answers[1]["content"] == "not run: wall-clock limit reached"
    assert answers[2]["content"] == "not run: wall-clock limit reached"


def test_the_attempt_note_and_directory_listing_agree_on_trailing_slashes(tmp_path):
    steps = [
        reply(None, tc("write_file", "w1", path="attempts/toy-1/a.txt", content="a")),
        reply(None, tc("list_dir", "l1", path="attempts/toy-1/")),
        reply(None, check()),
    ]
    ctx, backend = make_ctx(tmp_path, steps)
    run_stage(ToyStage([GateResult(True, [])]), ctx)
    assert "attempts/toy-1/" in backend.calls[0].messages[1]["content"]
    listing = [m for m in backend.calls[2].messages if m.get("tool_call_id") == "l1"]
    assert listing[0]["content"] == "a.txt"


# ------------------------------------------------------------------ the prepare_attempt hook


def test_prepare_attempt_runs_for_every_fresh_attempt_directory(tmp_path):
    class Preparing(ToyStage):
        def __init__(self, gates):
            super().__init__(gates)
            self.prepared: list[tuple[Path, list[str]]] = []

        def prepare_attempt(self, ctx, attempt_dir):
            self.prepared.append((attempt_dir, sorted(p.name for p in attempt_dir.iterdir())))
            (attempt_dir / "input.txt").write_text("seeded", encoding="utf-8")

    steps = [reply(None, check("k1")), reply(None, check("k2"))]
    ctx, backend = make_ctx(tmp_path, steps)
    stage = Preparing([GateResult(False, ["no"]), GateResult(True, [])])
    assert run_stage(stage, ctx).ok
    assert [d.name for d, _ in stage.prepared] == ["toy-1", "toy-2"]
    assert all(
        listing == [] for _, listing in stage.prepared
    )  # called on the fresh, empty directory
    assert (ctx.run.root / "attempts" / "toy-2" / "input.txt").read_text() == "seeded"
    assert stage.prepared[0][0] == stage.gate_dirs[0]


def test_prepare_attempt_defaults_to_doing_nothing(tmp_path):
    ctx, _ = make_ctx(tmp_path, [reply(None, check())])
    stage = ToyStage([GateResult(True, [])])
    assert stage.prepare_attempt(ctx, ctx.run.attempt_dir("toy", 1)) is None
    assert list((ctx.run.root / "attempts" / "toy-1").iterdir()) == []


def test_prepare_attempt_errors_propagate_as_tool_failure(tmp_path):
    class BrokenInputs(ToyStage):
        def prepare_attempt(self, ctx, attempt_dir):
            raise ToolFailure("the pipeline order is broken")

    ctx, _ = make_ctx(tmp_path, [reply(None, check())])
    with pytest.raises(ToolFailure, match="pipeline order"):
        run_stage(BrokenInputs([GateResult(True, [])]), ctx)

    class BadFiles(ToyStage):
        def prepare_attempt(self, ctx, attempt_dir):
            raise AgentInputError("cannot copy the inputs")

    (tmp_path / "second").mkdir()
    ctx, _ = make_ctx(tmp_path / "second", [reply(None, check())])
    with pytest.raises(ToolFailure, match="cannot copy the inputs"):
        run_stage(BadFiles([GateResult(True, [])]), ctx)


# ------------------------------------------------------------------ the diagnosis hook (R43)

DIAGNOSE = "Explain the cause in two sentences."


class DiagnosingStage(ToyStage):
    """A toy stage that asks for a diagnosis after every failed gate."""

    def __init__(self, gates, **kwargs):
        super().__init__(gates, **kwargs)
        self.requests: list[list[str]] = []

    def diagnosis_request(self, ctx, errors):
        self.requests.append(list(errors))
        return DIAGNOSE


def test_a_stage_without_the_hook_is_never_asked_for_a_diagnosis(tmp_path):
    ctx, backend = make_ctx(tmp_path, [reply(None, check("k1")), reply(None, check("k2"))])
    run_stage(ToyStage([GateResult(False, ["bad"]), GateResult(True, [])]), ctx)
    assert len(backend.calls) == 2
    assert [a.diagnosis for a in ctx.record.stages[0].attempts] == [None, None]


def test_a_diagnosis_is_asked_for_after_a_failed_gate_and_stored_on_that_attempt(tmp_path):
    steps = [reply(None, check("k1")), reply("The cause is X; fix Y."), reply(None, check("k2"))]
    ctx, backend = make_ctx(tmp_path, steps)
    stage = DiagnosingStage([GateResult(False, ["it broke"]), GateResult(True, [])])
    outcome = run_stage(stage, ctx)
    assert outcome.ok and outcome.attempts == 2
    attempts = ctx.record.stages[0].attempts
    assert [a.diagnosis for a in attempts] == ["The cause is X; fix Y.", None]
    assert stage.requests == [["it broke"]]  # not asked after the pass
    asked = backend.calls[1]
    assert asked.tools is None  # the diagnosis turn offers no tools
    assert asked.messages[-1]["role"] == "user" and DIAGNOSE in asked.messages[-1]["content"]
    assert asked.messages[-2]["role"] == "tool" and "it broke" in asked.messages[-2]["content"]
    resumed = backend.calls[2].messages  # the conversation goes on from a user message
    assert resumed[-1]["role"] == "user" and "attempt" in resumed[-1]["content"].lower()
    assert resumed[-2]["role"] == "assistant" and resumed[-2]["content"] == "The cause is X; fix Y."


def test_the_last_failed_attempt_also_gets_one_diagnosis_turn(tmp_path):
    steps = []
    for i in range(1, 4):
        steps += [reply(None, check(f"k{i}")), reply(f"diagnosis {i}")]
    steps.append(reply("never asked"))
    ctx, backend = make_ctx(tmp_path, steps)
    stage = DiagnosingStage([GateResult(False, [f"failure {i}"]) for i in range(1, 4)])
    outcome = run_stage(stage, ctx)
    assert not outcome.ok and outcome.attempts == 3
    assert len(backend.calls) == 6  # three work turns, three diagnosis turns, nothing more
    attempts = ctx.record.stages[0].attempts
    assert [a.diagnosis for a in attempts] == ["diagnosis 1", "diagnosis 2", "diagnosis 3"]
    assert "failure 3" in outcome.reason


def test_a_reply_without_a_tool_call_that_runs_the_gate_is_followed_by_the_diagnosis(tmp_path):
    steps = [reply("I am done."), reply("cause one"), reply("Now fixed.")]
    ctx, backend = make_ctx(tmp_path, steps)
    stage = DiagnosingStage([GateResult(False, ["output missing"]), GateResult(True, [])])
    outcome = run_stage(stage, ctx)
    assert outcome.ok and outcome.attempts == 2
    assert ctx.record.stages[0].attempts[0].diagnosis == "cause one"
    asked = backend.calls[1].messages
    assert "output missing" in asked[-2]["content"] and DIAGNOSE in asked[-1]["content"]
    assert asked[-3]["role"] == "assistant"


def test_the_diagnosis_turn_is_billed_but_does_not_use_a_turn_of_the_next_attempt(tmp_path):
    one_attempt = RunLimits(max_attempts_per_stage=1)
    with_diagnosis, _ = make_ctx(
        tmp_path / "a", [reply(None, check("k1")), reply("because")], limits=one_attempt
    )
    run_stage(DiagnosingStage([GateResult(False, ["x"])]), with_diagnosis)
    without, _ = make_ctx(tmp_path / "b", [reply(None, check("k1"))], limits=one_attempt)
    run_stage(ToyStage([GateResult(False, ["x"])]), without)
    assert with_diagnosis.cost.total_usd > without.cost.total_usd
    # One turn per attempt is enough for "check" in each attempt: the diagnosis turn in between
    # belongs to the failed attempt and leaves the next attempt its whole turn budget.
    steps = [reply(None, check("k1")), reply("because"), reply(None, check("k2"))]
    tight, backend = make_ctx(tmp_path / "tight", steps, limits=RunLimits(max_turns_per_attempt=1))
    outcome = run_stage(DiagnosingStage([GateResult(False, ["x"]), GateResult(True, [])]), tight)
    assert outcome.ok and len(backend.calls) == 3


def test_the_diagnosis_turn_is_logged_under_the_failed_attempt(tmp_path):
    steps = [reply(None, check("k1")), reply("cause one"), reply(None, check("k2"))]
    ctx, _ = make_ctx(tmp_path, steps)
    run_stage(DiagnosingStage([GateResult(False, ["x"]), GateResult(True, [])]), ctx)
    # work of attempt 1, its diagnosis (still attempt 1), work of attempt 2
    assert [line["attempt"] for line in read_transcript(ctx)] == [1, 1, 2]
    steps = []
    for i in range(1, 4):
        steps += [reply(None, check(f"k{i}")), reply(f"diagnosis {i}")]
    ctx, _ = make_ctx(tmp_path / "three", steps)
    run_stage(DiagnosingStage([GateResult(False, ["x"])] * 3), ctx)
    assert [line["attempt"] for line in read_transcript(ctx)] == [1, 1, 2, 2, 3, 3]


class SlowDiagnosisBackend(ScriptedBackend):
    def __init__(self, steps, clock):
        super().__init__(steps)
        self.clock = clock

    def chat(self, messages, model, **kwargs):
        if messages[-1].get("content") == DIAGNOSE:
            self.clock.now += 100  # the diagnosis takes 100 s
        return super().chat(messages, model, **kwargs)


def test_the_diagnosis_does_not_count_as_time_of_the_next_attempt(tmp_path):
    clock = FakeClock()
    steps = [reply(None, check("k1")), reply("because"), reply(None, check("k2"))]
    ctx, _ = make_ctx(tmp_path, steps, clock=clock)
    ctx.backend = SlowDiagnosisBackend(steps, clock)
    run_stage(DiagnosingStage([GateResult(False, ["x"]), GateResult(True, [])]), ctx)
    first, second = ctx.record.stages[0].attempts
    assert second.seconds < 1  # not 100 s


def test_the_record_is_saved_again_once_the_diagnosis_is_stored(tmp_path):
    steps = [reply(None, check("k1")), reply("because"), reply(None, check("k2"))]
    ctx, _ = make_ctx(tmp_path, steps)
    seen = []
    ctx.on_progress = lambda name: seen.append([a.diagnosis for a in ctx.record.stages[0].attempts])
    run_stage(DiagnosingStage([GateResult(False, ["x"]), GateResult(True, [])]), ctx)
    assert ["because"] in seen  # a save happened with the diagnosis in the record
    exhausted, _ = make_ctx(
        tmp_path / "e",
        [reply(None, check("k1")), reply("last words")],
        limits=RunLimits(max_attempts_per_stage=1),
    )
    seen.clear()
    exhausted.on_progress = lambda name: seen.append(
        [a.diagnosis for a in exhausted.record.stages[0].attempts]
    )
    run_stage(DiagnosingStage([GateResult(False, ["x"])]), exhausted)
    assert seen[-1] == ["last words"]


def test_tool_calls_in_the_diagnosis_reply_are_answered_and_not_run(tmp_path):
    steps = [
        reply(None, check("k1")),
        reply("It diverged.", tc("write_file", "w1", path="attempts/toy-2/a", content="x")),
        reply(None, check("k2")),
    ]
    ctx, backend = make_ctx(tmp_path, steps)
    outcome = run_stage(DiagnosingStage([GateResult(False, ["x"]), GateResult(True, [])]), ctx)
    assert outcome.ok
    assert ctx.record.stages[0].attempts[0].diagnosis == "It diverged."
    assert not (ctx.run.root / "attempts" / "toy-2" / "a").exists()
    answered = [m for m in backend.calls[2].messages if m.get("tool_call_id") == "w1"]
    assert len(answered) == 1 and "not run" in answered[0]["content"]


def test_an_unusable_diagnosis_reply_leaves_the_diagnosis_empty_and_the_stage_goes_on(tmp_path):
    steps = [
        reply(None, check("k1")),
        reply(None, finish_reason="length"),
        reply(None, check("k2")),
    ]
    ctx, _ = make_ctx(tmp_path, steps)
    outcome = run_stage(DiagnosingStage([GateResult(False, ["x"]), GateResult(True, [])]), ctx)
    assert outcome.ok
    assert ctx.record.stages[0].attempts[0].diagnosis is None


def test_a_hook_that_declines_costs_no_model_turn(tmp_path):
    class Declining(ToyStage):
        def diagnosis_request(self, ctx, errors):
            return None

    ctx, backend = make_ctx(tmp_path, [reply(None, check("k1")), reply(None, check("k2"))])
    run_stage(Declining([GateResult(False, ["x"]), GateResult(True, [])]), ctx)
    assert len(backend.calls) == 2


def test_the_cost_limit_during_the_diagnosis_stops_the_stage(tmp_path):
    steps = [reply(None, check("k1")), reply("because", usage=Usage(10_000_000, 0, None))]
    ctx, backend = make_ctx(tmp_path, steps, cost_limit=0.5)
    outcome = run_stage(DiagnosingStage([GateResult(False, ["x"])] * 3), ctx)
    assert not outcome.ok and "cost" in outcome.reason
    assert len(backend.calls) == 2


# ------------------------------------------------------------------ retry hooks (R45)


class NoRetryStage(DiagnosingStage):
    """A stage whose failures cannot be fixed by another attempt."""

    def retry_after_failure(self, errors):
        return False

    def retry_text(self, previous):
        return f"custom retry text about {previous}"


def test_a_stage_that_declines_a_retry_ends_after_collecting_the_diagnosis(tmp_path):
    steps = [reply(None, check("k1")), reply("cause"), reply("never asked")]
    ctx, backend = make_ctx(tmp_path, steps)
    stage = NoRetryStage([GateResult(False, ["it broke"]), GateResult(True, [])])
    outcome = run_stage(stage, ctx)
    assert not outcome.ok and outcome.attempts == 1
    assert outcome.errors == ["it broke"] and "it broke" in outcome.reason
    assert "not retried" in outcome.reason
    assert len(backend.calls) == 2  # the work turn and the diagnosis turn, nothing more
    assert [d.name for d in stage.extra_calls] == ["toy-1"]  # no fresh attempt was prepared
    assert stage.promoted == []
    (stage_record,) = ctx.record.stages
    assert not stage_record.ok and [a.diagnosis for a in stage_record.attempts] == ["cause"]
    shown = tool_messages(1, backend)[0]["content"]
    assert "it broke" in shown and "fresh attempt" not in shown and "No retry" in shown


def test_the_decision_is_made_per_failure(tmp_path):
    class RetryOnlyOnce(NoRetryStage):
        def retry_after_failure(self, errors):
            return errors == ["not run"]

    steps = [reply(None, check("k1")), reply("d1"), reply(None, check("k2")), reply("d2")]
    ctx, backend = make_ctx(tmp_path, steps)
    stage = RetryOnlyOnce([GateResult(False, ["not run"]), GateResult(False, ["real failure"])])
    outcome = run_stage(stage, ctx)
    assert not outcome.ok and outcome.attempts == 2
    assert [a.diagnosis for a in ctx.record.stages[0].attempts] == ["d1", "d2"]


def test_the_default_stage_retries_and_keeps_the_standard_retry_text(tmp_path):
    steps = [reply(None, check("k1")), reply(None, check("k2"))]
    ctx, backend = make_ctx(tmp_path, steps)
    run_stage(ToyStage([GateResult(False, ["x"]), GateResult(True, [])]), ctx)
    shown = tool_messages(1, backend)[0]["content"]
    assert "Write the corrected files there" in shown


def test_a_stage_can_replace_the_retry_text(tmp_path):
    class Custom(ToyStage):
        def retry_text(self, previous):
            return f"custom retry text about {previous}"

    steps = [reply(None, check("k1")), reply(None, check("k2"))]
    ctx, backend = make_ctx(tmp_path, steps)
    run_stage(Custom([GateResult(False, ["x"]), GateResult(True, [])]), ctx)
    shown = tool_messages(1, backend)[0]["content"]
    assert "custom retry text about attempts/toy-1/" in shown
    assert "Write the corrected files" not in shown


def test_the_resume_message_after_a_diagnosis_is_neutral(tmp_path):
    steps = [reply(None, check("k1")), reply("because"), reply(None, check("k2"))]
    ctx, backend = make_ctx(tmp_path, steps)
    run_stage(DiagnosingStage([GateResult(False, ["x"]), GateResult(True, [])]), ctx)
    resumed = backend.calls[2].messages[-1]["content"]
    assert "attempt 2 of 3" in resumed and "corrected" not in resumed
