"""Unit tests for the Critic (ADR-0007): its record types, the files it may read, the guard on its
verdict and the route a send-back becomes. A scripted model plays the Critic; no sandbox is used."""

import json
from pathlib import Path

import pytest
from test_casespec import _violations
from test_planner import FakeClock, calc, make_ctx, reply

from cfdagent.agents.critic import CRITIC_MAX_TURNS, REQUIRED_CHECKS, run_critic, verdict_problems
from cfdagent.agents.routing import RouteRequest
from cfdagent.agents.transcript import TRANSCRIPT_FILE
from cfdagent.config import RunLimits
from cfdagent.llm.client import ToolCall, strict_schema
from cfdagent.prompts import load_prompt
from cfdagent.record import CriticCheck, CriticVerdict

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
TASK = "Simulate the flow in the described domain. Report `centreline_u`."
SECRET = "0.31416"  # stands for a reference value that must never reach the Critic

CHECK_NAMES = ("requested_outputs", "task_parameters", "convergence", "plausibility")


def checks(*failed: str) -> list[dict]:
    return [
        {"name": name, "passed": name not in failed, "evidence": f"evidence for {name}"}
        for name in CHECK_NAMES
    ]


def verdict_json(
    verdict="success",
    *,
    failed=(),
    reasons=("All requested outputs are present and plausible.",),
    send_back_to=None,
    instruction=None,
    **changes,
) -> str:
    return json.dumps(
        {
            "verdict": verdict,
            "checks": checks(*failed),
            "reasons": list(reasons),
            "send_back_to": send_back_to,
            "instruction": instruction,
            **changes,
        }
    )


def read(path: str, call_id: str = "r1", **extra) -> ToolCall:
    return ToolCall(id=call_id, name="read_file", arguments={"path": path, **extra})


def list_dir(path: str = ".", call_id: str = "l1") -> ToolCall:
    return ToolCall(id=call_id, name="list_dir", arguments={"path": path})


def populate(run) -> None:
    """The canonical files of a finished run, as the Critic is allowed to see them, and files of
    the run directory that it is not allowed to see (marked with ``SECRET`` where they could leak
    a reference value)."""
    run.write_text("task.md", TASK)
    run.write_text("casespec.json", json.dumps({"title": "A plan"}))
    run.write_text("boundary_manifest.json", json.dumps({"patches": ["inlet"]}))
    run.write_text("mesh/mesh.log", "mesh script output\n")
    run.write_text("mesh/notes.md", "mesh notes\n")
    run.write_text("mesh/case/checkMesh.log", "Mesh OK.\n")
    run.write_text("logs/solver_run.json", json.dumps({"solver": "simpleFoam", "ok": True}))
    run.write_text("logs/log.simpleFoam", "Time = 1\nSIMPLE solution converged in 5 iterations\n")
    run.write_text("analysis.md", "The flow is steady and the mass balance closes.\n")
    run.write_text("results/quantities.json", json.dumps({"centreline_u": 1.0}))
    # not the Critic's:
    run.write_text("results/comparison.json", json.dumps({"passed": True, "reference": SECRET}))
    run.write_text("superseded/pass-1/analysis.md", f"an earlier pass, value {SECRET}\n")
    run.write_text("attempts/analyst-1/analysis.md", "attempt of pass 0\n")
    run.write_text("attempts/analyst-p1-1/analysis.md", "attempt of pass 1\n")
    run.write_text("attempts/mesh-1/notes.md", "a stage's attempt\n")
    run.write_text(TRANSCRIPT_FILE, '{"stage": "mesh"}\n')
    run.write_text("run_record.json", "{}")
    run.write_text("case/0/U", "internalField uniform (0 0 0);\n")
    run.write_text("benchmarks/bench-one/reference/profile.csv", f"x,value\n0.5,{SECRET}\n")


def tool_outputs(backend) -> list[str]:
    """The tool results the model saw, in the last call it made."""
    return [m["content"] for m in backend.calls[-1].messages if m["role"] == "tool"]


# ---------------------------------------------------------------------------- record types


def test_critic_schema_has_no_maps_or_tuples():
    schema = CriticVerdict.model_json_schema()
    assert _violations(schema) == []
    assert strict_schema(schema)["additionalProperties"] is False
    assert set(schema["properties"]) == {
        "verdict",
        "checks",
        "reasons",
        "send_back_to",
        "instruction",
    }
    assert set(CriticCheck.model_json_schema()["properties"]) == {"name", "passed", "evidence"}
    assert schema["$defs"]["CriticCheck"]["additionalProperties"] is False


def test_the_verdict_names_only_stages_a_fix_can_go_to():
    from cfdagent.agents.routing import ROUTE_TARGETS

    schema = CriticVerdict.model_json_schema()
    targets = [v for v in schema["properties"]["send_back_to"]["anyOf"] if "enum" in v]
    assert set(targets[0]["enum"]) == ROUTE_TARGETS
    assert set(schema["properties"]["verdict"]["enum"]) == {"success", "partial", "failed"}


def test_the_text_of_a_verdict_is_bounded_and_cleaned():
    verdict = CriticVerdict.model_validate_json(
        verdict_json(
            reasons=["see https://example.invalid/secret and " + "x" * 3000],
            send_back_to="mesh",
            instruction="y" * 5000,
        )
    )
    assert "https://" not in verdict.reasons[0] and len(verdict.reasons[0]) <= 500
    assert len(verdict.instruction) <= 1000
    long_evidence = {"name": "n" * 500, "passed": True, "evidence": "e" * 5000}
    check = CriticCheck.model_validate(long_evidence)
    assert len(check.name) <= 100 and len(check.evidence) <= 500


def test_a_verdict_with_too_many_checks_or_reasons_is_refused():
    many = [{"name": f"c{i}", "passed": True, "evidence": "ok"} for i in range(60)]
    data = json.loads(verdict_json())
    with pytest.raises(ValueError, match="checks"):
        CriticVerdict.model_validate({**data, "checks": many})
    with pytest.raises(ValueError, match="reasons"):
        CriticVerdict.model_validate({**data, "reasons": ["r"] * 60})


# ---------------------------------------------------------------------------- the prompt


def test_the_prompt_names_the_required_checks_and_the_targets():
    text = load_prompt("critic").text
    assert REQUIRED_CHECKS == CHECK_NAMES  # the guard demands what the prompt asks for
    for name in CHECK_NAMES:
        assert name in text, name
    for target in ("planner", "geometry", "mesh", "case_setup", "analyst"):
        assert target in text
    assert "reference data" in text  # it says what it does not have


# ---------------------------------------------------------------------------- the conversation


def test_a_clean_run_gets_a_success_verdict_and_nothing_is_routed(tmp_path):
    steps = [reply("I have read everything."), reply(verdict_json())]
    ctx, backend = make_ctx(tmp_path, steps)
    populate(ctx.run)
    outcome = run_critic(ctx, TASK)
    assert outcome.ok and outcome.route is None and outcome.attempts == 1
    assert ctx.record.critic_verdict is not None
    assert ctx.record.critic_verdict.verdict == "success"
    assert [c.name for c in ctx.record.critic_verdict.checks] == list(CHECK_NAMES)
    assert ctx.record.critic_rounds == 0
    stage = ctx.record.stages[-1]
    assert (stage.name, stage.ok, stage.pass_no) == ("critic", True, 0)
    assert [a.ok for a in stage.attempts] == [True]


def test_the_critic_starts_fresh_with_read_only_tools(tmp_path):
    steps = [reply("Nothing to look up."), reply(verdict_json())]
    ctx, backend = make_ctx(tmp_path, steps)
    populate(ctx.run)
    assert run_critic(ctx, TASK).ok
    first, second = backend.calls
    assert first.messages[0] == {"role": "system", "content": load_prompt("critic").text}
    assert len(first.messages) == 2  # the system prompt and one opening message, nothing else
    opening = first.messages[1]["content"]
    assert TASK in opening
    assert "SIMPLE solution converged" in opening  # the tail of the solver log
    assert {t["function"]["name"] for t in first.tools} == {"calculator", "read_file", "list_dir"}
    assert second.response_schema is CriticVerdict and not second.tools
    assert first.model == ctx.config.models["critic"]


def test_the_critic_can_read_the_canonical_files_and_use_the_calculator(tmp_path):
    allowed = [
        "task.md",
        "casespec.json",
        "boundary_manifest.json",
        "mesh/mesh.log",
        "mesh/notes.md",
        "mesh/case/checkMesh.log",
        "logs/solver_run.json",
        "logs/log.simpleFoam",
        "analysis.md",
        "results/quantities.json",
    ]
    calls = [read(path, f"r{i}") for i, path in enumerate(allowed)] + [calc("2 * 21", "c1")]
    steps = [reply("Reading.", *calls), reply("Done."), reply(verdict_json())]
    ctx, backend = make_ctx(tmp_path, steps)
    populate(ctx.run)
    assert run_critic(ctx, TASK).ok
    outputs = [m["content"] for m in backend.calls[1].messages if m["role"] == "tool"]
    assert len(outputs) == len(allowed) + 1
    assert not any(out.startswith("error:") for out in outputs), outputs
    assert "mass balance closes" in outputs[allowed.index("analysis.md")]
    assert outputs[-1] == "42"


def test_critic_cannot_read_comparison_or_benchmarks(tmp_path):
    forbidden = [
        "results/comparison.json",
        "benchmarks/bench-one/reference/profile.csv",
        "superseded/pass-1/analysis.md",
        TRANSCRIPT_FILE,
        "run_record.json",
        "../benchmarks/bench-one/reference/profile.csv",
        "status.json",
    ]
    calls = [read(path, f"r{i}") for i, path in enumerate(forbidden)]
    steps = [reply("Looking.", *calls), reply("Done."), reply(verdict_json())]
    ctx, backend = make_ctx(tmp_path, steps)
    populate(ctx.run)
    assert run_critic(ctx, TASK).ok
    outputs = [m["content"] for m in backend.calls[1].messages if m["role"] == "tool"]
    assert len(outputs) == len(forbidden)
    for path, output in zip(forbidden, outputs, strict=True):
        assert output.startswith("error:"), (path, output)
        assert SECRET not in output
    # nothing of the forbidden files reached the model in any call, and no tool can write
    for call in backend.calls:
        assert SECRET not in json.dumps(call.messages)
    assert "write_file" not in {t["function"]["name"] for t in backend.calls[0].tools}
    assert (ctx.run.root / "results" / "comparison.json").is_file()  # it exists, unseen


def test_critic_cannot_read_attempt_directories_of_earlier_passes(tmp_path):
    paths = [
        "attempts/analyst-1/analysis.md",
        "attempts/analyst-p1-1/analysis.md",
        "attempts/mesh-1/notes.md",
        "case/0/U",
    ]
    calls = [read(path, f"r{i}") for i, path in enumerate(paths)] + [list_dir(".", "l1")]
    calls.append(list_dir("attempts", "l2"))
    steps = [reply("Looking.", *calls), reply("Done."), reply(verdict_json())]
    ctx, backend = make_ctx(tmp_path, steps)
    populate(ctx.run)
    assert run_critic(ctx, TASK).ok
    outputs = [m["content"] for m in backend.calls[1].messages if m["role"] == "tool"]
    for path, output in zip(paths, outputs[: len(paths)], strict=True):
        assert output.startswith("error:"), (path, output)
    root_listing, attempts_listing = outputs[len(paths) :]
    assert sorted(root_listing.split("\n")) == [
        "analysis.md",
        "boundary_manifest.json",
        "casespec.json",
        "logs/",
        "mesh/",
        "results/",
        "task.md",
    ]
    assert attempts_listing.startswith("error:")
    top_level_results = [out for out in outputs if "comparison.json" in out]
    assert top_level_results == []


def test_the_solver_log_tail_is_the_end_of_the_log_not_the_start(tmp_path):
    ctx, backend = make_ctx(tmp_path, [reply("Fine."), reply(verdict_json())])
    populate(ctx.run)
    body = "".join(f"Time = {i}\n" for i in range(1, 2000)) + "End\n"
    ctx.run.write_text("logs/log.simpleFoam", body)
    assert run_critic(ctx, TASK).ok
    opening = backend.calls[0].messages[1]["content"]
    assert "Time = 1999" in opening and "End" in opening
    assert "Time = 5\n" not in opening
    (tmp_path / "other").mkdir()
    bare, _ = make_ctx(tmp_path / "other", [reply("Fine."), reply(verdict_json())])
    bare.run.write_text("task.md", TASK)  # a run without a solver log: no tail, no crash
    assert run_critic(bare, TASK).ok


def test_the_critic_has_at_most_twelve_read_only_turns(tmp_path):
    assert CRITIC_MAX_TURNS == 12
    looping = [reply("More.", calc("1 + 1", f"c{i}")) for i in range(CRITIC_MAX_TURNS)]
    ctx, backend = make_ctx(tmp_path, [*looping, reply(verdict_json())])
    populate(ctx.run)
    outcome = run_critic(ctx, TASK)
    assert outcome.ok  # running out of turns is not a failure: the verdict call follows
    tool_turns = [c for c in backend.calls if c.tools]
    assert len(tool_turns) == CRITIC_MAX_TURNS
    assert backend.calls[-1].response_schema is CriticVerdict
    assert len(backend.calls) == CRITIC_MAX_TURNS + 1


# ---------------------------------------------------------------------------- the guard


def test_success_with_failed_check_is_rejected(tmp_path):
    bad = verdict_json("success", failed=("convergence",))
    steps = [reply("Read."), reply(bad), reply(verdict_json("partial", failed=("convergence",)))]
    ctx, backend = make_ctx(tmp_path, steps)
    populate(ctx.run)
    outcome = run_critic(ctx, TASK)
    assert outcome.ok and outcome.attempts == 2
    stage = ctx.record.stages[-1]
    assert [a.ok for a in stage.attempts] == [False, True]
    assert "convergence" in stage.attempts[0].errors[0]
    assert ctx.record.critic_verdict.verdict == "partial"  # the accepted one, not the rejected
    told = backend.calls[2].messages[-1]
    assert told["role"] == "user" and "convergence" in told["content"]
    assert "success" in told["content"]
    # phase 2 has no tools, so the model cannot collect new evidence: it must not be invited to
    # "correct" the checks, only to change the verdict or to say what in its files shows a pass
    assert "correct the checks" not in told["content"]
    assert "partial or failed" in told["content"]


def make_verdict(**changes) -> CriticVerdict:
    return CriticVerdict.model_validate_json(verdict_json(**changes))


def test_success_without_the_required_checks_is_rejected(tmp_path):
    empty = json.loads(verdict_json())
    empty["checks"] = []
    partial_list = json.loads(verdict_json())
    partial_list["checks"] = checks()[:3]  # `plausibility` is missing
    renamed = json.loads(verdict_json())
    renamed["checks"][0]["name"] = "outputs"  # a name the prompt does not ask for
    steps = [
        reply("Read."),
        reply(json.dumps(empty)),
        reply(json.dumps(partial_list)),
        reply(json.dumps(renamed)),
    ]
    ctx, backend = make_ctx(tmp_path, steps)
    populate(ctx.run)
    outcome = run_critic(ctx, TASK)
    assert outcome.ok and ctx.record.critic_verdict is None  # three invalid replies: no verdict
    errors = [a.errors[0] for a in ctx.record.stages[-1].attempts]
    listing = "does not list these required checks: "
    assert listing + "requested_outputs, task_parameters, convergence, plausibility." in errors[0]
    assert listing + "plausibility." in errors[1]  # only the check that is missing
    assert listing + "requested_outputs." in errors[2]  # a name the prompt does not give
    told = backend.calls[2].messages[-1]["content"]
    assert "requested_outputs, task_parameters, convergence, plausibility" in told


def test_a_success_with_all_required_checks_and_extra_ones_is_accepted(tmp_path):
    data = json.loads(verdict_json())
    data["checks"].append({"name": "mesh_quality", "passed": True, "evidence": "checkMesh is ok"})
    ctx, _ = make_ctx(tmp_path, [reply("Read."), reply(json.dumps(data))])
    populate(ctx.run)
    assert run_critic(ctx, TASK).ok
    assert len(ctx.record.critic_verdict.checks) == 5


def test_a_verdict_with_empty_checks_and_empty_reasons_is_told_both_lists_are_empty():
    data = json.loads(verdict_json("failed", reasons=[]))
    data["checks"] = []
    (message,) = verdict_problems(CriticVerdict.model_validate(data))
    assert "`checks` and `reasons` are both empty" in message
    for name in REQUIRED_CHECKS:
        assert name in message  # the names that `checks` must list
    assert "evidence" in message and "at least one" in message


def test_the_empty_lists_message_is_only_for_empty_lists():
    reasonless = make_verdict(verdict="failed", reasons=[])  # four checks, all passed
    (message,) = verdict_problems(reasonless)
    assert "both empty" not in message and "no check failed" in message


def test_a_near_empty_summary_is_asked_for_once_more(tmp_path):
    steps = [reply("  "), reply("Everything was read; the run looks sound."), reply(verdict_json())]
    ctx, backend = make_ctx(tmp_path, steps)
    populate(ctx.run)
    assert run_critic(ctx, TASK).ok
    assert len(backend.calls) == 3
    asked = backend.calls[1].messages[-1]
    assert asked["role"] == "user" and "summary" in asked["content"]
    assert backend.calls[2].response_schema is CriticVerdict  # then the verdict is requested


def test_an_empty_summary_is_asked_for_only_once(tmp_path):
    steps = [reply(""), reply(""), reply(verdict_json())]
    ctx, backend = make_ctx(tmp_path, steps)
    populate(ctx.run)
    assert run_critic(ctx, TASK).ok
    assert len(backend.calls) == 3  # the second blank reply goes on to the verdict
    assert [c.response_schema for c in backend.calls] == [None, None, CriticVerdict]


def test_a_summary_of_a_normal_length_is_not_asked_for_again(tmp_path):
    ctx, backend = make_ctx(tmp_path, [reply("Read all the files; fine."), reply(verdict_json())])
    populate(ctx.run)
    assert run_critic(ctx, TASK).ok
    assert len(backend.calls) == 2


def test_a_verdict_that_is_not_a_success_needs_a_failed_check_or_a_reason():
    reasonless = make_verdict(verdict="failed", reasons=[])
    assert verdict_problems(reasonless) and "reason" in verdict_problems(reasonless)[0]
    assert (
        verdict_problems(make_verdict(verdict="failed", failed=("convergence",), reasons=[])) == []
    )
    assert (
        verdict_problems(make_verdict(verdict="partial", reasons=["One output is missing."])) == []
    )
    assert verdict_problems(make_verdict()) == []  # a complete success is fine
    # only a success must show all the required checks; a verdict that finds a fault may name just
    # the checks that found it (rejecting it would cost attempts and could end in no verdict)
    short_list = json.loads(verdict_json("partial", failed=("convergence",)))
    short_list["checks"] = [c for c in short_list["checks"] if c["name"] == "convergence"]
    assert verdict_problems(CriticVerdict.model_validate(short_list)) == []


@pytest.mark.parametrize("instruction", [None, "", "   "])
def test_send_back_requires_instruction(tmp_path, instruction):
    bad = json.loads(verdict_json("partial", failed=("plausibility",), send_back_to="mesh"))
    bad["instruction"] = instruction
    good = verdict_json(
        "partial",
        failed=("plausibility",),
        send_back_to="mesh",
        instruction="Refine the mesh in the boundary layer.",
    )
    ctx, _ = make_ctx(tmp_path, [reply("Read."), reply(json.dumps(bad)), reply(good)])
    populate(ctx.run)
    outcome = run_critic(ctx, TASK)
    stage = ctx.record.stages[-1]
    assert [a.ok for a in stage.attempts] == [False, True]
    assert "instruction" in stage.attempts[0].errors[0]
    assert outcome.route is not None and outcome.route.instruction.startswith("Refine")


def test_success_with_a_send_back_is_contradictory_and_rejected(tmp_path):
    bad = verdict_json("success", send_back_to="mesh", instruction="Refine it.")
    steps = [reply("Read."), reply(bad), reply(verdict_json())]
    ctx, _ = make_ctx(tmp_path, steps)
    populate(ctx.run)
    outcome = run_critic(ctx, TASK)
    assert outcome.ok and outcome.route is None
    assert [a.ok for a in ctx.record.stages[-1].attempts] == [False, True]
    assert ctx.record.critic_rounds == 0


def test_a_reply_that_is_not_a_verdict_costs_an_attempt_too(tmp_path):
    steps = [reply("Read."), reply("this is not json"), reply(verdict_json())]
    ctx, backend = make_ctx(tmp_path, steps)
    populate(ctx.run)
    outcome = run_critic(ctx, TASK)
    assert outcome.ok and outcome.attempts == 2
    assert [a.ok for a in ctx.record.stages[-1].attempts] == [False, True]
    assert "CriticVerdict" in backend.calls[2].messages[-1]["content"]


# ---------------------------------------------------------------------------- send-backs


def partial_send_back(**changes) -> str:
    data = {
        "failed": ("plausibility",),
        "reasons": ("The velocity at the wall is not zero.",),
        "send_back_to": "case_setup",
        "instruction": "Apply a no-slip condition on every wall.",
        **changes,
    }
    return verdict_json("partial", **data)


def test_critic_send_back_becomes_route(tmp_path):
    steps = [reply("Read."), reply(partial_send_back())]
    ctx, _ = make_ctx(tmp_path, steps)
    populate(ctx.run)
    outcome = run_critic(ctx, TASK)
    assert not outcome.ok
    assert outcome.route == RouteRequest(
        source="critic",
        target="case_setup",
        origin="critic",
        reason="The velocity at the wall is not zero.",
        instruction="Apply a no-slip condition on every wall.",
    )
    assert outcome.route.origin == "critic"
    assert ctx.record.critic_rounds == 1
    assert ctx.record.critic_verdict is None  # only a final verdict is the verdict of the run
    assert ctx.record.stages[-1].ok is False
    assert outcome.reason and "case_setup" in outcome.reason


def test_a_send_back_to_the_analyst_is_a_valid_target(tmp_path):
    steps = [reply("Read."), reply(partial_send_back(send_back_to="analyst"))]
    ctx, _ = make_ctx(tmp_path, steps)
    populate(ctx.run)
    assert run_critic(ctx, TASK).route.target == "analyst"


def test_a_send_back_without_rounds_left_is_final_and_not_a_failure(tmp_path):
    limits = RunLimits(max_critic_rounds=0)
    ctx, _ = make_ctx(tmp_path, [reply("Read."), reply(partial_send_back())], limits=limits)
    populate(ctx.run)
    outcome = run_critic(ctx, TASK)
    assert outcome.ok and outcome.route is None
    verdict = ctx.record.critic_verdict
    assert verdict.verdict == "partial" and verdict.send_back_to == "case_setup"
    assert verdict.instruction.startswith("Apply a no-slip")
    assert ctx.record.critic_rounds == 0


# ---------------------------------------------------------------------------- the Critic fails


def test_three_invalid_replies_leave_no_verdict_and_do_not_fail_the_step(tmp_path):
    bad = verdict_json("success", failed=("convergence",))
    ctx, _ = make_ctx(tmp_path, [reply("Read."), reply(bad), reply(bad), reply(bad)])
    populate(ctx.run)
    outcome = run_critic(ctx, TASK)
    assert outcome.ok and outcome.route is None  # the run goes on
    assert outcome.attempts == 3 and outcome.reason
    assert ctx.record.critic_verdict is None and ctx.record.critic_rounds == 0
    stage = ctx.record.stages[-1]
    assert [a.ok for a in stage.attempts] == [False, False, False] and not stage.ok


def test_a_cost_stop_inside_the_critic_leaves_no_verdict(tmp_path):
    ctx, _ = make_ctx(tmp_path, [reply("Read."), reply(verdict_json())], cost_limit=0.0)
    populate(ctx.run)
    outcome = run_critic(ctx, TASK)
    assert outcome.ok and "cost limit" in outcome.reason
    assert ctx.record.critic_verdict is None


def test_a_wall_clock_stop_inside_the_critic_leaves_no_verdict(tmp_path):
    clock = FakeClock()
    ctx, backend = make_ctx(tmp_path, [reply("Read.")], clock=clock)
    populate(ctx.run)
    clock.now += ctx.config.limits.wall_clock_s + 1  # the budget is gone before the first call
    outcome = run_critic(ctx, TASK)
    assert outcome.ok and "wall-clock" in outcome.reason
    assert ctx.record.critic_verdict is None
    assert backend.calls == []


def test_the_critic_fixture_is_a_success_verdict_for_a_finished_run(tmp_path):
    from cfdagent.llm.scripted import ScriptedBackend

    ctx, _ = make_ctx(tmp_path, [])
    ctx.backend = ScriptedBackend.from_json(FIXTURES / "scripted" / "critic_ok.json")
    populate(ctx.run)
    outcome = run_critic(ctx, TASK)
    assert outcome.ok and outcome.route is None
    assert ctx.record.critic_verdict.verdict == "success"
    assert all(check.passed for check in ctx.record.critic_verdict.checks)
