"""Unit tests for the case consistency check and the Case Setup stage (fake sandbox, no OpenFOAM).

The case files are the hand-written dictionaries of ``tests/fixtures/handwritten/cavity_re100``
(a fixture, not source) with a generated ``constant/polyMesh/boundary``.
"""

import json
import shutil
from pathlib import Path

import pytest
from test_geometry_stage import Clock, FakeSandbox, make_ctx, plan
from test_mesh_stage import boundary_text

from cfdagent.agents.case_setup import (
    GATE_TIMEOUT_S,
    TRIAL_STEPS,
    TRIAL_TIMEOUT_S,
    CaseSetupStage,
)
from cfdagent.agents.loop import run_stage
from cfdagent.agents.tools import ToolRegistry
from cfdagent.errors import ToolFailure
from cfdagent.executor.protocol import OpResponse
from cfdagent.llm.client import ChatResult, ToolCall, Usage
from cfdagent.llm.scripted import ScriptedBackend
from cfdagent.prompts import load_prompt
from cfdagent.sandbox.client import OUTER_TIMEOUT_MARGIN_S
from cfdagent.tools.case_check import check_case_consistency

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CASE_FIXTURE = FIXTURES / "handwritten" / "cavity_re100" / "case"
CAVITY_PATCHES = {
    "movingWall": ("wall", 64),
    "fixedWalls": ("wall", 192),
    "frontAndBack": ("empty", 8192),
}


def block(name, kind):
    """The text of one patch entry as it is written in the fixture's ``0/p``."""
    return f"{name}\n    {{\n        type            {kind};"


ZERO_GRADIENT_FRONT = block("frontAndBack", "zeroGradient")
FIXED_WALLS_ZERO_GRADIENT = block("fixedWalls", "zeroGradient")
FIXED_WALLS_EMPTY = block("fixedWalls", "empty")
MOVING_WALL_BLOCK = "    " + block("movingWall", "zeroGradient") + "\n    }\n"


def make_case(tmp_path, patches=None, name="case"):
    """A copy of the hand-written dictionaries with the boundary file of ``patches``."""
    case = tmp_path / name
    shutil.copytree(CASE_FIXTURE, case)
    polymesh = case / "constant" / "polyMesh"
    polymesh.mkdir(parents=True)
    text = boundary_text(CAVITY_PATCHES if patches is None else patches)
    (polymesh / "boundary").write_bytes(text.encode("utf-8"))
    return case


def edit(case, rel, old, new):
    path = case / rel
    text = path.read_bytes().decode("utf-8")
    assert old in text, f"{old!r} not in {rel}"
    path.write_bytes(text.replace(old, new).encode("utf-8"))


def errors_of(case, solver="icoFoam"):
    result = check_case_consistency(case, solver)
    assert result.ok == (result.errors == [])
    return result.errors


# ------------------------------------------------------------------ the consistency check


def test_the_hand_written_case_is_consistent(tmp_path):
    result = check_case_consistency(make_case(tmp_path), "icoFoam")
    assert result.ok and result.errors == []
    assert result.data["patches"] == {
        "movingWall": "wall",
        "fixedWalls": "wall",
        "frontAndBack": "empty",
    }
    assert sorted(result.data["fields"]) == ["U", "p"]


def test_bc_consistency_flags_missing_patch_entry(tmp_path):
    case = make_case(tmp_path)
    edit(case, "0/p", "    fixedWalls\n    {\n        type            zeroGradient;\n    }\n", "")
    errors = errors_of(case)
    assert len(errors) == 1
    assert "0/p" in errors[0] and "fixedWalls" in errors[0] and "no entry" in errors[0]


def test_every_field_and_every_missing_patch_is_reported_at_once(tmp_path):
    case = make_case(tmp_path)
    edit(
        case,
        "0/p",
        "movingWall\n    {\n        type            zeroGradient;",
        "gone\n    {\n        type zeroGradient;",
    )
    edit(case, "0/U", "    movingWall\n", "    other\n")
    errors = errors_of(case)
    assert any("0/p" in e and "movingWall" in e for e in errors)
    assert any("0/U" in e and "movingWall" in e for e in errors)


def test_empty_patches_must_have_type_empty(tmp_path):
    case = make_case(tmp_path)
    edit(
        case,
        "0/p",
        "frontAndBack\n    {\n        type            empty;",
        "frontAndBack\n    {\n        type            zeroGradient;",
    )
    errors = errors_of(case)
    assert len(errors) == 1
    assert "0/p" in errors[0] and "frontAndBack" in errors[0] and "empty" in errors[0]
    assert "zeroGradient" in errors[0]


@pytest.mark.parametrize("kind", ["wedge", "symmetryPlane", "symmetry"])
def test_constraint_patches_need_the_matching_type(tmp_path, kind):
    patches = {"inlet": ("patch", 4), "side": (kind, 8)}
    case = make_case(tmp_path, patches)
    for rel in ("0/U", "0/p"):
        (case / rel).write_bytes(
            (
                "FoamFile { version 2.0; format ascii; class volScalarField; object f; }\n"
                "dimensions [0 0 0 0 0 0 0];\ninternalField uniform 0;\n"
                "boundaryField\n{\n    inlet { type zeroGradient; }\n"
                f"    side {{ type {kind}; }}\n}}\n"
            ).encode()
        )
    assert errors_of(case) == []
    edit(case, "0/p", f"side {{ type {kind}; }}", "side { type zeroGradient; }")
    errors = errors_of(case)
    assert len(errors) == 1 and "0/p" in errors[0] and kind in errors[0]


def test_a_constraint_type_on_an_ordinary_patch_is_flagged(tmp_path):
    case = make_case(tmp_path)
    edit(
        case,
        "0/p",
        "fixedWalls\n    {\n        type            zeroGradient;",
        "fixedWalls\n    {\n        type            empty;",
    )
    errors = errors_of(case)
    assert len(errors) == 1
    assert "fixedWalls" in errors[0] and "empty" in errors[0] and "wall" in errors[0]


def test_an_entry_can_match_by_regular_expression_or_patch_group(tmp_path):
    case = make_case(tmp_path)
    edit(
        case,
        "0/p",
        "    movingWall\n    {\n        type            zeroGradient;\n    }\n\n    fixedWalls\n",
        '    ".*Wall.*"\n    {\n        type            zeroGradient;\n    }\n\n    fixedWalls\n',
    )
    (case / "0" / "U").write_bytes(
        b"FoamFile { version 2.0; format ascii; class volVectorField; object U; }\n"
        b"dimensions [0 1 -1 0 0 0 0];\ninternalField uniform (0 0 0);\n"
        b"boundaryField\n{\n    wall { type noSlip; }\n    empty { type empty; }\n}\n"
    )
    assert errors_of(case) == []  # `wall` and `empty` are patch groups, ".*Wall.*" is a regex


def test_an_include_in_the_boundary_field_means_the_entries_are_not_judged_missing(tmp_path):
    case = make_case(tmp_path)
    (case / "0" / "p").write_bytes(
        b"FoamFile { version 2.0; format ascii; class volScalarField; object p; }\n"
        b"dimensions [0 2 -2 0 0 0 0];\ninternalField uniform 0;\n"
        b'boundaryField\n{\n    #include "include/patches"\n'
        b"    frontAndBack { type empty; }\n}\n"
    )
    assert errors_of(case) == []


def test_solver_must_match_the_application_of_the_control_dict(tmp_path):
    case = make_case(tmp_path)
    errors = errors_of(case, solver="pimpleFoam")
    assert len(errors) == 1
    assert "application" in errors[0] and "icoFoam" in errors[0] and "pimpleFoam" in errors[0]
    edit(case, "system/controlDict", "application     icoFoam;\n", "")
    errors = errors_of(case)
    assert len(errors) == 1 and "application" in errors[0]


@pytest.mark.parametrize("end", ["0", "-1"])
def test_end_time_must_be_above_start_time(tmp_path, end):
    case = make_case(tmp_path)
    edit(case, "system/controlDict", "endTime         40;", f"endTime         {end};")
    errors = errors_of(case)
    assert len(errors) == 1 and "endTime" in errors[0] and "startTime" in errors[0]


def test_missing_end_time_and_start_time_are_reported(tmp_path):
    case = make_case(tmp_path)
    edit(case, "system/controlDict", "endTime         40;", "")
    edit(case, "system/controlDict", "startTime       0;", "")
    errors = errors_of(case)
    assert any("endTime" in e for e in errors) and any("startTime" in e for e in errors)


def test_a_missing_control_dict_and_a_missing_boundary_file_are_reported(tmp_path):
    case = make_case(tmp_path)
    (case / "system" / "controlDict").unlink()
    assert any("controlDict" in e for e in errors_of(case))
    other = make_case(tmp_path, name="other")
    (other / "constant" / "polyMesh" / "boundary").unlink()
    assert any("polyMesh/boundary" in e for e in errors_of(other))


def test_a_field_that_cannot_be_read_is_skipped_not_judged(tmp_path):
    case = make_case(tmp_path)
    (case / "0" / "T").write_bytes(b"FoamFile { version 2.0;\nboundaryField {\n")
    result = check_case_consistency(case, "icoFoam")
    assert result.ok, result.errors  # `check_dictionaries` reports the syntax error
    assert any("0/T" in note for note in result.data["skipped"])


def test_a_case_without_a_field_file_is_reported(tmp_path):
    case = make_case(tmp_path)
    shutil.rmtree(case / "0")
    (case / "0").mkdir()
    assert any("0/" in e and "field" in e for e in errors_of(case))


def test_symbolic_links_and_hidden_files_in_0_are_not_followed(tmp_path):
    case = make_case(tmp_path)
    (case / "0" / ".hidden").write_bytes(b"not a field")
    (case / "0" / "p.orig").write_bytes(b"not a field either")
    assert errors_of(case) == []
    try:
        (case / "0" / "link").symlink_to(case / "0" / "p")
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available here")
    assert any("link" in e and "symbolic link" in e for e in errors_of(case))


# ====================================================================== the stage

ATTEMPT = "attempts/case_setup-1"
CASE = f"{ATTEMPT}/case"
POLYMESH_FILES = {
    "boundary": boundary_text(CAVITY_PATCHES),
    "points": "points of the mesh\n",
    "faces": "faces of the mesh\n",
    "owner": "owner of the mesh\n",
    "neighbour": "neighbour of the mesh\n",
}
DICTIONARIES_OK = OpResponse(ok=True, data={"errors": [], "checked": 6})


def consistent(adjust=False, delta_t=0.005):
    report = {"patches": {}, "adjust_time_step": adjust, "delta_t": delta_t}
    return OpResponse(ok=True, data={"ok": True, "errors": [], "report": report})


CONSISTENT = consistent()
TRIAL_OK = OpResponse(ok=True, data={"completed": True, "end_time": 0.015, "last_step": 3})


def failed(*errors):
    return OpResponse(ok=False, data={"errors": list(errors)}, error="\n".join(errors))


def solver_spec(name):
    return plan(solver={"suggested_solver": name, "end_time_s": 1.0, "time_step_s": 0.005})


def stage_ctx(tmp_path, solver="icoFoam", with_mesh=True, **responses):
    """A context whose run directory holds the Mesh stage's result (``case/constant/polyMesh``)."""
    sandbox = FakeSandbox(
        {
            "check_dictionaries": responses.pop("dictionaries", DICTIONARIES_OK),
            "check_case_consistency": responses.pop("consistency", CONSISTENT),
            "trial_run": responses.pop("trial", TRIAL_OK),
            "search_knowledge": responses.pop("search", OpResponse(ok=True, data={"hits": []})),
            "read_knowledge": responses.pop("read", OpResponse(ok=True, data={"text": "dict"})),
        }
    )
    tmp_path.mkdir(parents=True, exist_ok=True)
    ctx = make_ctx(tmp_path, sandbox, spec=solver_spec(solver), **responses)
    if with_mesh:
        for name, text in POLYMESH_FILES.items():
            ctx.run.write_text(f"case/constant/polyMesh/{name}", text)
    return ctx


def write_case(ctx, number=1, skip=()):
    """What a correct agent writes: the hand-written dictionaries into the attempt's case."""
    root = ctx.run.root / "attempts" / f"case_setup-{number}" / "case"
    for path in sorted(CASE_FIXTURE.rglob("*")):
        rel = path.relative_to(CASE_FIXTURE).as_posix()
        if path.is_file() and rel not in skip:
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())


class Setup:
    """One stage instance (it remembers the mesh it handed to each attempt) and its context."""

    def __init__(self, tmp_path, **kwargs):
        self.ctx = stage_ctx(tmp_path, **kwargs)
        self.stage = CaseSetupStage()
        self.attempt = None

    def prepare(self, number=1):
        self.attempt = self.ctx.run.attempt_dir("case_setup", number)
        self.stage.prepare_attempt(self.ctx, self.attempt)
        return self.attempt

    def gate(self, number=1, controldict=True):
        self.prepare(number)
        write_case(self.ctx, number, skip=() if controldict else ("system/controlDict",))
        return self.stage.execute_and_gate(self.ctx, self.attempt)

    def calls(self):
        return [c[0] for c in self.ctx.sandbox.calls]


def test_prepare_attempt_copies_the_mesh_into_a_fresh_case(tmp_path):
    setup = Setup(tmp_path)
    attempt = setup.prepare()
    polymesh = attempt / "case" / "constant" / "polyMesh"
    assert sorted(p.name for p in polymesh.iterdir()) == sorted(POLYMESH_FILES)
    assert (polymesh / "boundary").read_bytes() == POLYMESH_FILES["boundary"].encode()
    assert sorted(p.name for p in (attempt / "case").iterdir()) == ["constant"]
    assert sorted(p.name for p in (attempt / "case" / "constant").iterdir()) == ["polyMesh"]
    # A later attempt starts from the mesh again, not from the earlier attempt's files.
    write_case(setup.ctx)
    again = setup.prepare(2)
    assert sorted(p.name for p in (again / "case").iterdir()) == ["constant"]


def test_prepare_attempt_needs_the_mesh_and_a_known_solver(tmp_path):
    setup = Setup(tmp_path, with_mesh=False)
    with pytest.raises(ToolFailure, match="case/constant/polyMesh"):
        setup.prepare()
    wrong = Setup(tmp_path / "other", solver="blockMesh")
    with pytest.raises(ToolFailure, match="blockMesh"):
        wrong.prepare()
    assert wrong.calls() == []


def test_a_planner_solver_outside_the_allowed_ones_is_a_tool_failure_before_any_call(tmp_path):
    setup = Setup(tmp_path)
    setup.prepare()
    write_case(setup.ctx)
    setup.ctx.casespec = solver_spec("rhoPimpleFoam")
    with pytest.raises(ToolFailure, match="rhoPimpleFoam"):
        setup.stage.execute_and_gate(setup.ctx, setup.attempt)
    assert setup.calls() == []


def test_the_gate_runs_the_three_checks_in_order_with_the_plan_solver(tmp_path):
    setup = Setup(tmp_path)
    result = setup.gate()
    assert result.ok and result.errors == []
    assert setup.calls() == ["check_dictionaries", "check_case_consistency", "trial_run"]
    (_, dict_args, dict_timeout), (_, check_args, check_timeout), (_, trial_args, trial_timeout) = (
        setup.ctx.sandbox.calls
    )
    assert dict_args.case_dir == CASE
    assert (check_args.case_dir, check_args.solver) == (CASE, "icoFoam")
    assert (trial_args.case_dir, trial_args.solver, trial_args.steps) == (CASE, "icoFoam", 3)
    assert TRIAL_STEPS == 3
    assert dict_timeout == check_timeout == GATE_TIMEOUT_S + OUTER_TIMEOUT_MARGIN_S
    assert trial_timeout == TRIAL_TIMEOUT_S + OUTER_TIMEOUT_MARGIN_S


def test_the_gate_solver_is_the_one_the_plan_suggests(tmp_path):
    setup = Setup(tmp_path, solver="pimpleFoam")
    setup.gate()
    assert setup.ctx.sandbox.calls[1][1].solver == "pimpleFoam"
    assert setup.ctx.sandbox.calls[2][1].solver == "pimpleFoam"


def test_gate_operation_timeouts_are_clipped_to_the_time_left(tmp_path):
    clock = Clock()
    setup = Setup(tmp_path, clock=clock)
    clock.now += setup.ctx.config.limits.wall_clock_s - 50  # 50 s of the run are left
    setup.gate()
    for _, _, timeout in setup.ctx.sandbox.calls:
        assert timeout == pytest.approx(50 + OUTER_TIMEOUT_MARGIN_S)


def test_case_setup_rejects_missing_controldict_without_a_sandbox_call(tmp_path):
    setup = Setup(tmp_path)
    result = setup.gate(controldict=False)
    assert not result.ok
    assert "controlDict" in result.errors[0] and f"{CASE}/system" in result.errors[0]
    assert setup.calls() == []


def test_syntax_and_consistency_problems_are_reported_together_and_the_trial_is_skipped(tmp_path):
    setup = Setup(
        tmp_path,
        dictionaries=failed("system/fvSchemes cannot be parsed:\nbad brace"),
        consistency=failed("0/p has no entry for patch `a`"),
    )
    result = setup.gate()
    assert not result.ok
    assert "system/fvSchemes cannot be parsed" in result.errors[0]
    assert "0/p has no entry for patch `a`" in result.errors[-1]
    assert setup.calls() == ["check_dictionaries", "check_case_consistency"]


def test_every_consistency_problem_is_reported_at_once_and_the_trial_is_skipped(tmp_path):
    problems = ("0/p has no entry for patch `a`", "0/U has no entry for patch `b`", "application")
    setup = Setup(tmp_path, consistency=failed(*problems))
    result = setup.gate()
    assert not result.ok
    assert result.errors == list(problems)
    assert setup.calls() == ["check_dictionaries", "check_case_consistency"]


def test_trial_run_failure_reaches_the_agent_in_a_unit_run(tmp_path):
    tail = "noise\n" * 800 + "--> FOAM FATAL IO ERROR: keyword div(phi,U) is undefined\n"
    trial = OpResponse(
        ok=False,
        data={"tail": tail, "fatal_error": "keyword div(phi,U) is undefined", "completed": False},
        error="the solver step exited with code 1\n" + tail,
    )
    setup = Setup(tmp_path, trial=trial)
    result = setup.gate()
    assert not result.ok
    text = "\n".join(result.errors)
    assert "keyword div(phi,U) is undefined" in text and "trial run" in text
    assert len(text.encode("utf-8")) <= 4000  # the loop shows the model at most 4096 bytes
    assert setup.calls()[-1] == "trial_run"


def test_a_trial_run_failure_without_output_still_says_what_failed(tmp_path):
    trial = OpResponse(ok=False, data={"tail": ""}, error="trial run impossible: no startTime")
    result = Setup(tmp_path, trial=trial).gate()
    assert not result.ok and "no startTime" in result.errors[0]


@pytest.mark.parametrize("op", ["dictionaries", "consistency", "trial"])
def test_an_internal_failure_is_not_blamed_on_the_agent(tmp_path, op):
    broken = OpResponse(ok=False, internal=True, error="the executor failed")
    with pytest.raises(ToolFailure, match="executor"):
        Setup(tmp_path, **{op: broken}).gate()


# ------------------------------------------------------------------ the mesh stays as it was (R35)


def edit_points(polymesh):
    (polymesh / "points").write_bytes(b"edited by the agent\n")


def same_size_points(polymesh):
    (polymesh / "points").write_bytes(b"Z" * len(POLYMESH_FILES["points"]))


def add_file(polymesh):
    (polymesh / "extra").write_bytes(b"x")


def remove_file(polymesh):
    (polymesh / "owner").unlink()


def link_file(polymesh):
    target = polymesh / "faces"
    target.unlink()
    try:
        target.symlink_to(polymesh / "owner")
    except (OSError, NotImplementedError):
        pytest.skip("cannot create symbolic links here")


def replace_by_directory(polymesh):
    (polymesh / "neighbour").unlink()
    (polymesh / "neighbour").mkdir()


def remove_polymesh(polymesh):
    for item in polymesh.iterdir():
        item.unlink()
    polymesh.rmdir()


def tampered(tmp_path, action):
    setup = Setup(tmp_path)
    setup.prepare()
    write_case(setup.ctx)
    action(setup.attempt / "case" / "constant" / "polyMesh")
    return setup, setup.stage.execute_and_gate(setup.ctx, setup.attempt)


@pytest.mark.parametrize(
    "action",
    [
        edit_points,
        same_size_points,
        add_file,
        remove_file,
        link_file,
        replace_by_directory,
        remove_polymesh,
    ],
)
def test_a_changed_mesh_fails_the_gate_without_a_sandbox_call(tmp_path, action):
    setup, result = tampered(tmp_path, action)
    assert not result.ok
    text = " ".join(result.errors)
    assert "constant/polyMesh" in text and "do not edit the mesh" in text.lower()
    assert setup.calls() == []


def test_an_unchanged_mesh_passes_the_comparison(tmp_path):
    setup, result = tampered(tmp_path, lambda polymesh: None)
    assert result.ok, result.errors


def test_any_stage_instance_can_gate_an_attempt_prepared_by_another(tmp_path):
    setup = Setup(tmp_path)
    attempt = setup.prepare()
    write_case(setup.ctx)
    assert CaseSetupStage().execute_and_gate(setup.ctx, attempt).ok  # no state on the instance
    (attempt / "case" / "constant" / "polyMesh" / "points").write_bytes(b"edited\n")
    assert not CaseSetupStage().execute_and_gate(setup.ctx, attempt).ok


def test_the_mesh_is_read_with_a_size_limit(tmp_path, monkeypatch):
    import cfdagent.agents.case_setup as module

    monkeypatch.setattr(module, "POLYMESH_MAX_BYTES", 10)
    setup = Setup(tmp_path)
    with pytest.raises(ToolFailure, match="polyMesh.*larger than 10 bytes"):
        setup.prepare()


def test_a_gate_run_without_the_run_mesh_is_a_pipeline_error(tmp_path):
    setup = Setup(tmp_path)
    attempt = setup.prepare()
    write_case(setup.ctx)
    shutil.rmtree(setup.ctx.run.root / "case")
    with pytest.raises(ToolFailure, match="case/constant/polyMesh"):
        setup.stage.execute_and_gate(setup.ctx, attempt)


# ------------------------------------------------------------------ file scope (R35)


def test_scope_allows_dictionaries_and_notes_but_never_the_mesh():
    scope = CaseSetupStage.scope
    for path in (
        f"{CASE}/0/U",
        f"{CASE}/0/nut",
        f"{CASE}/constant/transportProperties",
        f"{CASE}/constant/momentumTransport",
        f"{CASE}/system/controlDict",
        f"{CASE}/system/sampleDict",
        f"{ATTEMPT}/notes.md",
    ):
        assert scope.can_write(path), path
    for path in (
        f"{CASE}/constant/polyMesh/points",
        f"{CASE}/constant/polyMesh/boundary",
        f"{CASE}/constant/polyMesh/sets/x",
        f"{CASE}/constant/polyMesh",
        "case/system/controlDict",  # the run's own case
        "case/constant/polyMesh/points",
        "mesh/notes.md",
        "casespec.json",
        "boundary_manifest.json",
        f"{ATTEMPT}/run.sh",
        f"{CASE}/1/U",
    ):
        assert not scope.can_write(path), path


def test_scope_lets_the_agent_read_its_case_the_plan_and_the_mesh_notes():
    scope = CaseSetupStage.scope
    for path in (
        "casespec.json",
        "mesh/notes.md",
        f"{CASE}/constant/polyMesh/boundary",
        f"{CASE}/system/controlDict",
        "attempts/case_setup-2/case/0/p",
        "attempts/case_setup-1/notes.md",
    ):
        assert scope.can_read(path), path
    for path in ("benchmarks/x/reference.csv", "mesh/mesh.py", "geometry/geometry.step"):
        assert not scope.can_read(path), path
    assert not scope.can_read(".sandbox/x")


# ------------------------------------------------------------------ promotion


def test_promote_replaces_the_run_case_and_keeps_notes(tmp_path):
    setup = Setup(tmp_path)
    assert setup.gate().ok
    (setup.attempt / "notes.md").write_bytes(b"Courant number 0.3\n")
    setup.ctx.run.write_text("case/stale.txt", "left from earlier")
    setup.stage.promote(setup.ctx, setup.attempt)
    root = setup.ctx.run.root
    assert sorted(p.name for p in (root / "case").iterdir()) == ["0", "constant", "system"]
    controldict = root / "case" / "system" / "controlDict"
    assert controldict.read_bytes() == (CASE_FIXTURE / "system" / "controlDict").read_bytes()
    boundary = root / "case" / "constant" / "polyMesh" / "boundary"
    assert boundary.read_bytes() == POLYMESH_FILES["boundary"].encode()
    assert (root / "case_setup" / "notes.md").read_bytes() == b"Courant number 0.3\n"
    assert not (root / "case_setup" / "case").exists()  # the case is kept once, in case/


def test_promote_refuses_a_case_whose_mesh_was_changed(tmp_path):
    setup = Setup(tmp_path)
    attempt = setup.prepare()
    (attempt / "case" / "constant" / "polyMesh" / "points").write_bytes(b"edited\n")
    with pytest.raises(ToolFailure, match="polyMesh"):
        setup.stage.promote(setup.ctx, attempt)


# ------------------------------------------------------------------ opening message and prompt


def test_opening_message_carries_the_plan_the_solver_and_where_to_work(tmp_path):
    setup = Setup(tmp_path, solver="pimpleFoam")
    text = setup.stage.opening_message(setup.ctx)
    assert "pimpleFoam" in text and "requested_outputs" in text
    assert "constant/polyMesh" in text and "do not change" in text.lower()
    assert "mesh/notes.md" not in text  # the Mesh stage left no notes
    setup.ctx.run.write_text("mesh/notes.md", "cell Reynolds number 5 near the lid\n")
    assert "mesh/notes.md" in setup.stage.opening_message(setup.ctx)


def test_the_stage_needs_the_casespec(tmp_path):
    setup = Setup(tmp_path)
    setup.ctx.casespec = None
    with pytest.raises(ToolFailure, match="CaseSpec"):
        setup.stage.opening_message(setup.ctx)


def test_prompt_is_loaded_and_covers_the_brief():
    stage = CaseSetupStage()
    assert stage.agent == "case_setup" and stage.name == "case_setup"
    text = stage.system_prompt()
    assert text == load_prompt("case_setup").text
    lowered = text.lower()
    for needle in (
        "search_knowledge",
        "read_knowledge",
        "validate_dictionaries",
        "check_stage",
        "calculator",
        "tutorial",
        "kinematic viscosity",
        "courant",
        "writeinterval",
        "requested_outputs",
        "writeformat ascii",
        "solverinfo",
        "mesh/notes.md",
        "steady",
        "transient",
        "constant/polymesh",
        "empty",
        "wedge",
        "end points",
        "p/rho",
    ):
        assert needle in lowered, needle


# ------------------------------------------------------------------ the agent's extra tools


def registry_of(setup):
    registry = ToolRegistry()
    for tool in setup.stage.extra_tools(setup.ctx, setup.prepare()):
        registry.add(tool)
    return registry


def call(name, **arguments):
    return ToolCall(id="c1", name=name, arguments=arguments)


def test_the_stage_offers_three_extra_tools(tmp_path):
    registry = registry_of(Setup(tmp_path))
    assert registry.names() == ["search_knowledge", "read_knowledge", "validate_dictionaries"]


def test_search_knowledge_tool_calls_the_op_and_lists_the_hits(tmp_path):
    hits = [
        {"path": "flow/x/system/fvSchemes", "line": 12, "text": "div(phi,U) Gauss linear;"},
        {"path": "flow/y/notes", "line": 0, "text": "(the file name matches)"},
    ]
    setup = Setup(tmp_path, search=OpResponse(ok=True, data={"hits": hits}))
    out = registry_of(setup).dispatch(call("search_knowledge", query="div phi"))
    assert "flow/x/system/fvSchemes:12: div(phi,U) Gauss linear;" in out
    assert "flow/y/notes" in out
    ((op, args, timeout),) = setup.ctx.sandbox.calls
    assert (op, args.query) == ("search_knowledge", "div phi")
    assert timeout > OUTER_TIMEOUT_MARGIN_S


def test_search_knowledge_without_hits_says_so_and_a_bad_query_is_a_tool_error(tmp_path):
    setup = Setup(tmp_path)
    registry = registry_of(setup)
    assert "no tutorial file" in registry.dispatch(call("search_knowledge", query="zzz")).lower()
    assert registry.dispatch(call("search_knowledge", query="x" * 300)).startswith("error:")
    assert registry.dispatch(call("search_knowledge")).startswith("error:")
    refused = Setup(tmp_path / "b", search=failed("the query is empty"))
    out = registry_of(refused).dispatch(call("search_knowledge", query=" "))
    assert out.startswith("error:") and "the query is empty" in out


def test_read_knowledge_tool_returns_the_text_and_bounds_the_request(tmp_path):
    setup = Setup(tmp_path, read=OpResponse(ok=True, data={"text": "application icoFoam;"}))
    out = registry_of(setup).dispatch(call("read_knowledge", path="a/b/controlDict"))
    assert out == "application icoFoam;"
    ((op, args, _),) = setup.ctx.sandbox.calls
    assert (op, args.path) == ("read_knowledge", "a/b/controlDict")
    assert args.max_bytes <= 7500  # the tool output is capped at 8000 bytes


def test_read_knowledge_refusals_are_tool_errors_not_failures(tmp_path):
    refused = failed("'../x' must stay inside the tutorial directory (no '..')")
    out = registry_of(Setup(tmp_path, read=refused)).dispatch(call("read_knowledge", path="../x"))
    assert out.startswith("error:") and "tutorial directory" in out


@pytest.mark.parametrize("tool", ["search_knowledge", "read_knowledge"])
def test_knowledge_tool_internal_failures_propagate(tmp_path, tool):
    broken = OpResponse(ok=False, internal=True, error="FOAM_TUTORIALS is not set")
    key = {"search_knowledge": "search", "read_knowledge": "read"}[tool]
    registry = registry_of(Setup(tmp_path, **{key: broken}))
    arguments = {"query": "x"} if tool == "search_knowledge" else {"path": "x"}
    with pytest.raises(ToolFailure, match="FOAM_TUTORIALS"):
        registry.dispatch(call(tool, **arguments))


def test_validate_dictionaries_reports_parse_and_consistency_problems(tmp_path):
    setup = Setup(tmp_path)
    registry = registry_of(setup)
    write_case(setup.ctx)
    assert "no problems" in registry.dispatch(call("validate_dictionaries")).lower()
    assert setup.calls() == ["check_dictionaries", "check_case_consistency"]  # no trial run
    assert setup.ctx.sandbox.calls[0][1].case_dir == CASE

    bad = Setup(tmp_path / "b", consistency=failed("0/p has no entry for patch `x`"))
    registry = registry_of(bad)
    write_case(bad.ctx)
    assert "0/p has no entry for patch `x`" in registry.dispatch(call("validate_dictionaries"))

    unparsed = Setup(tmp_path / "c", dictionaries=failed("system/fvSchemes cannot be parsed"))
    registry = registry_of(unparsed)
    write_case(unparsed.ctx)
    out = registry.dispatch(call("validate_dictionaries"))
    assert "system/fvSchemes cannot be parsed" in out
    assert unparsed.calls() == ["check_dictionaries", "check_case_consistency"]


def test_validate_dictionaries_without_a_control_dict_says_so(tmp_path):
    setup = Setup(tmp_path)
    registry = registry_of(setup)
    out = registry.dispatch(call("validate_dictionaries"))
    assert "controlDict" in out and setup.calls() == []


# ------------------------------------------------------------------ the whole loop


def reply(*calls):
    return ChatResult(
        content=None,
        tool_calls=list(calls),
        usage=Usage(100, 50, None),
        parsed=None,
        raw={},
        finish_reason="tool_calls",
    )


def write_call(path, content):
    return ToolCall(id=f"w-{path}", name="write_file", arguments={"path": path, "content": content})


def test_run_stage_with_a_scripted_agent_promotes_the_case(tmp_path):
    steps = []
    for rel in ("0/U", "0/p", "constant/transportProperties", "system/controlDict"):
        text = (CASE_FIXTURE / rel).read_bytes().decode("utf-8")
        steps.append(reply(write_call(f"{CASE}/{rel}", text)))
    steps.append(reply(write_call(f"{CASE}/constant/polyMesh/points", "mine\n")))  # refused
    steps.append(reply(ToolCall(id="k1", name="check_stage", arguments={})))
    ctx = stage_ctx(tmp_path)
    ctx.backend = ScriptedBackend(steps)
    outcome = run_stage(CaseSetupStage(), ctx)
    assert outcome.ok, outcome
    assert (ctx.run.root / "case" / "0" / "U").is_file()
    points = ctx.run.root / "case" / "constant" / "polyMesh" / "points"
    assert points.read_bytes() == POLYMESH_FILES["points"].encode()
    assert [a.ok for a in ctx.record.stages[0].attempts] == [True]


# ------------------------------------------------------------------ fix round 1: the checker


def write_field(case, rel, body, kind="volScalarField"):
    head = f"FoamFile {{ version 2.0; format ascii; class {kind}; object f; }}\n"
    (case / rel).write_bytes((head + "dimensions [0 0 0 0 0 0 0];\n" + body).encode("utf-8"))


def test_a_wildcard_with_constraint_types_included_is_accepted(tmp_path):
    case = make_case(tmp_path)
    write_field(
        case,
        "0/p",
        "internalField uniform 0;\nboundaryField\n{\n"
        "    movingWall { type zeroGradient; }\n"
        '    ".*" { type zeroGradient; }\n'
        '    #includeEtc "caseDicts/setConstraintTypes"\n}\n',
    )
    assert errors_of(case) == []


def test_entries_are_looked_up_in_openfoam_order_exact_group_then_regex(tmp_path):
    case = make_case(tmp_path)
    write_field(
        case,
        "0/p",
        "internalField uniform 0;\nboundaryField\n{\n"
        "    movingWall { type zeroGradient; }\n"
        "    empty { type empty; }\n"
        '    ".*" { type zeroGradient; }\n}\n',
    )
    assert errors_of(case) == []


def test_an_exact_entry_beats_a_later_regular_expression(tmp_path):
    case = make_case(tmp_path)
    body = (
        "internalField uniform 0;\nboundaryField\n{\n"
        "    frontAndBack { type empty; }\n"
        '    ".*" { type zeroGradient; }\n}\n'
    )
    write_field(case, "0/p", body)
    assert errors_of(case) == []


def test_the_type_of_an_empty_patch_is_never_judged_from_a_regular_expression(tmp_path):
    case = make_case(tmp_path)
    write_field(
        case,
        "0/p",
        'internalField uniform 0;\nboundaryField\n{\n    ".*" { type zeroGradient; }\n}\n',
    )
    assert errors_of(case) == []


def test_a_regular_expression_is_still_judged_for_a_wedge_patch_without_includes(tmp_path):
    case = make_case(tmp_path, {"inlet": ("patch", 4), "side": ("wedge", 8)})
    for rel in ("0/U", "0/p"):
        write_field(
            case,
            rel,
            'internalField uniform 0;\nboundaryField\n{\n    ".*" { type zeroGradient; }\n}\n',
        )
    errors = errors_of(case)
    assert errors and all("side" in e and "wedge" in e for e in errors)


def test_the_missing_entry_message_says_the_pipeline_requires_it(tmp_path):
    case = make_case(tmp_path)
    edit(case, "0/p", "    frontAndBack\n    {\n        type            empty;\n    }\n", "")
    (message,) = errors_of(case)
    assert "this pipeline requires" in message.lower() and "frontAndBack" in message
    assert "OpenFOAM requires an entry" not in message


def test_point_fields_are_not_judged_with_the_volume_field_rules(tmp_path):
    case = make_case(tmp_path)
    write_field(
        case,
        "0/pointDisplacement",
        "internalField uniform (0 0 0);\nboundaryField\n{\n    movingWall {type fixed;}\n}\n",
        kind="pointVectorField",
    )
    result = check_case_consistency(case, "icoFoam")
    assert result.ok, result.errors
    assert "pointDisplacement" not in result.data["fields"]


CONTROL_BASE = "endTime         40;"


@pytest.mark.parametrize(
    ("old", "new", "needle"),
    [
        ("writeControl    timeStep;", "writeControl    writeTime;", "function objects"),
        ("writeControl    timeStep;", "writeControl    clockTime;", "writeControl"),
        ("writeControl    timeStep;", "writeControl    none;", "writeControl"),
        ("writeInterval   1000;", "writeInterval   0.1;", "writeInterval"),
        ("writeInterval   1000;", "writeInterval   0;", "writeInterval"),
        ("writeInterval   1000;", "writeInterval   -5;", "writeInterval"),
        ("writeInterval   1000;", "writeInterval   100000;", "write time"),
        ("startFrom       startTime;", "startFrom       sometime;", "startFrom"),
        ("stopAt          endTime;", "stopAt          never;", "stopAt"),
        ("writeFormat     ascii;", "writeFormat     binary;", "ascii"),
        ("writeCompression off;", "writeCompression on;", "writeCompression"),
    ],
)
def test_control_dict_keys_that_the_trial_run_overwrites_are_validated(tmp_path, old, new, needle):
    case = make_case(tmp_path)
    edit(case, "system/controlDict", old, new)
    errors = errors_of(case)
    assert len(errors) == 1, errors
    assert "system/controlDict" in errors[0] and needle in errors[0]


def test_run_time_write_controls_need_a_write_time_before_the_end(tmp_path):
    for control, interval, ok in (
        ("runTime", "5", True),
        ("runTime", "50", False),
        ("adjustableRunTime", "10", True),
        ("adjustableRunTime", "41", False),
        ("adjustable", "10", True),
    ):
        case = make_case(tmp_path, name=f"case-{control}-{interval}")
        edit(case, "system/controlDict", "writeControl    timeStep;", f"writeControl {control};")
        edit(case, "system/controlDict", "writeInterval   1000;", f"writeInterval {interval};")
        assert (errors_of(case) == []) == ok, (control, interval)


def test_missing_control_dict_keys_are_reported_unless_an_include_may_supply_them(tmp_path):
    case = make_case(tmp_path)
    edit(case, "system/controlDict", "startFrom       startTime;\n", "")
    edit(case, "system/controlDict", "stopAt          endTime;\n", "")
    edit(case, "system/controlDict", "writeInterval   1000;\n", "")
    errors = errors_of(case)
    assert not any("startFrom" in e or "stopAt" in e for e in errors)  # OpenFOAM has defaults
    assert any("writeInterval" in e for e in errors)  # but it fails without writeInterval

    other = make_case(tmp_path, name="other")
    (other / "system" / "controlDict").write_bytes(
        b"FoamFile { version 2.0; format ascii; class dictionary; object controlDict; }\n"
        b'#include "timeSettings"\napplication icoFoam;\n'
    )
    assert errors_of(other) == []  # nothing is judged missing; what is there is right
    (other / "system" / "controlDict").write_bytes(
        b"FoamFile { version 2.0; format ascii; class dictionary; object controlDict; }\n"
        b'#include "timeSettings"\napplication pimpleFoam;\n'
    )
    assert any("application" in e for e in errors_of(other))


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("", False),
        ("adjustTimeStep yes;\n", True),
        ("adjustTimeStep on;\n", True),
        ("adjustTimeStep no;\n", False),
        ("adjustTimeStep true;\n", True),
    ],
)
def test_the_report_carries_the_time_step_settings(tmp_path, line, expected):
    case = make_case(tmp_path)
    edit(case, "system/controlDict", "deltaT          0.005;", "deltaT          0.005;\n" + line)
    data = check_case_consistency(case, "icoFoam").data
    assert data["adjust_time_step"] is expected
    assert data["delta_t"] == 0.005


# ------------------------------------------------------------------ fix round 1: the stage


def trial_with(courant):
    return OpResponse(
        ok=True, data={"completed": True, "max_courant": courant, "delta_t": 0.005, "last_step": 3}
    )


def test_a_fixed_time_step_far_above_the_courant_limit_fails_the_gate(tmp_path):
    setup = Setup(tmp_path, trial=trial_with(50.0))
    result = setup.gate()
    assert not result.ok
    text = "\n".join(result.errors)
    assert "50" in text and "Courant" in text and "0.005" in text
    assert "5e-05" in text or "0.00005" in text  # deltaT * 0.5 / 50
    assert "0.5" in text
    assert setup.calls()[-1] == "trial_run"


@pytest.mark.parametrize("solver", ["icoFoam", "pimpleFoam"])
def test_the_courant_limit_is_exceeded_only_above_ten(tmp_path, solver):
    from cfdagent.agents.case_setup import TRIAL_MAX_COURANT

    assert TRIAL_MAX_COURANT == 10
    assert Setup(tmp_path / "a", solver=solver, trial=trial_with(10.0)).gate().ok
    assert Setup(tmp_path / "b", solver=solver, trial=trial_with(0.3)).gate().ok
    assert Setup(tmp_path / "c", solver=solver, trial=trial_with(None)).gate().ok
    assert not Setup(tmp_path / "d", solver=solver, trial=trial_with(10.01)).gate().ok


def test_the_courant_gate_is_skipped_with_an_adjustable_time_step_and_for_steady_solvers(tmp_path):
    adjustable = Setup(
        tmp_path / "a",
        solver="pimpleFoam",
        consistency=consistent(adjust=True),
        trial=trial_with(80),
    )
    assert adjustable.gate().ok
    steady = Setup(tmp_path / "b", solver="simpleFoam", trial=trial_with(80.0))
    assert steady.gate().ok


def test_a_directory_polymesh_under_0_fails_the_gate(tmp_path):
    setup = Setup(tmp_path)
    setup.prepare()
    write_case(setup.ctx)
    shadow = setup.attempt / "case" / "0" / "polyMesh"
    shadow.mkdir()
    (shadow / "points").write_bytes(b"shadow mesh\n")
    result = setup.stage.execute_and_gate(setup.ctx, setup.attempt)
    assert not result.ok and "0/polyMesh" in result.errors[0]
    assert setup.calls() == []
    scope = CaseSetupStage.scope
    assert not scope.can_write(f"{CASE}/0/polyMesh/points")
    assert not scope.can_write(f"{CASE}/0/polyMesh")
    assert scope.can_write(f"{CASE}/0/U")


def test_the_mesh_logs_of_the_mesh_stage_are_readable_and_named_to_the_agent(tmp_path):
    scope = CaseSetupStage.scope
    assert scope.can_read("mesh/mesh.log") and scope.can_read("mesh/case/checkMesh.log")
    assert not scope.can_read("mesh/mesh.py")
    setup = Setup(tmp_path)
    text = setup.stage.opening_message(setup.ctx)
    assert "mesh/mesh.log" in text and "mesh/case/checkMesh.log" in text
    assert "mesh/mesh.log" in setup.stage.system_prompt()
    assert "checkMesh.log" in setup.stage.system_prompt()


def test_the_mesh_size_cap_allows_the_largest_mesh_a_run_may_have():
    import cfdagent.agents.case_setup as module
    from cfdagent.config import RunConfig

    max_cells = RunConfig.default().limits.max_cells
    assert module.POLYMESH_MAX_BYTES >= 1 << 30
    assert module.POLYMESH_MAX_BYTES >= 400 * max_cells  # an ascii polyMesh is ~170 bytes/cell


def test_a_huge_trial_failure_message_stays_small(tmp_path):
    trial = OpResponse(
        ok=False,
        data={"tail": "t" * 20000, "fatal_error": "f" * 20000},
        error="h" * 20000 + "\n" + "t" * 20000,
    )
    result = Setup(tmp_path, trial=trial).gate()
    assert not result.ok
    assert len("\n".join(result.errors).encode("utf-8")) <= 4000


def test_the_scripted_cavity_fixture_matches_the_handwritten_dictionaries():
    steps = json.loads((FIXTURES / "scripted" / "case_setup_ok.json").read_text(encoding="utf-8"))
    written = {}
    for step in steps:
        for tool_call in step["tool_calls"]:
            if tool_call["name"] == "write_file":
                path = tool_call["arguments"]["path"].removeprefix("attempts/case_setup-1/case/")
                written[path] = tool_call["arguments"]["content"]
    on_disk = {
        path.relative_to(CASE_FIXTURE).as_posix(): path.read_bytes().decode("utf-8")
        for path in CASE_FIXTURE.rglob("*")
        if path.is_file()
    }
    assert written == on_disk


def test_prompt_carries_the_rules_of_fix_round_1():
    lowered = load_prompt("case_setup").text.lower()
    for needle in (
        "`writetime` is valid only inside function objects",
        "mesh/mesh.log",
        "mesh/case/checkmesh.log",
        "dropped without any message",
        "cos(a/2)",
        "sagitta",
        "`axis`",
        "`npoints`",
        "cuttingplane",
        "sampledsurface",
        "cofr",
        "0.orig",
        "limitedlinearv",
        "named exactly",
        "noutercorrectors",
        "round-off",
        "perturbation",
        "0.16",
        "starting from",
    ):
        assert needle in lowered, needle


# ------------------------------------------------------------------ fix round 2


def test_icofoam_is_judged_even_when_it_is_told_to_adjust_the_time_step(tmp_path):
    # icoFoam ignores adjustTimeStep, so writing it must not switch the Courant gate off.
    setup = Setup(
        tmp_path, solver="icoFoam", consistency=consistent(adjust=True), trial=trial_with(114.0)
    )
    result = setup.gate()
    assert not result.ok and "Courant" in result.errors[0]


def test_start_from_is_listed_with_start_time_first_and_judged_only_when_present():
    from cfdagent.tools.case_check import START_FROM

    assert START_FROM[0] == "startTime"


def test_macros_and_directives_in_the_judged_keys_are_not_judged(tmp_path):
    case = make_case(tmp_path)
    edit(case, "system/controlDict", "writeFormat     ascii;", "writeFormat     $fmt;")
    edit(case, "system/controlDict", "writeCompression off;", "writeCompression $comp;")
    edit(case, "system/controlDict", "stopAt          endTime;", "stopAt          $stop;")
    assert errors_of(case) == []


def test_uncompressed_is_a_valid_way_to_switch_compression_off(tmp_path):
    case = make_case(tmp_path)
    edit(case, "system/controlDict", "writeCompression off;", "writeCompression uncompressed;")
    assert errors_of(case) == []


def test_a_write_time_exactly_at_the_end_is_not_a_rounding_failure(tmp_path):
    case = make_case(tmp_path)
    edit(case, "system/controlDict", "endTime         40;", "endTime         0.3;")
    edit(case, "system/controlDict", "deltaT          0.005;", "deltaT          0.1;")
    edit(case, "system/controlDict", "writeInterval   1000;", "writeInterval   3;")
    assert errors_of(case) == []


def test_the_stage_remembers_every_knowledge_path_it_was_asked_to_read(tmp_path):
    """Blind-evaluation audit: the orchestrator stores these paths in the run record."""
    setup = Setup(tmp_path, read=failed("no such file"))
    assert setup.stage.knowledge_reads == []
    registry = registry_of(setup)
    registry.dispatch(call("read_knowledge", path="a/b/controlDict"))
    registry.dispatch(call("read_knowledge", path="a/b/controlDict"))
    registry.dispatch(call("read_knowledge", path="../escape"))  # refused, still audited
    registry.dispatch(call("search_knowledge", query="div phi"))  # a search reads no file
    assert setup.stage.knowledge_reads == ["a/b/controlDict", "../escape"]
    assert CaseSetupStage().knowledge_reads == []  # per instance, never shared
