"""The Analyst stage end to end inside the image: scripted models write the geometry, mesh and case
dictionaries, the real solver runs, and the hand-written post-processing script (the scripted
Analyst's output) runs in the local-mode executor. The Scorer is called by the test, never by the
stage. Local mode imports the ``cfdagent`` under test (the checked-out sources), see
``SandboxClient._command``.
"""

import json
from pathlib import Path

import pytest
from test_geometry_stage import CAVITY_SPEC, call, reply
from test_solver_stage import SHORT_RUN, case_setup_done, check_call, handmade_case, tool_results

from cfdagent.agents.analyst import AnalystStage
from cfdagent.agents.casespec import CaseSpec
from cfdagent.agents.loop import run_stage
from cfdagent.agents.solver_runner import SolverRunnerStage
from cfdagent.bench.registry import load_registry
from cfdagent.bench.scoring import score
from cfdagent.llm.scripted import ScriptedBackend

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
OUTPUTS = [
    {"name": "u_vertical_centreline", "kind": "profile", "description": "u at x = 0.5 m, y / L"},
    {"name": "v_horizontal_centreline", "kind": "profile", "description": "v at y = 0.5 m, x / L"},
]


def with_outputs(ctx):
    ctx.casespec = CaseSpec.model_validate({**CAVITY_SPEC, "requested_outputs": OUTPUTS})
    return ctx


@pytest.fixture(scope="module")
def analysed(tmp_path_factory):
    """The whole scripted pipeline up to and including the Analyst (the solver run takes a minute),
    shared by the tests that look at its result."""
    ctx = case_setup_done(tmp_path_factory.mktemp("analysed"))
    ctx.backend = ScriptedBackend.from_json(FIXTURES / "scripted" / "solver_ok.json")
    outcome = run_stage(SolverRunnerStage(), ctx)
    assert outcome.ok, outcome
    with_outputs(ctx)
    backend = ScriptedBackend.from_json(FIXTURES / "scripted" / "analyst_ok.json")
    ctx.backend = backend
    return ctx, backend, run_stage(AnalystStage(), ctx)


def test_analyst_stage_passes_scripted_cavity(analysed):
    ctx, backend, outcome = analysed
    assert outcome.ok, outcome
    assert outcome.attempts == 1

    root = ctx.run.root
    stage_record = ctx.record.stages[4]
    assert stage_record.name == "analyst"
    assert [(a.n, a.ok) for a in stage_record.attempts] == [(1, True)]
    quantities = json.loads((root / "results" / "quantities.json").read_text(encoding="utf-8"))
    assert set(quantities) == {"u_vertical_centreline", "v_horizontal_centreline"}
    for profile in quantities.values():
        assert len(profile["x"]) == len(profile["value"]) == 129
        assert profile["x"] == sorted(profile["x"])
    assert "converge" in (root / "analysis.md").read_text(encoding="utf-8").lower()
    # The solved case of the Solver Runner is untouched: post-processing worked on a copy.
    assert (root / "case" / "40").is_dir()
    assert (root / "attempts" / "analyst-1" / "case" / "40").is_dir()
    # The real foamListTimes ran in the working copy of the case and the model saw its answer.
    listed = tool_results(backend)["u1"]
    assert "foamListTimes" in listed and "40" in listed


def test_scored_quantities_pass_the_scorer(analysed):
    ctx, _, outcome = analysed
    assert outcome.ok, outcome
    quantities = json.loads(
        (ctx.run.root / "results" / "quantities.json").read_text(encoding="utf-8")
    )
    result = score(load_registry(ROOT / "benchmarks")["cavity-re100"], quantities)
    report = "\n".join(
        f"  {c.quantity}: passed={c.passed} error={c.error} threshold={c.threshold} ({c.reason})"
        for c in result.criteria
    )
    assert result.passed is True, "benchmark criteria failed:\n" + report


def test_postprocess_runs_in_the_case_and_the_gate_reports_a_missing_script(tmp_path):
    """The allowlisted utilities really run in the sandbox, with cwd in the working copy of the
    case; a utility outside the allowlist never reaches it."""
    ctx = handmade_case(tmp_path, SHORT_RUN)
    ctx.backend = ScriptedBackend(
        [reply(call("r1", "run_solver", solver="icoFoam", n_procs=1)), reply(check_call("k1"))]
    )
    assert run_stage(SolverRunnerStage(), ctx).ok
    with_outputs(ctx)
    backend = ScriptedBackend(
        [
            reply(
                call("t1", "run_utility", argv=["foamListTimes"]),
                call(
                    "c1",
                    "run_utility",
                    argv=["postProcess", "-func", "writeCellCentres", "-latestTime"],
                ),
                call("x1", "run_utility", argv=["icoFoam"]),
            ),
            reply(call("k1", "check_stage")),
            reply(call("k2", "check_stage")),
            reply(call("k3", "check_stage")),
        ]
    )
    ctx.backend = backend
    outcome = run_stage(AnalystStage(), ctx)
    assert not outcome.ok and outcome.attempts == 3  # no post.py was ever written
    results = tool_results(backend)
    assert "1" in results["t1"]
    assert not results["c1"].startswith("error:")
    attempt = ctx.run.root / "attempts" / "analyst-1" / "case"
    assert (attempt / "1" / "C").is_file()  # the cell centres were written into the copy
    assert not (ctx.run.root / "case" / "1" / "C").exists()  # and not into the solved case
    assert results["x1"].startswith("error:") and "postProcess" in results["x1"]
    errors = "\n".join(ctx.record.stages[-1].attempts[0].errors)
    assert "post.py does not exist" in errors
