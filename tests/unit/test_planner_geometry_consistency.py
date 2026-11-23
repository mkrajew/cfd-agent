"""Plan gate: the volume, the wedge y range and the wedge z range must agree with the bbox."""

import math

import pytest
from test_casespec import SPEC, make_spec

from cfdagent.agents.planner import RESOURCE_PREFIX, _only_resource_errors, plan_gate
from cfdagent.config import RunLimits

LIMITS = RunLimits()
TASK = "Simulate the flow. Report `centreline_u` and `drag`."

ANGLE = 5.0
RADIUS = 0.005
HALF_Z = RADIUS * math.sin(math.radians(ANGLE / 2))
WEDGE_VOLUME = 5.45e-07


def wedge_bbox(**changes) -> dict:
    return {
        "xmin": 0.0,
        "xmax": 0.5,
        "ymin": 0.0,
        "ymax": RADIUS,
        "zmin": -HALF_Z,
        "zmax": HALF_Z,
        **changes,
    }


def axisymmetric(angle=ANGLE, volume=WEDGE_VOLUME, **bbox_changes):
    geometry = {
        **SPEC["geometry"],
        "bbox": wedge_bbox(**bbox_changes),
        "volume_m3": volume,
        "thickness_m": None,
        "wedge_angle_deg": angle,
    }
    return make_spec(dimensionality="axisymmetric", geometry=geometry)


def planar(volume=0.4, **bbox_changes):
    bbox = {**SPEC["geometry"]["bbox"], **bbox_changes}
    return make_spec(geometry={**SPEC["geometry"], "bbox": bbox, "volume_m3": volume})


def errors_of(spec) -> list[str]:
    return plan_gate(spec, TASK, LIMITS).errors


def test_a_valid_2d_plan_passes():
    assert errors_of(planar()) == []


def test_a_valid_axisymmetric_plan_passes():
    assert errors_of(axisymmetric()) == []


def test_a_volume_equal_to_the_bbox_volume_passes():
    assert errors_of(planar(volume=0.4)) == []
    assert errors_of(planar(volume=0.4 * (1 + 5e-4))) == []


def test_a_volume_that_is_unknown_is_not_compared():
    assert errors_of(planar(volume=None)) == []


def test_a_volume_above_the_bbox_volume_fails_in_2d():
    errors = errors_of(planar(volume=0.5))
    assert len(errors) == 1
    message = errors[0]
    assert "volume_m3" in message and "bounding box" in message
    assert "(0.5)" in message and "(0.4)" in message
    assert "in-plane fluid area times geometry.thickness_m" in message
    assert "wedge_angle_deg/360" not in message
    assert "Check geometry.bbox and geometry.volume_m3" in message


def test_a_volume_one_percent_above_the_bbox_volume_fails():
    assert len(errors_of(planar(volume=0.4 * 1.01))) == 1


def test_the_full_solid_of_revolution_volume_fails_for_a_wedge():
    errors = errors_of(axisymmetric(volume=3.93e-05))
    assert len(errors) == 1
    assert "(3.93e-05)" in errors[0] and "bounding box" in errors[0]
    assert "wedge_angle_deg/360" in errors[0] and "subtracted" in errors[0]
    assert "in-plane" not in errors[0]
    assert "Check geometry.bbox and geometry.volume_m3" in errors[0]


def test_the_volume_check_is_skipped_for_an_invalid_bbox():
    # the bbox errors report the problem; no comparison with a meaningless box volume
    errors = errors_of(planar(volume=0.5, xmax=-4.0))
    assert len(errors) == 1 and "xmax" in errors[0]


def test_a_negative_wedge_ymin_fails():
    errors = errors_of(axisymmetric(ymin=-RADIUS))
    assert len(errors) == 1
    message = errors[0]
    assert "ymin" in message and "cos(wedge_angle_deg/2)" in message
    assert "symmetric" in message
    assert message.endswith(
        "Set ymin to r*cos(wedge_angle_deg/2), which is 0 when the domain reaches the axis."
    )


def test_a_wedge_ymin_that_is_zero_up_to_rounding_passes():
    assert errors_of(axisymmetric(ymin=-1e-9 * RADIUS)) == []
    big = {"ymax": 0.5, "zmin": -0.5 * math.sin(math.radians(ANGLE / 2))}
    big["zmax"] = -big["zmin"]
    assert errors_of(axisymmetric(ymin=-1e-9, volume=None, **big)) == []


def test_a_wedge_ymin_that_does_not_reach_the_axis_but_is_positive_passes():
    inner = 0.001
    cos_half = math.cos(math.radians(ANGLE / 2))
    ymin = inner * cos_half
    # annular wedge: a/360 of the ring between r and R over the length of 0.5
    volume = ANGLE / 360 * math.pi * (RADIUS**2 - inner**2) * 0.5
    assert 5.0e-07 < volume < 5.5e-07
    spec = axisymmetric(ymin=ymin, volume=volume)
    assert errors_of(spec) == []


def test_a_wedge_z_range_from_the_wrong_radius_fails():
    # z computed from a radius that is not the largest radius of the domain (ymax)
    bad = axisymmetric(ymax=0.0045, volume=4e-07)
    errors = errors_of(bad)
    assert len(errors) == 1
    message = errors[0]
    assert "sin(wedge_angle_deg/2)" in message
    expected = 0.0045 * math.sin(math.radians(ANGLE / 2))
    assert f"zmin = {-expected:.6g}" in message and f"zmax = {expected:.6g}" in message


def test_a_wedge_with_zmax_off_by_two_percent_fails_with_the_expected_numbers():
    errors = errors_of(axisymmetric(zmin=-HALF_Z, zmax=HALF_Z * 1.02))
    assert len(errors) == 1
    assert f"zmin = {-HALF_Z:.6g}" in errors[0] and f"zmax = {HALF_Z:.6g}" in errors[0]


def test_a_wedge_with_zmax_off_by_half_a_percent_fails():
    assert len(errors_of(axisymmetric(zmax=HALF_Z * 1.005))) == 1


def test_a_wedge_z_range_that_is_not_symmetric_fails():
    errors = errors_of(axisymmetric(zmin=0.0))
    assert len(errors) == 1
    assert f"zmin = {-HALF_Z:.6g}" in errors[0] and f"zmax = {HALF_Z:.6g}" in errors[0]


def test_a_wedge_z_range_within_a_tenth_of_a_percent_passes():
    spec = axisymmetric(zmin=-HALF_Z * 1.001, zmax=HALF_Z * 0.999)
    assert errors_of(spec) == []


@pytest.mark.parametrize("angle", [None, 0.0, -5.0, 10.5, math.nan, math.inf])
def test_the_z_check_is_skipped_for_an_invalid_wedge_angle(angle):
    errors = errors_of(axisymmetric(angle=angle))
    assert len(errors) == 1 and "wedge_angle_deg" in errors[0]


def test_the_wedge_checks_do_not_apply_to_other_dimensionalities():
    spec = planar(ymin=-0.5, ymax=0.5)  # a 2D plan may have a negative y
    assert errors_of(spec) == []


def test_the_new_errors_are_ordinary_errors_that_are_retried():
    bad_volume = errors_of(planar(volume=0.5))
    bad_y = errors_of(axisymmetric(ymin=-RADIUS))
    bad_z = errors_of(axisymmetric(zmax=HALF_Z * 2))
    for errors in (bad_volume, bad_y, bad_z):
        assert errors and not any(e.startswith(RESOURCE_PREFIX) for e in errors)
        assert not _only_resource_errors(errors)
