"""The Mesh stage, stage 3 of the pipeline (design 6.2, 6.3).

The agent writes a Gmsh script, ``mesh.py``, into its attempt directory. The loop has already put
copies of the Geometry stage's ``geometry.step`` and ``boundary_manifest.json`` there
(``prepare_attempt``). The gate runs the script in the sandbox (``run_script`` with
``kind="mesh"``: it must write ``mesh.msh``, MSH 2.2 ASCII), converts the mesh into a fresh
``case/`` inside the attempt directory (``convert_mesh``: ``gmshToFoam`` and the patch types from
the manifest) and lets the sandbox check the result against the plan (``check_mesh``: checkMesh,
patch names and placement, bounding box, one cell through the thickness, quality limits).

The host adds checks that need the plan. Before the script runs: every boundary of the plan needs
a patch of its name in the manifest, except the axis of an axisymmetric plan (an axis word in its
name or role; it has no face) and as many names as the manifest has ``empty`` or ``wedge`` patches
with names the plan does not list (the Geometry gate lets those patches have any name; each of them
absorbs one plan name). After the conversion: a boundary with a manifest patch must have faces in
``polyMesh/boundary``. After ``check_mesh``: the cell count must not exceed ``limits.max_cells``.

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
import stat
from pathlib import Path

from cfdagent.agents.casespec import BoundarySpec, CaseSpec
from cfdagent.agents.gatehelp import last_bytes, names, read_manifest
from cfdagent.agents.geometry import PROMOTED_DIR as GEOMETRY_DIR
from cfdagent.agents.geometry import STEP_NAME
from cfdagent.agents.planner import RESOURCE_PREFIX
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
# An axis of revolution has no face, so a plan boundary that names it needs no patch. Words are
# found in the name and the role after splitting camelCase, underscores, digits and spaces.
_AXIS_WORDS = frozenset({"axis", "centreline", "centerline", "revolution"})
_AXIS_PAIRS = frozenset({"centreline", "centerline"})  # also written as two words
INPUT_MAX_BYTES = 256 * 1024 * 1024  # the Geometry stage's STEP file is megabytes at most
_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+")
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
        message += f"\n{tail_label}:\n{last_bytes(tail, SCRIPT_TAIL_BYTES)}"
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


def _is_axis(spec: CaseSpec, boundary: BoundarySpec) -> bool:
    """An axisymmetric plan's boundary that names the axis of revolution (it has no face)."""
    if spec.dimensionality != "axisymmetric":
        return False
    for text in (boundary.name, boundary.role):
        words = [w.lower() for w in _WORD.findall(text)]
        pairs = {a + b for a, b in zip(words, words[1:], strict=False)}
        if set(words) & _AXIS_WORDS or pairs & _AXIS_PAIRS:
            return True
    return False


def _free_structural_patches(spec: CaseSpec, manifest: BoundaryManifest) -> int:
    """How many ``empty`` and ``wedge`` patches carry a name the plan does not list.

    The Geometry gate lets those patches have any name, so each of them can stand for one plan
    boundary that has no patch of its own name (a plan name absorbed by a patch is used up).
    """
    planned = {b.name for b in spec.boundaries}
    return sum(1 for p in manifest.patches if p.type in _STRUCTURAL_TYPES and p.name not in planned)


def _unlisted_problems(spec: CaseSpec, manifest: BoundaryManifest) -> list[str]:
    """Plan boundaries that have no patch in the manifest and cannot be a structural patch.

    Decided from the plan and the manifest alone, so before any script is run: the Mesh agent
    cannot repair it. Not counted: the axis of an axisymmetric plan, and as many names as there
    are ``empty`` or ``wedge`` patches with names of their own (each patch absorbs one name).
    """
    in_manifest = {p.name for p in manifest.patches}
    unmatched = [
        b.name for b in spec.boundaries if b.name not in in_manifest and not _is_axis(spec, b)
    ]
    free = _free_structural_patches(spec, manifest)
    if len(unmatched) <= free:
        return []
    spare = (
        f" (at most {free} of them can be `empty` or `wedge` patches that the manifest names "
        "differently)"
        if free
        else ""
    )
    return [
        f"these boundaries of the plan have no patch in {MANIFEST_FILE_NAME}: "
        f"{names(unmatched)}{spare}. The Geometry stage lumped or dropped them. "
        f"{MANIFEST_FILE_NAME} is an input of this stage and is not editable, so the mesh cannot "
        "name them. Write what you found in notes.md in the attempt directory so that it is "
        "recorded; the stage cannot pass until the Geometry stage names them"
    ]


def _meshless_problems(
    spec: CaseSpec, manifest: BoundaryManifest, counts: dict[str, int]
) -> list[str]:
    """Plan boundaries with a patch in the manifest that has no faces in the converted mesh."""
    structural = {p.name for p in manifest.patches if p.type in _STRUCTURAL_TYPES}
    in_manifest = {p.name for p in manifest.patches}
    with_faces = {name for name, n in counts.items() if n > 0}
    meshless = [
        b.name
        for b in spec.boundaries
        if b.name in in_manifest and b.name not in structural and b.name not in with_faces
    ]
    if not meshless:
        return []
    return [
        "these boundaries of the plan have no faces in the converted mesh "
        f"(constant/polyMesh/boundary): {names(meshless)}. Their physical groups are missing or "
        f"empty in {MESH_NAME}. Create one physical group per patch of boundary_manifest.json "
        "with assign_physical_groups(manifest) and do not drop or merge boundary surfaces"
    ]


# ------------------------------------------------------------------ files the script could touch


class _RestoreError(Exception):
    """A file could not be put back."""


def _remove(path: Path) -> None:
    """Remove whatever is at ``path`` (file, link, FIFO, directory tree)."""
    if os.path.lexists(path):
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            path.unlink()


def _write_clean(root: Path, rel: str, data: bytes) -> None:
    """Write ``root/rel`` as a regular file whatever stands in its way, links in the directory
    path included. Raises ``_RestoreError`` instead of an ``OSError``."""
    try:
        current = root
        parts = rel.split("/")
        for part in parts[:-1]:
            current = current / part
            if os.path.islink(current) or (os.path.lexists(current) and not current.is_dir()):
                current.unlink()
            current.mkdir(exist_ok=True)
        target = current / parts[-1]
        _remove(target)
        target.write_bytes(data)
    except OSError as exc:
        raise _RestoreError(f"{rel}: {type(exc).__name__}") from exc


def _holds(root: Path, rel: str, data: bytes) -> bool:
    """``root/rel`` is a regular file, reached over no link, with exactly these bytes."""
    current = root
    for part in rel.split("/"):
        current = current / part
        if os.path.islink(current):
            return False
    try:
        status = os.lstat(current)
        if not stat.S_ISREG(status.st_mode):
            return False  # a directory or a FIFO: do not open it
        if status.st_size != len(data):
            return False  # and never read a file of another size: it may be huge
        with open(current, "rb") as handle:
            return handle.read(len(data) + 1) == data
    except OSError:
        return False


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
            "must resolve what requested_outputs asks for. Aim for about "
            f"{spec.resource_estimate.cells} cells (the plan's resource estimate, which the run "
            f"time was planned for); the hard limit of this run is {ctx.config.limits.max_cells} "
            f'cells, and a mesh above it fails the check ("{RESOURCE_PREFIX.rstrip(":")}"). Units '
            "are metres, as in the geometry."
            f"{extra}"
        )

    def prepare_attempt(self, ctx: StageContext, attempt_dir: Path) -> None:
        rel = ctx.run.relative(attempt_dir)
        try:
            for name, data in self._read_inputs(ctx).items():
                _write_clean(ctx.run.root, f"{rel}/{name}", data)
        except _RestoreError as exc:
            raise ToolFailure(f"could not copy the geometry into {rel}/: {exc}") from exc

    def execute_and_gate(self, ctx: StageContext, attempt_dir: Path) -> GateResult:
        spec = self._spec(ctx)
        expected_bbox = _mesh_expected_bbox(spec)  # a plan without its wedge angle: ToolFailure
        rel = ctx.run.relative(attempt_dir)
        manifest, _ = read_manifest(ctx, f"{rel}/{MANIFEST_FILE_NAME}")
        if manifest is not None:
            unlisted = _unlisted_problems(spec, manifest)
            if unlisted:
                return GateResult(False, unlisted)
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
        try:
            _remove(ctx.run.root / case_rel)  # a note may have been written into it
        except OSError as exc:
            raise ToolFailure(f"could not clear {case_rel}: {type(exc).__name__}") from exc
        inputs = self._read_inputs(ctx)  # what the script is allowed to read, and nothing else

        script_s = ctx.inner_timeout(SCRIPT_TIMEOUT_S)
        run = ctx.sandbox.call(
            "run_script",
            RunScriptArgs(script=script, cwd=rel, timeout_s=script_s, kind="mesh"),
            outer_timeout(script_s),
        )
        if run.internal:
            raise _internal(run, "running the mesh script")
        # The script ran with write access to the run directory: its inputs must be as they were.
        errors = self._restore_inputs(ctx, rel, inputs)
        if not run.ok:
            errors.append(
                _tool_failure(
                    run,
                    f"{SCRIPT_NAME} did not run successfully",
                    "End of the script's output (including your prints)",
                )
            )
        if errors:
            return GateResult(False, errors)
        if os.path.lexists(ctx.run.root / case_rel):
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

        errors = self._meshless(ctx, spec, manifest, case_rel)
        check = ctx.sandbox.call(
            "check_mesh",
            CheckMeshArgs(case_dir=case_rel, expected_bbox=expected_bbox),
            outer_timeout(ctx.inner_timeout(GATE_TIMEOUT_S)),
        )
        if check.internal:
            raise _internal(check, "checking the mesh")
        if not check.ok:
            errors += _gate_errors(check)
        elif not errors:
            errors += self._cell_limit(ctx, spec, check)
        return GateResult(not errors, errors)

    def promote(self, ctx: StageContext, attempt_dir: Path) -> None:
        """``mesh/``: the attempt (script, mesh, logs); ``case/``: ``constant/polyMesh`` only.

        The polyMesh is not kept twice: ``mesh/case/`` keeps the gate's logs but not the mesh. The
        manifest is copied from ``geometry/`` to the run directory beside ``case/``, where the
        case-level ops look for it by default. Anything an earlier promotion or a script left at
        these places is replaced.
        """
        rel = ctx.run.relative(attempt_dir)
        polymesh_rel = f"{rel}/{CASE_NAME}/constant/polyMesh"
        if not ctx.run.path(polymesh_rel).is_dir():
            raise ToolFailure(f"{polymesh_rel} does not exist; the gate has not converted a mesh")
        manifest = self._read_inputs(ctx, (MANIFEST_FILE_NAME,))[MANIFEST_FILE_NAME]
        try:
            ctx.run.promote(attempt_dir, PROMOTED_DIR)
            _remove(ctx.run.root / PROMOTED_DIR / CASE_NAME / "constant")
            _remove(ctx.run.root / PROMOTED_CASE_DIR)
            ctx.run.promote(ctx.run.path(polymesh_rel), f"{PROMOTED_CASE_DIR}/constant/polyMesh")
            _write_clean(ctx.run.root, MANIFEST_FILE_NAME, manifest)
        except (OSError, _RestoreError) as exc:
            raise ToolFailure(f"promoting the mesh failed: {type(exc).__name__}: {exc}") from exc

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _spec(ctx: StageContext) -> CaseSpec:
        if ctx.casespec is None:
            raise ToolFailure("the mesh stage needs the CaseSpec; run the planner first")
        return ctx.casespec

    @staticmethod
    def _read_inputs(ctx: StageContext, wanted: tuple[str, ...] = INPUT_FILES) -> dict[str, bytes]:
        """The bytes of promoted geometry files; a missing or oversized one is a broken pipeline."""
        data = {}
        for name in wanted:
            try:
                path = ctx.run.path(f"{GEOMETRY_DIR}/{name}")
                if not path.is_file():
                    raise ToolFailure(
                        f"{GEOMETRY_DIR}/{name} does not exist: the geometry stage has not "
                        "produced it, so the pipeline order is broken"
                    )
                if path.stat().st_size > INPUT_MAX_BYTES:
                    raise ToolFailure(
                        f"{GEOMETRY_DIR}/{name} is larger than {INPUT_MAX_BYTES} bytes; the "
                        "geometry stage should not have produced it"
                    )
                data[name] = path.read_bytes()
            except RunDirError as exc:
                raise ToolFailure(f"cannot read {GEOMETRY_DIR}/{name}: {exc}") from exc
        return data

    @staticmethod
    def _restore_inputs(ctx: StageContext, rel: str, inputs: dict[str, bytes]) -> list[str]:
        """Compare the inputs, in the attempt directory and in ``geometry/``, with what they were
        before the script ran; put back what changed and say which files the script modified."""
        root = ctx.run.root
        errors = []
        for name, data in inputs.items():
            for path in (f"{rel}/{name}", f"{GEOMETRY_DIR}/{name}"):
                if _holds(root, path, data):
                    continue
                label = name if path.startswith(rel) else path
                errors.append(f"{SCRIPT_NAME} modified {label}; it must only read it")
                try:
                    _write_clean(root, path, data)
                except _RestoreError as exc:
                    errors.append(f"{label} could not be put back ({exc})")
        return errors

    @staticmethod
    def _meshless(
        ctx: StageContext, spec: CaseSpec, manifest: BoundaryManifest | None, case_rel: str
    ) -> list[str]:
        """Plan boundaries without faces in the converted mesh. A manifest or boundary file that
        cannot be read is left to the sandbox gate, which reports the details."""
        if manifest is None:
            return []
        try:
            text = ctx.run.read_text(f"{case_rel}/constant/polyMesh/boundary", BOUNDARY_MAX_BYTES)
        except (RunDirError, OSError, UnicodeDecodeError):
            return []
        counts = _boundary_face_counts(text)
        return _meshless_problems(spec, manifest, counts) if counts else []

    @staticmethod
    def _cell_limit(ctx: StageContext, spec: CaseSpec, check: OpResponse) -> list[str]:
        """The run's own limit on cells (the planner checks the plan against it, too)."""
        report = (check.data or {}).get("report")
        cells = report.get("n_cells") if isinstance(report, dict) else None
        if not isinstance(cells, int) or isinstance(cells, bool):
            raise ToolFailure(
                "check_mesh passed without report.n_cells; the executor and the stage disagree"
            )
        limit = ctx.config.limits.max_cells
        if cells > limit:
            return [
                f"{RESOURCE_PREFIX} the mesh has {cells} cells but this run allows at most "
                f"{limit}. Coarsen the mesh where the physics allows it (the plan's estimate is "
                f"{spec.resource_estimate.cells} cells) and keep the resolution at walls and in "
                "shear layers"
            ]
        return []
