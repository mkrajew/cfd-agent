"""Geometry stage: run a CadQuery script and gate its output (STEP + boundary manifest)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cfdagent.manifest import MANIFEST_FILE_NAME, BoundaryManifest
from cfdagent.tools.process import ToolResult, run_tool

STEP_FILE_NAME = "geometry.step"
GEOMETRY_LOG_NAME = "geometry.log"


@dataclass(frozen=True)
class GateResult:
    """Outcome of a deterministic stage gate; ``errors`` is empty when ``ok``."""

    ok: bool
    errors: list[str]


def run_geometry_script(script: Path, workdir: Path, timeout_s: float = 300.0) -> ToolResult:
    """Run a geometry script with ``cwd=workdir``.

    The script must write ``geometry.step`` and ``boundary_manifest.json`` into its working
    directory. Output goes to ``geometry.log`` in ``workdir``.
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    return run_tool(
        ["python", str(Path(script).resolve())],
        cwd=workdir,
        log_path=workdir / GEOMETRY_LOG_NAME,
        timeout_s=timeout_s,
    )


def _fmt_bbox(bbox: tuple[float, ...]) -> str:
    return "(" + ", ".join(f"{v:.6g}" for v in bbox) + ")"


def check_geometry(
    workdir: Path, expected_bbox: tuple[float, ...], rel_tol: float = 1e-3
) -> GateResult:
    """Check the geometry stage output in ``workdir``.

    Verifies that ``geometry.step`` and ``boundary_manifest.json`` exist, the manifest parses, the
    STEP holds at least one solid and every solid is valid, and the combined bounding box
    (xmin, ymin, zmin, xmax, ymax, zmax) matches ``expected_bbox`` within ``rel_tol`` times the
    largest expected dimension, per coordinate.
    """
    workdir = Path(workdir)
    errors: list[str] = []

    if len(expected_bbox) != 6:
        return GateResult(
            False, [f"expected bounding box must have 6 values, got {len(expected_bbox)}"]
        )

    step_path = workdir / STEP_FILE_NAME
    manifest_path = workdir / MANIFEST_FILE_NAME

    if not manifest_path.is_file():
        errors.append(f"{MANIFEST_FILE_NAME} is missing in {workdir}")
    else:
        try:
            BoundaryManifest.from_file(manifest_path)
        except Exception as exc:  # pydantic ValidationError, JSON or encoding errors
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
    expected = tuple(float(v) for v in expected_bbox)
    largest = max(expected[3] - expected[0], expected[4] - expected[1], expected[5] - expected[2])
    tol = rel_tol * largest
    if any(abs(a - e) > tol for a, e in zip(actual, expected, strict=True)):
        errors.append(
            f"bounding box mismatch: expected {_fmt_bbox(expected)}, got {_fmt_bbox(actual)} "
            f"(tolerance {tol:.3g})"
        )

    return GateResult(not errors, errors)
