"""The Geometry stage, stage 2 of the pipeline (design 6.2, 6.3).

The agent writes a CadQuery script, ``geometry.py``, into its attempt directory. The gate runs it
in the sandbox (``run_script`` with ``kind="geometry"``: working directory is the attempt
directory, the script must write ``geometry.step`` and ``boundary_manifest.json``), reads the
manifest on the host with a size limit, and then lets the sandbox check the STEP and the manifest
against the plan (``check_geometry``): one valid solid, bounding box and volume of the ``CaseSpec``
within ``REL_TOL``, every boundary face named by exactly one patch.

The host reads the manifest only to compare it with the plan: patch names, dimensionality and the
patch types that a 2D (one ``empty`` patch) or axisymmetric (two ``wedge`` patches) case needs.
A manifest that does not parse is left to the sandbox gate, which reports the details.

Errors the agent can fix come back as a failed ``GateResult``. A failed executor or tool
(``OpResponse.internal``) raises ``ToolFailure``: it is not the agent's fault and the loop lets it
propagate.

This module is host-side: it imports no tool, CAD or mesh code.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from cfdagent.agents.casespec import CaseSpec
from cfdagent.agents.stage import Stage, StageContext
from cfdagent.agents.tools import FileScope
from cfdagent.errors import ToolFailure
from cfdagent.executor.protocol import CheckGeometryArgs, OpResponse, RunScriptArgs
from cfdagent.gates import GateResult
from cfdagent.manifest import MANIFEST_FILE_NAME, BoundaryManifest
from cfdagent.rundir import FileTooLarge, RunDirError
from cfdagent.sandbox.client import outer_timeout

STAGE = "geometry"
AGENT = "geometry"
SCRIPT_NAME = "geometry.py"
STEP_NAME = "geometry.step"
PROMOTED_DIR = "geometry"
SCRIPT_TIMEOUT_S = 300.0
GATE_TIMEOUT_S = 120.0
REL_TOL = 1e-3  # ruling R11
MANIFEST_MAX_BYTES = 200_000  # a manifest of a real geometry is a few kilobytes
SCRIPT_TAIL_BYTES = 3000  # the loop shows the model at most 4096 bytes of all errors together
_MAX_NAMES_SHOWN = 12
_MAX_NAME_CHARS = 40
Z_PLANE_TOL = 1e-6  # relative to the largest extent, like the sandbox's face matching


def _last_bytes(text: str, limit: int) -> str:
    """The end of ``text`` (at most ``limit`` UTF-8 bytes), where a traceback keeps its cause."""
    data = text.encode("utf-8", "replace")
    if len(data) <= limit:
        return text
    return "[earlier output omitted]\n" + data[-limit:].decode("utf-8", "ignore")


def _names(names: list[str] | set[str]) -> str:
    """A bounded, readable list of names (the plan's names have no length limit)."""
    ordered = sorted(names)
    shown = []
    for name in ordered[:_MAX_NAMES_SHOWN]:
        text = name if len(name) <= _MAX_NAME_CHARS else name[:_MAX_NAME_CHARS] + "..."
        shown.append(f"`{text}`")
    more = f" and {len(ordered) - _MAX_NAMES_SHOWN} more" if len(ordered) > _MAX_NAMES_SHOWN else ""
    return ", ".join(shown) + more


def _internal(response: OpResponse, label: str) -> ToolFailure:
    return ToolFailure(f"{label} failed outside the agent's work: {response.error}")


def _script_failure(response: OpResponse) -> str:
    """The message for a script that did not run to the end or did not write its outputs."""
    data = response.data or {}
    error = response.error or "the script failed"
    headline, _, rest = error.partition("\n")
    tail = data.get("tail")
    if not isinstance(tail, str) or not tail.strip():
        tail = rest
    message = f"{SCRIPT_NAME} did not run successfully: {headline}"
    if tail.strip():
        shown = _last_bytes(tail, SCRIPT_TAIL_BYTES)
        message += f"\nEnd of the script's output (including your prints):\n{shown}"
    return message


def _gate_errors(response: OpResponse) -> list[str]:
    data = response.data or {}
    errors = data.get("errors")
    if isinstance(errors, list) and errors:
        return [str(e) for e in errors]
    return [response.error or "the geometry checks failed without a message"]


def _z_face_problems(spec: CaseSpec, manifest: BoundaryManifest) -> list[str]:
    """2D: the faces on z = zmin and z = zmax are the front and back faces; they must be `empty`."""
    box = spec.geometry.bbox
    largest = max(box.xmax - box.xmin, box.ymax - box.ymin, box.zmax - box.zmin)
    tol = 1e-7 + Z_PLANE_TOL * largest
    wrong = []
    for patch in manifest.patches:
        on_z_face = any(
            loc.kind == "plane"
            and loc.axis == "z"
            and loc.value is not None
            and (abs(loc.value - box.zmin) <= tol or abs(loc.value - box.zmax) <= tol)
            for loc in patch.locators
        )
        if on_z_face and patch.type != "empty":
            wrong.append(f"`{patch.name}` ({patch.type})")
    if not wrong:
        return []
    return [
        "in a 2D case the faces on the planes z = zmin and z = zmax of the plan are the front and "
        "back faces, and both must belong to patches of type `empty`; these patches have a plane "
        f"locator there but another type: {', '.join(wrong)}"
    ]


def _structural_problems(spec: CaseSpec, manifest: BoundaryManifest) -> list[str]:
    """The patch types that the dimensionality of the plan requires or forbids."""
    kinds = [p.type for p in manifest.patches]
    empties, wedges = kinds.count("empty"), kinds.count("wedge")
    if spec.dimensionality == "2D":
        problems = []
        if wedges:
            problems.append("a 2D case has no `wedge` patch; the front and back faces are `empty`")
        return problems + _z_face_problems(spec, manifest)
    if spec.dimensionality == "axisymmetric":
        if wedges != 2 or empties:
            return [
                "an axisymmetric case needs exactly two patches of type `wedge`, one for the "
                "front face and one for the back face (two separate planar patches), and no "
                f"`empty` patch; {MANIFEST_FILE_NAME} has {wedges} `wedge` and {empties} "
                "`empty` patches"
            ]
        return []
    if empties or wedges:
        return [
            f"a 3D case has no `empty` and no `wedge` patch; {MANIFEST_FILE_NAME} has {empties} "
            f"`empty` and {wedges} `wedge` patches"
        ]
    return []


def _manifest_problems(spec: CaseSpec, manifest: BoundaryManifest) -> list[str]:
    """What the manifest says that the plan does not allow.

    The check must be satisfiable for every plan that the Planner gate accepts, so it never
    demands a name the plan may lack: the patches that the dimensionality prescribes (`empty` in
    2D, `wedge` in axisymmetric) may carry any name, and a patch name that the plan does not list
    is an error only for the other patches. The plan's names without a patch are a hint.
    """
    problems = []
    structural = {"2D": "empty", "axisymmetric": "wedge"}.get(spec.dimensionality)
    planned = {b.name for b in spec.boundaries}
    named = {p.name for p in manifest.patches}
    unknown = {p.name for p in manifest.patches if p.type != structural} - planned
    if unknown:
        hint = ""
        if planned - named:
            hint = f"; boundaries of the plan with no patch of that name: {_names(planned - named)}"
        problems.append(
            f"the patch names {_names(unknown)} in {MANIFEST_FILE_NAME} are not boundaries of the "
            "plan. Name each patch exactly like a boundary of the plan"
            + (f" (only the {structural} patches may have any name)" if structural else "")
            + hint
        )
    if manifest.dimensionality != spec.dimensionality:
        problems.append(
            f'{MANIFEST_FILE_NAME} says dimensionality "{manifest.dimensionality}" but the plan '
            f'says "{spec.dimensionality}"; write the value of the plan'
        )
    return problems + _structural_problems(spec, manifest)


def _read_manifest(ctx: StageContext, rel: str) -> tuple[BoundaryManifest | None, list[str]]:
    """The manifest, read with a size limit; ``(None, [])`` if it is missing or does not parse
    (the sandbox gate describes that), ``(None, [problem])`` if it is too large."""
    try:
        data = ctx.run.read_json(rel, MANIFEST_MAX_BYTES)
        return BoundaryManifest.model_validate(data), []
    except FileTooLarge:
        return None, [
            f"{MANIFEST_FILE_NAME} is larger than {MANIFEST_MAX_BYTES} bytes; a manifest names a "
            "handful of patches with a few locators each. Write a much smaller file"
        ]
    except (RunDirError, ValidationError, FileNotFoundError):
        return None, []


class GeometryStage(Stage):
    """Stage 2: a CadQuery script, run in the sandbox, gated against the ``CaseSpec``."""

    name = STAGE
    agent = AGENT
    scope = FileScope(
        readable=("casespec.json", "attempts/geometry-*/*"),
        writable=("attempts/geometry-*/geometry.py", "attempts/geometry-*/*.md"),
    )

    def opening_message(self, ctx: StageContext) -> str:
        spec = self._spec(ctx)
        plan = {
            "title": spec.title,
            "dimensionality": spec.dimensionality,
            "geometry": spec.geometry.model_dump(),
            "boundaries": [b.model_dump() for b in spec.boundaries],
        }
        return (
            "Build the fluid domain of this plan as a CadQuery model.\n\n"
            f"{json.dumps(plan, indent=2)}\n\n"
            f"Write {SCRIPT_NAME} so that the solid has exactly this bounding box and volume, and "
            f"{MANIFEST_FILE_NAME} names every boundary face with exactly the boundary names "
            "above. Units are metres."
        )

    def execute_and_gate(self, ctx: StageContext, attempt_dir: Path) -> GateResult:
        spec = self._spec(ctx)
        rel = ctx.run.relative(attempt_dir)
        script = f"{rel}/{SCRIPT_NAME}"
        if not ctx.run.path(script).is_file():
            return GateResult(
                False,
                [
                    f"{SCRIPT_NAME} does not exist in {rel}/. Write it there with write_file "
                    f"(path {script}); it must write {STEP_NAME} and {MANIFEST_FILE_NAME} "
                    "into its working directory"
                ],
            )

        script_s = ctx.inner_timeout(SCRIPT_TIMEOUT_S)
        run = ctx.sandbox.call(
            "run_script",
            RunScriptArgs(script=script, cwd=rel, timeout_s=script_s, kind="geometry"),
            outer_timeout(script_s),
        )
        if run.internal:
            raise _internal(run, "running the geometry script")
        if not run.ok:
            return GateResult(False, [_script_failure(run)])

        manifest, errors = _read_manifest(ctx, f"{rel}/{MANIFEST_FILE_NAME}")
        if manifest is not None:
            errors += _manifest_problems(spec, manifest)

        gate_s = ctx.inner_timeout(GATE_TIMEOUT_S)
        check = ctx.sandbox.call(
            "check_geometry",
            CheckGeometryArgs(
                workdir=rel,
                expected_bbox=list(spec.expected_bbox()),
                expected_volume=spec.expected_volume(),
                rel_tol=REL_TOL,
            ),
            outer_timeout(gate_s),
        )
        if check.internal:
            raise _internal(check, "checking the geometry")
        if not check.ok:
            errors += _gate_errors(check)
        return GateResult(not errors, errors)

    def promote(self, ctx: StageContext, attempt_dir: Path) -> None:
        ctx.run.promote(attempt_dir, PROMOTED_DIR)

    @staticmethod
    def _spec(ctx: StageContext) -> CaseSpec:
        if ctx.casespec is None:
            raise ToolFailure("the geometry stage needs the CaseSpec; run the planner first")
        return ctx.casespec
