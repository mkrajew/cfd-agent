"""Bounding-box formatting and comparison shared by the geometry gate, mesh gate and Gmsh helpers.

Every comparison fails closed: a non-finite value (NaN, inf) in the actual or the expected box, or
a tolerance that is not a finite positive number, is reported as an error and never passes.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

BBox = tuple[float, float, float, float, float, float]  # xmin, ymin, zmin, xmax, ymax, zmax


def format_bbox(bbox: Sequence[float]) -> str:
    return "(" + ", ".join(f"{v:.6g}" for v in bbox) + ")"


def _largest_extent(bbox: Sequence[float]) -> float:
    return max(bbox[3 + i] - bbox[i] for i in range(3))


def _axis_tolerances(expected: Sequence[float], rel_tol: float) -> tuple[float, float, float]:
    """Absolute tolerance per axis: ``rel_tol`` times that axis' expected extent.

    An axis with zero extent (a flat expected box) falls back to ``rel_tol`` times the largest
    extent, because ``rel_tol * 0`` would demand an exact match.
    """
    largest = _largest_extent(expected)
    tols = []
    for i in range(3):
        extent = expected[3 + i] - expected[i]
        tols.append(rel_tol * (extent if extent != 0 else largest))
    return tols[0], tols[1], tols[2]


def validate_expected_bbox(expected: Sequence[float], rel_tol: float) -> str | None:
    """Return an error message if ``expected`` and ``rel_tol`` cannot define a meaningful check.

    The expected box must have 6 finite values with ``min <= max`` on every axis, ``rel_tol`` must
    be finite and positive, and the largest expected extent must be positive (otherwise no
    tolerance can be derived).
    """
    if len(expected) != 6:
        return f"expected bounding box must have 6 values, got {len(expected)}"
    if not all(math.isfinite(v) for v in expected):
        return f"expected bounding box contains non-finite values: {format_bbox(expected)}"
    if not (math.isfinite(rel_tol) and rel_tol > 0):
        return f"bounding box tolerance must be a finite positive number, got {rel_tol!r}"
    if any(expected[3 + i] < expected[i] for i in range(3)):
        return f"expected bounding box {format_bbox(expected)} has a minimum above its maximum"
    tol = rel_tol * _largest_extent(expected)
    if not (math.isfinite(tol) and tol > 0):
        return (
            f"expected bounding box {format_bbox(expected)} has no positive extent, so no "
            "tolerance can be derived from it"
        )
    return None


def compare_bbox(
    actual: Sequence[float],
    expected: Sequence[float],
    rel_tol: float,
    mismatch_hint: str = "",
) -> str | None:
    """Compare two boxes coordinate by coordinate; return an error message or ``None`` if equal.

    The tolerance is applied per axis: ``rel_tol`` times the expected extent of that axis (extent 0
    falls back to ``rel_tol`` times the largest extent). The box matches only if
    ``abs(a - e) <= tol`` holds for every coordinate, so NaN can never pass. ``mismatch_hint`` is
    appended to the message of a plain mismatch (not to validation errors).

    Rationale (a proposal awaiting confirmation by ``cfd-reviewer``): a tolerance tied to the
    largest extent is far too loose for a slender body. A thin feature (small radius, thin plate,
    one-cell-thick domain) can be wrong by many percent of its own size and still sit within
    ``rel_tol`` of the largest extent. Scaling by the extent of the axis in question keeps the
    check as strict, relative to the feature, on every axis.
    """
    invalid = validate_expected_bbox(expected, rel_tol)
    if invalid is not None:
        return invalid
    if len(actual) != 6 or not all(math.isfinite(v) for v in actual):
        return (
            "bounding box of the result contains non-finite values or does not have 6 values: "
            f"{format_bbox(actual)}"
        )
    tols = _axis_tolerances(expected, rel_tol)
    # Coordinates 0..2 are the minima and 3..5 the maxima, so the axis of coordinate i is i % 3.
    pairs = enumerate(zip(actual, expected, strict=True))
    if all(abs(a - e) <= tols[i % 3] for i, (a, e) in pairs):
        return None
    return (
        f"bounding box mismatch: expected {format_bbox(expected)}, got {format_bbox(actual)} "
        f"(tolerance per axis x, y, z: {format_bbox(tols)[1:-1]})." + mismatch_hint
    )
