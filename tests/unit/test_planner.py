"""Unit tests for the Planner: the gate on a CaseSpec and the two-phase planning loop."""

import json
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from test_casespec import SPEC, make_spec

from cfdagent.agents.casespec import CaseSpec
from cfdagent.agents.planner import plan_gate, run_planner
from cfdagent.agents.stage import StageContext
from cfdagent.agents.transcript import TRANSCRIPT_FILE, TranscriptWriter
from cfdagent.config import RunConfig, RunLimits
from cfdagent.gates import GateResult
from cfdagent.llm.client import (
    ChatResult,
    LLMTransportError,
    SchemaError,
    StructuredOutputError,
    ToolCall,
    TruncatedOutputError,
    Usage,
)
from cfdagent.llm.cost import PRICING, CostTracker
from cfdagent.llm.scripted import ScriptedBackend
from cfdagent.prompts import load_prompt
from cfdagent.record import RunRecord
from cfdagent.rundir import RunDir

LIMITS = RunLimits()
TASK = "Simulate the flow. Report `centreline_u` and `drag`. Use the mesh file `case.msh`."
USAGE = Usage(100, 50, None)


def spec_json(**changes) -> str:
    return json.dumps({**SPEC, **changes})


def reply(content=None, *calls, usage=USAGE, finish_reason=None) -> ChatResult:
    return ChatResult(
        content=content,
        tool_calls=list(calls),
        usage=usage,
        parsed=None,
        raw={},
        finish_reason=finish_reason or ("tool_calls" if calls else "stop"),
    )


def calc(expression: str, call_id: str = "c1") -> ToolCall:
    return ToolCall(id=call_id, name="calculator", arguments={"expression": expression})


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_ctx(tmp_path, steps, *, limits=None, cost_limit=1.0, clock=None, progress=None):
    root = tmp_path / "run"
    root.mkdir(exist_ok=True)
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
    if progress is not None:
        extra["on_progress"] = progress.append
    ctx = StageContext(
        run=run,
        config=config,
        backend=backend,
        sandbox=SimpleNamespace(),  # the Planner never calls the sandbox
        cost=cost,
        record=record,
        transcript=TranscriptWriter(run, cost),
        **extra,
    )
    return ctx, backend


# ---------------------------------------------------------------------------- the gate


def test_planner_gate_accepts_a_consistent_spec():
    assert plan_gate(make_spec(), TASK, LIMITS) == GateResult(True, [])


def test_planner_gate_requires_every_backticked_identifier():
    task = "Report `centreline_u`, `drag` and `wall_shear`. Mesh: `case.msh`, `x/L`, `1e-5`."
    result = plan_gate(make_spec(), task, LIMITS)
    assert not result.ok
    assert len(result.errors) == 1
    assert "`wall_shear`" in result.errors[0]
    assert "centreline_u" in result.errors[0]  # names the outputs that do exist
    # Backticked file names and expressions are not identifiers and are ignored.
    assert plan_gate(make_spec(), "Plot `centreline_u`, save `out.png`, use `x/L`.", LIMITS).ok


def test_planner_gate_identifier_match_is_exact():
    result = plan_gate(make_spec(), "Report `Drag`.", LIMITS)  # the output is named `drag`
    assert not result.ok
    assert "`Drag`" in result.errors[0]


def test_planner_gate_lists_each_missing_identifier_once():
    result = plan_gate(make_spec(), "`aaa` and `bbb` and `aaa` again", LIMITS)
    assert [("`aaa`" in e, "`bbb`" in e) for e in result.errors] == [(True, False), (False, True)]


def test_planner_gate_checks_reynolds_consistency():
    # U L / nu = 1 * 1 / 0.01 = 100
    assert plan_gate(make_spec(), TASK, LIMITS).ok
    off = make_spec(reference={"length_m": 1.0, "velocity_m_s": 1.0, "reynolds_number": 100.2})
    result = plan_gate(off, TASK, LIMITS)
    assert not result.ok
    assert "reynolds" in result.errors[0].lower()
    assert "100" in result.errors[0]
    close = make_spec(reference={"length_m": 1.0, "velocity_m_s": 1.0, "reynolds_number": 100.05})
    assert plan_gate(close, TASK, LIMITS).ok  # 0.05 % is inside the 0.1 % tolerance


def test_planner_gate_skips_reynolds_check_when_not_given():
    spec = make_spec(reference={"length_m": 1.0, "velocity_m_s": 5.0, "reynolds_number": None})
    assert plan_gate(spec, TASK, LIMITS).ok


def test_planner_gate_reynolds_with_nonpositive_viscosity_is_an_error_not_a_crash():
    spec = make_spec(fluid={"density_kg_m3": 1.0, "kinematic_viscosity_m2_s": 0.0})
    result = plan_gate(spec, TASK, LIMITS)
    assert not result.ok
    assert "kinematic_viscosity_m2_s" in " ".join(result.errors)


def test_planner_gate_rejects_an_empty_bbox_axis():
    geometry = {
        **SPEC["geometry"],
        "bbox": {"xmin": 0.0, "ymin": 0.5, "zmin": 0.0, "xmax": 4.0, "ymax": 0.5, "zmax": 0.1},
    }
    result = plan_gate(make_spec(geometry=geometry), TASK, LIMITS)
    assert not result.ok
    assert len(result.errors) == 1
    assert "bbox" in result.errors[0] and "y" in result.errors[0]


def test_planner_gate_rejects_a_nonpositive_end_time():
    for bad in (0.0, -5.0):
        spec = make_spec(solver={**SPEC["solver"], "end_time_s": bad})
        result = plan_gate(spec, TASK, LIMITS)
        assert not result.ok
        assert "end_time_s" in result.errors[0]


def test_planner_rejects_2d_without_thickness():
    for bad in (None, 0.0, -1.0):
        spec = make_spec(geometry={**SPEC["geometry"], "thickness_m": bad})
        result = plan_gate(spec, TASK, LIMITS)
        assert not result.ok
        assert "thickness_m" in result.errors[0]
    for fine in ("3D", "axisymmetric"):
        spec = make_spec(dimensionality=fine, geometry={**SPEC["geometry"], "thickness_m": None})
        assert plan_gate(spec, TASK, LIMITS).ok


@pytest.mark.parametrize(
    ("estimate", "word"),
    [
        ({"cells": 2_000_001, "runtime_minutes": 5.0, "memory_gb": 0.5}, "cells"),
        ({"cells": 8000, "runtime_minutes": 90.5, "memory_gb": 0.5}, "runtime"),
        ({"cells": 8000, "runtime_minutes": 5.0, "memory_gb": 12.5}, "memory"),
    ],
)
def test_planner_gate_flags_resource_excess_with_prefix(estimate, word):
    result = plan_gate(make_spec(resource_estimate=estimate), TASK, LIMITS)
    assert not result.ok
    assert len(result.errors) == 1
    assert result.errors[0].startswith("exceeds resources:")
    assert word in result.errors[0]


def test_planner_gate_allows_resources_exactly_at_the_limits():
    estimate = {"cells": 2_000_000, "runtime_minutes": 90.0, "memory_gb": 12.0}
    assert plan_gate(make_spec(resource_estimate=estimate), TASK, LIMITS).ok


def test_planner_gate_reports_several_resources_in_one_message():
    estimate = {"cells": 9_000_000, "runtime_minutes": 500.0, "memory_gb": 50.0}
    result = plan_gate(make_spec(resource_estimate=estimate), TASK, LIMITS)
    assert len(result.errors) == 1
    for word in ("cells", "runtime", "memory"):
        assert word in result.errors[0]


def test_planner_gate_collects_independent_errors():
    spec = make_spec(solver={**SPEC["solver"], "end_time_s": 0.0})
    result = plan_gate(spec, "Report `missing_one`.", LIMITS)
    assert len(result.errors) == 2


# ---------------------------------------------------------------------------- the loop


def test_planner_writes_casespec_json(tmp_path):
    steps = [
        reply(None, calc("1 * 1 / 0.01")),
        reply("Re = 100 gives nu = 0.01."),
        reply(spec_json()),
    ]
    ctx, _ = make_ctx(tmp_path, steps)
    outcome = run_planner(ctx, TASK)
    assert outcome.ok and outcome.attempts == 1 and outcome.errors == [] and outcome.reason is None
    written = CaseSpec.model_validate_json((tmp_path / "run" / "casespec.json").read_text("utf-8"))
    assert written == make_spec()
    assert ctx.casespec == written


def test_planner_phase_one_uses_calculator_only_and_phase_two_uses_the_schema(tmp_path):
    steps = [
        reply(None, calc("1 * 1 / 0.01")),
        reply("Re = 100 gives nu = 0.01."),
        reply(spec_json()),
    ]
    ctx, backend = make_ctx(tmp_path, steps)
    run_planner(ctx, TASK)
    first, second, third = backend.calls
    assert [t["function"]["name"] for t in first.tools] == ["calculator"]
    assert first.response_schema is None
    assert first.messages[0] == {"role": "system", "content": load_prompt("planner").text}
    assert TASK in first.messages[1]["content"]
    # the calculator result reached the model
    assert any(m["role"] == "tool" and m["content"] == "100" for m in second.messages)
    # phase 2: schema, no tools, the phase-1 conversation, the structured token budget
    assert third.response_schema is CaseSpec
    assert not third.tools
    assert any(m["role"] == "tool" and m["content"] == "100" for m in third.messages)
    assert third.model.max_tokens == ctx.config.planner_structured_max_tokens
    assert first.model.max_tokens == ctx.config.models["planner"].max_tokens
    # the identifiers the task names are spelled out for the model
    assert "centreline_u" in third.messages[-1]["content"]
    assert "drag" in third.messages[-1]["content"]


def test_planner_records_attempts_cost_and_transcript(tmp_path):
    progress: list[str] = []
    steps = [reply("No tool needed."), reply("not json at all"), reply(spec_json())]
    ctx, _ = make_ctx(tmp_path, steps, progress=progress)
    outcome = run_planner(ctx, TASK)
    assert outcome.ok and outcome.attempts == 2
    stage = ctx.record.stages[-1]
    assert stage.name == "planner" and stage.ok
    assert [a.ok for a in stage.attempts] == [False, True]
    assert stage.attempts[0].errors
    assert progress and set(progress) == {"planner"}
    assert ctx.cost.total_usd > 0
    lines = (tmp_path / "run" / TRANSCRIPT_FILE).read_text("utf-8").splitlines()
    entries = [json.loads(line) for line in lines]
    assert len(entries) == 3
    assert {e["stage"] for e in entries} == {"planner"}
    assert {e["agent"] for e in entries} == {"planner"}
    assert [e["attempt"] for e in entries] == [1, 1, 2]
    assert entries[1]["error"] and "StructuredOutputError" in entries[1]["error"]


def test_planner_retries_after_invalid_reply_then_succeeds(tmp_path):
    steps = [reply("Plan ready."), reply('{"title": "incomplete"}'), reply(spec_json())]
    ctx, backend = make_ctx(tmp_path, steps)
    outcome = run_planner(ctx, TASK)
    assert outcome.ok and outcome.attempts == 2
    problem = backend.calls[2].messages[-1]
    assert problem["role"] == "user"
    assert "schema" in problem["content"].lower()
    assert ctx.casespec == make_spec()


def test_planner_retries_after_a_truncated_reply(tmp_path):
    steps = [reply("Plan ready."), TruncatedOutputError('{"title": "cut'), reply(spec_json())]
    ctx, backend = make_ctx(tmp_path, steps)
    outcome = run_planner(ctx, TASK)
    assert outcome.ok and outcome.attempts == 2
    assert "cut off" in backend.calls[2].messages[-1]["content"]


def test_planner_feeds_gate_errors_back_and_retries(tmp_path):
    bad = spec_json(requested_outputs=[SPEC["requested_outputs"][1]])  # lacks `centreline_u`
    steps = [reply("Plan ready."), reply(bad), reply(spec_json())]
    ctx, backend = make_ctx(tmp_path, steps)
    outcome = run_planner(ctx, TASK)
    assert outcome.ok and outcome.attempts == 2
    assert "`centreline_u`" in backend.calls[2].messages[-1]["content"]
    # the rejected reply stays in the conversation so the model can correct it
    assert any(m["role"] == "assistant" and m["content"] == bad for m in backend.calls[2].messages)
    assert [a.ok for a in ctx.record.stages[-1].attempts] == [False, True]


def test_planner_gives_up_after_three_attempts(tmp_path):
    steps = [reply("Plan ready."), reply("{}"), reply("[]"), reply("nope")]
    ctx, backend = make_ctx(tmp_path, steps)
    outcome = run_planner(ctx, TASK)
    assert not outcome.ok
    assert outcome.attempts == 3
    assert outcome.errors
    assert outcome.reason and "planner" in outcome.reason and "3" in outcome.reason
    assert len(backend.calls) == 4  # one phase-1 call and three structured attempts
    assert not (tmp_path / "run" / "casespec.json").exists()
    assert ctx.casespec is None
    stage = ctx.record.stages[-1]
    assert not stage.ok and [a.ok for a in stage.attempts] == [False, False, False]


def test_planner_respects_the_attempt_limit_of_the_run(tmp_path):
    steps = [reply("Plan ready."), reply("{}"), reply("{}")]
    ctx, _ = make_ctx(tmp_path, steps, limits=RunLimits(max_attempts_per_stage=2))
    outcome = run_planner(ctx, TASK)
    assert not outcome.ok and outcome.attempts == 2


def test_planner_does_not_retry_when_only_resources_are_exceeded(tmp_path):
    big = {"cells": 5_000_000, "runtime_minutes": 5.0, "memory_gb": 0.5}
    steps = [reply("Plan ready."), reply(spec_json(resource_estimate=big))]  # no third step
    ctx, backend = make_ctx(tmp_path, steps)
    outcome = run_planner(ctx, TASK)
    assert not outcome.ok
    assert outcome.attempts == 1
    assert outcome.reason and outcome.reason.startswith("exceeds resources:")
    assert len(backend.calls) == 2
    written = json.loads((tmp_path / "run" / "casespec.json").read_text("utf-8"))
    assert written["resource_estimate"]["cells"] == 5_000_000  # the recorded estimate
    stage = ctx.record.stages[-1]
    assert not stage.ok and len(stage.attempts) == 1


def test_planner_retries_when_resources_are_exceeded_together_with_other_errors(tmp_path):
    big = {"cells": 5_000_000, "runtime_minutes": 5.0, "memory_gb": 0.5}
    bad = spec_json(resource_estimate=big, solver={**SPEC["solver"], "end_time_s": 0.0})
    steps = [reply("Plan ready."), reply(bad), reply(spec_json())]
    ctx, backend = make_ctx(tmp_path, steps)
    outcome = run_planner(ctx, TASK)
    assert outcome.ok and outcome.attempts == 2
    feedback = backend.calls[2].messages[-1]["content"]
    assert "exceeds resources:" in feedback and "end_time_s" in feedback


def test_planner_moves_on_after_eight_tool_turns(tmp_path):
    steps = [reply(None, calc(f"{i} + 1", f"c{i}")) for i in range(1, 9)]
    steps += [reply(spec_json())]  # the ninth call is the structured one
    ctx, backend = make_ctx(tmp_path, steps)
    outcome = run_planner(ctx, TASK)
    assert outcome.ok
    assert len(backend.calls) == 9
    assert backend.calls[8].response_schema is CaseSpec
    # every tool call of phase 1 was answered before phase 2 started
    final = backend.calls[8].messages
    ids = [c["id"] for m in final if m.get("tool_calls") for c in m["tool_calls"]]
    answered = [m["tool_call_id"] for m in final if m["role"] == "tool"]
    assert ids == answered and len(ids) == 8


def test_planner_survives_a_bad_tool_call_in_phase_one(tmp_path):
    steps = [
        reply(None, ToolCall("c1", "calculator", {"expression": "import os"})),
        reply(None, ToolCall("c2", "write_file", {"path": "x", "content": "y"})),
        reply("Done."),
        reply(spec_json()),
    ]
    ctx, backend = make_ctx(tmp_path, steps)
    assert run_planner(ctx, TASK).ok
    tool_texts = [m["content"] for m in backend.calls[2].messages if m["role"] == "tool"]
    assert len(tool_texts) == 2 and all(t.startswith("error") for t in tool_texts)


def test_planner_stops_at_the_cost_limit(tmp_path):
    steps = [reply("Plan ready."), reply(spec_json())]
    ctx, _ = make_ctx(tmp_path, steps, cost_limit=0.0000001)
    outcome = run_planner(ctx, TASK)
    assert not outcome.ok
    assert outcome.reason and "cost limit" in outcome.reason
    assert ctx.casespec is None


def test_planner_stops_when_the_wall_clock_is_used_up(tmp_path):
    clock = FakeClock()
    ctx, backend = make_ctx(tmp_path, [reply("never asked")], clock=clock)
    clock.now += ctx.config.limits.wall_clock_s + 1
    outcome = run_planner(ctx, TASK)
    assert not outcome.ok
    assert outcome.reason and "wall-clock" in outcome.reason
    assert backend.calls == []


def test_planner_reports_a_failing_model_service(tmp_path):
    down = LLMTransportError("boom", status_code=400, retryable=False)
    ctx, _ = make_ctx(tmp_path, [reply("Plan ready."), down])
    outcome = run_planner(ctx, TASK)
    assert not outcome.ok
    assert outcome.reason and "model service failed" in outcome.reason


def test_planner_retries_a_transient_service_error_with_a_pause(tmp_path):
    sleeps: list[float] = []
    steps = [
        LLMTransportError("busy", status_code=429, retryable=True),
        reply("Plan ready."),
        reply(spec_json()),
    ]
    ctx, _ = make_ctx(tmp_path, steps)
    ctx.sleep = sleeps.append
    assert run_planner(ctx, TASK).ok
    assert len(sleeps) == 1


def test_planner_lets_a_schema_error_propagate(tmp_path):
    ctx, _ = make_ctx(tmp_path, [reply("Plan ready."), SchemaError("programming error")])
    with pytest.raises(SchemaError):
        run_planner(ctx, TASK)


def test_scripted_backend_raises_structured_error_for_an_invalid_reply():
    """Guards the assumption of the retry tests: an invalid reply raises StructuredOutputError."""
    backend = ScriptedBackend([reply("{}")])
    with pytest.raises(StructuredOutputError):
        backend.chat([], RunConfig.default().models["planner"], response_schema=CaseSpec)


def test_planner_prompt_states_the_required_instructions():
    text = load_prompt("planner").text
    for phrase in (
        "calculator",
        "assumptions",
        "SI units",
        "Flow regime",
        "symmetry",
        "exact",
        "requested_outputs",
    ):
        assert phrase in text, phrase
