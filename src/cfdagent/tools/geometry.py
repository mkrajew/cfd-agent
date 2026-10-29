"""Geometry stage: run a CadQuery script and gate its output (STEP + boundary manifest)."""

from __future__ import annotations

import math
from pathlib import Path

from cfdagent.gates import GateResult
from cfdagent.manifest import MANIFEST_FILE_NAME, BoundaryManifest, ManifestError
from cfdagent.tools.bbox import compare_bbox, validate_expected_bbox
from cfdagent.tools.process import ToolResult, fail_after_run, run_tool

STEP_FILE_NAME = "geometry.step"
GEOMETRY_LOG_NAME = "geometry.log"


def run_geometry_script(script: Path, workdir: Path, timeout_s: float = 300.0) -> ToolResult:
    """Run a geometry script with ``cwd=workdir``.

    The script must write ``geometry.step`` and ``boundary_manifest.json`` into its working
    directory. Output goes to ``geometry.log`` in ``workdir``. Stale copies of both files from an
    earlier run are removed first. If the script exits normally but did not write both, the
    returned result has ``ok=False`` and the reason is appended to the log (and shown in ``tail``).
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    outputs = [workdir / STEP_FILE_NAME, workdir / MANIFEST_FILE_NAME]
    for path in outputs:
        path.unlink(missing_ok=True)

    result = run_tool(
        ["python", str(Path(script).resolve())],
        cwd=workdir,
        log_path=workdir / GEOMETRY_LOG_NAME,
        timeout_s=timeout_s,
    )
    if not result.ok:
        return result

    missing = [path.name for path in outputs if not path.is_file()]
    if not missing:
        return result
    problem = f"the geometry script finished but did not write {', '.join(missing)} in {workdir}"
    return fail_after_run(result, problem)


def _manifest_coverage_error(step_path: Path, manifest: BoundaryManifest) -> str | None:
    """Import the STEP into a scratch Gmsh model and match its boundary surfaces to the manifest.

    Returns an error message (naming the geometry stage) if the STEP cannot be imported or the
    manifest does not cover the boundary exactly, otherwise ``None``. Gmsh is imported here so the
    module loads on hosts without it. A Gmsh session started here is finalised again; a session
    that was already running is left alone apart from the scratch model, which is removed.
    """
    import gmsh

    from cfdagent.tools.gmsh_helpers import match_surfaces

    started_here = not gmsh.isInitialized()
    if started_here:
        gmsh.initialize()
        gmsh.option.setNumber("General.Terminal", 0)
    try:
        gmsh.model.add("geometry_gate_scratch")
        try:
            gmsh.model.occ.importShapes(str(step_path))
            gmsh.model.occ.synchronize()
            match_surfaces(manifest)
        except ManifestError as exc:
            return f"geometry stage: the boundary manifest does not cover {STEP_FILE_NAME}: {exc}"
        except Exception as exc:
            return f"geometry stage: {STEP_FILE_NAME} could not be checked in Gmsh: {exc}"
        finally:
            gmsh.model.remove()
    finally:
        if started_here:
            gmsh.finalize()
    return None


def check_geometry(
    workdir: Path,
    expected_bbox: tuple[float, ...],
    expected_volume: float | None = None,
    rel_tol: float = 1e-3,
) -> GateResult:
    """Check the geometry stage output in ``workdir``.

    Verifies that ``geometry.step`` and ``boundary_manifest.json`` exist, the manifest parses, the
    STEP holds at least one solid and every solid is valid, and the combined bounding box
    (xmin, ymin, zmin, xmax, ymax, zmax) matches ``expected_bbox`` within ``rel_tol`` times the
    expected extent of each axis (see ``compare_bbox``). If ``expected_volume`` is given, the sum
    of the volumes of all solids must equal it within ``rel_tol`` (relative); this catches a
    wrong size that leaves the bounding box unchanged. Finally the manifest must cover the
    geometry: every boundary surface matched by exactly one patch and no patch without a surface
    (an empty patch list is rejected). NaN or inf in the actual or expected values, and a
    ``rel_tol`` that is not a finite positive number, are gate errors and never pass.
    """
    workdir = Path(workdir)
    errors: list[str] = []

    invalid = validate_expected_bbox(expected_bbox, rel_tol)
    if invalid is not None:
        return GateResult(False, [invalid])
    if expected_volume is not None and not (math.isfinite(expected_volume) and expected_volume > 0):
        return GateResult(
            False, [f"expected volume must be a finite positive number, got {expected_volume!r}"]
        )

    step_path = workdir / STEP_FILE_NAME
    manifest_path = workdir / MANIFEST_FILE_NAME

    manifest: BoundaryManifest | None = None
    if not manifest_path.is_file():
        errors.append(f"{MANIFEST_FILE_NAME} is missing in {workdir}")
    else:
        try:
            manifest = BoundaryManifest.from_file(manifest_path)
        except ManifestError as exc:
            errors.append(f"{MANIFEST_FILE_NAME} is invalid: {exc}")

    if not step_path.is_file():
        errors.append(f"{STEP_FILE_NAME} is missing in {workdir}")
        return GateResult(False, errors)

    import cadquery as cq  # imported here so the module loads on hosts without CadQuery

    try:
        solids = cq.importers.importStep(str(step_path)).solids().vals()
    except Exception as exc:
        errors.append(f"{STEP_FILE_NAME} could not be read: {exc}")
        return GateResult(False, errors)

    if not solids:
        errors.append(f"{STEP_FILE_NAME} contains no solid")
        return GateResult(False, errors)

    for i, solid in enumerate(solids):
        if not solid.isValid():
            errors.append(f"solid {i} in {STEP_FILE_NAME} is not valid (isValid() is False)")

    boxes = [s.BoundingBox() for s in solids]
    actual = (
        min(b.xmin for b in boxes),
        min(b.ymin for b in boxes),
        min(b.zmin for b in boxes),
        max(b.xmax for b in boxes),
        max(b.ymax for b in boxes),
        max(b.zmax for b in boxes),
    )
    mismatch = compare_bbox(actual, tuple(float(v) for v in expected_bbox), rel_tol)
    if mismatch is not None:
        errors.append(mismatch)

    if expected_volume is not None:
        actual_volume = sum(s.Volume() for s in solids)
        # Written so that a NaN volume fails: the comparison is false for NaN.
        if not abs(actual_volume - expected_volume) <= rel_tol * expected_volume:
            errors.append(
                f"volume mismatch: expected {expected_volume:.6g}, got {actual_volume:.6g} "
                f"(tolerance {rel_tol * expected_volume:.3g})"
            )

    if manifest is not None:
        coverage = _manifest_coverage_error(step_path, manifest)
        if coverage is not None:
            errors.append(coverage)

    return GateResult(not errors, errors)
