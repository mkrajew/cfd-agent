"""The Solver Runner stage end to end inside the image: scripted models write the geometry, mesh
and case dictionaries, the local-mode executor runs the real solver and the real gate judges it.
Local mode imports the ``cfdagent`` under test (the checked-out sources), see
``SandboxClient._command``.

The divergent cases bypass the Case Setup stage (it would reject a huge Courant number): the case
is written by hand into the run directory, from the fixture's dictionaries.
"""

import json
import os
from pathlib import Path

import pytest
from test_case_setup_stage import CASE_FILES, fixture_text, pipeline
from test_geometry_stage import call, reply

from cfdagent.agents.case_setup import CaseSetupStage
from cfdagent.agents.loop import run_stage
from cfdagent.agents.solver_runner import SolverRunnerStage
from cfdagent.llm.client import ChatResult, Usage
from cfdagent.llm.scripted import ScriptedBackend

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SHORT_RUN = {
    "endTime         40;": "endTime         1;",
    "writeInterval   1000;": "writeInterval   100;",
}


def hardware_threads():
    affinity = getattr(os, "sched_getaffinity", None)
    return len(affinity(0)) if affinity else (os.cpu_count() or 1)


def case_setup_done(tmp_path):
    ctx = pipeline(tmp_path)
    ctx.backend = ScriptedBackend.from_json(FIXTURES / "scripted" / "case_setup_ok.json")
    outcome = run_stage(CaseSetupStage(), ctx)
    assert outcome.ok, outcome
    return ctx


def handmade_case(tmp_path, changes):
    """The Mesh stage's mesh with the fixture's dictionaries, ``changes`` applied to the
    controlDict (old text -> new text); no Case Setup stage in between."""
    ctx = pipeline(tmp_path)
    for rel in CASE_FILES:
        text = fixture_text(rel)
        if rel == "system/controlDict":
            for old, new in changes.items():
                assert old in text, old
                text = text.replace(old, new)
        ctx.run.write_text(f"case/{rel}", text)
    return ctx


def run_solver_call(call_id, n_procs=1, solver="icoFoam"):
    return call(call_id, "run_solver", solver=solver, n_procs=n_procs)


def check_call(call_id):
    return call(call_id, "check_stage")


def text_reply(text):
    return ChatResult(
        content=text,
        tool_calls=[],
        usage=Usage(100, 50, None),
        parsed=None,
        raw={},
        finish_reason="stop",
    )


def tool_results(backend):
    """The tool messages the model was shown in its last turn, by tool call id."""
    messages = backend.calls[-1].messages
    return {m["tool_call_id"]: m["content"] for m in messages if m["role"] == "tool"}


def test_solver_stage_passes_scripted_cavity(tmp_path):
    ctx = case_setup_done(tmp_path)
    backend = ScriptedBackend.from_json(FIXTURES / "scripted" / "solver_ok.json")
    ctx.backend = backend
    outcome = run_stage(SolverRunnerStage(), ctx)
    assert outcome.ok, outcome
    assert outcome.attempts == 1

    root = ctx.run.root
    stage_record = ctx.record.stages[3]
    assert stage_record.name == "solver_runner"
    assert [(a.n, a.ok, a.diagnosis) for a in stage_record.attempts] == [(1, True, None)]
    assert stage_record.ok
    case = root / "case"
    assert (case / "40").is_dir()  # the solved end time is in the promoted case
    assert (case / "postProcessing").is_dir()  # what the function objects wrote
    assert (case / "constant" / "polyMesh" / "boundary").is_file()
    assert "End" in (root / "logs" / "log.icoFoam").read_text(encoding="utf-8")
    summary = json.loads((root / "logs" / "solver_run.json").read_text(encoding="utf-8"))
    assert summary["ok"] is True and summary["solver"] == "icoFoam" and summary["n_procs"] == 1
    assert summary["end_time"] == 40 and summary["max_courant"] < 1
    assert summary["final_residuals"]["p"] < 1e-3


def test_the_model_sees_the_summary_of_the_run(tmp_path):
    ctx = handmade_case(tmp_path, SHORT_RUN)
    backend = ScriptedBackend(
        [reply(run_solver_call("r1")), reply(check_call("k1")), reply(check_call("k2"))]
    )
    ctx.backend = backend
    outcome = run_stage(SolverRunnerStage(), ctx)
    assert outcome.ok, outcome
    text = [
        m["content"]
        for m in backend.calls[1].messages
        if m["role"] == "tool" and m["tool_call_id"] == "r1"
    ][0]
    assert "reached the end time" in text and "Maximum Courant number" in text
    assert "Final residuals" in text and "Transient run" in text and "End of the log" in text


def test_solver_stage_fails_on_divergent_case(tmp_path):
    huge = {
        "deltaT          0.005;": "deltaT          50;",
        "endTime         40;": "endTime         5000;",
        "writeInterval   1000;": "writeInterval   20;",
    }
    ctx = handmade_case(tmp_path, huge)
    steps = []
    for number in (1, 2, 3):
        steps += [
            reply(run_solver_call(f"r{number}")),
            reply(check_call(f"k{number}")),
            text_reply(f"diagnosis {number}: the Courant number is far too high"),
        ]
    backend = ScriptedBackend(steps)
    ctx.backend = backend
    outcome = run_stage(SolverRunnerStage(), ctx)
    assert not outcome.ok and outcome.attempts == 3
    attempts = ctx.record.stages[-1].attempts
    assert [a.ok for a in attempts] == [False, False, False]
    assert [a.diagnosis for a in attempts] == [
        f"diagnosis {n}: the Courant number is far too high" for n in (1, 2, 3)
    ]
    errors = "\n".join(attempts[0].errors)
    assert "icoFoam run" in errors and "failed" in errors and "diverged" in errors
    assert len(errors.encode("utf-8")) <= 4000
    assert not (ctx.run.root / "logs").exists()  # nothing is promoted from a failed stage
    assert len(backend.calls) == 9


def test_a_second_call_starts_from_the_original_case(tmp_path):
    ctx = handmade_case(tmp_path, SHORT_RUN)
    backend = ScriptedBackend(
        [
            reply(run_solver_call("r1")),
            reply(run_solver_call("r2")),
            reply(call("l1", "read_solver_log", offset_lines=0, n_lines=5)),
            reply(check_call("k1")),
        ]
    )
    ctx.backend = backend
    outcome = run_stage(SolverRunnerStage(), ctx)
    assert outcome.ok, outcome
    results = tool_results(backend)
    # Both runs started at the case's start time, not at the end time of the first one.
    assert "first time 0.005" in results["r1"] and "first time 0.005" in results["r2"]
    assert results["l1"].startswith("log.icoFoam, lines 1-5 of") and "OpenFOAM" in results["l1"]


def test_the_log_reader_pages_through_the_real_log(tmp_path):
    ctx = handmade_case(tmp_path, SHORT_RUN)
    backend = ScriptedBackend(
        [
            reply(run_solver_call("r1")),
            reply(
                call("l1", "read_solver_log", offset_lines=0, n_lines=200),
                call("l2", "read_solver_log", offset_lines=10_000_000, n_lines=5),
                call("l3", "read_solver_log", offset_lines=-1, n_lines=5),
            ),
            reply(check_call("k1")),
        ]
    )
    ctx.backend = backend
    outcome = run_stage(SolverRunnerStage(), ctx)
    assert outcome.ok, outcome
    results = tool_results(backend)
    assert results["l1"].startswith("log.icoFoam, lines 1-")
    assert results["l2"].startswith("error:") and "end of the log" in results["l2"]
    assert results["l3"].startswith("error:")


@pytest.mark.skipif(hardware_threads() < 2, reason="needs at least 2 hardware threads")
def test_a_parallel_run_decomposes_runs_and_reconstructs(tmp_path):
    ctx = handmade_case(tmp_path, SHORT_RUN)
    backend = ScriptedBackend([reply(run_solver_call("r1", n_procs=2)), reply(check_call("k1"))])
    ctx.backend = backend
    outcome = run_stage(SolverRunnerStage(), ctx)
    assert outcome.ok, outcome
    root = ctx.run.root
    assert (root / "case" / "1").is_dir()  # reconstructed
    assert (root / "logs" / "log.decomposePar").is_file()
    summary = json.loads((root / "logs" / "solver_run.json").read_text(encoding="utf-8"))
    assert summary["n_procs"] == 2


@pytest.mark.skipif(hardware_threads() >= 4, reason="this machine has the 4 threads asked for")
def test_more_ranks_than_hardware_threads_is_refused_as_a_tool_error(tmp_path):
    ctx = handmade_case(tmp_path, SHORT_RUN)
    backend = ScriptedBackend(
        [
            reply(run_solver_call("r1", n_procs=4)),
            reply(run_solver_call("r2")),
            reply(check_call("k")),
        ]
    )
    ctx.backend = backend
    outcome = run_stage(SolverRunnerStage(), ctx)
    assert outcome.ok and outcome.attempts == 1  # the refusal did not use an attempt
    refused = [
        m["content"]
        for m in backend.calls[1].messages
        if m["role"] == "tool" and m["tool_call_id"] == "r1"
    ][0]
    assert refused.startswith("error:") and "hardware threads" in refused
