"""Shared bounding-box helper used by the geometry gate, the mesh gate and the Gmsh helpers."""

import math

import pytest

from cfdagent.tools.bbox import compare_bbox, format_bbox, validate_expected_bbox

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
