"""Bounding-box formatting and comparison shared by the geometry gate, mesh gate and Gmsh helpers.

Every comparison fails closed: a non-finite value (NaN, inf) in the actual or the expected box, or
a tolerance that is not a finite positive number, is reported as an error and never passes.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

BBox = tuple[float, float, float, float, float, float]  # xmin, ymin, zmin, xmax, ymax, zmax

# Representation floor: the bounding box printed by checkMesh has 6 significant digits (the
# polyMesh points themselves have more), so a printed coordinate is off by up to 5e-6 of its own
# magnitude. 1e-5 leaves a factor of two. The floor is relative to the coordinate, not to the
# extent: a box whose offset from the origin exceeds its extent by more than about 100 times
# cannot be resolved at 6 digits, and for such a box the floor (not ``rel_tol``) decides.
REPR_EPS = 1e-5


def format_bbox(bbox: Sequence[float]) -> str:
    return "(" + ", ".join(f"{v:.6g}" for v in bbox) + ")"


def _largest_extent(bbox: Sequence[float]) -> float:
    return max(bbox[3 + i] - bbox[i] for i in range(3))


def _axis_tolerances(expected: Sequence[float], rel_tol: float) -> tuple[float, ...]:
    """Absolute tolerance per coordinate (6 values: xmin, ymin, zmin, xmax, ymax, zmax).

    For coordinate ``i`` on axis ``i % 3``: ``max(rel_tol * E, REPR_EPS * abs(expected[i]))`` where
    ``E`` is the expected extent of that axis (an axis with zero extent uses the largest extent,
    because ``rel_tol * 0`` would demand an exact match).
    """
    largest = _largest_extent(expected)
    tols = []
    for i in range(6):
        extent = expected[3 + i % 3] - expected[i % 3]
        scale = extent if extent != 0 else largest
        tols.append(max(rel_tol * scale, REPR_EPS * abs(expected[i])))
    return tuple(tols)


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

    For each coordinate ``i`` (axis ``i % 3``) the box matches if
    ``abs(actual[i] - expected[i]) <= max(rel_tol * E, REPR_EPS * abs(expected[i]))``, where ``E``
    is the expected extent of that axis (zero extent: the largest expected extent). NaN never
    passes. ``mismatch_hint`` is appended to the message of a plain mismatch (not to validation
    errors).

    Decision (cfd-reviewer, controller ruling R11) and reasons:

    - Per axis, not per largest extent. For a CAD-exact box a tolerance tied to the largest extent
      is far too loose for a slender body: a thin feature (small radius, thin plate, one-cell-thick
      domain) can be wrong by many percent of its own size and still sit within ``rel_tol`` of the
      largest extent. Scaling by the extent of the axis in question keeps the check equally strict
      relative to the feature on every axis.
    - Representation floor ``REPR_EPS * abs(expected[i])``. The mesh box comes from ``checkMesh``
      output with 6 significant digits (worst-case relative rounding 5e-6); far from the origin
      that exceeds ``rel_tol`` times a small extent. An offset-to-extent ratio above about 100 is
      unresolvable at 6 digits, and the floor accepts it instead of failing a correct mesh.
    - A mesh of a curved boundary is an inscribed polygon, so its box is short of the CAD box by up
      to ``R * (1 - cos(pi / N))`` for ``N`` nodes around. That is a mesh effect, not an error;
      the mesh gate therefore uses a looser default ``rel_tol`` than the geometry gate.
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
    if all(abs(a - e) <= t for a, e, t in zip(actual, expected, tols, strict=True)):
        return None
    return (
        f"bounding box mismatch: expected {format_bbox(expected)}, got {format_bbox(actual)} "
        f"(tolerance per coordinate: {format_bbox(tols)})." + mismatch_hint
    )


def one_sided_wedge_hint(actual: Sequence[float], expected: Sequence[float], rel_tol: float) -> str:
    """A hint for an axisymmetric wedge that lies on one side of the x-y plane, else ``""``.

    The expected wedge is symmetric about z = 0 with half-thickness ``h = zmax`` and outer radius
    ``R = ymax``. An unrotated wedge reaches z = R sin(a) = 2 h sqrt(1 - (h / R)^2), a little less
    than the total extent ``E = 2 h`` (by a factor cos(a / 2), more than the gate tolerance above
    about 5 degrees). If the actual z range is, within the per-coordinate tolerance of the gate,
    [0, E] or [0, E_true] (or the same below z = 0), the solid has the right size and sits on one
    side: the usual cause is a rotation applied to the sketch before ``.revolve``, which does not
    move the revolved solid. The hint names the rotation that centres it.
    """
    if len(actual) != 6 or len(expected) != 6:
        return ""
    if not all(math.isfinite(v) for v in (*actual, *expected)):
        return ""
    extent = expected[5] - expected[2]
    if extent <= 0 or not (math.isfinite(rel_tol) and rel_tol > 0):
        return ""
    tol_low, tol_high = _axis_tolerances(expected, rel_tol)[2::3]
    if abs(expected[2] + expected[5]) > tol_low:
        return ""  # the expected box is not symmetric about z = 0: not a wedge
    half, radius = expected[5], expected[4]
    candidates = [extent]
    if 0 < half < radius:
        candidates.append(2 * half * math.sqrt(1 - (half / radius) ** 2))
    direction = None
    if abs(actual[2]) <= tol_low and any(abs(actual[5] - c) <= tol_high for c in candidates):
        direction = "minus"  # z from 0 up: rotate by minus half the angle
    elif abs(actual[5]) <= tol_high and any(abs(actual[2] + c) <= tol_low for c in candidates):
        direction = "plus"  # z from below up to 0
    if direction is None:
        return ""
    return (
        " Hint: the wedge lies on one side of the x-y plane: rotate the revolved solid "
        f"about the x axis by {direction} half the wedge angle after .revolve (rotating "
        "the sketch before revolving has no effect)."
    )
