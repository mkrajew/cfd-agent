"""Unit tests for the Solver Runner stage (fake sandbox, no OpenFOAM)."""

import json
import re
from pathlib import Path

import pytest
from test_geometry_stage import Clock, FakeSandbox, make_ctx, plan

from cfdagent.agents.loop import run_stage
from cfdagent.agents.solver_runner import (
    MAX_LOG_LINES,
    MAX_PROCS,
    SolverRunnerStage,
)
from cfdagent.agents.tools import ToolRegistry
from cfdagent.config import RunLimits
from cfdagent.errors import AgentInputError, ToolFailure
from cfdagent.executor.protocol import OpResponse
from cfdagent.llm.client import ChatResult, ToolCall, Usage
from cfdagent.prompts import load_prompt
from cfdagent.sandbox.client import OUTER_TIMEOUT_MARGIN_S

ATTEMPT = "attempts/solver_runner-1"
CASE = f"{ATTEMPT}/case"
CHECKMESH_LOG = "Mesh stats\n    points:           4225\n    cells:            4096\n"


def run_data(**changes):
    data = {  # what the executor sends: its "ok" and "error" are the response's own fields
        "returncode": 0,
        "timed_out": False,
        "tail": "Time = 40s\n\nEnd\n",
        "completed": True,
        "diverged": False,
        "fatal_error": None,
        "end_time": 40.0,
        "first_time": 0.005,
        "last_step": 0.005,
        "final_residuals": {"Ux": 1.2e-8, "p": 4.5e-9},
        "max_courant": 0.32,
        "converged_at": None,
        "case_end_time": 40.0,
        "delta_t": 0.005,
        "solver_log": f"{CASE}/log.icoFoam",
        "tool_log": f"{CASE}/log.icoFoam",
    }
    data.update(changes)
    return data


RUN_OK = OpResponse(ok=True, data=run_data())


def run_failed(message="the solver step exited with code 1", **changes):
    data = run_data(**{"completed": False, "returncode": 1, **changes})
    return OpResponse(ok=False, data=data, error=message)


def solver_spec(name="icoFoam"):
    return plan(solver={"suggested_solver": name, "end_time_s": 40.0, "time_step_s": 0.005})


class Setup:
    """A context whose run directory holds the Case Setup stage's result and a fake sandbox."""

    def __init__(self, tmp_path, solver="icoFoam", run=RUN_OK, with_case=True, **ctx_kwargs):
        self.sandbox = FakeSandbox({"run_solver": run})
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.ctx = make_ctx(tmp_path, self.sandbox, spec=solver_spec(solver), **ctx_kwargs)
        if with_case:
            write_run_case(self.ctx)
        self.stage = SolverRunnerStage()
        self.attempt = None
        self.registry = None

    def prepare(self, number=1):
        self.attempt = self.ctx.run.attempt_dir("solver_runner", number)
        self.stage.prepare_attempt(self.ctx, self.attempt)
        self.registry = ToolRegistry()
        for tool in self.stage.extra_tools(self.ctx, self.attempt):
            self.registry.add(tool)
        return self.attempt

    def tool(self, name, **arguments):
        """The text a model would be shown for one tool call."""
        return self.registry.dispatch(ToolCall(id="c1", name=name, arguments=arguments))

    def gate(self):
        return self.stage.execute_and_gate(self.ctx, self.attempt)

    def calls(self):
        return [c[0] for c in self.sandbox.calls]


def write_run_case(ctx):
    """What the Case Setup stage leaves in the run directory."""
    ctx.run.write_text("case/system/controlDict", "application icoFoam;\nendTime 40;\n")
    ctx.run.write_text("case/0/U", "internalField uniform (0 0 0);\n")
    ctx.run.write_text("case/constant/polyMesh/boundary", "0 ()\n")
    ctx.run.write_text("mesh/case/checkMesh.log", CHECKMESH_LOG)
    ctx.run.write_text("boundary_manifest.json", "{}")


def prepared(tmp_path, **kwargs):
    setup = Setup(tmp_path, **kwargs)
    setup.prepare()
    return setup


# ------------------------------------------------------------------ the run_solver tool


@pytest.mark.parametrize(
    ("solver", "n_procs", "message"),
    [
        ("pimpleFoam", 1, "icoFoam"),  # a solver of the pipeline, but not the plan's
        ("rhoPimpleFoam", 1, "icoFoam"),
        ("blockMesh", 1, "icoFoam"),
        ("icoFoam", 0, "n_procs"),
        ("icoFoam", -2, "n_procs"),
        ("icoFoam", 5, "at most 4"),
        ("icoFoam", 64, "at most 4"),
    ],
)
def test_run_solver_tool_rejects_bad_arguments(tmp_path, solver, n_procs, message):
    setup = prepared(tmp_path)
    tool = next(
        t for t in setup.stage.extra_tools(setup.ctx, setup.attempt) if t.name == "run_solver"
    )
    with pytest.raises(AgentInputError, match=message):
        tool.handler(tool.args(solver=solver, n_procs=n_procs))
    text = setup.tool("run_solver", solver=solver, n_procs=n_procs)
    assert text.startswith("error:") and message in text  # shown to the model as text
    assert setup.calls() == []  # nothing reached the sandbox
    assert not setup.gate().ok  # and no run counts for the gate


def test_the_rank_limit_follows_the_run_configuration(tmp_path):
    setup = prepared(tmp_path)
    setup.ctx.config = setup.ctx.config.model_copy(update={"limits": RunLimits(max_ranks=2)})
    assert "at most 2" in setup.tool("run_solver", solver="icoFoam", n_procs=3)
    assert setup.calls() == []
    assert MAX_PROCS == 4
    assert setup.tool("run_solver", solver="icoFoam", n_procs=2).startswith("icoFoam")
    assert setup.sandbox.calls[0][1].n_procs == 2


@pytest.mark.parametrize("arguments", [{"solver": "icoFoam", "n_procs": "two"}, {"solver": "x"}])
def test_run_solver_arguments_of_the_wrong_type_are_text_errors(tmp_path, arguments):
    setup = prepared(tmp_path)
    assert setup.tool("run_solver", **arguments).startswith("error:")
    assert setup.calls() == []


def test_run_solver_names_the_planned_solver_when_the_agent_picks_another(tmp_path):
    setup = prepared(tmp_path, solver="simpleFoam")
    text = setup.tool("run_solver", solver="icoFoam", n_procs=1)
    assert "simpleFoam" in text and "planned" in text


def test_run_solver_sends_the_case_the_solver_and_the_time_left_to_the_sandbox(tmp_path):
    clock = Clock()
    setup = prepared(tmp_path, clock=clock)
    clock.now += setup.ctx.config.limits.wall_clock_s - 900  # 900 s of the run are left
    text = setup.tool("run_solver", solver="icoFoam", n_procs=1)
    assert text.startswith("icoFoam")
    ((op, args, outer),) = setup.sandbox.calls
    assert op == "run_solver"
    assert (args.case_dir, args.solver, args.n_procs) == (CASE, "icoFoam", 1)
    assert args.timeout_s == pytest.approx(900)
    assert outer == pytest.approx(900 + OUTER_TIMEOUT_MARGIN_S)


def test_the_time_limit_stays_within_what_the_op_accepts(tmp_path):
    setup = prepared(tmp_path)
    setup.ctx.config = setup.ctx.config.model_copy(
        update={"limits": RunLimits(wall_clock_s=10 * 86_400)}
    )
    setup.tool("run_solver", solver="icoFoam", n_procs=1)
    assert setup.sandbox.calls[0][1].timeout_s == 86_400


def test_more_ranks_than_the_hardware_has_is_an_error_for_the_agent_not_a_run(tmp_path):
    refused = OpResponse(
        ok=False, error="n_procs=4 but only 2 hardware threads are available; use n_procs <= 2"
    )
    setup = prepared(tmp_path, run=refused)
    text = setup.tool("run_solver", solver="icoFoam", n_procs=4)
    assert text.startswith("error:") and "2 hardware threads" in text
    assert not setup.gate().ok and "not run" in setup.gate().errors[0]


def test_an_internal_failure_of_the_executor_is_not_the_agents_fault(tmp_path):
    broken = OpResponse(ok=False, internal=True, error="the executor failed")
    setup = prepared(tmp_path, run=broken)
    tool = next(
        t for t in setup.stage.extra_tools(setup.ctx, setup.attempt) if t.name == "run_solver"
    )
    with pytest.raises(ToolFailure, match="executor"):
        tool.handler(tool.args(solver="icoFoam", n_procs=1))


# ------------------------------------------------------------------ what the agent is told


def test_the_summary_of_a_clean_run(tmp_path):
    setup = prepared(tmp_path)
    text = setup.tool("run_solver", solver="icoFoam", n_procs=1)
    assert "reached the end time" in text and "40" in text
    assert "Ux" in text and "1.2e-08" in text and "p" in text
    assert "0.32" in text  # the Courant number
    assert "transient" in text.lower()
    assert "End" in text  # the log tail


def test_a_steady_run_that_converged_says_so(tmp_path):
    run = OpResponse(ok=True, data=run_data(converged_at=312, end_time=312.0))
    setup = prepared(tmp_path, solver="simpleFoam", run=run)
    text = setup.tool("run_solver", solver="simpleFoam", n_procs=1)
    assert "converged" in text and "312" in text and "residualControl" in text
    assert "not converged" not in text.lower() and "without" not in text


def test_a_steady_run_that_reached_the_end_time_without_converging_says_so(tmp_path):
    setup = prepared(tmp_path, solver="simpleFoam")
    text = setup.tool("run_solver", solver="simpleFoam", n_procs=1)
    assert "NOT converged" in text and "residualControl" in text and "end time" in text
    assert setup.gate().ok  # R44: the gate does not judge convergence


def test_a_failed_run_shows_the_headline_the_fatal_error_and_the_tail(tmp_path):
    run = run_failed(
        "FOAM fatal error: keyword div(phi,U) is undefined",
        fatal_error="keyword div(phi,U) is undefined",
        diverged=False,
        tail="noise\n" * 2000 + "--> FOAM FATAL IO ERROR: keyword div(phi,U) is undefined\n",
    )
    setup = prepared(tmp_path, run=run)
    text = setup.tool("run_solver", solver="icoFoam", n_procs=1)
    assert "FAILED" in text and "keyword div(phi,U) is undefined" in text
    assert "FOAM FATAL IO ERROR" in text  # the end of the log
    assert len(text.encode("utf-8")) <= 8000


def test_a_diverged_run_says_so_and_a_timeout_says_so(tmp_path):
    diverged = run_failed("solution diverged", diverged=True, max_courant=3.4e12)
    setup = prepared(tmp_path / "a", run=diverged)
    assert "diverged" in setup.tool("run_solver", solver="icoFoam", n_procs=1)
    timed = run_failed("the solver step timed out after 100 s and was killed", timed_out=True)
    setup = prepared(tmp_path / "b", run=timed)
    assert "timed out" in setup.tool("run_solver", solver="icoFoam", n_procs=1)


def test_missing_values_are_not_invented(tmp_path):
    run = OpResponse(ok=True, data=run_data(final_residuals={}, max_courant=None, first_time=None))
    setup = prepared(tmp_path, run=run)
    text = setup.tool("run_solver", solver="icoFoam", n_procs=1)
    assert "None" not in text and "nan" not in text.lower()
    assert "no Courant number" in text and "no residuals" in text


# ------------------------------------------------------------------ the gate


def test_stage_fails_when_solver_never_run(tmp_path):
    setup = prepared(tmp_path)
    result = setup.gate()
    assert not result.ok
    assert result.errors == [result.errors[0]] and "solver was not run" in result.errors[0]
    assert "run_solver" in result.errors[0]
    assert setup.calls() == []


def test_the_gate_is_the_last_run_of_the_attempt(tmp_path):
    answers = [run_failed("first failed"), RUN_OK]
    setup = prepared(tmp_path, run=lambda args: answers.pop(0))
    setup.tool("run_solver", solver="icoFoam", n_procs=1)
    assert not setup.gate().ok
    setup.tool("run_solver", solver="icoFoam", n_procs=1)
    result = setup.gate()
    assert result.ok and result.errors == []
    assert setup.calls() == ["run_solver", "run_solver"]  # the gate runs nothing itself
    answers = [RUN_OK, run_failed("second failed")]
    other = prepared(tmp_path / "other", run=lambda args: answers.pop(0))
    other.tool("run_solver", solver="icoFoam", n_procs=1)
    other.tool("run_solver", solver="icoFoam", n_procs=1)
    assert not other.gate().ok


def test_a_rejected_call_after_a_good_run_does_not_change_the_verdict(tmp_path):
    setup = prepared(tmp_path)
    setup.tool("run_solver", solver="icoFoam", n_procs=1)
    setup.tool("run_solver", solver="icoFoam", n_procs=9)
    assert setup.gate().ok


def test_a_failed_run_fails_the_gate_with_bounded_errors_that_carry_the_cause(tmp_path):
    tail = "noise\n" * 3000 + "--> FOAM FATAL ERROR: cannot find file p\n"
    run = run_failed(
        "FOAM fatal error: cannot find file p", fatal_error="cannot find file p", tail=tail
    )
    setup = prepared(tmp_path, run=run)
    setup.tool("run_solver", solver="icoFoam", n_procs=1)
    result = setup.gate()
    assert not result.ok
    text = "\n".join(result.errors)
    assert "cannot find file p" in text and "icoFoam" in text
    assert len(text.encode("utf-8")) <= 4000  # the loop shows the model at most 4096 bytes


def test_a_run_that_stopped_short_of_the_end_time_fails_the_gate(tmp_path):
    run = run_failed("the last time 12 does not reach endTime 40", end_time=12.0, completed=True)
    setup = prepared(tmp_path, run=run)
    setup.tool("run_solver", solver="icoFoam", n_procs=1)
    result = setup.gate()
    assert not result.ok and "does not reach endTime" in "\n".join(result.errors)


def test_the_gate_reports_whether_a_steady_run_converged(tmp_path):
    steady = prepared(tmp_path / "a", solver="simpleFoam")
    steady.tool("run_solver", solver="simpleFoam", n_procs=1)
    data = steady.gate().data
    assert data["steady"] is True and data["converged"] is False
    assert data["solver"] == "simpleFoam" and data["n_procs"] == 1
    done = OpResponse(ok=True, data=run_data(converged_at=88, end_time=88.0))
    converged = prepared(tmp_path / "b", solver="simpleFoam", run=done)
    converged.tool("run_solver", solver="simpleFoam", n_procs=1)
    data = converged.gate().data
    assert data["converged"] is True and data["converged_at"] == 88
    transient = prepared(tmp_path / "c")
    transient.tool("run_solver", solver="icoFoam", n_procs=1)
    data = transient.gate().data
    assert data["steady"] is False and data["converged"] is None
    assert data["max_courant"] == 0.32 and data["final_residuals"]["p"] == 4.5e-9


def test_the_summary_of_the_run_is_left_in_the_attempt_for_the_analyst(tmp_path):
    setup = prepared(tmp_path, solver="simpleFoam")
    setup.tool("run_solver", solver="simpleFoam", n_procs=1)
    summary = json.loads((setup.attempt / "solver_run.json").read_text(encoding="utf-8"))
    assert summary["converged"] is False and summary["ok"] is True
    assert summary["solver"] == "simpleFoam" and summary["n_procs"] == 1


# ------------------------------------------------------------------ the case the solver runs on


def test_prepare_attempt_copies_the_whole_case_of_the_run(tmp_path):
    setup = prepared(tmp_path)
    case = setup.attempt / "case"
    assert (case / "system" / "controlDict").is_file() and (case / "0" / "U").is_file()
    assert (case / "constant" / "polyMesh" / "boundary").is_file()
    assert not (setup.attempt / "boundary_manifest.json").exists()
    (case / "0" / "U").write_text("changed in the attempt\n", encoding="utf-8")
    assert "changed" not in (setup.ctx.run.root / "case" / "0" / "U").read_text(encoding="utf-8")


def test_prepare_attempt_needs_the_case_and_a_known_solver(tmp_path):
    missing = Setup(tmp_path / "a", with_case=False)
    with pytest.raises(ToolFailure, match="case/system/controlDict"):
        missing.prepare()
    no_mesh = Setup(tmp_path / "b", with_case=False)
    no_mesh.ctx.run.write_text("case/system/controlDict", "application icoFoam;\n")
    with pytest.raises(ToolFailure, match="polyMesh"):
        no_mesh.prepare()
    wrong = Setup(tmp_path / "c", solver="blockMesh")
    with pytest.raises(ToolFailure, match="blockMesh"):
        wrong.prepare()
    no_plan = Setup(tmp_path / "d")
    no_plan.ctx.casespec = None
    with pytest.raises(ToolFailure, match="CaseSpec"):
        no_plan.prepare()


def test_every_run_starts_from_a_fresh_copy_of_the_case(tmp_path):
    seen = []

    def solver(args):
        case = Path(setup.ctx.run.path(args.case_dir))
        seen.append(sorted(p.name for p in case.iterdir()))
        (case / "0.5").mkdir(exist_ok=True)  # a time directory the run leaves behind
        (case / "0.5" / "U").write_text("result\n", encoding="utf-8")
        (case / "log.icoFoam").write_text("log\n", encoding="utf-8")
        return RUN_OK

    setup = prepared(tmp_path, run=solver)
    setup.tool("run_solver", solver="icoFoam", n_procs=1)
    setup.tool("run_solver", solver="icoFoam", n_procs=1)
    assert seen[0] == seen[1] == ["0", "constant", "system"]  # no 0.5, no log, from the original
    assert (setup.attempt / "case" / "0.5" / "U").is_file()  # the last run's files are there


# ------------------------------------------------------------------ the log reader


def write_log(setup, lines):
    path = setup.attempt / "case" / "log.icoFoam"
    path.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")


def test_read_solver_log_returns_the_requested_window(tmp_path):
    setup = prepared(tmp_path)
    setup.tool("run_solver", solver="icoFoam", n_procs=1)
    write_log(setup, [f"line {i}" for i in range(1, 501)])
    text = setup.tool("read_solver_log", offset_lines=10, n_lines=3)
    assert "line 11" in text and "line 13" in text
    assert "line 10\n" not in text and "line 14" not in text
    assert "11-13 of 500" in text
    first = setup.tool("read_solver_log", offset_lines=0, n_lines=2)
    assert "line 1\n" in first and "line 2" in first


def test_read_solver_log_gives_at_most_200_lines_per_call(tmp_path):
    setup = prepared(tmp_path)
    setup.tool("run_solver", solver="icoFoam", n_procs=1)
    write_log(setup, [f"l{i}" for i in range(1, 1001)])
    assert MAX_LOG_LINES == 200
    text = setup.tool("read_solver_log", offset_lines=0, n_lines=200)
    assert "l200\n" in text or text.rstrip().endswith("l200")
    assert "l201" not in text
    too_many = setup.tool("read_solver_log", offset_lines=0, n_lines=201)
    assert too_many.startswith("error:") and "200" in too_many


@pytest.mark.parametrize(
    "arguments", [{"offset_lines": -1, "n_lines": 5}, {"offset_lines": 0, "n_lines": 0}]
)
def test_read_solver_log_refuses_out_of_range_arguments(tmp_path, arguments):
    setup = prepared(tmp_path)
    setup.tool("run_solver", solver="icoFoam", n_procs=1)
    write_log(setup, ["a", "b", "c"])
    assert setup.tool("read_solver_log", **arguments).startswith("error:")


def test_read_solver_log_past_the_end_says_how_long_the_log_is(tmp_path):
    setup = prepared(tmp_path)
    setup.tool("run_solver", solver="icoFoam", n_procs=1)
    write_log(setup, ["a", "b", "c"])
    text = setup.tool("read_solver_log", offset_lines=3, n_lines=5)
    assert text.startswith("error:") and "3 lines" in text


def test_read_solver_log_cuts_long_lines_and_keeps_the_output_bounded(tmp_path):
    setup = prepared(tmp_path)
    setup.tool("run_solver", solver="icoFoam", n_procs=1)
    write_log(setup, ["x" * 5000 for _ in range(200)])
    text = setup.tool("read_solver_log", offset_lines=0, n_lines=200)
    assert len(text.encode("utf-8")) <= 8000
    assert "x" * 1000 not in text and "cut" in text
    assert "offset_lines=" in text  # it says where to continue


def test_read_solver_log_needs_a_run_and_a_log(tmp_path):
    setup = prepared(tmp_path)
    assert "run_solver" in setup.tool("read_solver_log", offset_lines=0, n_lines=5)
    setup.tool("run_solver", solver="icoFoam", n_procs=1)  # the fake solver wrote no log
    text = setup.tool("read_solver_log", offset_lines=0, n_lines=5)
    assert text.startswith("error:") and "no log" in text


def test_read_solver_log_falls_back_to_the_decomposition_log(tmp_path):
    run = run_failed("decomposePar failed", tool_log=f"{CASE}/log.decomposePar")
    setup = prepared(tmp_path, run=run)
    setup.tool("run_solver", solver="icoFoam", n_procs=2)
    (setup.attempt / "case" / "log.decomposePar").write_text("decompose says no\n", "utf-8")
    assert "decompose says no" in setup.tool("read_solver_log", offset_lines=0, n_lines=5)


def test_read_solver_log_reads_binary_garbage_without_failing(tmp_path):
    setup = prepared(tmp_path)
    setup.tool("run_solver", solver="icoFoam", n_procs=1)
    (setup.attempt / "case" / "log.icoFoam").write_bytes(b"ok\n\xff\xfe\x00bad\nlast\n")
    text = setup.tool("read_solver_log", offset_lines=0, n_lines=10)
    assert "ok" in text and "last" in text


# ------------------------------------------------------------------ files, scope, promotion


def test_the_stage_offers_run_solver_and_read_solver_log_and_no_writable_file(tmp_path):
    setup = prepared(tmp_path)
    assert setup.registry.names() == ["run_solver", "read_solver_log"]
    scope = SolverRunnerStage.scope
    assert scope.writable == ()
    assert not scope.can_write(f"{CASE}/system/controlDict")
    assert not scope.can_write(f"{ATTEMPT}/notes.md")


def test_the_scope_reads_the_attempt_the_plan_and_the_mesh_report_only(tmp_path):
    scope = SolverRunnerStage.scope
    for path in (
        f"{CASE}/log.icoFoam",
        f"{CASE}/system/controlDict",
        f"{CASE}/0/U",
        f"{ATTEMPT}/solver_run.json",
        "casespec.json",
        "mesh/case/checkMesh.log",
        "case_setup/notes.md",
    ):
        assert scope.can_read(path), path
    for path in ("benchmarks/x/reference.json", "run_record.json", "case/0/U", ".sandbox/x"):
        assert not scope.can_read(path), path


def test_write_file_is_refused_everywhere(tmp_path):
    ctx = Setup(tmp_path).ctx
    stage = SolverRunnerStage()
    registry = ToolRegistry()
    from cfdagent.agents.tools import builtin_tools

    for tool in builtin_tools(ctx.run, stage.scope):
        registry.add(tool)
    out = registry.dispatch(
        ToolCall(id="w", name="write_file", arguments={"path": f"{CASE}/0/U", "content": "x"})
    )
    assert out.startswith("error:") and "not writable" in out


def test_promote_puts_the_solved_case_in_case_and_the_logs_in_logs(tmp_path):
    def solver(args):
        case = Path(setup.ctx.run.path(args.case_dir))
        (case / "0.5").mkdir()
        (case / "0.5" / "U").write_text("solved\n", encoding="utf-8")
        (case / "postProcessing" / "probes" / "0").mkdir(parents=True)
        (case / "postProcessing" / "probes" / "0" / "p").write_text("1 2\n", encoding="utf-8")
        (case / "log.icoFoam").write_text("solver log\n", encoding="utf-8")
        (case / "log.decomposePar").write_text("decompose log\n", encoding="utf-8")
        return RUN_OK

    setup = prepared(tmp_path, run=solver)
    setup.tool("run_solver", solver="icoFoam", n_procs=1)
    assert setup.gate().ok
    setup.stage.promote(setup.ctx, setup.attempt)
    root = setup.ctx.run.root
    assert (root / "case" / "0.5" / "U").read_text(encoding="utf-8") == "solved\n"
    assert (root / "case" / "postProcessing" / "probes" / "0" / "p").is_file()
    assert (root / "case" / "constant" / "polyMesh" / "boundary").is_file()
    assert (root / "logs" / "log.icoFoam").read_text(encoding="utf-8") == "solver log\n"
    assert (root / "logs" / "log.decomposePar").is_file()
    summary = json.loads((root / "logs" / "solver_run.json").read_text(encoding="utf-8"))
    assert summary["ok"] is True and summary["solver"] == "icoFoam"
    assert (root / "boundary_manifest.json").is_file()  # still beside case/
    assert (root / "mesh" / "case" / "checkMesh.log").is_file()


def test_promote_replaces_earlier_logs(tmp_path):
    setup = prepared(tmp_path)
    setup.ctx.run.write_text("logs/old.log", "stale\n")
    setup.tool("run_solver", solver="icoFoam", n_procs=1)
    (setup.attempt / "case" / "log.icoFoam").write_text("new\n", encoding="utf-8")
    setup.stage.promote(setup.ctx, setup.attempt)
    assert not (setup.ctx.run.root / "logs" / "old.log").exists()
    assert (setup.ctx.run.root / "logs" / "log.icoFoam").is_file()


# ------------------------------------------------------------------ the task and the prompt


def test_the_opening_message_names_the_solver_the_cell_count_and_the_rank_rules(tmp_path):
    setup = Setup(tmp_path, solver="simpleFoam")
    text = setup.stage.opening_message(setup.ctx)
    assert "simpleFoam" in text and "4096" in text
    assert "mesh/case/checkMesh.log" in text
    assert "hardware" in text and "4" in text
    assert "steady" in text.lower() or "residualControl" in text


def test_the_opening_message_survives_a_missing_cell_count(tmp_path):
    setup = Setup(tmp_path, with_case=False)
    text = setup.stage.opening_message(setup.ctx)
    assert "icoFoam" in text and "mesh/case/checkMesh.log" in text


def test_the_stage_identity():
    stage = SolverRunnerStage()
    assert (stage.name, stage.agent) == ("solver_runner", "solver_runner")


def test_the_prompt_states_the_role_the_tools_and_the_diagnosis_duty():
    text = load_prompt("solver_runner").text
    for needle in (
        "run_solver",
        "read_solver_log",
        "check_stage",
        "n_procs",
        "mesh/case/checkMesh.log",
        "FOAM FATAL",
        "Courant",
        "residual",
        "diagnosis",
        "residualControl",
    ):
        assert needle in text, needle
    assert re.search(r"never (edit|change)", text, re.IGNORECASE)
    assert "no write tool" in text.lower() or "cannot edit" in text.lower()
    assert re.search(r"\b4\b", text) and "hardware" in text.lower()


def test_the_prompt_is_general():
    text = load_prompt("solver_runner").text
    assert not re.search(r"\b(cavity|pipe|cylinder|ghia|reynolds number of)\b", text, re.I)


def test_the_diagnosis_question_asks_for_a_cause_and_a_specific_fix(tmp_path):
    setup = prepared(tmp_path)
    question = setup.stage.diagnosis_request(setup.ctx, ["the solver diverged"])
    assert question and "cause" in question.lower() and "fix" in question.lower()
    assert "cannot" in question.lower()  # the agent is told it cannot edit the case


# ------------------------------------------------------------------ the whole loop


def reply(content=None, *calls):
    return ChatResult(
        content=content,
        tool_calls=list(calls),
        usage=Usage(100, 50, None),
        parsed=None,
        raw={},
        finish_reason="tool_calls" if calls else "stop",
    )


def tool_call(call_id, name, **arguments):
    return ToolCall(id=call_id, name=name, arguments=arguments)


def run_call(call_id, n_procs=1):
    return tool_call(call_id, "run_solver", solver="icoFoam", n_procs=n_procs)


def scripted(setup, steps):
    from cfdagent.llm.scripted import ScriptedBackend

    setup.ctx.backend = ScriptedBackend(list(steps))
    return setup.ctx.backend


def test_diagnosis_stored_in_record(tmp_path):
    answers = [run_failed("the solver diverged", diverged=True), RUN_OK]
    setup = Setup(tmp_path, run=lambda args: answers.pop(0))
    backend = scripted(
        setup,
        [
            reply(None, run_call("r1")),
            reply(None, tool_call("k1", "check_stage")),
            reply("Divergence from the first step; the time step is too large for the mesh."),
            reply(None, run_call("r2")),
            reply(None, tool_call("k2", "check_stage")),
        ],
    )
    outcome = run_stage(setup.stage, setup.ctx)
    assert outcome.ok and outcome.attempts == 2
    first, second = setup.ctx.record.stages[0].attempts
    assert not first.ok and first.diagnosis is not None
    assert "time step is too large" in first.diagnosis
    assert second.ok and second.diagnosis is None  # a pass is not diagnosed
    asked = backend.calls[2]
    assert asked.tools is None and "cause" in asked.messages[-1]["content"].lower()
    assert (setup.ctx.run.root / "logs").is_dir()  # the passing attempt was promoted


def test_every_failed_attempt_is_diagnosed_the_last_one_too(tmp_path):
    setup = Setup(tmp_path, run=run_failed("the solver diverged", diverged=True))
    steps = []
    for i in range(1, 4):
        steps += [
            reply(None, run_call(f"r{i}")),
            reply(None, tool_call(f"k{i}", "check_stage")),
            reply(f"diagnosis {i}"),
        ]
    steps.append(reply("never asked"))
    backend = scripted(setup, steps)
    outcome = run_stage(setup.stage, setup.ctx)
    assert not outcome.ok and outcome.attempts == 3
    attempts = setup.ctx.record.stages[0].attempts
    assert [a.diagnosis for a in attempts] == ["diagnosis 1", "diagnosis 2", "diagnosis 3"]
    assert len(backend.calls) == 9  # no turn beyond the one diagnosis after the last attempt
    assert not (setup.ctx.run.root / "logs").exists()  # nothing is promoted


def test_a_stage_that_checks_before_running_the_solver_fails_the_attempt(tmp_path):
    setup = Setup(tmp_path)
    scripted(
        setup,
        [
            reply(None, tool_call("k1", "check_stage")),
            reply("I did not run the solver."),
            reply(None, run_call("r2")),
            reply(None, tool_call("k2", "check_stage")),
        ],
    )
    outcome = run_stage(setup.stage, setup.ctx)
    assert outcome.ok and outcome.attempts == 2
    first = setup.ctx.record.stages[0].attempts[0]
    assert "solver was not run" in first.errors[0]
    assert first.diagnosis == "I did not run the solver."
