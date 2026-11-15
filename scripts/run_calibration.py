"""Run a hand-written benchmark solution through the toolchain and score it.

Usage, inside the Docker image (from the repository root, bind-mounted at /work):

    python scripts/run_calibration.py tests/fixtures/handwritten/cavity_re1000 \\
        --benchmark cavity-re1000 [--n-procs N] [--keep DIR]

The fixture directory holds ``geometry.py``, ``mesh.py``, ``case/`` (the OpenFOAM dictionaries),
``post.py`` and ``fixture.json`` (``{"expected_bbox": [xmin, ymin, zmin, xmax, ymax, zmax]}``,
which the geometry and mesh gates need). The steps are the ones the pipeline stages use, through
``cfdagent.tools``: geometry, geometry gate, mesh, conversion to OpenFOAM, patch types, mesh gate,
copy of ``case/``, solver (named by ``application`` in the case ``controlDict``), ``post.py``,
then the deterministic Scorer against ``benchmarks/<id>``. Everything happens in a temporary
directory that is removed afterwards unless ``--keep DIR`` is given; nothing is written into the
repository.

The report lists, per criterion, the value, the reference, the error, the threshold and the
verdict. For a profile criterion value and reference are taken at the position of the largest
error (``at``). It also gives cells, solver, the end time or iteration count, whether
``residualControl`` ended a steady run, the last residuals and the wall times.

Exit status: 0 only if every step succeeded and every criterion passed; 1 otherwise; 2 for a
usage error (missing fixture directory or ``fixture.json``, unknown benchmark).
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import shutil
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
# Import the cfdagent checked out next to this script, not a copy installed in the image (which is
# stale when the image was not rebuilt); pytest does the same through ``pythonpath = ["src"]``.
sys.path.insert(0, str(REPO_ROOT / "src"))
SAMPLING_NAME = "postProcessing"
SOLVER_TIMEOUT_S = 3600.0  # a steady run takes minutes; a hang must not stall the caller
CONTROL_DICT = Path("system") / "controlDict"

_APPLICATION_RE = re.compile(r"^\s*application\s+(\w+)\s*;", re.MULTILINE)


class CalibrationUsageError(Exception):
    """The command line or the fixture directory is unusable (exit status 2)."""


class _StepFailed(Exception):
    """A pipeline step failed; the message names the step and carries the log tail."""


@dataclass
class CalibrationResult:
    """The outcome of one calibration run.

    ``criteria``: one dict per criterion with ``quantity``, ``passed``, ``error``, ``threshold``,
    ``reason`` and, when they could be found, ``at`` / ``value`` / ``reference`` (position of the
    largest error of a profile with the simulated and the reference value there). ``failure`` is
    set when a step did not succeed; then ``criteria`` is empty and ``passed`` is False.
    """

    passed: bool
    criteria: list[dict[str, Any]]
    wall_seconds: float
    failure: str | None = None
    workdir: Path | None = None
    n_cells: int | None = None
    solver: str | None = None
    case_end_time: float | None = None
    end_time: float | None = None
    converged_at: float | None = None
    solver_wall_seconds: float | None = None
    final_residuals: dict[str, float] = field(default_factory=dict)
    last_initial_residuals: dict[str, float] = field(default_factory=dict)
    sample_times: dict[str, float] = field(default_factory=dict)

    @property
    def ended_by_residual_control(self) -> bool:
        """True when the steady solver announced convergence before the iteration cap."""
        return (
            self.converged_at is not None
            and self.case_end_time is not None
            and self.converged_at < self.case_end_time
        )


def solver_name(control_dict_text: str) -> str:
    """The ``application`` entry of a controlDict text (the first one that is not commented)."""
    text = re.sub(r"//.*", "", control_dict_text)
    match = _APPLICATION_RE.search(text)
    if match is None:
        raise CalibrationUsageError(f"no 'application' entry in the case {CONTROL_DICT}")
    return match.group(1)


def _read_reference(path: Path) -> tuple[list[float], list[float]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = [row for row in csv.reader(handle) if row][1:]  # header is x,value
    return [float(r[0]) for r in rows], [float(r[1]) for r in rows]


def profile_detail(profile: dict, reference_file: Path, normalise_by: float | None) -> dict | None:
    """Where a simulated profile differs most from its reference: ``at``, ``value``, ``reference``.

    Same comparison as the Scorer (the simulated profile linearly interpolated onto the reference
    abscissae); ``value`` is the interpolated simulated value at that position. None when the data
    cannot be read; the Scorer then reports the reason itself.
    """
    import numpy as np

    try:
        ref_x, ref_v = _read_reference(reference_file)
        sim_x = np.asarray(profile["x"], dtype=float)
        sim_v = np.asarray(profile["value"], dtype=float)
        order = np.argsort(sim_x, kind="stable")
        interpolated = np.interp(ref_x, sim_x[order], sim_v[order])
        difference = np.abs(interpolated - np.asarray(ref_v)) / (normalise_by or 1.0)
        worst = int(np.argmax(difference))
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return None
    return {
        "at": float(ref_x[worst]),
        "value": float(interpolated[worst]),
        "reference": float(ref_v[worst]),
    }


def _criterion_rows(benchmark, score_result, quantities) -> list[dict[str, Any]]:
    rows = []
    for criterion, scored in zip(benchmark.criteria, score_result.criteria, strict=True):
        row: dict[str, Any] = {
            "quantity": scored.quantity,
            "passed": scored.passed,
            "error": scored.error,
            "threshold": scored.threshold,
            "reason": scored.reason,
        }
        value = quantities.get(criterion.quantity)
        if criterion.kind == "profile" and isinstance(value, dict):
            reference_file = benchmark.root / str(criterion.reference_file)
            row.update(profile_detail(value, reference_file, criterion.normalise_by) or {})
        elif criterion.kind == "scalar" and isinstance(value, int | float):
            row["value"] = float(value)
            if criterion.reference_value is not None:
                row["reference"] = criterion.reference_value
            elif criterion.reference_range is not None:
                low, high = criterion.reference_range
                row["reference"] = f"{low:g}..{high:g}"
        rows.append(row)
    return rows


def _fixture_settings(fixture_dir: Path) -> tuple[float, ...]:
    """The expected bounding box from ``fixture.json``; usage errors for anything unusable."""
    path = fixture_dir / "fixture.json"
    if not path.is_file():
        raise CalibrationUsageError(f"{path} is missing (needs {{'expected_bbox': [6 numbers]}})")
    try:
        bbox = json.loads(path.read_text(encoding="utf-8"))["expected_bbox"]
        values = tuple(float(v) for v in bbox)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CalibrationUsageError(f"{path}: cannot read 'expected_bbox' ({exc})") from exc
    if len(values) != 6 or not all(math.isfinite(v) for v in values):
        raise CalibrationUsageError(
            f"{path}: 'expected_bbox' must be 6 finite numbers [xmin, ymin, zmin, xmax, ymax, zmax]"
        )
    return values


def _check_inputs(fixture_dir: Path, benchmark_id: str, repo_root: Path):
    """Validate the arguments; return ``(benchmark, expected_bbox)``."""
    from cfdagent.bench.registry import RegistryError, load_registry

    if not fixture_dir.is_dir():
        raise CalibrationUsageError(f"{fixture_dir} is not a directory")
    try:
        registry = load_registry(repo_root / "benchmarks")
    except RegistryError as exc:
        raise CalibrationUsageError(f"cannot load the benchmarks: {exc}") from exc
    if benchmark_id not in registry:
        raise CalibrationUsageError(
            f"unknown benchmark {benchmark_id!r}; known: {', '.join(sorted(registry))}"
        )
    bbox = _fixture_settings(fixture_dir)
    for name in ("geometry.py", "mesh.py", "post.py"):
        if not (fixture_dir / name).is_file():
            raise CalibrationUsageError(f"{fixture_dir / name} is missing")
    if not (fixture_dir / "case").is_dir():
        raise CalibrationUsageError(f"{fixture_dir / 'case'} is missing")
    return registry[benchmark_id], bbox


def _require(ok: bool, step: str, detail: str) -> None:
    if not ok:
        raise _StepFailed(f"{step}: {detail}")


def _sample_times(case_dir: Path) -> dict[str, float]:
    """Latest time directory of every function object under ``postProcessing``."""
    from cfdagent.tools.post import PostProcessingError, latest_time_dir

    times: dict[str, float] = {}
    root = case_dir / SAMPLING_NAME
    for directory in sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []:
        try:
            times[directory.name] = float(latest_time_dir(directory).name)
        except PostProcessingError:
            continue
    return times


def _run_steps(
    fixture_dir: Path, benchmark, bbox, workdir: Path, n_procs: int, result: CalibrationResult
) -> None:
    """The pipeline steps; fills ``result`` as it goes, raises ``_StepFailed`` on a failure."""
    from cfdagent.bench.scoring import score
    from cfdagent.manifest import MANIFEST_FILE_NAME, BoundaryManifest
    from cfdagent.tools.foam import run_solver
    from cfdagent.tools.geometry import check_geometry, run_geometry_script
    from cfdagent.tools.mesh import apply_patch_types, check_mesh, convert_to_foam, run_mesh_script
    from cfdagent.tools.process import run_tool

    geometry = run_geometry_script(fixture_dir / "geometry.py", workdir)
    _require(geometry.ok, "geometry script", geometry.tail)
    gate = check_geometry(workdir, bbox)
    _require(gate.ok, "geometry gate", "; ".join(gate.errors))

    meshed = run_mesh_script(fixture_dir / "mesh.py", workdir)
    _require(meshed.ok, "mesh script", meshed.tail)
    case_dir = workdir / "case"
    converted = convert_to_foam(workdir / "mesh.msh", case_dir)
    _require(converted.ok, "gmshToFoam", converted.tail)
    manifest = BoundaryManifest.from_file(workdir / MANIFEST_FILE_NAME)
    apply_patch_types(case_dir, manifest)
    mesh_gate = check_mesh(case_dir, manifest, bbox)
    result.n_cells = getattr(mesh_gate.data, "n_cells", None)
    _require(mesh_gate.ok, "mesh gate", "; ".join(mesh_gate.errors))

    shutil.copytree(fixture_dir / "case", case_dir, dirs_exist_ok=True)
    result.solver = solver_name((case_dir / CONTROL_DICT).read_text(encoding="utf-8"))
    started = time.monotonic()
    solved = run_solver(case_dir, result.solver, n_procs=n_procs, timeout_s=SOLVER_TIMEOUT_S)
    result.solver_wall_seconds = time.monotonic() - started
    summary = solved.summary
    result.case_end_time = solved.case_end_time
    result.end_time = summary.end_time
    result.converged_at = summary.converged_at
    result.final_residuals = dict(summary.final_residuals)
    result.last_initial_residuals = dict(summary.initial_residuals)
    _require(solved.ok, result.solver, solved.tool.tail)

    quantities_path = workdir / "quantities.json"
    post = run_tool(
        ["python", str(fixture_dir / "post.py"), str(case_dir), str(quantities_path)],
        cwd=workdir,
        log_path=workdir / "post.log",
    )
    _require(post.ok, "post.py", post.tail)
    result.sample_times = _sample_times(case_dir)
    quantities = json.loads(quantities_path.read_text(encoding="utf-8"))
    scored = score(benchmark, quantities)
    result.criteria = _criterion_rows(benchmark, scored, quantities)
    result.passed = scored.passed


def calibrate(
    fixture_dir: Path,
    benchmark_id: str,
    *,
    n_procs: int = 1,
    keep_dir: Path | None = None,
    workdir: Path | None = None,
    repo_root: Path = REPO_ROOT,
) -> CalibrationResult:
    """Run one hand-written solution and score it.

    The work happens in ``workdir`` if given (the caller owns it), else in ``keep_dir`` if given
    (kept), else in a temporary directory that is removed. A failing step does not raise: it gives
    a result with ``failure`` set. Raises ``CalibrationUsageError`` for unusable inputs.
    """
    fixture_dir = Path(fixture_dir).resolve()  # scripts run with another working directory
    benchmark, bbox = _check_inputs(fixture_dir, benchmark_id, Path(repo_root))
    temporary = None
    if workdir is None and keep_dir is None:
        temporary = tempfile.TemporaryDirectory(prefix="cfdagent-calibration-")
    chosen = Path(workdir or keep_dir or temporary.name)  # type: ignore[union-attr]
    chosen.mkdir(parents=True, exist_ok=True)
    result = CalibrationResult(
        passed=False,
        criteria=[],
        wall_seconds=0.0,
        workdir=None if temporary else chosen.resolve(),
    )

    # Scripts run in subprocesses: make them import the cfdagent checked out next to this file.
    saved_path = os.environ.get("PYTHONPATH")
    src = str(Path(repo_root) / "src")
    os.environ["PYTHONPATH"] = os.pathsep.join(filter(None, [src, saved_path]))
    started = time.monotonic()
    try:
        _run_steps(fixture_dir, benchmark, bbox, chosen.resolve(), n_procs, result)
    except _StepFailed as exc:
        result.failure, result.passed, result.criteria = str(exc), False, []
    finally:
        result.wall_seconds = time.monotonic() - started
        if saved_path is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = saved_path
        if temporary is not None:
            temporary.cleanup()
    return result


def _fmt(value: Any, width: int = 0) -> str:
    if value is None:
        text = "-"
    elif isinstance(value, bool):
        text = "yes" if value else "no"
    elif isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        text = str(int(value))
    elif isinstance(value, float):
        text = f"{value:.4g}" if abs(value) >= 1e-4 or value == 0 else f"{value:.3e}"
    else:
        text = str(value)
    return text.ljust(width)


def _residuals(residuals: dict[str, float]) -> str:
    return ", ".join(f"{name} {value:.2e}" for name, value in residuals.items()) or "-"


def format_report(result: CalibrationResult) -> str:
    """The table and the run summary as text."""
    headers = ("criterion", "at", "value", "reference", "error", "threshold", "pass")
    keys = ("quantity", "at", "value", "reference", "error", "threshold", "passed")
    rows = [[_fmt(c.get(k)) for k in keys] for c in result.criteria]
    widths = [
        max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)
    ]
    lines = []
    if result.failure:
        lines += ["FAILED STEP", *("  " + ln for ln in result.failure.splitlines()), ""]
    if rows:
        lines.append("  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True)).rstrip())
        lines += [
            "  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip() for r in rows
        ]
        lines.append(
            "(profile criteria: 'at' is the position of the largest error; value and reference "
            "are the simulated and the reference value there)"
        )
        lines += [f"{c['quantity']}: {c.get('reason')}" for c in result.criteria if not c["passed"]]
        lines.append("")
    iterations = result.end_time if result.converged_at is None else result.converged_at
    lines += [
        f"cells: {_fmt(result.n_cells)}",
        f"solver: {_fmt(result.solver)}",
        f"end time / iterations: {_fmt(iterations)} (cap: endTime {_fmt(result.case_end_time)})",
        f"residualControl ended the run: {_fmt(result.ended_by_residual_control)}",
        f"last initial residuals: {_residuals(result.last_initial_residuals)}",
        f"last final residuals: {_residuals(result.final_residuals)}",
        f"post-processing sample times: {_fmt(result.sample_times)}",
        "wall time: "
        f"total {result.wall_seconds:.1f} s, solver {_fmt(result.solver_wall_seconds)} s",
        f"result: {'PASS' if result.passed else 'FAIL'}",
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("fixture_dir", type=Path, help="hand-written solution directory")
    parser.add_argument("--benchmark", required=True, help="benchmark id, e.g. cavity-re1000")
    parser.add_argument("--n-procs", type=int, default=1, help="MPI ranks, 1 to 4 (default 1)")
    parser.add_argument("--keep", type=Path, metavar="DIR", help="work in DIR and keep it")
    args = parser.parse_args(argv)
    try:
        if not 1 <= args.n_procs <= 4:
            raise CalibrationUsageError("--n-procs must be from 1 to 4")
        result = calibrate(
            args.fixture_dir, args.benchmark, n_procs=args.n_procs, keep_dir=args.keep
        )
    except CalibrationUsageError as exc:
        print(f"usage error: {exc}", file=sys.stderr)
        return 2
    print(format_report(result))
    if result.workdir is not None:
        print(f"work directory kept: {result.workdir}")
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
