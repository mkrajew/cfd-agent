"""Shared bounding-box helper used by the geometry gate, the mesh gate and the Gmsh helpers."""

import math

import pytest

from cfdagent.tools.bbox import REPR_EPS, compare_bbox, format_bbox, validate_expected_bbox

BOX = (0.0, 0.0, 0.0, 1.0, 1.0, 0.1)
NAN = math.nan
INF = math.inf


def test_format_bbox():
    assert format_bbox((0, 0, 0, 1.5, 2, 0.1)) == "(0, 0, 0, 1.5, 2, 0.1)"


def test_matching_box_passes():
    assert compare_bbox(BOX, BOX, 1e-3) is None


def test_tolerance_is_relative_to_largest_extent():
    assert compare_bbox((0, 0, 0, 1.0005, 1, 0.1), BOX, 1e-3) is None
    message = compare_bbox((0, 0, 0, 1.01, 1, 0.1), BOX, 1e-3)
    assert message is not None
    assert "bounding box mismatch" in message
    assert "tolerance" in message


def test_mismatch_message_carries_the_hint():
    message = compare_bbox((0, 0, 0, 2, 1, 0.1), BOX, 1e-3, mismatch_hint=" HINT")
    assert message is not None
    assert message.endswith(" HINT")


@pytest.mark.parametrize("bad", [NAN, INF, -INF])
def test_non_finite_actual_is_rejected(bad):
    message = compare_bbox((0, 0, 0, bad, 1, 0.1), BOX, 1e-3)
    assert message is not None
    assert "bounding box" in message
    assert "non-finite" in message


def test_all_nan_actual_is_rejected():
    assert compare_bbox((NAN,) * 6, BOX, 1e-3) is not None


@pytest.mark.parametrize("bad", [NAN, INF, -INF])
def test_non_finite_expected_is_rejected(bad):
    expected = (0, 0, 0, bad, 1, 0.1)
    assert "bounding box" in (validate_expected_bbox(expected, 1e-3) or "")
    assert compare_bbox(BOX, expected, 1e-3) is not None


@pytest.mark.parametrize("rel_tol", [0.0, -1e-3, NAN, INF])
def test_invalid_rel_tol_is_rejected(rel_tol):
    assert validate_expected_bbox(BOX, rel_tol) is not None
    assert compare_bbox(BOX, BOX, rel_tol) is not None


def test_degenerate_expected_box_is_rejected():
    # All extents zero gives a zero tolerance, so nothing could ever match.
    assert validate_expected_bbox((1, 1, 1, 1, 1, 1), 1e-3) is not None


def test_wrong_length_is_rejected():
    message = validate_expected_bbox((0, 0, 1, 1), 1e-3)
    assert message is not None
    assert "6 values" in message


def test_valid_expected_box_is_accepted():
    assert validate_expected_bbox(BOX, 1e-3) is None


def test_per_axis_tolerance_rejects_small_radius_error():
    # Slender box 0.01 x 0.01 x 1: a 10 % error in x is 1e-3 in absolute terms. The old rule
    # (rel_tol times the largest extent, here 1e-2 * 1 = 1e-2) accepted it; the per-axis rule
    # (rel_tol times the extent of that axis, here 1e-2 * 0.01 = 1e-4) must not.
    expected = (0.0, 0.0, 0.0, 0.01, 0.01, 1.0)
    actual = (0.0, 0.0, 0.0, 0.011, 0.01, 1.0)
    old_tol = 1e-2 * 1.0
    assert abs(actual[3] - expected[3]) <= old_tol  # premise: the old rule would pass this
    message = compare_bbox(actual, expected, 1e-2)
    assert message is not None
    assert "bounding box mismatch" in message


def test_per_axis_tolerance_accepts_small_error_on_every_axis():
    expected = (0.0, 0.0, 0.0, 0.01, 0.01, 1.0)
    actual = (0.0, 0.0, 0.0, 0.01 * (1 + 5e-3), 0.01, 1.0 * (1 + 5e-3))
    assert compare_bbox(actual, expected, 1e-2) is None


def test_zero_extent_axis_uses_largest_extent():
    # A flat expected box has extent 0 in z; that axis falls back to rel_tol * largest extent.
    flat = (0.0, 0.0, 0.0, 1.0, 1.0, 0.0)
    assert compare_bbox((0, 0, 0.0005, 1, 1, 0.0005), flat, 1e-3) is None
    assert compare_bbox((0, 0, 0.01, 1, 1, 0.01), flat, 1e-3) is not None


def test_inverted_expected_box_is_rejected():
    message = validate_expected_bbox((0, 0, 0, -1, 1, 1), 1e-3)
    assert message is not None
    assert "bounding box" in message


def _polygon_bbox(radius, n_sides, length):
    """Bounding box of a prism along x whose cross-section is a regular n-gon, vertex on +y."""
    ys = [radius * math.cos(2 * math.pi * k / n_sides) for k in range(n_sides)]
    zs = [radius * math.sin(2 * math.pi * k / n_sides) for k in range(n_sides)]
    return (0.0, min(ys), min(zs), length, max(ys), max(zs))


def test_inscribed_polygon_bbox_passes_at_mesh_tolerance_only():
    # A mesh of a curved boundary is an inscribed polygon, so its box is slightly short of the CAD
    # box: a 30-gon of radius 5 mm has no vertex on the z axis and is R * (1 - sin(84 degrees)),
    # about 2.7e-5 m, short in z.
    expected = (0.0, -5e-3, -5e-3, 0.5, 5e-3, 5e-3)
    actual = _polygon_bbox(5e-3, 30, 0.5)
    assert expected[5] - actual[5] > 2e-5  # premise: a real shortfall
    assert compare_bbox(actual, expected, 1e-2) is None  # mesh tolerance
    assert compare_bbox(actual, expected, 1e-3) is not None  # CAD tolerance


def test_six_digit_rounding_of_an_offset_box_passes_thanks_to_repr_eps():
    # polyMesh points and checkMesh print 6 significant digits; far from the origin that is a
    # larger absolute error than rel_tol times the (small) extent.
    expected = (100.0123456, 0.0, 0.0, 100.0223456, 0.01, 0.01)
    actual = (100.012, 0.0, 0.0, 100.022, 0.01, 0.01)
    assert abs(actual[0] - expected[0]) > 1e-3 * 0.01  # the per-axis rule alone would reject it
    assert compare_bbox(actual, expected, 1e-3) is None


def test_repr_eps_floor_does_not_hide_a_real_error_far_from_the_origin():
    expected = (100.0, 0.0, 0.0, 100.01, 0.01, 0.01)
    actual = (100.1, 0.0, 0.0, 100.11, 0.01, 0.01)  # shifted by 1e-3 relative to the offset
    assert abs(actual[0] - expected[0]) > REPR_EPS * abs(expected[0])
    assert compare_bbox(actual, expected, 1e-3) is not None
