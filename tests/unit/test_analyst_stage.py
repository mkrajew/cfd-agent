"""Unit tests for the Analyst stage (fake sandbox, no OpenFOAM)."""

import json
import os
from pathlib import Path

import pytest
from test_casespec import make_spec
from test_geometry_stage import FakeSandbox, make_ctx

from cfdagent.agents.analyst import (
    ANALYSIS_NAME,
    QUANTITIES_MAX_BYTES,
    SCRIPT_NAME,
    UTILITIES,
    AnalystStage,
)
from cfdagent.agents.loop import run_stage
from cfdagent.agents.tools import ToolRegistry
from cfdagent.errors import AgentInputError, ToolFailure
from cfdagent.executor import ops
from cfdagent.executor.protocol import OpResponse
from cfdagent.llm.client import ToolCall
from cfdagent.llm.scripted import ScriptedBackend
from cfdagent.prompts import load_prompt
from cfdagent.sandbox.client import OUTER_TIMEOUT_MARGIN_S

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
ATTEMPT = "attempts/analyst-1"
RUN_OK = OpResponse(ok=True, data={"tail": "post.py done", "returncode": 0})
UTILITY_OK = OpResponse(ok=True, data={"tail": "Time = 40\n", "returncode": 0})
# The default plan of test_casespec asks for the profile `centreline_u` and the scalar `drag`.
GOOD = {"centreline_u": {"x": [0.0, 0.5, 1.0], "value": [0.0, 0.4, 1.0]}, "drag": 2.5}
GOOD_ANALYSIS = (
    "# Analysis\n\nThe initial residuals fell by six orders of magnitude and the profiles changed "
    "by 1e-8 between the last two write times: the run is converged.\n"
)
SOLVER_RUN = {
    "solver": "simpleFoam",
    "ok": True,
    "steady_expected": True,
    "stopped_by_residual_control": False,
    "initial_residuals": {"p": 3e-4},
    "warnings": ["a warning of the solver stage"],
}


class Setup:
    """A context whose run directory holds the solved case, and a fake sandbox whose
    ``run_script`` plays ``post.py`` by writing ``quantities`` (a dict, a string, or None)."""

    def __init__(
        self, tmp_path, quantities=GOOD, run=RUN_OK, with_case=True, spec=None, effect=None
    ):
        self.quantities = quantities
        self.effect = effect  # called with the run directory root while "post.py" runs
        self.run = run
        self.sandbox = FakeSandbox({"run_script": self._run_script, "run_utility": UTILITY_OK})
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.ctx = make_ctx(tmp_path, self.sandbox, spec=make_spec() if spec is None else spec)
        if with_case:
            write_solved_case(self.ctx)
        self.stage = AnalystStage()
        self.attempt = None
        self.registry = None

    def _run_script(self, args):
        directory = self.ctx.run.path(args.cwd)
        if self.effect is not None:
            self.effect(self.ctx.run.root)
        if self.quantities is not None:
            text = (
                self.quantities if isinstance(self.quantities, str) else json.dumps(self.quantities)
            )
            (directory / "quantities.json").write_text(text, encoding="utf-8")
        return self.run

    def prepare(self, number=1, script=True, analysis=GOOD_ANALYSIS):
        self.attempt = self.ctx.run.attempt_dir("analyst", number)
        self.stage.prepare_attempt(self.ctx, self.attempt)
        if script:
            (self.attempt / SCRIPT_NAME).write_text("print('post')\n", encoding="utf-8")
        if analysis is not None:
            (self.attempt / ANALYSIS_NAME).write_text(analysis, encoding="utf-8")
        self.registry = ToolRegistry()
        for tool in self.stage.extra_tools(self.ctx, self.attempt):
            self.registry.add(tool)
        return self.attempt

    def gate(self):
        return self.stage.execute_and_gate(self.ctx, self.attempt)

    def tool(self, name, **arguments):
        return self.registry.dispatch(ToolCall(id="c1", name=name, arguments=arguments))


def write_solved_case(ctx):
    """What the Solver Runner leaves in the run directory."""
    ctx.run.write_text("case/system/controlDict", "application simpleFoam;\nendTime 100;\n")
    ctx.run.write_text("case/constant/polyMesh/boundary", "0 ()\n")
    ctx.run.write_text("case/0/U", "internalField uniform (0 0 0);\n")
    ctx.run.write_text("case/100/U", "internalField uniform (1 0 0);\n")
    ctx.run.write_text("case/postProcessing/line/100/line_U.xy", "0 1 0 0\n")
    ctx.run.write_text("logs/log.simpleFoam", "End\n")
    ctx.run.write_text("logs/solver_run.json", json.dumps(SOLVER_RUN))


def prepared(tmp_path, **kwargs):
    prepare = {k: kwargs.pop(k) for k in ("script", "analysis") if k in kwargs}
    setup = Setup(tmp_path, **kwargs)
    setup.prepare(**prepare)
    return setup


# ------------------------------------------------------------------ the gate


def test_gate_passes_with_every_requested_output_and_an_analysis(tmp_path):
    setup = prepared(tmp_path)
    result = setup.gate()
    assert result.ok and result.errors == []
    op, args, timeout = setup.sandbox.calls[0]
    assert op == "run_script"
    assert (args.script, args.cwd, args.kind) == (f"{ATTEMPT}/post.py", ATTEMPT, None)
    assert timeout == pytest.approx(args.timeout_s + OUTER_TIMEOUT_MARGIN_S)


def test_gate_requires_every_requested_output(tmp_path):
    setup = prepared(tmp_path, quantities={"centreline_u": GOOD["centreline_u"]})
    result = setup.gate()
    assert not result.ok
    text = "\n".join(result.errors)
    assert "drag" in text and "missing" in text
    assert "`centreline_u`" in text.split("(found")[-1]  # it is listed as found, not as missing
    assert "`drag` (scalar)" in text
    # An unrequested extra quantity does not stand in for a missing one.
    setup = prepared(
        tmp_path / "b", quantities={"other": 1.0, "centreline_u": GOOD["centreline_u"]}
    )
    assert not setup.gate().ok


def test_gate_names_all_missing_outputs_at_once(tmp_path):
    setup = prepared(tmp_path, quantities={"unrelated": 3.0})
    text = "\n".join(setup.gate().errors)
    assert "centreline_u" in text and "drag" in text


@pytest.mark.parametrize(
    ("quantities", "needle"),
    [
        ({**GOOD, "drag": {"x": [0.0, 1.0], "value": [1.0, 2.0]}}, "drag"),  # profile for a scalar
        ({**GOOD, "centreline_u": 3.0}, "centreline_u"),  # scalar for a profile
        ({**GOOD, "centreline_u": {"x": [0.0, 1.0], "value": [0.0, "a"]}}, "centreline_u"),
        ({**GOOD, "centreline_u": {"x": [0.0], "value": [1.0]}}, "at least 2"),
        ({**GOOD, "drag": True}, "bool"),
    ],
)
def test_gate_rejects_wrong_kind_and_nonfinite_structure(tmp_path, quantities, needle):
    setup = prepared(tmp_path, quantities=quantities)
    result = setup.gate()
    assert not result.ok
    assert needle in "\n".join(result.errors)


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
def test_gate_rejects_nonfinite_numbers(tmp_path, bad):
    text = '{"centreline_u": {"x": [0.0, 1.0], "value": [0.0, ' + bad + ']}, "drag": 1.0}'
    setup = prepared(tmp_path, quantities=text)
    result = setup.gate()
    assert not result.ok
    assert "finite" in "\n".join(result.errors)
    setup = prepared(
        tmp_path / "scalar", quantities='{"centreline_u": ' + bad + ', "drag": ' + bad + "}"
    )
    assert not setup.gate().ok


def test_gate_names_the_kind_it_expected(tmp_path):
    setup = prepared(tmp_path, quantities={**GOOD, "drag": {"x": [0.0, 1.0], "value": [1.0, 2.0]}})
    text = "\n".join(setup.gate().errors)
    assert "scalar" in text and "profile" in text


def test_gate_rejects_a_profile_that_is_not_sorted_by_position(tmp_path):
    unsorted = {"x": [0.0, 1.0, 0.5], "value": [0.0, 1.0, 0.4]}
    setup = prepared(tmp_path, quantities={**GOOD, "centreline_u": unsorted})
    result = setup.gate()
    assert not result.ok
    assert "sorted" in "\n".join(result.errors) and "centreline_u" in "\n".join(result.errors)


def test_gate_accepts_a_profile_with_a_repeated_position(tmp_path):
    repeated = {"x": [0.0, 0.5, 0.5, 1.0], "value": [0.0, 0.2, 0.3, 1.0]}
    assert prepared(tmp_path, quantities={**GOOD, "centreline_u": repeated}).gate().ok


def test_gate_ignores_the_order_of_unrequested_profiles(tmp_path):
    extra = {"x": [1.0, 0.0], "value": [0.0, 1.0]}
    assert prepared(tmp_path, quantities={**GOOD, "extra": extra}).gate().ok


def test_gate_requires_analysis_md(tmp_path):
    setup = prepared(tmp_path, analysis=None)
    result = setup.gate()
    assert not result.ok
    assert "analysis.md does not exist" in "\n".join(result.errors)
    # Fixing it passes.
    (setup.attempt / ANALYSIS_NAME).write_text(GOOD_ANALYSIS, encoding="utf-8")
    assert setup.gate().ok


def test_gate_requires_analysis_to_discuss_convergence(tmp_path):
    setup = prepared(tmp_path, analysis="# Analysis\n\nThe flow looks fine.\n")
    result = setup.gate()
    assert not result.ok
    text = "\n".join(result.errors)
    assert "converge" in text.lower()


def test_gate_accepts_an_honest_verdict_of_non_convergence(tmp_path):
    analysis = "The residualControl was not met: the run is NOT converged (Initial p 1e-2).\n"
    assert prepared(tmp_path, analysis=analysis).gate().ok


@pytest.mark.parametrize("analysis", ["", "   \n\n"])
def test_gate_rejects_an_empty_analysis(tmp_path, analysis):
    result = prepared(tmp_path, analysis=analysis).gate()
    assert not result.ok and "empty" in "\n".join(result.errors)


def test_gate_reports_analysis_and_quantity_problems_together(tmp_path):
    setup = prepared(tmp_path, quantities={"centreline_u": GOOD["centreline_u"]}, analysis=None)
    text = "\n".join(setup.gate().errors)
    assert "drag" in text and "analysis.md" in text


def test_gate_requires_the_script(tmp_path):
    setup = prepared(tmp_path, script=False)
    result = setup.gate()
    assert not result.ok and "post.py does not exist" in "\n".join(result.errors)
    assert setup.sandbox.calls == []


def test_gate_reports_a_failing_script_with_its_output(tmp_path):
    failed = OpResponse(
        ok=False,
        data={"tail": "Traceback...\nKeyError: 'x'", "returncode": 1},
        error="the script 'post.py' exited with code 1",
    )
    setup = prepared(tmp_path, run=failed, quantities=None)
    text = "\n".join(setup.gate().errors)
    assert "post.py" in text and "exited with code 1" in text and "KeyError" in text


def test_a_stale_quantities_file_is_not_accepted(tmp_path):
    setup = prepared(tmp_path, quantities=None)
    (setup.attempt / "quantities.json").write_text(json.dumps(GOOD), encoding="utf-8")
    result = setup.gate()
    assert not result.ok
    assert "quantities.json" in "\n".join(result.errors)
    assert not (setup.attempt / "quantities.json").exists()


def test_gate_explains_a_script_that_wrote_no_quantities(tmp_path):
    text = "\n".join(prepared(tmp_path, quantities=None).gate().errors)
    assert "quantities.json" in text and "write_quantities" in text


def test_gate_rejects_quantities_that_are_not_json(tmp_path):
    result = prepared(tmp_path, quantities="{not json").gate()
    assert not result.ok and "JSON" in "\n".join(result.errors)


def test_gate_rejects_an_oversized_quantities_file(tmp_path):
    setup = prepared(tmp_path, quantities=" " * (QUANTITIES_MAX_BYTES + 1))
    result = setup.gate()
    assert not result.ok and "larger" in "\n".join(result.errors)


def test_gate_rejects_a_quantities_file_that_is_a_link(tmp_path):
    setup = prepared(tmp_path, quantities=None)
    outside = tmp_path / "elsewhere.json"
    outside.write_text(json.dumps(GOOD), encoding="utf-8")
    link = setup.attempt / "quantities.json"
    original = setup.sandbox.responses["run_script"]

    def make_link(args):
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            pytest.skip("symbolic links are not available here")
        return original(args)

    setup.sandbox.responses["run_script"] = make_link
    assert not setup.gate().ok


def test_gate_ignores_the_plots_but_promote_keeps_them(tmp_path):
    setup = prepared(tmp_path)
    assert setup.gate().ok  # no plots/ is not an error
    plots = setup.attempt / "plots"
    plots.mkdir()
    (plots / "a.png").write_bytes(b"\x89PNG")
    (plots / "notes.txt").write_text("not a plot", encoding="utf-8")
    assert setup.gate().ok
    setup.stage.promote(setup.ctx, setup.attempt)
    root = setup.ctx.run.root
    assert (root / "results" / "plots" / "a.png").read_bytes() == b"\x89PNG"
    assert not (root / "results" / "plots" / "notes.txt").exists()


# ------------------------------------------------------------------ what the script may change


def rewrite_analysis(text):
    def effect(root):
        (root / ATTEMPT / "analysis.md").write_text(text, encoding="utf-8")

    return effect


def delete_analysis(root):
    (root / ATTEMPT / "analysis.md").unlink()


def test_a_script_that_rewrites_analysis_md_without_convergence_fails_the_gate(tmp_path):
    setup = prepared(tmp_path, effect=rewrite_analysis("# Report\n\nAll fine.\n"))
    result = setup.gate()  # checked after the script, because promote copies the file it leaves
    assert not result.ok and "converge" in "\n".join(result.errors).lower()


def test_a_script_that_empties_analysis_md_fails_the_gate(tmp_path):
    result = prepared(tmp_path, effect=rewrite_analysis("")).gate()
    assert not result.ok and "empty" in "\n".join(result.errors)


def test_a_script_that_deletes_analysis_md_is_a_gate_failure_not_a_tool_failure(tmp_path):
    setup = prepared(tmp_path, effect=delete_analysis)
    result = setup.gate()
    assert not result.ok and "analysis.md does not exist" in "\n".join(result.errors)


def test_a_failing_script_that_deletes_analysis_md_reports_both(tmp_path):
    failed = OpResponse(ok=False, data={"tail": "boom", "returncode": 1}, error="exited with 1")
    setup = prepared(tmp_path, run=failed, effect=delete_analysis, quantities=None)
    text = "\n".join(setup.gate().errors)
    assert "did not run successfully" in text and "analysis.md does not exist" in text


def test_a_script_that_writes_a_good_analysis_md_passes(tmp_path):
    setup = prepared(tmp_path, analysis=None, effect=rewrite_analysis(GOOD_ANALYSIS))
    assert setup.gate().ok  # the script may produce the report; the gate looks after the run


def edit(rel, text):
    def effect(root):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")

    return effect


def remove(rel):
    def effect(root):
        (root / rel).unlink()

    return effect


@pytest.mark.parametrize(
    ("effect", "path"),
    [
        (edit("case/system/controlDict", "application icoFoam;\n"), "case/system/controlDict"),
        (edit("case/0/U", "internalField uniform (9 9 9);\n"), "case/0/U"),
        (edit("case/100/U", "internalField uniform (0 0 0);\n"), "case/100/U"),
        (edit("logs/solver_run.json", json.dumps({"ok": True})), "logs/solver_run.json"),
        (edit("logs/log.simpleFoam", "End\nedited\n"), "logs/log.simpleFoam"),
        (remove("case/100/U"), "case/100/U"),
        (edit("case/200/U", "x\n"), "case/200/U"),  # a new file in the solved case
    ],
)
def test_a_script_that_changes_the_solved_case_or_the_logs_fails_the_gate(tmp_path, effect, path):
    setup = prepared(tmp_path, effect=effect)
    result = setup.gate()
    assert not result.ok
    text = "\n".join(result.errors)
    assert "modified" in text and path in text and "only read" in text


def test_a_script_may_write_in_its_attempt_directory_including_the_case_copy(tmp_path):
    def effect(root):
        (root / ATTEMPT / "case" / "case.foam").write_text("", encoding="utf-8")
        (root / ATTEMPT / "case" / "100" / "U").write_text("changed\n", encoding="utf-8")
        (root / ATTEMPT / "plots").mkdir(exist_ok=True)

    assert prepared(tmp_path, effect=effect).gate().ok


def test_the_opening_message_clips_a_long_warning_list(tmp_path):
    setup = prepared(tmp_path)
    summary = {**SOLVER_RUN, "warnings": [f"warning {i} " + "x" * 3000 for i in range(60)]}
    setup.ctx.run.write_text("logs/solver_run.json", json.dumps(summary))
    text = setup.stage.opening_message(setup.ctx)
    assert "warning 0 " in text and "warning 59" not in text
    assert "and 40 more" in text
    assert len(text) < 20_000


# ------------------------------------------------------------------ promote


def test_promote_copies_results_and_analysis_and_replaces_older_ones(tmp_path):
    setup = prepared(tmp_path)
    root = setup.ctx.run.root
    (root / "results" / "stale").mkdir(parents=True)
    (root / "results" / "stale" / "old.txt").write_text("old", encoding="utf-8")
    assert setup.gate().ok
    setup.stage.promote(setup.ctx, setup.attempt)
    assert json.loads((root / "results" / "quantities.json").read_text(encoding="utf-8")) == GOOD
    assert (root / "analysis.md").read_text(encoding="utf-8") == GOOD_ANALYSIS
    assert not (root / "results" / "stale").exists()
    assert not (root / "results" / "case").exists()  # the working copy of the case is not a result


def test_promote_without_a_gated_attempt_is_a_tool_failure(tmp_path):
    setup = prepared(tmp_path, analysis=None)
    with pytest.raises(ToolFailure):
        setup.stage.promote(setup.ctx, setup.attempt)


# ------------------------------------------------------------------ the attempt


def test_prepare_attempt_copies_the_solved_case(tmp_path):
    setup = prepared(tmp_path)
    case = setup.attempt / "case"
    assert (case / "100" / "U").is_file()
    assert (case / "postProcessing" / "line" / "100" / "line_U.xy").is_file()
    assert (case / "system" / "controlDict").is_file()


def test_prepare_attempt_needs_the_solved_case_and_the_run_summary(tmp_path):
    setup = Setup(tmp_path / "a", with_case=False)
    attempt = setup.ctx.run.attempt_dir("analyst", 1)
    with pytest.raises(ToolFailure, match="pipeline order"):
        setup.stage.prepare_attempt(setup.ctx, attempt)
    setup = Setup(tmp_path / "b")
    (setup.ctx.run.root / "logs" / "solver_run.json").unlink()
    attempt = setup.ctx.run.attempt_dir("analyst", 1)
    with pytest.raises(ToolFailure, match="solver_run.json"):
        setup.stage.prepare_attempt(setup.ctx, attempt)


def test_the_stage_needs_the_casespec(tmp_path):
    setup = Setup(tmp_path)
    setup.ctx.casespec = None
    with pytest.raises(ToolFailure, match="CaseSpec"):
        setup.stage.opening_message(setup.ctx)


def test_opening_message_states_the_task_the_outputs_and_the_run(tmp_path):
    setup = prepared(tmp_path)
    text = setup.stage.opening_message(setup.ctx)
    assert ATTEMPT not in text or "attempts/analyst-1" in text
    for needle in ("centreline_u", "drag", "post.py", "quantities.json", "analysis.md", "plots/"):
        assert needle in text
    assert "stopped_by_residual_control" in text and "a warning of the solver stage" in text
    assert "processor" in text  # only root time directories


def test_opening_message_survives_a_summary_that_is_not_a_dict(tmp_path):
    setup = prepared(tmp_path)
    setup.ctx.run.write_text("logs/solver_run.json", "[1, 2]")
    assert "solver_run.json" in setup.stage.opening_message(setup.ctx)


# ------------------------------------------------------------------ scope and tools


def test_scope_gives_no_access_beyond_the_run_results(tmp_path):
    scope = AnalystStage.scope
    for readable in (
        "casespec.json",
        "case/system/controlDict",
        "case/postProcessing/line/100/line_U.xy",
        "logs/solver_run.json",
        "logs/log.simpleFoam",
        f"{ATTEMPT}/post.py",
        f"{ATTEMPT}/case/100/U",
    ):
        assert scope.can_read(readable), readable
    for hidden in (
        "benchmarks/cavity-re100/task.md",
        "benchmarks/x/reference/a.csv",
        "geometry/geometry.step",
        "mesh/mesh.py",
        "case_setup/notes.md",
        "attempts/solver_runner-1/solver_run.json",
        "attempts/mesh-1/mesh.py",
        ".sandbox/x.request.json",
        "run.log",
    ):
        assert not scope.can_read(hidden), hidden
    for writable in (
        f"{ATTEMPT}/post.py",
        f"{ATTEMPT}/analysis.md",
        f"{ATTEMPT}/plots/profile.png",
    ):
        assert scope.can_write(writable), writable
    for fixed in (
        f"{ATTEMPT}/quantities.json",
        f"{ATTEMPT}/case/system/controlDict",
        "case/system/controlDict",
        "logs/solver_run.json",
        "analysis.md",
        "results/quantities.json",
        "casespec.json",
    ):
        assert not scope.can_write(fixed), fixed


def test_the_analyst_has_no_knowledge_tools(tmp_path):
    setup = prepared(tmp_path)
    assert setup.registry.names() == ["run_utility"]
    assert "knowledge" not in " ".join(setup.registry.names())


def test_the_allowlist_is_a_subset_of_the_executors(tmp_path):
    assert UTILITIES <= ops.UTILITIES
    assert UTILITIES == {"postProcess", "foamListTimes"}


@pytest.mark.parametrize(
    "argv",
    [
        ["python", "-c", "print(1)"],
        ["simpleFoam"],
        ["icoFoam", "-case", "."],
        ["mpirun", "-np", "2", "postProcess"],
        ["foamDictionary", "-set", "x=1", "system/controlDict"],
        ["changeDictionary"],
        ["rm", "-rf", "."],
        ["bash", "-c", "ls"],
        ["/usr/bin/postProcess"],
        ["../postProcess"],
        ["postProcess;ls"],
    ],
)
def test_run_utility_refuses_everything_but_the_allowlist(tmp_path, argv):
    setup = prepared(tmp_path)
    text = setup.tool("run_utility", argv=argv)
    assert text.startswith("error:") and "postProcess" in text
    assert setup.sandbox.calls == []  # nothing reached the sandbox


def test_run_utility_with_no_command_is_a_text_error(tmp_path):
    setup = prepared(tmp_path)
    assert setup.tool("run_utility", argv=[]).startswith("error:")
    assert setup.sandbox.calls == []


def test_run_utility_runs_in_the_working_copy_of_the_case(tmp_path):
    setup = prepared(tmp_path)
    text = setup.tool("run_utility", argv=["postProcess", "-func", "writeCellCentres"])
    op, args, timeout = setup.sandbox.calls[0]
    assert op == "run_utility"
    assert args.argv == ["postProcess", "-func", "writeCellCentres"]
    assert args.cwd == f"{ATTEMPT}/case"
    assert timeout > 0
    assert "postProcess" in text and "Time = 40" in text


def test_run_utility_failure_is_text_for_the_agent(tmp_path):
    setup = prepared(tmp_path)
    setup.sandbox.responses["run_utility"] = OpResponse(
        ok=False,
        data={"tail": "FOAM FATAL ERROR: function not found", "returncode": 1},
        error="postProcess exited with code 1",
    )
    text = setup.tool("run_utility", argv=["postProcess", "-func", "nope"])
    assert "exited with code 1" in text and "function not found" in text


def test_run_utility_executor_failure_raises(tmp_path):
    setup = prepared(tmp_path)
    setup.sandbox.responses["run_utility"] = OpResponse(
        ok=False, data=None, error="the container did not start", internal=True
    )
    tool = next(t for t in setup.stage.extra_tools(setup.ctx, setup.attempt))
    with pytest.raises(ToolFailure):
        tool.handler(tool.args(argv=["foamListTimes"]))


def test_run_utility_refusal_of_the_executor_is_a_tool_error(tmp_path):
    setup = prepared(tmp_path)
    setup.sandbox.responses["run_utility"] = OpResponse(
        ok=False, data=None, error="argument '..' is not allowed"
    )
    tool = next(t for t in setup.stage.extra_tools(setup.ctx, setup.attempt))
    with pytest.raises(AgentInputError, match="not allowed"):
        tool.handler(tool.args(argv=["postProcess", ".."]))


# ------------------------------------------------------------------ retry and prompt


def test_a_failed_attempt_is_retried_and_asks_for_no_diagnosis(tmp_path):
    setup = prepared(tmp_path)
    assert setup.stage.retry_after_failure(["anything"]) is True
    assert setup.stage.diagnosis_request(setup.ctx, ["anything"]) is None
    assert "post.py" in setup.stage.retry_text("attempts/analyst-1")


def test_the_retry_text_with_carried_files_says_they_are_already_there(tmp_path):
    setup = prepared(tmp_path)
    carried = ["attempts/analyst-2/post.py"]
    text = setup.stage.retry_text("attempts/analyst-1", carried)
    assert "attempts/analyst-2/post.py" in text and "already in the new directory" in text
    assert "fresh copy of the solved case" in text
    assert setup.stage.retry_text("attempts/analyst-1", None) == setup.stage.retry_text(
        "attempts/analyst-1"
    )


def test_the_prompt_states_the_rules_of_the_stage():
    text = load_prompt("analyst").text
    for needle in (
        "post.py",
        "quantities.json",
        "analysis.md",
        "write_quantities",
        "run_utility",
        "p/rho",
        "processor",
        "convergence",
        "converge",
        "plots/",
        "never by hand",
        "check_stage",
    ):
        assert needle in text, needle
    # The blind-evaluation rule: no tutorial or knowledge tool, and no reference data.
    assert "read_knowledge" not in text and "search_knowledge" not in text
    assert "benchmarks/" not in text


def test_the_prompt_names_only_postprocess_functions_that_exist():
    text = load_prompt("analyst").text
    assert "flowRatePatch" in text and "patchFlowRate" not in text


def test_the_prompt_does_not_claim_a_maximum_principle_for_velocity():
    text = load_prompt("analyst").text
    assert "within the range that the boundary conditions allow" not in text
    assert "order of magnitude" in text and "bulk speed" in text


def test_the_prompt_scales_integrated_quantities_of_reduced_geometries():
    text = load_prompt("analyst").text
    assert "360 / wedge angle" in text and "mesh thickness" in text and "state the factor" in text


def test_the_prompt_qualifies_the_steadiness_check_and_the_pressure_level():
    text = load_prompt("analyst").text
    assert "spacing" in text and "does not apply" in text
    assert "arbitrary" in text
    assert "add it from the patch field" in text


def test_the_prompt_tells_to_take_end_points_from_the_mesh():
    text = load_prompt("analyst").text.lower()
    assert "end points" in text and "mesh" in text and "wedge" in text


# ------------------------------------------------------------------ with the stage loop


def test_fixture_script_is_the_handwritten_post_script():
    steps = json.loads((FIXTURES / "scripted" / "analyst_ok.json").read_text(encoding="utf-8"))
    writes = {
        c["arguments"]["path"]: c["arguments"]["content"]
        for step in steps
        for c in step["tool_calls"]
        if c["name"] == "write_file"
    }
    handwritten = FIXTURES / "handwritten" / "cavity_re100" / "post.py"
    assert writes[f"{ATTEMPT}/post.py"] == handwritten.read_text(encoding="utf-8")
    assert "converge" in writes[f"{ATTEMPT}/analysis.md"].lower()
    assert steps[-1]["tool_calls"][0]["name"] == "check_stage"
    assert any(c["name"] == "run_utility" for s in steps for c in s["tool_calls"])


PROFILE = {"x": [0.0, 0.5, 1.0], "value": [0.0, -0.2, 0.0]}
CAVITY_OUTPUTS = [
    {"name": "u_vertical_centreline", "kind": "profile", "description": "u on a line"},
    {"name": "v_horizontal_centreline", "kind": "profile", "description": "v on a line"},
]


def test_stage_loop_passes_scripted_fixture_and_promotes(tmp_path):
    spec = make_spec(requested_outputs=CAVITY_OUTPUTS)
    setup = Setup(
        tmp_path,
        spec=spec,
        quantities={"u_vertical_centreline": PROFILE, "v_horizontal_centreline": PROFILE},
    )
    backend = ScriptedBackend.from_json(FIXTURES / "scripted" / "analyst_ok.json")
    setup.ctx.backend = backend
    outcome = run_stage(AnalystStage(), setup.ctx)
    assert outcome.ok and outcome.attempts == 1, outcome
    root = setup.ctx.run.root
    assert (root / "results" / "quantities.json").is_file()
    assert "converge" in (root / "analysis.md").read_text(encoding="utf-8").lower()
    first = backend.calls[0]
    assert first.messages[0]["content"] == load_prompt("analyst").text
    assert {t["function"]["name"] for t in first.tools} == {
        "calculator",
        "read_file",
        "list_dir",
        "write_file",
        "run_utility",
        "check_stage",
    }
    assert [(a.n, a.ok) for a in setup.ctx.record.stages[0].attempts] == [(1, True)]
    assert setup.ctx.record.stages[0].name == "analyst"


def test_opening_messages_state_input_paths(tmp_path):
    setup = prepared(tmp_path)
    text = setup.stage.opening_message(setup.ctx)
    assert "working directory" in text
    assert "../../casespec.json" in text
    for name in ("`case/`", "quantities.json", "plots/", "analysis.md"):
        assert name in text
