"""The Mesh stage, stage 3 of the pipeline (design 6.2, 6.3).

The agent writes a Gmsh script, ``mesh.py``, into its attempt directory. The loop has already put
copies of the Geometry stage's ``geometry.step`` and ``boundary_manifest.json`` there
(``prepare_attempt``). The gate runs the script in the sandbox (``run_script`` with
``kind="mesh"``: it must write ``mesh.msh``, MSH 2.2 ASCII), converts the mesh into a fresh
``case/`` inside the attempt directory (``convert_mesh``: ``gmshToFoam`` and the patch types from
the manifest) and lets the sandbox check the result against the plan (``check_mesh``: checkMesh,
patch names and placement, bounding box, one cell through the thickness, quality limits).

The host adds one check that needs the plan: every boundary of the plan that is not structural must
have a patch with faces in the converted ``polyMesh/boundary``. Structural boundaries are the
``empty`` and ``wedge`` patches of the manifest (any name) and, by their role, the front and back
faces of a 2D case and the axis of an axisymmetric case, which have no patch of that name.

An axisymmetric mesh is one flat-faced cell through the wedge angle ``a``: its vertices lie on the
true radii, but the faces between them are flat. The bounding box of such a mesh is therefore
short of the CAD box in y by the factor ``cos(a/2)`` at the largest radius (the CAD box reaches
``R`` on the mid-plane, the mesh only ``R cos(a/2)``); at the smallest radius the CAD box already
has ``r cos(a/2)``, which the mesh reproduces, and z is the same in both.

Errors the agent can fix come back as a failed ``GateResult``. A failed executor or tool
(``OpResponse.internal``) and a missing input raise ``ToolFailure``: it is not the agent's fault.

This module is host-side: it imports no tool, CAD or mesh code.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
from pathlib import Path

from cfdagent.agents.casespec import BoundarySpec, CaseSpec
from cfdagent.agents.geometry import (
    PROMOTED_DIR as GEOMETRY_DIR,
)
from cfdagent.agents.geometry import (
    STEP_NAME,
    _last_bytes,
    _names,
    _read_manifest,
)
from cfdagent.agents.stage import Stage, StageContext
from cfdagent.agents.tools import FileScope
from cfdagent.errors import ToolFailure
from cfdagent.executor.protocol import CheckMeshArgs, ConvertMeshArgs, OpResponse, RunScriptArgs
from cfdagent.gates import GateResult
from cfdagent.manifest import MANIFEST_FILE_NAME, BoundaryManifest
from cfdagent.rundir import RunDirError
from cfdagent.sandbox.client import outer_timeout

STAGE = "mesh"
AGENT = "mesh"
SCRIPT_NAME = "mesh.py"
MESH_NAME = "mesh.msh"
CASE_NAME = "case"
PROMOTED_DIR = "mesh"
PROMOTED_CASE_DIR = "case"  # the case directory of the later stages: constant/polyMesh only
INPUT_FILES = (STEP_NAME, MANIFEST_FILE_NAME)
SCRIPT_TIMEOUT_S = 900.0  # meshing is slower than building a solid
GATE_TIMEOUT_S = 300.0
SCRIPT_TAIL_BYTES = 3000  # the loop shows the model at most 4096 bytes of all errors together
BOUNDARY_MAX_BYTES = 1_000_000  # constant/polyMesh/boundary: a few lines per patch
_STRUCTURAL_TYPES = frozenset({"empty", "wedge"})
# Role words that mark a boundary without a patch of its own name. The plan lists the front and
# back faces of a 2D case and the wedge faces and axis of an axisymmetric case in free text.
_STRUCTURAL_ROLE_WORDS = {
    "2D": frozenset({"empty", "front", "back"}),
    "axisymmetric": frozenset({"empty", "wedge", "front", "back", "axis"}),
    "3D": frozenset(),
}
_CONVERSION_HINT = (
    "gmshToFoam names the patches after the physical groups of mesh.msh and puts every boundary "
    "face that is in no 2D physical group into a patch called `defaultFaces`. Create one "
    "physical group per patch of boundary_manifest.json with assign_physical_groups(manifest) "
    "from cfdagent.tools.gmsh_helpers, after the geometry is final and before generating the "
    "mesh, and do not set Mesh.SaveAll."
)


def _internal(response: OpResponse, label: str) -> ToolFailure:
    return ToolFailure(f"{label} failed outside the agent's work: {response.error}")


def _tool_failure(response: OpResponse, what: str, tail_label: str) -> str:
    """The message for a tool that did not run to the end: headline and the end of its output."""
    data = response.data or {}
    error = response.error or f"{what} failed"
    headline, _, rest = error.partition("\n")
    tail = data.get("tail")
    if not isinstance(tail, str) or not tail.strip():
        tail = rest
    message = f"{what}: {headline}"
    if tail.strip():
        message += f"\n{tail_label}:\n{_last_bytes(tail, SCRIPT_TAIL_BYTES)}"
    return message


def _gate_errors(response: OpResponse) -> list[str]:
    data = response.data or {}
    errors = data.get("errors")
    if isinstance(errors, list) and errors:
        return [str(e) for e in errors]
    return [response.error or "the mesh checks failed without a message"]


def _boundary_face_counts(text: str) -> dict[str, int]:
    """Patch name -> ``nFaces`` from the text of ``constant/polyMesh/boundary``.

    A token scan, not a regular expression: each patch is ``name { ... nFaces N; ... }`` and the
    names come from the agent's mesh. Returns ``{}`` for text that holds no patch.
    """
    tokens = re.findall(r"[{}]|[^\s{}]+", text)
    counts: dict[str, int] = {}
    i = 0
    while i < len(tokens):
        if tokens[i] == "{" and i > 0:
            end = i + 1
            while end < len(tokens) and tokens[end] != "}":
                end += 1
            block = tokens[i + 1 : end]
            name = tokens[i - 1]
            if name != "FoamFile" and "nFaces" in block:
                at = block.index("nFaces")
                value = block[at + 1].rstrip(";") if at + 1 < len(block) else ""
                if value.isdigit():
                    counts[name] = int(value)
            i = end
        i += 1
    return counts


def _is_structural(spec: CaseSpec, boundary: BoundarySpec, manifest: BoundaryManifest) -> bool:
    """A boundary of the plan that needs no patch of its own name."""
    if any(p.name == boundary.name and p.type in _STRUCTURAL_TYPES for p in manifest.patches):
        return True
    words = set(re.findall(r"[a-z]+", boundary.role.lower()))
    return bool(words & _STRUCTURAL_ROLE_WORDS[spec.dimensionality])


def _plan_boundary_problems(
    spec: CaseSpec, manifest: BoundaryManifest, counts: dict[str, int]
) -> list[str]:
    """Every boundary of the plan that is not structural needs faces in the converted mesh."""
    in_manifest = {p.name for p in manifest.patches}
    with_faces = {name for name, n in counts.items() if n > 0}
    unmeshed = [
        b.name
        for b in spec.boundaries
        if b.name not in with_faces and not _is_structural(spec, b, manifest)
    ]
    problems = []
    meshless = [n for n in unmeshed if n in in_manifest]
    if meshless:
        problems.append(
            f"these boundaries of the plan have no faces in the converted mesh "
            f"(constant/polyMesh/boundary): {_names(meshless)}. Their physical groups are missing "
            f"or empty in {MESH_NAME}. Create one physical group per patch of "
            "boundary_manifest.json with assign_physical_groups(manifest) and do not drop or "
            "merge boundary surfaces"
        )
    unnamed = [n for n in unmeshed if n not in in_manifest]
    # The structural patches may carry names the plan does not list; a plan boundary with no
    # patch of its name can be one of them, so up to that many missing names are not an error.
    planned = {b.name for b in spec.boundaries}
    free = sum(1 for p in manifest.patches if p.type in _STRUCTURAL_TYPES and p.name not in planned)
    if unnamed and len(unnamed) <= free:
        unnamed = []
    if unnamed:
        spare = (
            f" (at most {free} of them can be `empty` or `wedge` patches that the manifest "
            "names differently)"
            if free
            else ""
        )
        problems.append(
            f"these boundaries of the plan have no patch in {MANIFEST_FILE_NAME} and no faces in "
            f"the mesh: {_names(unnamed)}{spare}. The Geometry stage lumped or dropped them. "
            f"{MANIFEST_FILE_NAME} is an input of this stage and is not editable, so the mesh "
            "cannot name them. Write what you found in notes.md in the attempt directory so that "
            "it is recorded; the stage cannot pass until the Geometry stage names them"
        )
    return problems


def _mesh_expected_bbox(spec: CaseSpec) -> list[float]:
    """The box that the converted mesh is checked against (see the module docstring)."""
    box = list(spec.expected_bbox())
    if spec.dimensionality != "axisymmetric":
        return box
    angle = spec.geometry.wedge_angle_deg
    if angle is None:
        raise ToolFailure(
            "the plan is axisymmetric but geometry.wedge_angle_deg is missing; "
            "the planner gate should have rejected it"
        )
    box[4] *= math.cos(math.radians(angle) / 2)
    return box


class MeshStage(Stage):
    """Stage 3: a Gmsh script, run in the sandbox, converted and gated against the ``CaseSpec``."""

    name = STAGE
    agent = AGENT
    scope = FileScope(
        readable=("casespec.json", "attempts/mesh-*/*"),
        writable=("attempts/mesh-*/mesh.py", "attempts/mesh-*/*.md"),
    )

    def opening_message(self, ctx: StageContext) -> str:
        spec = self._spec(ctx)
        plan = {
            "title": spec.title,
            "dimensionality": spec.dimensionality,
            "flow_regime": spec.flow_regime,
            "time_dependence": spec.time_dependence,
            "fluid": spec.fluid.model_dump(),
            "reference": spec.reference.model_dump(),
            "geometry": spec.geometry.model_dump(),
            "boundaries": [b.model_dump() for b in spec.boundaries],
            "mesh": spec.mesh.model_dump(),
            "solver": spec.solver.model_dump(),
            "requested_outputs": [o.model_dump() for o in spec.requested_outputs],
            "resource_estimate_cells": spec.resource_estimate.cells,
        }
        extra = ""
        if spec.dimensionality == "axisymmetric":
            extra = (
                " The mesh of a wedge has one cell with flat faces through wedge_angle_deg, so "
                "its largest y is the largest radius times cos(wedge_angle_deg / 2), not the "
                "radius itself; the check expects that."
            )
        return (
            "Mesh the fluid domain of this plan.\n\n"
            f"{json.dumps(plan, indent=2)}\n\n"
            f"The attempt directory holds {STEP_NAME} and {MANIFEST_FILE_NAME} (copies; you cannot "
            f"change them). Write {SCRIPT_NAME} so that it reads them and writes {MESH_NAME} "
            "(MSH 2.2 ASCII) with one physical group per patch of the manifest. Choose the "
            "resolution from the physics of the plan, not only from mesh.target_cells: the mesh "
            "must resolve what requested_outputs asks for, with a total near mesh.target_cells "
            "and not above resource_estimate_cells. Units are metres, as in the geometry."
            f"{extra}"
        )

    def prepare_attempt(self, ctx: StageContext, attempt_dir: Path) -> None:
        self._copy_inputs(ctx, attempt_dir)

    def execute_and_gate(self, ctx: StageContext, attempt_dir: Path) -> GateResult:
        spec = self._spec(ctx)
        rel = ctx.run.relative(attempt_dir)
        script = f"{rel}/{SCRIPT_NAME}"
        if not ctx.run.path(script).is_file():
            return GateResult(
                False,
                [
                    f"{SCRIPT_NAME} does not exist in {rel}/. Write it there with write_file "
                    f"(path {script}); it must write {MESH_NAME} into its working directory"
                ],
            )
        case_rel = f"{rel}/{CASE_NAME}"
        self._remove_case(ctx, case_rel)

        script_s = ctx.inner_timeout(SCRIPT_TIMEOUT_S)
        run = ctx.sandbox.call(
            "run_script",
            RunScriptArgs(script=script, cwd=rel, timeout_s=script_s, kind="mesh"),
            outer_timeout(script_s),
        )
        if run.internal:
            raise _internal(run, "running the mesh script")
        if not run.ok:
            return GateResult(
                False,
                [
                    _tool_failure(
                        run,
                        f"{SCRIPT_NAME} did not run successfully",
                        "End of the script's output (including your prints)",
                    )
                ],
            )

        # The script ran with write access to its directory: restore the inputs the gate relies on.
        try:
            self._copy_inputs(ctx, attempt_dir)
        except RunDirError as exc:
            return GateResult(
                False,
                [
                    f"an input file in {rel}/ was replaced by something that is not a regular "
                    f"file ({exc}); {SCRIPT_NAME} must only read {STEP_NAME} and "
                    f"{MANIFEST_FILE_NAME}"
                ],
            )
        if self._case_exists(ctx, case_rel):
            return GateResult(
                False,
                [
                    f"{SCRIPT_NAME} created {CASE_NAME}/ in its working directory. The gate "
                    f"creates {CASE_NAME}/ itself from {MESH_NAME}; write only {MESH_NAME} (and "
                    "logs) and do not create that directory"
                ],
            )

        gate_s = ctx.inner_timeout(GATE_TIMEOUT_S)
        convert = ctx.sandbox.call(
            "convert_mesh",
            ConvertMeshArgs(msh=f"{rel}/{MESH_NAME}", case_dir=case_rel),
            outer_timeout(gate_s),
        )
        if convert.internal:
            raise _internal(convert, "converting the mesh")
        if not convert.ok:
            message = _tool_failure(
                convert, f"converting {MESH_NAME} to OpenFOAM failed", "End of the output"
            )
            return GateResult(False, [f"{message}\n{_CONVERSION_HINT}"])

        errors = self._plan_problems(ctx, spec, rel, case_rel)
        check = ctx.sandbox.call(
            "check_mesh",
            CheckMeshArgs(case_dir=case_rel, expected_bbox=_mesh_expected_bbox(spec)),
            outer_timeout(ctx.inner_timeout(GATE_TIMEOUT_S)),
        )
        if check.internal:
            raise _internal(check, "checking the mesh")
        if not check.ok:
            errors += _gate_errors(check)
        return GateResult(not errors, errors)

    def promote(self, ctx: StageContext, attempt_dir: Path) -> None:
        """``mesh/``: the attempt (script, mesh, logs); ``case/``: ``constant/polyMesh`` only.

        The polyMesh is not kept twice: ``mesh/case/`` keeps the gate's logs but not the mesh. The
        manifest is copied to the run directory beside ``case/``, where the case-level ops look
        for it by default.
        """
        rel = ctx.run.relative(attempt_dir)
        polymesh_rel = f"{rel}/{CASE_NAME}/constant/polyMesh"
        if not ctx.run.path(polymesh_rel).is_dir():
            raise ToolFailure(f"{polymesh_rel} does not exist; the gate has not converted a mesh")
        manifest = self._input(ctx, MANIFEST_FILE_NAME)

        ctx.run.promote(attempt_dir, PROMOTED_DIR)
        shutil.rmtree(ctx.run.path(f"{PROMOTED_DIR}/{CASE_NAME}/constant"), ignore_errors=True)
        case = ctx.run.path(PROMOTED_CASE_DIR)
        if os.path.lexists(case):
            shutil.rmtree(case)
        ctx.run.promote(ctx.run.path(polymesh_rel), f"{PROMOTED_CASE_DIR}/constant/polyMesh")
        shutil.copyfile(manifest, ctx.run.path(MANIFEST_FILE_NAME))

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _spec(ctx: StageContext) -> CaseSpec:
        if ctx.casespec is None:
            raise ToolFailure("the mesh stage needs the CaseSpec; run the planner first")
        return ctx.casespec

    @staticmethod
    def _input(ctx: StageContext, name: str) -> Path:
        source = ctx.run.path(f"{GEOMETRY_DIR}/{name}")
        if not source.is_file():
            raise ToolFailure(
                f"{GEOMETRY_DIR}/{name} does not exist: the geometry stage has not produced it, "
                "so the pipeline order is broken"
            )
        return source

    def _copy_inputs(self, ctx: StageContext, attempt_dir: Path) -> None:
        rel = ctx.run.relative(attempt_dir)
        for name in INPUT_FILES:
            source = self._input(ctx, name)
            shutil.copyfile(source, ctx.run.path(f"{rel}/{name}"))

    @staticmethod
    def _case_exists(ctx: StageContext, case_rel: str) -> bool:
        try:
            return os.path.lexists(ctx.run.path(case_rel))
        except RunDirError:
            return True  # a link in the way counts as something the script left behind

    def _remove_case(self, ctx: StageContext, case_rel: str) -> None:
        """Start from no ``case/``: a file tool may have written a note into it."""
        if not self._case_exists(ctx, case_rel):
            return
        path = ctx.run.path(case_rel)  # refuses a link: nothing outside the run is deleted
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()

    @staticmethod
    def _plan_problems(ctx: StageContext, spec: CaseSpec, rel: str, case_rel: str) -> list[str]:
        """What the plan says about the boundaries that the converted mesh does not satisfy.

        A manifest or boundary file that cannot be read is left to the sandbox gate, which
        reports the details.
        """
        manifest, _ = _read_manifest(ctx, f"{rel}/{MANIFEST_FILE_NAME}")
        if manifest is None:
            return []
        try:
            text = ctx.run.read_text(f"{case_rel}/constant/polyMesh/boundary", BOUNDARY_MAX_BYTES)
        except (RunDirError, OSError, UnicodeDecodeError):
            return []
        counts = _boundary_face_counts(text)
        if not counts:
            return []
        return _plan_boundary_problems(spec, manifest, counts)
