"""Deterministic scoring of simulation results against benchmark reference data.

Profiles are compared by linear interpolation of the simulated profile onto the reference
abscissae. The interpolation never extrapolates: a reference point outside the sampled range is an
error, because clamping would silently turn a missing region into a (possibly good) score.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from cfdagent.bench.registry import Benchmark, Criterion

# Profile: {"x": [...], "value": [...]}; scalar: float.
Quantities = dict[str, float | dict[str, list[float]]]

_RANGE_TOLERANCE = 1e-9  # relative to the sampled range


@dataclass(frozen=True)
class CriterionResult:
    quantity: str
    passed: bool
    error: float | None
    threshold: float | None
    reason: str


@dataclass(frozen=True)
class ScoreResult:
    passed: bool
    criteria: list[CriterionResult]


def _finite_array(values, name: str) -> np.ndarray:
    try:
        array = np.asarray(values, dtype=float)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} is not a numeric sequence") from exc
    if array.ndim != 1:
        raise ValueError(f"{name} must be one-dimensional")
    if not np.all(np.isfinite(array)):
        raise ValueError(f"{name} contains non-finite values")
    return array


def profile_error(sim_x, sim_v, ref_x, ref_v, metric: str) -> float:
    """Error of a simulated profile against a reference profile.

    The simulated profile is linearly interpolated onto ``ref_x``. ``metric`` is
    ``"max_abs_error"`` or ``"rms_error"``. The simulated points are sorted by x. Raises
    ``ValueError`` on duplicate simulated x, non-finite or mismatched data, an unknown metric, or
    if any reference x lies outside ``[min(sim_x), max(sim_x)]`` by more than 1e-9 of that range.
    """
    if metric not in ("max_abs_error", "rms_error"):
        raise ValueError(f"unsupported profile metric {metric!r}")
    sx = _finite_array(sim_x, "sim_x")
    sv = _finite_array(sim_v, "sim_v")
    rx = _finite_array(ref_x, "ref_x")
    rv = _finite_array(ref_v, "ref_v")
    if sx.shape != sv.shape:
        raise ValueError("sim_x and sim_v must have the same length")
    if rx.shape != rv.shape:
        raise ValueError("ref_x and ref_v must have the same length")
    if sx.size < 2:
        raise ValueError("the simulated profile needs at least two points")
    if rx.size == 0:
        raise ValueError("the reference profile is empty")

    order = np.argsort(sx, kind="stable")
    sx, sv = sx[order], sv[order]
    if np.any(np.diff(sx) == 0.0):
        raise ValueError("sim_x contains duplicate x values")

    lo, hi = float(sx[0]), float(sx[-1])
    tolerance = _RANGE_TOLERANCE * (hi - lo)
    outside = (rx < lo - tolerance) | (rx > hi + tolerance)
    if np.any(outside):
        bad = rx[outside]
        raise ValueError(
            f"{int(bad.size)} reference point(s) lie outside the simulated range "
            f"[{lo:g}, {hi:g}] (e.g. x={float(bad[0]):g}); refusing to extrapolate"
        )

    difference = np.interp(rx, sx, sv) - rv
    if metric == "max_abs_error":
        return float(np.max(np.abs(difference)))
    return float(np.sqrt(np.mean(difference**2)))


def _read_reference(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """Read a reference CSV with header ``x,value``."""
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.reader(handle))
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot read reference file {path.name}: {exc}") from exc
    if not rows or [cell.strip() for cell in rows[0]] != ["x", "value"]:
        raise ValueError(f"reference file {path.name} must have the header 'x,value'")
    try:
        data = [(float(row[0]), float(row[1])) for row in rows[1:] if row]
    except (ValueError, IndexError) as exc:
        raise ValueError(f"reference file {path.name} has a malformed row: {exc}") from exc
    if not data:
        raise ValueError(f"reference file {path.name} has no data rows")
    x, v = zip(*data, strict=True)
    return np.array(x), np.array(v)


def _evaluate(criterion: Criterion, benchmark: Benchmark, value) -> float:
    """Raw (not yet normalised) error of ``value`` for one criterion; raises ``ValueError``."""
    if criterion.kind == "profile":
        if not isinstance(value, dict):
            raise ValueError("expected a profile ({'x': [...], 'value': [...]}), got a scalar")
        if "x" not in value or "value" not in value:
            raise ValueError("a profile needs the keys 'x' and 'value'")
        assert criterion.reference_file is not None  # guaranteed by Criterion validation
        ref_x, ref_v = _read_reference(benchmark.root / criterion.reference_file)
        return profile_error(value["x"], value["value"], ref_x, ref_v, criterion.metric)

    if isinstance(value, dict) or isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("expected a scalar number, got a profile or another type")
    if not math.isfinite(value):
        raise ValueError("the scalar value is not finite")
    if criterion.metric == "in_range":
        assert criterion.reference_range is not None
        low, high = criterion.reference_range
        return max(low - value, value - high, 0.0)
    assert criterion.reference_value is not None
    absolute = abs(value - criterion.reference_value)
    if criterion.metric == "rel_error":
        return absolute / abs(criterion.reference_value)
    return absolute


def _score_criterion(
    criterion: Criterion, benchmark: Benchmark, quantities: Quantities
) -> CriterionResult:
    def failed(reason: str) -> CriterionResult:
        return CriterionResult(criterion.quantity, False, None, criterion.threshold, reason)

    if criterion.quantity not in quantities:
        return failed("missing quantity")
    try:
        error = _evaluate(criterion, benchmark, quantities[criterion.quantity])
    except ValueError as exc:
        return failed(str(exc))

    if criterion.normalise_by is not None:
        error /= criterion.normalise_by
    if criterion.metric == "in_range":
        passed = error == 0.0
        reason = "within range" if passed else "outside the reference range"
    else:
        assert criterion.threshold is not None
        passed = error <= criterion.threshold
        reason = "within threshold" if passed else "exceeds threshold"
    return CriterionResult(criterion.quantity, passed, error, criterion.threshold, reason)


def score(benchmark: Benchmark, quantities: Quantities) -> ScoreResult:
    """Score ``quantities`` against every criterion of ``benchmark``.

    Never raises on bad simulation data: a missing, mistyped or unusable quantity gives a failed
    criterion with a reason. The result passes only if there is at least one criterion and all
    of them pass.
    """
    results = [_score_criterion(c, benchmark, quantities) for c in benchmark.criteria]
    return ScoreResult(passed=bool(results) and all(r.passed for r in results), criteria=results)
