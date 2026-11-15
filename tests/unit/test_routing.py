"""Routing between stages (ADR-0006): the route request, what a route back moves aside, and what a
target stage is seeded with. No model, no sandbox: plain files in a temporary run directory."""

import dataclasses

import pytest
from test_geometry_stage import FakeSandbox, make_ctx

from cfdagent.agents.analyst import AnalystStage
from cfdagent.agents.case_setup import CaseSetupStage
from cfdagent.agents.geometry import GeometryStage
from cfdagent.agents.mesh import MeshStage
from cfdagent.agents.routing import (
    MAX_TEXT_CHARS,
    ROUTE_TARGETS,
    STAGE_ORDER,
    RouteRequest,
    fix_request_text,
    move_downstream_aside,
)
from cfdagent.rundir import RunDir, RunDirError

# ------------------------------------------------------------------ the route request


def request(**changes) -> RouteRequest:
    values = {
        "source": "solver_runner",
        "target": "case_setup",
        "origin": "gate",
        "reason": "the residuals did not fall",
        "instruction": "set a residualControl block",
    }
    values.update(changes)
    return RouteRequest(**values)


def test_the_stage_order_and_the_targets_are_the_ones_of_the_adr():
    assert STAGE_ORDER == (
        "planner",
        "geometry",
        "mesh",
        "case_setup",
        "solver_runner",
        "analyst",
        "critic",
    )
    assert ROUTE_TARGETS == frozenset({"planner", "geometry", "mesh", "case_setup", "analyst"})


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("solver_runner", "case_setup"),
        ("solver_runner", "mesh"),
        ("solver_runner", "geometry"),
        ("solver_runner", "planner"),
        ("analyst", "case_setup"),
        ("critic", "analyst"),
        ("critic", "planner"),
        ("mesh", "geometry"),
    ],
)
def test_a_route_to_an_earlier_target_is_accepted(source, target):
    found = request(source=source, target=target, origin="critic" if source == "critic" else "gate")
    assert (found.source, found.target) == (source, target)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("analyst", "solver_runner"),  # the Solver Runner edits nothing: a fix goes to case_setup
        ("critic", "solver_runner"),
        ("critic", "critic"),  # the critic is never a target
        ("solver_runner", "analyst"),  # forward
        ("solver_runner", "solver_runner"),  # itself
        ("mesh", "mesh"),
        ("mesh", "case_setup"),  # forward
        ("geometry", "planner_x"),  # unknown
        ("nowhere", "mesh"),  # unknown source
    ],
)
def test_route_to_solver_runner_or_forward_is_rejected(source, target):
    with pytest.raises(ValueError):
        request(source=source, target=target)


def test_the_origin_is_the_gate_or_the_critic():
    assert request(origin="critic").origin == "critic"
    with pytest.raises(ValueError):
        request(origin="agent")


def test_a_route_request_is_frozen():
    found = request()
    with pytest.raises(dataclasses.FrozenInstanceError):
        found.target = "mesh"  # type: ignore[misc]


def test_reason_and_instruction_are_capped_and_cleaned_like_record_errors():
    found = request(
        reason="x" * 5000 + " https://example.test/secret",
        instruction="see http://example.test/a and mail me@example.test " + "y" * 5000,
    )
    assert len(found.reason) <= MAX_TEXT_CHARS == 1000
    assert len(found.instruction) <= MAX_TEXT_CHARS
    cleaned = request(
        reason="fetch https://example.test/secret now",
        instruction="write to me@example.test or 10.1.2.3",
    )
    assert "example.test" not in cleaned.reason and "<url>" in cleaned.reason
    assert "me@example.test" not in cleaned.instruction and "10.1.2.3" not in cleaned.instruction


def test_the_instruction_is_optional():
    assert request(instruction=None).instruction is None


def test_the_fix_request_text_names_the_source_the_reason_and_the_instruction():
    text = fix_request_text(request())
    assert text == (
        "Fix requested by solver_runner: the residuals did not fall. set a residualControl block"
    )
    assert fix_request_text(request(instruction=None)) == (
        "Fix requested by solver_runner: the residuals did not fall."
    )
    assert (
        fix_request_text(request(reason="the run diverged.", instruction="lower deltaT"))
        == "Fix requested by solver_runner: the run diverged. lower deltaT"
    )


# ------------------------------------------------------------------ what moves aside

# Canonical files of a run that has passed every stage once, plus files that are never moved.
RUN_FILES = {
    "casespec.json": "plan",
    "boundary_manifest.json": "manifest",
    "geometry/geometry.py": "g",
    "geometry/geometry.step": "step",
    "mesh/mesh.py": "m",
    "mesh/case/checkMesh.log": "log",
    "case/constant/polyMesh/points": "points",
    "case/constant/polyMesh/boundary": "boundary",
    "case/constant/transportProperties": "nu",
    "case/0/U": "u",
    "case/0/p": "p",
    "case/system/controlDict": "control",
    "case/10/U": "a time directory",
    "case/log.icoFoam": "solver log",
    "case/postProcessing/forces/0/force.dat": "forces",
    "case_setup/notes.md": "notes",
    "logs/log.icoFoam": "log",
    "logs/solver_run.json": "{}",
    "analyst/post.py": "post",
    "results/quantities.json": "{}",
    "results/plots/a.png": "png",
    "analysis.md": "analysis",
    # never moved:
    "attempts/mesh-1/mesh.py": "attempt",
    "attempts/case_setup-1/case/0/U": "attempt",
    "run_record.json": "{}",
    "llm_transcript.jsonl": "",
    "task.md": "task",
    "run.log": "log",
    "status.json": "{}",
}
NEVER_MOVED = {
    "attempts/mesh-1/mesh.py",
    "attempts/case_setup-1/case/0/U",
    "run_record.json",
    "llm_transcript.jsonl",
    "task.md",
    "run.log",
    "status.json",
    "casespec.json",
}


def populated_run(tmp_path, files=None) -> RunDir:
    run = RunDir(tmp_path / "run")
    run.root.mkdir(parents=True, exist_ok=True)
    for name, text in (RUN_FILES if files is None else files).items():
        run.write_text(name, text)
    return run


def present(run: RunDir) -> set[str]:
    return {run.relative(p) for p in run.iter_files()}


def in_place_after(run: RunDir, pass_no: int) -> set[str]:
    return {name for name in present(run) if not name.startswith(f"superseded/pass-{pass_no}/")}


def test_a_route_to_case_setup_moves_the_solved_case_and_keeps_the_mesh(tmp_path):
    run = populated_run(tmp_path)
    moved = move_downstream_aside(run, "case_setup", 1)
    found = present(run)
    assert "case/constant/polyMesh/points" in found and "case/constant/polyMesh/boundary" in found
    # everything of the solved case except the mesh is gone from the canonical place ...
    for name in (
        "case/0/U",
        "case/0/p",
        "case/system/controlDict",
        "case/constant/transportProperties",
        "case/10/U",
        "case/log.icoFoam",
        "case/postProcessing/forces/0/force.dat",
    ):
        assert name not in found
        assert f"superseded/pass-1/{name}" in found
    # ... and so are the later stages' outputs
    for name in (
        "logs/log.icoFoam",
        "logs/solver_run.json",
        "analyst/post.py",
        "results/quantities.json",
        "results/plots/a.png",
        "analysis.md",
    ):
        assert name not in found and f"superseded/pass-1/{name}" in found
    # the stages up to the target and the target's own notes stay
    for name in (
        "geometry/geometry.py",
        "mesh/mesh.py",
        "mesh/case/checkMesh.log",
        "boundary_manifest.json",
        "case_setup/notes.md",
    ):
        assert name in found
    assert NEVER_MOVED <= found
    assert moved and all(isinstance(item, str) for item in moved)
    assert "case/constant/polyMesh" not in moved
    assert not any(item.startswith("case/constant/polyMesh") for item in moved)


def test_a_route_to_mesh_moves_the_whole_case_and_everything_after_the_mesh(tmp_path):
    run = populated_run(tmp_path)
    moved = move_downstream_aside(run, "mesh", 2)
    found = present(run)
    for name in (
        "case/constant/polyMesh/points",
        "case/0/U",
        "case_setup/notes.md",
        "logs/solver_run.json",
        "analyst/post.py",
        "results/quantities.json",
        "analysis.md",
    ):
        assert name not in found and f"superseded/pass-2/{name}" in found
    assert not (run.root / "case").exists()
    for name in ("geometry/geometry.py", "mesh/mesh.py", "boundary_manifest.json"):
        assert name in found  # the target's own files and what is before it stay
    assert NEVER_MOVED <= found
    assert "case" in moved and "logs" in moved


def test_a_route_to_geometry_moves_the_mesh_and_its_manifest_too(tmp_path):
    run = populated_run(tmp_path)
    move_downstream_aside(run, "geometry", 1)
    found = present(run)
    for name in ("mesh/mesh.py", "boundary_manifest.json", "case/0/U", "case_setup/notes.md"):
        assert name not in found and f"superseded/pass-1/{name}" in found
    assert "geometry/geometry.py" in found and NEVER_MOVED <= found


def test_a_route_to_the_planner_moves_every_stage_output_but_the_plan(tmp_path):
    run = populated_run(tmp_path)
    move_downstream_aside(run, "planner", 3)
    found = present(run)
    assert {n for n in found if not n.startswith("superseded/")} == NEVER_MOVED
    for name in ("geometry/geometry.py", "mesh/mesh.py", "results/quantities.json"):
        assert f"superseded/pass-3/{name}" in found


def test_a_route_to_the_analyst_moves_nothing(tmp_path):
    run = populated_run(tmp_path)
    before = present(run)
    assert move_downstream_aside(run, "analyst", 1) == []
    assert present(run) == before
    assert not (run.root / "superseded").exists()


def test_what_was_moved_is_returned_as_run_relative_paths_that_existed(tmp_path):
    run = populated_run(tmp_path)
    moved = move_downstream_aside(run, "case_setup", 1)
    assert moved == sorted(set(moved), key=moved.index)  # no duplicates
    for item in moved:
        assert not item.startswith("superseded/")
        assert (run.root / "superseded" / "pass-1" / item).exists()
        assert not (run.root / item).exists()


def test_missing_outputs_are_skipped_not_an_error(tmp_path):
    run = populated_run(tmp_path, {"casespec.json": "plan", "case/constant/polyMesh/points": "p"})
    assert move_downstream_aside(run, "mesh", 1) == ["case"]
    assert (
        move_downstream_aside(populated_run(tmp_path / "other", {"task.md": "t"}), "mesh", 1) == []
    )


def test_the_mesh_stays_when_the_case_has_no_other_file_of_the_constant_directory(tmp_path):
    run = populated_run(tmp_path, {"case/constant/polyMesh/points": "p", "case/0/U": "u"})
    moved = move_downstream_aside(run, "case_setup", 1)
    assert moved == ["case/0"]
    assert present(run) == {
        "case/constant/polyMesh/points",
        "superseded/pass-1/case/0/U",
    }


def test_passes_do_not_overwrite_each_other(tmp_path):
    run = populated_run(tmp_path, {"results/quantities.json": "pass 0", "logs/a": "x"})
    move_downstream_aside(run, "case_setup", 1)
    run.write_text("results/quantities.json", "pass 1")
    move_downstream_aside(run, "case_setup", 2)
    assert run.read_text("superseded/pass-1/results/quantities.json") == "pass 0"
    assert run.read_text("superseded/pass-2/results/quantities.json") == "pass 1"


def test_a_pass_directory_that_exists_already_is_an_error_not_a_merge(tmp_path):
    run = populated_run(tmp_path, {"results/quantities.json": "pass 1"})
    run.write_text("superseded/pass-1/results/quantities.json", "older")
    with pytest.raises(RunDirError):
        move_downstream_aside(run, "case_setup", 1)
    assert run.read_text("results/quantities.json") == "pass 1"


@pytest.mark.parametrize("target", ["solver_runner", "critic", "nowhere"])
def test_only_a_route_target_can_be_moved_to(tmp_path, target):
    with pytest.raises(ValueError):
        move_downstream_aside(populated_run(tmp_path), target, 1)


@pytest.mark.parametrize("pass_no", [0, -1, True, 1.5])
def test_the_new_pass_number_is_at_least_one(tmp_path, pass_no):
    with pytest.raises(ValueError):
        move_downstream_aside(populated_run(tmp_path), "mesh", pass_no)


# ------------------------------------------------------------------ the seed of a target stage

CANONICAL_FOR_SEEDS = {
    "geometry/geometry.py": "geometry script",
    "geometry/geometry.step": "step",
    "geometry/boundary_manifest.json": "{}",
    "geometry/notes.md": "not a script",
    "mesh/mesh.py": "mesh script",
    "mesh/mesh.msh": "msh",
    "mesh/case/checkMesh.log": "log",
    "case/0/U": "u",
    "case/0/p": "p",
    "case/0/.hidden": "never",
    "case/0/polyMesh/points": "a mesh in a time directory",
    "case/constant/transportProperties": "nu",
    "case/constant/polyMesh/points": "the mesh",
    "case/system/controlDict": "control",
    "case/system/fvSchemes": "schemes",
    "case/10/U": "a time directory",
    "case/log.icoFoam": "solver log",
    "case/postProcessing/forces/0/force.dat": "forces",
    "case_setup/notes.md": "notes",
    "analyst/post.py": "post script",
    "analysis.md": "old numbers",
    "results/quantities.json": "{}",
}


def seed(tmp_path, stage, name, number=1, pass_no=1, files=None):
    ctx = make_ctx(tmp_path, FakeSandbox({}))
    for rel, text in (CANONICAL_FOR_SEEDS if files is None else files).items():
        ctx.run.write_text(rel, text)
    attempt = ctx.run.attempt_dir(name, number, pass_no)
    carried = stage.seed_from_canonical(ctx, attempt)
    found = sorted(ctx.run.relative(p) for p in ctx.run.iter_files() if attempt in p.parents)
    return ctx, attempt, carried, found


def test_in_place_target_seeds_from_canonical_files_geometry(tmp_path):
    _, _, carried, found = seed(tmp_path, GeometryStage(), "geometry")
    assert carried == found == ["attempts/geometry-p1-1/geometry.py"]


def test_in_place_target_seeds_from_canonical_files_mesh(tmp_path):
    ctx, attempt, carried, found = seed(tmp_path, MeshStage(), "mesh")
    assert carried == found == ["attempts/mesh-p1-1/mesh.py"]
    assert (attempt / "mesh.py").read_text(encoding="utf-8") == "mesh script"


def test_in_place_target_seeds_from_canonical_files_case_setup(tmp_path):
    _, _, carried, found = seed(tmp_path, CaseSetupStage(), "case_setup")
    prefix = "attempts/case_setup-p1-1/case"
    assert (
        carried
        == found
        == [
            f"{prefix}/0/U",
            f"{prefix}/0/p",
            f"{prefix}/constant/transportProperties",
            f"{prefix}/system/controlDict",
            f"{prefix}/system/fvSchemes",
        ]
    )


def test_in_place_target_seeds_from_canonical_files_analyst(tmp_path):
    ctx, attempt, carried, found = seed(tmp_path, AnalystStage(), "analyst")
    assert carried == found == ["attempts/analyst-p1-1/post.py"]
    assert (attempt / "post.py").read_text(encoding="utf-8") == "post script"


@pytest.mark.parametrize(
    ("stage", "name"),
    [
        (GeometryStage(), "geometry"),
        (MeshStage(), "mesh"),
        (CaseSetupStage(), "case_setup"),
        (AnalystStage(), "analyst"),
    ],
)
def test_a_seed_is_empty_when_the_stage_has_no_canonical_files_yet(tmp_path, stage, name):
    _, _, carried, found = seed(tmp_path, stage, name, files={"casespec.json": "plan"})
    assert carried == [] and found == []


def test_a_seed_never_copies_a_symbolic_link(tmp_path):
    ctx = make_ctx(tmp_path, FakeSandbox({}))
    outside = tmp_path / "outside.py"
    outside.write_text("secret\n", encoding="utf-8")
    (ctx.run.root / "geometry").mkdir()
    try:
        (ctx.run.root / "geometry" / "geometry.py").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available here")
    attempt = ctx.run.attempt_dir("geometry", 1, 1)
    assert GeometryStage().seed_from_canonical(ctx, attempt) == []
    assert list(attempt.iterdir()) == []
