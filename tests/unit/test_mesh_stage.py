"""Unit tests for the Mesh stage with a fake sandbox (no executor, no Gmsh, no OpenFOAM)."""

import json
import math
import os
import re
from pathlib import Path

import pytest
from test_casespec import make_spec
from test_geometry_stage import (
    RUN_OK,
    Clock,
    FakeSandbox,
    axisymmetric_spec,
    gate_failed,
    make_ctx,
    manifest,
    plan,
    plane,
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


def check_with_cells(cells):
    return OpResponse(
        ok=True, data={"ok": True, "errors": [], "report": {"n_cells": cells, "patches": {}}}
    )


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
            "check_mesh": kwargs.pop("check", check_with_cells(4096)),
        }
    )
    ctx = make_ctx(tmp_path, sandbox, spec=spec, **kwargs)
    holder.append(ctx)
    data = manifest() if geometry_manifest is None else geometry_manifest
    promoted = ctx.run.root / "geometry"
    promoted.mkdir()
    (promoted / "geometry.step").write_bytes(STEP_TEXT.encode("utf-8"))
    (promoted / "boundary_manifest.json").write_bytes(json.dumps(data).encode("utf-8"))
    ctx.originals = {  # what the promoted geometry held before any script ran
        name: (promoted / name).read_bytes() for name in ("geometry.step", "boundary_manifest.json")
    }
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


def original_manifest_bytes(ctx):
    return ctx.originals["boundary_manifest.json"]


def tampering(ctx_holder, action):
    """A fake ``run_script`` that runs ``action(attempt_dir, root)`` in the attempt directory."""

    def run_script(args):
        ctx = ctx_holder[0]
        action(ctx.run.root / args.cwd, ctx.run.root)
        return RUN_OK

    return run_script


def tampered_gate(tmp_path, action):
    holder = []
    ctx = stage_ctx(tmp_path, run=tampering(holder, action))
    holder.append(ctx)
    attempt = prepared_attempt(ctx)
    return ctx, attempt, MeshStage().execute_and_gate(ctx, attempt)


def assert_inputs_restored(ctx, attempt):
    for directory in (attempt, ctx.run.root / "geometry"):
        step, manifest_file = directory / "geometry.step", directory / "boundary_manifest.json"
        assert (
            step.is_file()
            and not step.is_symlink()
            and step.read_bytes() == ctx.originals["geometry.step"]
        )
        assert manifest_file.is_file() and not manifest_file.is_symlink()
        assert manifest_file.read_bytes() == original_manifest_bytes(ctx)


def test_a_script_that_changes_an_input_fails_the_gate_and_the_inputs_are_restored(tmp_path):
    def action(attempt, root):
        (attempt / "boundary_manifest.json").write_text('{"tampered": true}', encoding="utf-8")
        (attempt / "geometry.step").write_text("tampered", encoding="utf-8")

    ctx, attempt, result = tampered_gate(tmp_path, action)
    assert not result.ok
    text = " ".join(result.errors)
    assert "mesh.py modified" in text and "it must only read it" in text
    assert "geometry.step" in text and "boundary_manifest.json" in text
    assert [c[0] for c in ctx.sandbox.calls] == ["run_script"]  # nothing is converted or checked
    assert_inputs_restored(ctx, attempt)


def test_a_script_that_changes_the_promoted_geometry_fails_the_gate(tmp_path):
    def action(attempt, root):
        (root / "geometry" / "boundary_manifest.json").write_text("{}", encoding="utf-8")
        (root / "geometry" / "geometry.step").unlink()

    ctx, attempt, result = tampered_gate(tmp_path, action)
    assert not result.ok
    text = " ".join(result.errors)
    assert "mesh.py modified" in text and "geometry/boundary_manifest.json" in text
    assert "geometry/geometry.step" in text
    assert_inputs_restored(ctx, attempt)


def test_a_script_that_deletes_an_input_fails_the_gate(tmp_path):
    ctx, attempt, result = tampered_gate(
        tmp_path, lambda attempt, root: (attempt / "geometry.step").unlink()
    )
    assert not result.ok and "mesh.py modified geometry.step" in " ".join(result.errors)
    assert_inputs_restored(ctx, attempt)


@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_an_input_replaced_by_something_that_is_no_file_is_a_failed_gate_not_a_crash(
    tmp_path, kind
):
    def action(attempt, root):
        target = attempt / "boundary_manifest.json"
        target.unlink()
        if kind == "directory":
            target.mkdir()
            (target / "inner.txt").write_text("x", encoding="utf-8")
        else:
            if not hasattr(os, "mkfifo"):
                pytest.skip("no named pipes on this platform")
            os.mkfifo(target)

    ctx, attempt, result = tampered_gate(tmp_path, action)
    assert not result.ok and "mesh.py modified boundary_manifest.json" in " ".join(result.errors)
    assert_inputs_restored(ctx, attempt)


def test_a_link_left_by_the_script_is_a_failed_gate_not_a_write_through(tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text("keep", encoding="utf-8")

    def action(attempt, root):
        link = attempt / "boundary_manifest.json"
        link.unlink()
        try:
            link.symlink_to(outside)
        except OSError:
            pytest.skip("cannot create symbolic links here")

    ctx, attempt, result = tampered_gate(tmp_path, action)
    assert not result.ok
    assert outside.read_text(encoding="utf-8") == "keep"
    assert_inputs_restored(ctx, attempt)


def test_a_promoted_geometry_directory_swapped_for_a_link_is_put_back(tmp_path):
    decoy = tmp_path / "decoy"
    decoy.mkdir()

    def action(attempt, root):
        moved = root / "geometry"
        for item in moved.iterdir():
            (decoy / item.name).write_bytes(item.read_bytes())
            item.unlink()
        moved.rmdir()
        try:
            moved.symlink_to(decoy, target_is_directory=True)
        except OSError:
            pytest.skip("cannot create symbolic links here")

    ctx, attempt, result = tampered_gate(tmp_path, action)
    assert not result.ok and "geometry/" in " ".join(result.errors)
    assert not (ctx.run.root / "geometry").is_symlink()
    assert_inputs_restored(ctx, attempt)


@pytest.mark.parametrize("where", ["attempt", "geometry"])
def test_a_planted_oversized_input_is_never_read_into_memory(tmp_path, monkeypatch, where):
    big = b"x" * 5_000_000

    def action(attempt, root):
        directory = attempt if where == "attempt" else root / "geometry"
        (directory / "geometry.step").write_bytes(big)

        def forbidden(*args, **kwargs):
            raise AssertionError("the planted file was read")

        monkeypatch.setattr(Path, "read_bytes", forbidden)
        monkeypatch.setattr(Path, "read_text", forbidden)
        real_open = open

        class Bounded:
            def __init__(self, handle):
                self._handle = handle

            def read(self, size=-1):
                if size is None or size < 0 or size > 1_000_000:
                    raise AssertionError("an unbounded read of the planted file")
                return self._handle.read(size)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self._handle.close()

            def __getattr__(self, name):
                return getattr(self._handle, name)

        def guarded_open(file, mode="r", *args, **kwargs):
            handle = real_open(file, mode, *args, **kwargs)
            return Bounded(handle) if "b" in mode and "r" in mode else handle

        monkeypatch.setattr("builtins.open", guarded_open)

    holder = []
    ctx = stage_ctx(tmp_path, run=tampering(holder, action))
    holder.append(ctx)
    attempt = prepared_attempt(ctx)
    result = MeshStage().execute_and_gate(ctx, attempt)
    monkeypatch.undo()
    assert not result.ok and "mesh.py modified" in " ".join(result.errors)
    assert_inputs_restored(ctx, attempt)


def test_a_same_size_change_is_still_detected(tmp_path):
    def action(attempt, root):
        original = (attempt / "geometry.step").read_bytes()
        (attempt / "geometry.step").write_bytes(b"Z" * len(original))

    ctx, attempt, result = tampered_gate(tmp_path, action)
    assert not result.ok and "mesh.py modified geometry.step" in " ".join(result.errors)
    assert_inputs_restored(ctx, attempt)


def test_an_oversized_promoted_input_is_a_pipeline_error(tmp_path, monkeypatch):
    import cfdagent.agents.mesh as mesh_module

    ctx = stage_ctx(tmp_path)
    monkeypatch.setattr(mesh_module, "INPUT_MAX_BYTES", 5)
    with pytest.raises(ToolFailure, match="geometry/geometry.step.*larger than 5 bytes"):
        MeshStage().prepare_attempt(ctx, ctx.run.attempt_dir("mesh", 1))


def test_a_restore_that_fails_is_a_failed_gate_not_an_exception(tmp_path, monkeypatch):
    import cfdagent.agents.mesh as mesh_module

    def action(attempt, root):
        (attempt / "geometry.step").write_bytes(b"tampered")

        def broken(root_dir, rel, data):
            raise mesh_module._RestoreError(f"{rel}: PermissionError")

        monkeypatch.setattr(mesh_module, "_write_clean", broken)

    ctx, attempt, result = tampered_gate(tmp_path, action)
    text = " ".join(result.errors)
    assert not result.ok and "mesh.py modified geometry.step" in text
    assert "could not be put back" in text and "PermissionError" in text


def test_an_unmodified_script_leaves_the_inputs_alone(tmp_path):
    ctx, attempt, result = tampered_gate(tmp_path, lambda attempt, root: None)
    assert result.ok, result.errors
    assert_inputs_restored(ctx, attempt)


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
    ctx = stage_ctx(tmp_path, spec=spec)
    result = gate(ctx)
    assert not result.ok
    assert ctx.sandbox.calls == []  # decided from the plan and the manifest, before any work
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
    ctx = stage_ctx(tmp_path, spec=spec, geometry_manifest=data, patches=patches)
    result = gate(ctx)
    assert not result.ok and "`shaft`" in "\n".join(result.errors)
    assert ctx.sandbox.calls == []


def test_a_dropped_wall_cannot_hide_behind_the_empty_patch_that_absorbs_another_name(tmp_path):
    """Review repro: one free-named `empty` patch stands for one plan name, not for two."""
    boundaries = [
        {"name": "inlet", "role": "inlet", "description": "x"},
        {"name": "outlet", "role": "outlet", "description": "x"},
        {"name": "frontAndBack", "role": "empty", "description": "x"},
        {"name": "wall_bottom", "role": "wall", "description": "x"},
    ]
    data = manifest(names=("inlet", "outlet", "walls", "sides"))
    ctx = stage_ctx(tmp_path, spec=make_spec(boundaries=boundaries), geometry_manifest=data)
    result = gate(ctx)
    text = " ".join(result.errors)
    assert not result.ok and "`wall_bottom`" in text and "`frontAndBack`" in text
    assert "at most 1" in text and ctx.sandbox.calls == []


def test_the_role_text_exempts_nothing_in_a_2d_plan(tmp_path):
    """Count path only: `back` has role `empty` but the manifest has no free `empty` patch."""
    boundaries = [
        {"name": "inlet", "role": "inlet", "description": "x"},
        {"name": "walls", "role": "wall", "description": "x"},
        {"name": "front", "role": "empty", "description": "x"},
        {"name": "back", "role": "empty", "description": "x"},
    ]
    data = manifest(names=("inlet", "outlet", "walls", "front"))  # `front` is empty, `back` gone
    ctx = stage_ctx(tmp_path, spec=make_spec(boundaries=boundaries), geometry_manifest=data)
    result = gate(ctx)
    assert not result.ok and "`back`" in " ".join(result.errors)
    assert "`front`" not in " ".join(result.errors)


def test_two_unlisted_empty_patches_absorb_two_unmatched_names(tmp_path):
    names = ("inlet", "outlet", "walls", "a", "b")
    boundaries = [{"name": n, "role": "x", "description": "x"} for n in names]
    data = manifest(names=("inlet", "outlet", "walls", "sides"))
    data["patches"].append({"name": "lid", "type": "empty", "locators": [plane("z", 0.0)]})
    patches = {**DEFAULT_PATCHES, "sides": ("empty", 10), "lid": ("empty", 10)}
    del patches["frontAndBack"]
    spec = make_spec(boundaries=boundaries)
    assert gate(stage_ctx(tmp_path, spec=spec, geometry_manifest=data, patches=patches)).ok


def axisymmetric_plan(extra=(), drop=()):
    spec = axisymmetric_spec(("inlet", "outlet", "side", "wedgeFront", "wedgeBack")).model_dump()
    boundaries = [b for b in spec["boundaries"] if b["name"] not in drop]
    boundaries += [{"name": n, "role": r, "description": "x"} for n, r in extra]
    return make_spec(
        dimensionality="axisymmetric", geometry=spec["geometry"], boundaries=boundaries
    )


@pytest.mark.parametrize(
    ("name", "role"),
    [
        ("axis", "symmetry"),
        ("centreline", "symmetry line"),
        ("line", "centerline of the body"),
        ("symmetry_axis", "symmetry"),
        ("axisLine", "x"),
        ("axis2", "x"),
        ("x", "axis of revolution"),
        ("centreLine", "x"),
        ("center_line", "x"),
        ("x", "centre line of the body"),
        ("Centerline", "x"),
        ("x", "the center  line"),
    ],
)
def test_the_axis_of_an_axisymmetric_plan_is_found_in_name_or_role(tmp_path, name, role):
    spec = axisymmetric_plan(extra=[(name, role)])
    ctx = stage_ctx(tmp_path, spec=spec, geometry_manifest=wedge_manifest(), patches=WEDGE_PATCHES)
    result = gate(ctx)
    assert result.ok, result.errors


def test_an_axis_boundary_does_not_use_up_a_free_structural_patch(tmp_path):
    # `wedgeFront` is not in the plan, so the manifest's patch of that name is one free patch.
    kwargs = {"geometry_manifest": wedge_manifest(), "patches": WEDGE_PATCHES}
    one = axisymmetric_plan(extra=[("axis", "symmetry"), ("nose", "wall")], drop=("wedgeFront",))
    assert gate(stage_ctx(tmp_path, spec=one, **kwargs)).ok  # `nose` takes the free patch
    (tmp_path / "again").mkdir()
    two = axisymmetric_plan(
        extra=[("axis", "symmetry"), ("nose", "wall"), ("tail", "wall")], drop=("wedgeFront",)
    )
    assert not gate(stage_ctx(tmp_path / "again", spec=two, **kwargs)).ok


def test_words_that_only_look_like_an_axis_are_no_exemption(tmp_path):
    for name, role in (("center", "line load"), ("outletCenter", "wall"), ("line", "centre")):
        (tmp_path / name).mkdir()
        spec = axisymmetric_plan(extra=[(name, role)])
        ctx = stage_ctx(
            tmp_path / name, spec=spec, geometry_manifest=wedge_manifest(), patches=WEDGE_PATCHES
        )
        assert not gate(ctx).ok, (name, role)


def test_front_of_the_body_is_no_axis_and_no_structural_face(tmp_path):
    spec = axisymmetric_plan(extra=[("nose", "wall at the front of the body")])
    ctx = stage_ctx(tmp_path, spec=spec, geometry_manifest=wedge_manifest(), patches=WEDGE_PATCHES)
    result = gate(ctx)
    assert not result.ok and "`nose`" in " ".join(result.errors)


def test_axis_words_are_no_exemption_in_a_2d_plan(tmp_path):
    names = ("inlet", "outlet", "walls", "frontAndBack", "axis")
    boundaries = [
        {"name": n, "role": "axis" if n == "axis" else "x", "description": "x"} for n in names
    ]
    assert not gate(stage_ctx(tmp_path, spec=make_spec(boundaries=boundaries))).ok


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


# ------------------------------------------------------------------ the cell limit (R33)


def limited(tmp_path, cells, max_cells=5000):
    ctx = stage_ctx(tmp_path, check=check_with_cells(cells))
    ctx.config = ctx.config.model_copy(
        update={"limits": ctx.config.limits.model_copy(update={"max_cells": max_cells})}
    )
    return ctx


def test_a_mesh_above_the_cell_limit_of_the_run_fails_the_gate(tmp_path):
    result = gate(limited(tmp_path, 6000))
    assert not result.ok
    (message,) = result.errors
    assert message.startswith("exceeds resources:")
    assert "6000" in message and "5000" in message
    assert "coarsen" in message.lower()


def test_a_mesh_at_the_cell_limit_passes(tmp_path):
    assert gate(limited(tmp_path, 5000)).ok


@pytest.mark.parametrize("data", [None, {"ok": True, "errors": []}, {"report": {"cells": 1}}])
def test_a_passing_check_without_a_cell_count_is_a_wiring_break(tmp_path, data):
    ctx = stage_ctx(tmp_path, check=OpResponse(ok=True, data=data))
    with pytest.raises(ToolFailure, match="n_cells"):
        gate(ctx)


def test_the_resource_message_uses_the_planners_prefix(tmp_path):
    from cfdagent.agents.planner import RESOURCE_PREFIX

    (message,) = gate(limited(tmp_path, 6000)).errors
    assert message.startswith(RESOURCE_PREFIX)


def test_a_failed_check_reports_its_own_errors_not_the_cell_limit(tmp_path):
    check = OpResponse(
        ok=False,
        data={"ok": False, "errors": ["max skewness is 5"], "report": {"n_cells": 10**7}},
        error="max skewness is 5",
    )
    assert gate(stage_ctx(tmp_path, check=check)).errors == ["max skewness is 5"]


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
    assert ctx.sandbox.calls == []  # found before the script is run


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


def test_promotion_replaces_what_an_earlier_one_or_a_script_left_behind(tmp_path):
    ctx = stage_ctx(tmp_path)
    root = ctx.run.root
    (root / "case" / "0").mkdir(parents=True)
    (root / "case" / "0" / "U").write_text("old", encoding="utf-8")
    (root / "case" / "system").mkdir()
    (root / "case" / "system" / "controlDict").write_text("old", encoding="utf-8")
    (root / "mesh").mkdir()
    (root / "mesh" / "stale.txt").write_text("old", encoding="utf-8")
    (root / "boundary_manifest.json").mkdir()  # something in the way of the manifest
    (root / "boundary_manifest.json" / "x").write_text("x", encoding="utf-8")

    MeshStage().promote(ctx, converted_attempt(ctx))
    MeshStage().promote(ctx, ctx.run.root / "attempts" / "mesh-1")  # and once more

    held = sorted(p.relative_to(root / "case").as_posix() for p in (root / "case").rglob("*"))
    assert held == [
        "constant",
        "constant/polyMesh",
        "constant/polyMesh/boundary",
        "constant/polyMesh/points",
    ]
    assert not (root / "mesh" / "stale.txt").exists()
    assert (root / "boundary_manifest.json").is_file()
    assert json.loads((root / "boundary_manifest.json").read_text(encoding="utf-8")) == manifest()


def test_promotion_puts_the_geometry_manifest_even_if_the_attempt_copy_was_changed(tmp_path):
    ctx = stage_ctx(tmp_path)
    attempt = converted_attempt(ctx)
    (attempt / "boundary_manifest.json").write_text("{}", encoding="utf-8")
    MeshStage().promote(ctx, attempt)
    root_manifest = json.loads((ctx.run.root / "boundary_manifest.json").read_text("utf-8"))
    assert root_manifest == manifest()


def test_promote_needs_only_the_manifest_of_the_geometry(tmp_path):
    ctx = stage_ctx(tmp_path)
    attempt = converted_attempt(ctx)
    (ctx.run.root / "geometry" / "geometry.step").unlink()  # not needed to hand the mesh on
    MeshStage().promote(ctx, attempt)
    assert (ctx.run.root / "boundary_manifest.json").is_file()


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
        "2000000",  # the run's hard limit on cells
        "hard limit",
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
        "getParametrizationBounds",  # the orientation-independent grading helper
        "on its own",
        "setAsBoundaryLayer",
        "Quads",
        "cell-centre",
        "2 y",
        "Mesh.Algorithm",
        "notes",  # Re_cell above 2 goes into a note for Case Setup
        "half-width",
        "hard limit",
    ):
        assert needle in text, needle
    assert "40 degrees" not in text  # no self-check the agent cannot make
    assert "usually all hexahedral" in text and "gives an all-hexahedral" not in text
    assert "geometry.step's size" not in text
    assert "never above resource_estimate_cells" not in text
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
        check_with_cells(4096),
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


def test_opening_messages_state_input_paths(tmp_path):
    text = MeshStage().opening_message(stage_ctx(tmp_path))
    assert "working directory" in text
    assert "../../casespec.json" in text
    for name in ("geometry.step", "boundary_manifest.json", "mesh.msh"):
        assert name in text
