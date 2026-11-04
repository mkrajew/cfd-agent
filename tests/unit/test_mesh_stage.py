"""Unit tests for the Mesh stage with a fake sandbox (no executor, no Gmsh, no OpenFOAM)."""

import json
import math
import re
from pathlib import Path

import pytest
from test_casespec import make_spec
from test_geometry_stage import (
    GATE_OK,
    RUN_OK,
    Clock,
    FakeSandbox,
    axisymmetric_spec,
    gate_failed,
    make_ctx,
    manifest,
    plan,
    wedge_manifest,
)

from cfdagent.agents.loop import run_stage
from cfdagent.agents.mesh import GATE_TIMEOUT_S, SCRIPT_TIMEOUT_S, MeshStage
from cfdagent.errors import ToolFailure
from cfdagent.executor.protocol import OpResponse
from cfdagent.llm.scripted import ScriptedBackend
from cfdagent.prompts import load_prompt
from cfdagent.sandbox.client import OUTER_TIMEOUT_MARGIN_S

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
ATTEMPT = "attempts/mesh-1"
STEP_TEXT = "ISO-10303-21;\n"

DEFAULT_PATCHES = {
    "inlet": ("patch", 40),
    "outlet": ("patch", 40),
    "walls": ("wall", 400),
    "frontAndBack": ("empty", 3200),
}
WEDGE_PATCHES = {
    "inlet": ("patch", 10),
    "outlet": ("patch", 10),
    "side": ("wall", 40),
    "wedgeFront": ("wedge", 400),
    "wedgeBack": ("wedge", 400),
}


def boundary_text(patches):
    """A ``constant/polyMesh/boundary`` file as OpenFOAM writes it (``name: (type, nFaces)``)."""
    header = (
        "/*--------------------------------*- C++ -*----------------------------------*\\\n"
        "| =========                 |                                                 |\n"
        "\\*---------------------------------------------------------------------------*/\n"
        "FoamFile\n{\n    version     2.0;\n    format      ascii;\n"
        '    arch        "LSB;label=32;scalar=64";\n'
        "    class       polyBoundaryMesh;\n"
        '    location    "constant/polyMesh";\n    object      boundary;\n}\n\n'
        "// * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * //\n\n"
    )
    body = "".join(
        f"    {name}\n    {{\n        type            {kind};\n"
        f"        inGroups        List<word> 1({kind});\n"
        f"        nFaces          {count};\n        startFace       0;\n    }}\n"
        for name, (kind, count) in patches.items()
    )
    return f"{header}{len(patches)}\n(\n{body})\n"


def write_boundary(ctx, args, patches):
    case = ctx.run.path(args.case_dir)
    polymesh = case / "constant" / "polyMesh"
    polymesh.mkdir(parents=True)
    (polymesh / "boundary").write_text(boundary_text(patches), encoding="utf-8")
    (polymesh / "points").write_text("p", encoding="utf-8")
    (case / "gmshToFoam.log").write_text("converted", encoding="utf-8")


def stage_ctx(tmp_path, spec=None, geometry_manifest=None, **kwargs):
    """A context with a promoted geometry (step and manifest) and a fake sandbox.

    ``patches``: the boundary patches that the fake conversion writes; ``run``, ``convert`` and
    ``check``: replacements for the responses (or callables) of the three ops.
    """
    holder = []
    patches = kwargs.pop("patches", DEFAULT_PATCHES)

    def default_convert(args):
        write_boundary(holder[0], args, patches)
        return RUN_OK

    sandbox = FakeSandbox(
        {
            "run_script": kwargs.pop("run", RUN_OK),
            "convert_mesh": kwargs.pop("convert", default_convert),
            "check_mesh": kwargs.pop("check", GATE_OK),
        }
    )
    ctx = make_ctx(tmp_path, sandbox, spec=spec, **kwargs)
    holder.append(ctx)
    data = manifest() if geometry_manifest is None else geometry_manifest
    promoted = ctx.run.root / "geometry"
    promoted.mkdir()
    (promoted / "geometry.step").write_text(STEP_TEXT, encoding="utf-8")
    (promoted / "boundary_manifest.json").write_text(json.dumps(data), encoding="utf-8")
    return ctx


def prepared_attempt(ctx, number=1, script=True):
    """What the loop hands the agent: a fresh directory with the inputs, plus the agent's script."""
    path = ctx.run.attempt_dir("mesh", number)
    MeshStage().prepare_attempt(ctx, path)
    if script:
        (path / "mesh.py").write_text("print('meshing')\n", encoding="utf-8")
    return path


def gate(ctx):
    return MeshStage().execute_and_gate(ctx, prepared_attempt(ctx))


# ------------------------------------------------------------------ the gate


def test_gate_passes_and_sends_the_plan_numbers_to_the_sandbox(tmp_path):
    ctx = stage_ctx(tmp_path)
    result = gate(ctx)
    assert result.ok and result.errors == []
    calls = ctx.sandbox.calls
    assert [c[0] for c in calls] == ["run_script", "convert_mesh", "check_mesh"]
    (
        (_, run_args, run_timeout),
        (_, convert_args, convert_timeout),
        (_, check_args, check_timeout),
    ) = calls
    assert run_args.script == f"{ATTEMPT}/mesh.py"
    assert run_args.cwd == ATTEMPT
    assert run_args.kind == "mesh"
    assert run_args.timeout_s == SCRIPT_TIMEOUT_S
    assert run_timeout == SCRIPT_TIMEOUT_S + OUTER_TIMEOUT_MARGIN_S
    assert convert_args.msh == f"{ATTEMPT}/mesh.msh"
    assert convert_args.case_dir == f"{ATTEMPT}/case"
    assert convert_args.manifest is None  # the manifest beside case/ is the attempt's copy
    assert convert_timeout == GATE_TIMEOUT_S + OUTER_TIMEOUT_MARGIN_S
    assert check_args.case_dir == f"{ATTEMPT}/case"
    assert check_args.expected_bbox == [0.0, -0.5, 0.0, 4.0, 0.5, 0.1]
    assert check_args.manifest is None
    assert check_timeout == GATE_TIMEOUT_S + OUTER_TIMEOUT_MARGIN_S


def test_gate_operation_timeouts_are_clipped_to_the_time_left(tmp_path):
    clock = Clock()
    ctx = stage_ctx(tmp_path, clock=clock)
    clock.now += ctx.config.limits.wall_clock_s - 50  # 50 s of the run are left
    gate(ctx)
    assert ctx.sandbox.calls[0][1].timeout_s == pytest.approx(50)
    for _, _, timeout in ctx.sandbox.calls[1:]:
        assert timeout == pytest.approx(50 + OUTER_TIMEOUT_MARGIN_S)


def test_missing_script_is_reported_without_a_sandbox_call(tmp_path):
    ctx = stage_ctx(tmp_path)
    result = MeshStage().execute_and_gate(ctx, prepared_attempt(ctx, script=False))
    assert not result.ok
    assert "mesh.py" in result.errors[0] and "write_file" in result.errors[0]
    assert "mesh.msh" in result.errors[0]
    assert ctx.sandbox.calls == []


def test_gate_needs_the_casespec(tmp_path):
    ctx = stage_ctx(tmp_path)
    attempt = prepared_attempt(ctx)
    ctx.casespec = None
    with pytest.raises(ToolFailure, match="CaseSpec"):
        MeshStage().execute_and_gate(ctx, attempt)


def test_script_failure_includes_the_tail_and_stops_the_gate(tmp_path):
    tail = "Traceback (most recent call last):\n  File mesh.py, line 7\nValueError: boom"
    failed = OpResponse(
        ok=False,
        data={"tail": tail, "returncode": 1, "timed_out": False},
        error=f"the script 'attempts/mesh-1/mesh.py' exited with code 1\n{tail}",
    )
    ctx = stage_ctx(tmp_path, run=failed)
    result = gate(ctx)
    assert not result.ok
    text = "\n".join(result.errors)
    assert "ValueError: boom" in text and "exited with code 1" in text
    assert [c[0] for c in ctx.sandbox.calls] == ["run_script"]


def test_a_long_script_tail_keeps_its_end_and_stays_small(tmp_path):
    tail = "noise line\n" * 600 + "RuntimeError: the real problem"
    failed = OpResponse(
        ok=False, data={"tail": tail, "returncode": 1}, error=f"exited with code 1\n{tail}"
    )
    result = gate(stage_ctx(tmp_path, run=failed))
    text = "\n".join(result.errors)
    assert "RuntimeError: the real problem" in text
    assert len(text.encode("utf-8")) <= 4000  # the loop shows the model at most 4096 bytes


def test_a_script_that_wrote_no_mesh_is_the_agents_failure(tmp_path):
    failed = OpResponse(
        ok=False,
        data={"tail": "all done", "returncode": 0},
        error="the mesh script finished but did not write mesh.msh in /job\nall done",
    )
    result = gate(stage_ctx(tmp_path, run=failed))
    assert not result.ok and "did not write mesh.msh" in result.errors[0]


@pytest.mark.parametrize("op", ["run_script", "convert_mesh", "check_mesh"])
def test_an_internal_failure_is_not_blamed_on_the_agent(tmp_path, op):
    broken = OpResponse(ok=False, internal=True, error="the executor failed")
    kwargs = {
        "run_script": {"run": broken},
        "convert_mesh": {"convert": broken},
        "check_mesh": {"check": broken},
    }[op]
    ctx = stage_ctx(tmp_path, **kwargs)
    with pytest.raises(ToolFailure, match="executor"):
        gate(ctx)


def test_a_failed_conversion_is_fed_back_with_the_defaultfaces_hint(tmp_path):
    refused = OpResponse(
        ok=False, error="patch 'inlet' from the manifest is not in constant/polyMesh/boundary"
    )
    ctx = stage_ctx(tmp_path, convert=refused)
    result = gate(ctx)
    assert not result.ok
    text = "\n".join(result.errors)
    assert "patch 'inlet' from the manifest is not in" in text
    assert "defaultFaces" in text and "assign_physical_groups" in text
    assert [c[0] for c in ctx.sandbox.calls] == ["run_script", "convert_mesh"]


def test_a_failed_gmshtofoam_run_shows_the_end_of_its_output(tmp_path):
    tail = "x\n" * 3000 + "FOAM FATAL ERROR: cannot read mesh.msh"
    refused = OpResponse(
        ok=False,
        data={"tail": tail, "returncode": 1},
        error=f"gmshToFoam exited with code 1\n{tail}",
    )
    result = gate(stage_ctx(tmp_path, convert=refused))
    text = "\n".join(result.errors)
    assert "FOAM FATAL ERROR: cannot read mesh.msh" in text
    assert len(text.encode("utf-8")) <= 4000


def test_gate_errors_from_check_mesh_are_returned(tmp_path):
    check = gate_failed(
        "max non-orthogonality is 80, above the limit 70. Refine the mesh",
        "the mesh is not one cell thick: the 'empty' patches hold 10 faces",
    )
    result = gate(stage_ctx(tmp_path, check=check))
    assert not result.ok
    assert result.errors == [
        "max non-orthogonality is 80, above the limit 70. Refine the mesh",
        "the mesh is not one cell thick: the 'empty' patches hold 10 faces",
    ]


def test_a_stale_case_directory_is_removed_before_the_script_runs(tmp_path):
    ctx = stage_ctx(tmp_path)
    attempt = prepared_attempt(ctx)
    (attempt / "case").mkdir()
    (attempt / "case" / "notes.md").write_text("left over\n", encoding="utf-8")
    result = MeshStage().execute_and_gate(ctx, attempt)
    assert result.ok, result.errors
    assert not (attempt / "case" / "notes.md").exists()


def test_a_script_that_creates_the_case_directory_is_rejected(tmp_path):
    def run_script(args):
        (ctx.run.path(args.cwd) / "case" / "constant" / "polyMesh").mkdir(parents=True)
        return RUN_OK

    ctx = stage_ctx(tmp_path, run=run_script)
    result = gate(ctx)
    assert not result.ok
    assert "case/" in result.errors[0] and "mesh.py" in result.errors[0]
    assert [c[0] for c in ctx.sandbox.calls] == ["run_script"]


def test_the_script_cannot_change_the_inputs_the_gate_uses(tmp_path):
    def run_script(args):
        cwd = ctx.run.path(args.cwd)
        (cwd / "boundary_manifest.json").write_text('{"tampered": true}', encoding="utf-8")
        (cwd / "geometry.step").write_text("tampered", encoding="utf-8")
        return RUN_OK

    ctx = stage_ctx(tmp_path, run=run_script)
    attempt = prepared_attempt(ctx)
    result = MeshStage().execute_and_gate(ctx, attempt)
    assert result.ok, result.errors
    restored = json.loads((attempt / "boundary_manifest.json").read_text(encoding="utf-8"))
    assert restored == manifest()
    assert (attempt / "geometry.step").read_text(encoding="utf-8") == STEP_TEXT


def test_a_link_left_by_the_script_is_a_failed_gate_not_a_write_through(tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text("keep", encoding="utf-8")

    def run_script(args):
        link = ctx.run.path(args.cwd) / "boundary_manifest.json"
        link.unlink()
        try:
            link.symlink_to(outside)
        except OSError:
            pytest.skip("cannot create symbolic links here")
        return RUN_OK

    ctx = stage_ctx(tmp_path, run=run_script)
    assert not gate(ctx).ok
    assert outside.read_text(encoding="utf-8") == "keep"


# ------------------------------------------------------------------ the plan's boundaries (R28)


def test_a_planned_boundary_without_faces_in_the_mesh_is_an_error(tmp_path):
    patches = {k: v for k, v in DEFAULT_PATCHES.items() if k != "outlet"}
    result = gate(stage_ctx(tmp_path, patches=patches))
    assert not result.ok
    text = "\n".join(result.errors)
    assert "`outlet`" in text and "physical group" in text
    assert "lumped" not in text  # the manifest does have the patch


def test_a_zero_face_patch_does_not_count_as_a_patch_with_faces(tmp_path):
    patches = {**DEFAULT_PATCHES, "outlet": ("patch", 0)}
    result = gate(stage_ctx(tmp_path, patches=patches))
    assert not result.ok and "`outlet`" in "\n".join(result.errors)


def test_a_planned_boundary_missing_from_the_manifest_blames_the_geometry_stage(tmp_path):
    names = ("inlet", "outlet", "walls", "frontAndBack", "baffle")
    spec = make_spec(boundaries=[{"name": n, "role": "wall", "description": "x"} for n in names])
    result = gate(stage_ctx(tmp_path, spec=spec))
    assert not result.ok
    text = "\n".join(result.errors)
    assert "`baffle`" in text and "boundary_manifest.json" in text
    assert "lumped or dropped" in text and "Geometry stage" in text
    assert "not editable" in text and "notes.md" in text
    assert "`inlet`" not in text


def test_a_boundary_that_is_a_structural_patch_may_carry_any_name(tmp_path):
    # The plan calls the front and back faces frontAndBack; the manifest calls the patch `sides`.
    data = manifest(names=("inlet", "outlet", "walls", "sides"))
    patches = {**DEFAULT_PATCHES}
    patches["sides"] = patches.pop("frontAndBack")
    result = gate(stage_ctx(tmp_path, geometry_manifest=data, patches=patches))
    assert result.ok, result.errors


def test_only_as_many_unmatched_names_as_free_structural_patches_are_exempt(tmp_path):
    """One `empty` patch with a name of its own can stand for one plan name, not for two."""
    names = ("inlet", "outlet", "walls", "frontAndBack", "baffle")
    spec = make_spec(boundaries=[{"name": n, "role": "x", "description": "x"} for n in names])
    data = manifest(names=("inlet", "outlet", "walls", "sides"))
    patches = {**DEFAULT_PATCHES}
    patches["sides"] = patches.pop("frontAndBack")
    result = gate(stage_ctx(tmp_path, spec=spec, geometry_manifest=data, patches=patches))
    text = " ".join(result.errors)
    assert not result.ok and "`baffle`" in text and "`frontAndBack`" in text
    assert "at most 1" in text


def test_the_structural_exemption_comes_from_the_manifest_type_not_from_the_name(tmp_path):
    patches = {k: v for k, v in DEFAULT_PATCHES.items() if k != "walls"}  # a wall: needs faces
    result = gate(stage_ctx(tmp_path, patches=patches))
    assert not result.ok and "`walls`" in "\n".join(result.errors)


def test_a_planned_empty_boundary_the_manifest_lacks_is_exempt_in_2d(tmp_path):
    boundaries = [
        {"name": "inlet", "role": "inlet", "description": "x"},
        {"name": "outlet", "role": "outlet", "description": "x"},
        {"name": "walls", "role": "wall", "description": "x"},
        {"name": "frontAndBack", "role": "Empty (front and back)", "description": "x"},
    ]
    data = manifest(names=("inlet", "outlet", "walls", "sides"))
    patches = {**DEFAULT_PATCHES}
    patches["sides"] = patches.pop("frontAndBack")
    spec = make_spec(boundaries=boundaries)
    assert gate(stage_ctx(tmp_path, spec=spec, geometry_manifest=data, patches=patches)).ok


def test_the_axis_of_an_axisymmetric_plan_is_exempt(tmp_path):
    spec = axisymmetric_spec(("inlet", "outlet", "side", "wedgeFront", "wedgeBack"))
    dump = spec.model_dump()
    dump["boundaries"].append({"name": "centreline", "role": "axis", "description": "the axis"})
    spec = make_spec(**{k: dump[k] for k in ("dimensionality", "geometry", "boundaries")})
    ctx = stage_ctx(tmp_path, spec=spec, geometry_manifest=wedge_manifest(), patches=WEDGE_PATCHES)
    result = gate(ctx)
    assert result.ok, result.errors


def test_the_axis_role_is_no_exemption_in_a_3d_plan(tmp_path):
    names = ("inlet", "outlet", "walls", "shaft")
    boundaries = [
        {"name": n, "role": "axis" if n == "shaft" else "x", "description": "x"} for n in names
    ]
    data = manifest(dimensionality="3D")
    data["patches"] = [p for p in data["patches"] if p["type"] != "empty"]
    patches = {k: v for k, v in DEFAULT_PATCHES.items() if k != "frontAndBack"}
    spec = make_spec(dimensionality="3D", boundaries=boundaries)
    result = gate(stage_ctx(tmp_path, spec=spec, geometry_manifest=data, patches=patches))
    assert not result.ok and "`shaft`" in "\n".join(result.errors)


def test_many_missing_boundaries_are_listed_with_bounded_names(tmp_path):
    names = ["inlet", "outlet", "walls", "frontAndBack"] + [f"b{i}" + "z" * 100 for i in range(30)]
    spec = make_spec(boundaries=[{"name": n, "role": "x", "description": "x"} for n in names])
    text = "\n".join(gate(stage_ctx(tmp_path, spec=spec)).errors)
    assert "more" in text and len(text.encode("utf-8")) < 2000


def test_an_unreadable_boundary_file_is_left_to_the_sandbox_gate(tmp_path):
    def convert(args):
        polymesh = ctx.run.path(args.case_dir) / "constant" / "polyMesh"
        polymesh.mkdir(parents=True)
        (polymesh / "boundary").write_text("not a boundary file", encoding="utf-8")
        return RUN_OK

    ctx = stage_ctx(tmp_path, convert=convert, check=gate_failed("checkMesh failed"))
    assert gate(ctx).errors == ["checkMesh failed"]


def test_plan_problems_and_sandbox_problems_are_reported_together(tmp_path):
    patches = {k: v for k, v in DEFAULT_PATCHES.items() if k != "outlet"}
    ctx = stage_ctx(tmp_path, patches=patches, check=gate_failed("max skewness is 5"))
    errors = gate(ctx).errors
    assert len(errors) == 2 and "`outlet`" in errors[0] and errors[-1] == "max skewness is 5"


# ------------------------------------------------------------------ the expected box (R29)


def wedge_spec(angle=5.0, r_inner=0.2, r_outer=1.0):
    spec = axisymmetric_spec(("inlet", "outlet", "side")).model_dump()
    half = math.radians(angle) / 2
    spec["geometry"]["wedge_angle_deg"] = angle
    spec["geometry"]["bbox"] = {
        "xmin": 0.0,
        "ymin": r_inner * math.cos(half) if r_inner else 0.0,
        "zmin": -r_outer * math.sin(half),
        "xmax": 4.0,
        "ymax": r_outer,
        "zmax": r_outer * math.sin(half),
    }
    return make_spec(**{k: spec[k] for k in ("dimensionality", "geometry", "boundaries")})


def test_axisymmetric_gate_expects_the_flat_faced_wedge_mesh(tmp_path):
    spec = wedge_spec(angle=5.0, r_inner=0.2, r_outer=1.0)
    ctx = stage_ctx(tmp_path, spec=spec, geometry_manifest=wedge_manifest(), patches=WEDGE_PATCHES)
    result = gate(ctx)
    assert result.ok, result.errors
    sent = ctx.sandbox.calls[-1][1].expected_bbox
    box = spec.expected_bbox()
    c = math.cos(math.radians(5.0) / 2)
    assert sent[0] == box[0] and sent[3] == box[3]  # x is the axis direction: unchanged
    assert sent[2] == box[2] and sent[5] == box[5]  # z: the vertices sit at radius R, angle a/2
    # The plan's smallest y is already r*cos(a/2), which the mesh reproduces; its largest y is R
    # (reached on the mid-plane), which the flat-faced mesh does not reach: R*cos(a/2).
    assert sent[1] == box[1]
    assert sent[4] == pytest.approx(box[4] * c) and sent[4] < box[4]


def test_a_wedge_that_reaches_the_axis_keeps_ymin_at_zero(tmp_path):
    spec = wedge_spec(r_inner=0.0)
    ctx = stage_ctx(tmp_path, spec=spec, geometry_manifest=wedge_manifest(), patches=WEDGE_PATCHES)
    gate(ctx)
    assert ctx.sandbox.calls[-1][1].expected_bbox[1] == 0.0


def test_a_2d_gate_expects_the_plan_box_unchanged(tmp_path):
    ctx = stage_ctx(tmp_path)
    gate(ctx)
    assert ctx.sandbox.calls[-1][1].expected_bbox == list(plan().expected_bbox())


def test_an_axisymmetric_plan_without_an_angle_is_a_pipeline_error(tmp_path):
    spec = wedge_spec()
    spec.geometry.wedge_angle_deg = None
    ctx = stage_ctx(tmp_path, spec=spec, geometry_manifest=wedge_manifest(), patches=WEDGE_PATCHES)
    with pytest.raises(ToolFailure, match="wedge_angle_deg"):
        gate(ctx)


# ------------------------------------------------------------------ inputs and promotion


def test_prepare_attempt_copies_the_promoted_geometry(tmp_path):
    ctx = stage_ctx(tmp_path)
    path = ctx.run.attempt_dir("mesh", 1)
    MeshStage().prepare_attempt(ctx, path)
    assert (path / "geometry.step").read_text(encoding="utf-8") == STEP_TEXT
    assert json.loads((path / "boundary_manifest.json").read_text(encoding="utf-8")) == manifest()
    assert sorted(p.name for p in path.iterdir()) == ["boundary_manifest.json", "geometry.step"]


@pytest.mark.parametrize("missing", ["geometry.step", "boundary_manifest.json"])
def test_prepare_attempt_with_a_missing_input_is_a_pipeline_error(tmp_path, missing):
    ctx = stage_ctx(tmp_path)
    (ctx.run.root / "geometry" / missing).unlink()
    with pytest.raises(ToolFailure, match=re.escape(missing)) as info:
        MeshStage().prepare_attempt(ctx, ctx.run.attempt_dir("mesh", 1))
    assert "geometry stage" in str(info.value)


def converted_attempt(ctx):
    """An attempt directory as the gate leaves it after a successful conversion."""
    attempt = prepared_attempt(ctx)
    (attempt / "mesh.msh").write_text("$MeshFormat\n", encoding="utf-8")
    (attempt / "mesh.log").write_text("meshed\n", encoding="utf-8")
    case = attempt / "case"
    (case / "constant" / "polyMesh").mkdir(parents=True)
    (case / "constant" / "polyMesh" / "boundary").write_text("b", encoding="utf-8")
    (case / "constant" / "polyMesh" / "points").write_text("p", encoding="utf-8")
    (case / "checkMesh.log").write_text("Mesh OK.\n", encoding="utf-8")
    (case / "system").mkdir()
    (case / "system" / "controlDict").write_text("stray", encoding="utf-8")
    return attempt


def test_promote_keeps_script_and_logs_and_hands_on_only_the_polymesh(tmp_path):
    ctx = stage_ctx(tmp_path)
    attempt = converted_attempt(ctx)
    MeshStage().promote(ctx, attempt)

    root = ctx.run.root
    assert (root / "mesh" / "mesh.py").is_file() and (root / "mesh" / "mesh.msh").is_file()
    assert (root / "mesh" / "mesh.log").is_file()
    assert (root / "mesh" / "case" / "checkMesh.log").is_file()  # the gate's logs stay readable
    assert not (root / "mesh" / "case" / "constant").exists()  # no second copy of the mesh
    held = sorted(p.relative_to(root / "case").as_posix() for p in (root / "case").rglob("*"))
    assert held == [
        "constant",
        "constant/polyMesh",
        "constant/polyMesh/boundary",
        "constant/polyMesh/points",
    ]
    assert (root / "case" / "constant" / "polyMesh" / "points").read_text() == "p"
    assert attempt.is_dir()  # the attempt itself stays
    assert (attempt / "case" / "constant" / "polyMesh" / "points").is_file()


def test_promote_puts_the_manifest_beside_the_case_directory(tmp_path):
    ctx = stage_ctx(tmp_path)
    MeshStage().promote(ctx, converted_attempt(ctx))
    beside = ctx.run.root / "boundary_manifest.json"  # where the ops look by default
    assert json.loads(beside.read_text(encoding="utf-8")) == manifest()
    assert (
        beside.read_bytes() == (ctx.run.root / "geometry" / "boundary_manifest.json").read_bytes()
    )


def test_promote_without_a_converted_mesh_is_a_pipeline_error(tmp_path):
    ctx = stage_ctx(tmp_path)
    with pytest.raises(ToolFailure, match="polyMesh"):
        MeshStage().promote(ctx, prepared_attempt(ctx))


# ------------------------------------------------------------------ scope, identity, prompt


def test_scope_lets_the_agent_write_only_its_script_and_notes():
    scope = MeshStage.scope
    for n in (1, 2, 3):
        assert scope.can_write(f"attempts/mesh-{n}/mesh.py")
        assert scope.can_write(f"attempts/mesh-{n}/notes.md")
        assert scope.can_read(f"attempts/mesh-{n}/mesh.log")
        assert scope.can_read(f"attempts/mesh-{n}/boundary_manifest.json")
        assert scope.can_read(f"attempts/mesh-{n}/case/checkMesh.log")
    assert not scope.can_write("attempts/mesh-1/boundary_manifest.json")
    assert not scope.can_write("attempts/mesh-1/geometry.step")
    assert not scope.can_write("attempts/mesh-1/mesh.msh")
    assert not scope.can_write("mesh.py")
    assert not scope.can_write("casespec.json")
    assert not scope.can_write("geometry/boundary_manifest.json")
    assert not scope.can_write("attempts/geometry-1/geometry.py")
    assert not scope.can_read("attempts/geometry-1/geometry.py")
    assert not scope.can_read("benchmarks/x/reference.json")
    assert scope.can_read("casespec.json")


def test_stage_identity_and_extra_tools(tmp_path):
    stage = MeshStage()
    ctx = stage_ctx(tmp_path)
    assert (stage.name, stage.agent) == ("mesh", "mesh")
    assert stage.extra_tools(ctx, ctx.run.attempt_dir("mesh", 1)) == []
    assert stage.system_prompt() == load_prompt("mesh").text


def test_opening_message_states_what_the_mesh_must_resolve(tmp_path):
    text = MeshStage().opening_message(stage_ctx(tmp_path))
    for needle in (
        "inlet",
        "outlet",
        "2D",
        "thickness_m",
        "target_cells",
        "8000",
        "kinematic_viscosity_m2_s",
        "reynolds_number",
        "laminar",
        "centreline_u",  # the requested outputs the mesh must resolve
        "mesh.py",
        "mesh.msh",
        "geometry.step",
        "boundary_manifest.json",
    ):
        assert needle in text, needle
    assert "Incompressible laminar flow" not in text  # not the whole plan (the summary)


def test_opening_message_of_an_axisymmetric_plan_mentions_the_wedge(tmp_path):
    ctx = stage_ctx(tmp_path, spec=wedge_spec(), geometry_manifest=wedge_manifest())
    text = MeshStage().opening_message(ctx)
    assert "wedge_angle_deg" in text and "cos(" in text


def test_prompt_states_the_contract_and_the_physics_rules():
    text = load_prompt("mesh").text
    for needle in (
        "mesh.py",
        "mesh.msh",
        "geometry.step",
        "boundary_manifest.json",
        "Mesh.MshFileVersion",
        "assign_physical_groups",
        "cfdagent.tools.gmsh_helpers",
        "BoundaryManifest",
        "defaultFaces",
        "one cell",
        "recombine",
        "Reynolds",
        "boundary layer",
        "non-orthogonality",
        "aspect ratio",
        "skewness",
        "cell count",
        "wedge",
        "numElements",
        "target_cells",
        "resolve",
    ):
        assert needle in text, needle
    assert re.search(r"do not set\s+`?Geometry\.OCCTargetUnit", text, re.I)
    assert "```python" in text  # a skeleton of the script


def test_prompt_limits_match_the_gate():
    from cfdagent.tools.mesh import DEFAULT_THRESHOLDS

    text = load_prompt("mesh").text
    assert f"{DEFAULT_THRESHOLDS.max_non_orthogonality:g}" in text
    assert f"{DEFAULT_THRESHOLDS.max_skewness:g}" in text
    assert f"{DEFAULT_THRESHOLDS.max_aspect_ratio:g}" in text


# ------------------------------------------------------------------ with the stage loop


def load_steps(name):
    return json.loads((FIXTURES / "scripted" / name).read_text(encoding="utf-8"))


def test_fixture_script_is_the_handwritten_mesh_script():
    steps = load_steps("mesh_ok.json")
    written = steps[0]["tool_calls"][0]["arguments"]
    assert written["path"] == ATTEMPT + "/mesh.py"
    handwritten = FIXTURES / "handwritten" / "cavity_re100" / "mesh.py"
    assert written["content"] == handwritten.read_text(encoding="utf-8")
    assert steps[1]["tool_calls"][0]["name"] == "check_stage"


def writes_mesh(holder):
    def run_script(args):
        (holder[0].run.path(args.cwd) / "mesh.msh").write_text("$MeshFormat\n", encoding="utf-8")
        return RUN_OK

    return run_script


def test_stage_loop_passes_promotes_and_records_one_attempt(tmp_path):
    holder = []
    ctx = stage_ctx(tmp_path, run=writes_mesh(holder))
    holder.append(ctx)
    backend = ScriptedBackend.from_json(FIXTURES / "scripted" / "mesh_ok.json")
    ctx.backend = backend
    outcome = run_stage(MeshStage(), ctx)
    assert outcome.ok and outcome.attempts == 1, outcome
    root = ctx.run.root
    assert (root / "mesh" / "mesh.py").is_file() and (root / "mesh" / "mesh.msh").is_file()
    assert (root / "case" / "constant" / "polyMesh" / "boundary").is_file()
    assert (root / "boundary_manifest.json").is_file()
    first = backend.calls[0]
    assert first.messages[0]["content"] == load_prompt("mesh").text
    assert "attempts/mesh-1" in first.messages[1]["content"]
    assert {t["function"]["name"] for t in first.tools} == {
        "calculator",
        "read_file",
        "list_dir",
        "write_file",
        "check_stage",
    }
    assert [(a.n, a.ok) for a in ctx.record.stages[0].attempts] == [(1, True)]


def test_mesh_stage_gate_errors_are_fed_back(tmp_path):
    """A failed gate reaches the model as the tool result, and the next attempt starts with the
    inputs in place."""
    first = load_steps("mesh_ok.json")
    second = json.loads(json.dumps(first))
    second[0]["tool_calls"][0]["arguments"]["path"] = "attempts/mesh-2/mesh.py"
    second[1]["tool_calls"][0]["id"] = "k2"
    steps_file = tmp_path / "steps.json"
    steps_file.write_text(json.dumps(first + second), encoding="utf-8")

    verdicts = [
        gate_failed("max non-orthogonality is 81.5, above the limit 70. Refine near the corner"),
        GATE_OK,
    ]
    holder = []
    run_script = writes_mesh(holder)
    ctx = stage_ctx(tmp_path, run=run_script, check=lambda _args: verdicts.pop(0))
    holder.append(ctx)
    backend = ScriptedBackend.from_json(steps_file)
    ctx.backend = backend
    outcome = run_stage(MeshStage(), ctx)
    assert outcome.ok and outcome.attempts == 2, outcome
    feedback = [m for m in backend.calls[2].messages if m["role"] == "tool"][-1]["content"]
    assert "81.5" in feedback and "attempts/mesh-2" in feedback and "attempt 2 of" in feedback
    second_dir = ctx.run.root / "attempts" / "mesh-2"
    assert (second_dir / "geometry.step").read_text(encoding="utf-8") == STEP_TEXT
    assert (second_dir / "boundary_manifest.json").is_file()
    assert [(a.n, a.ok) for a in ctx.record.stages[0].attempts] == [(1, False), (2, True)]
    assert (ctx.run.root / "case" / "constant" / "polyMesh").is_dir()
