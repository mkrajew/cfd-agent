"""Calibration of ``pipe-re500``: the hand-written axisymmetric solution passes the Scorer.

Run in the image (about 10 s per test):
``pytest -m calibration tests/calibration/test_pipe_re500.py -s``.
The hand-written case is ``tests/fixtures/handwritten/pipe_re500``: one wedge of the pipe, a steady
``simpleFoam`` solution; the pressure drop is reported in Pa (``p * rho``).
"""

import json

import pytest

from cfdagent.tools.foam_results import latest_time

pytestmark = pytest.mark.calibration


def test_pipe_re500_handwritten_passes_scorer(calibrate):
    result = calibrate("pipe_re500", "pipe-re500")
    assert result.failure is None, result.failure
    assert result.passed, f"criteria failed: {result.criteria}"
    # The run ended because residualControl was met, not because the iteration cap was reached.
    assert result.converged_at is not None, "the solver log has no 'solution converged' line"
    assert result.ended_by_residual_control, (
        f"converged at {result.converged_at} but the cap is endTime {result.case_end_time}"
    )
    # post.py read the fields of the run that was just checked, not a stale write.
    assert latest_time(result.workdir / "case") == pytest.approx(result.end_time)


def test_pipe_profile_reaches_axis_and_wall(calibrate):
    result = calibrate("pipe_re500", "pipe-re500")
    assert result.failure is None, result.failure
    quantities = json.loads((result.workdir / "quantities.json").read_text(encoding="utf-8"))
    profile = quantities["u_profile_fully_developed"]
    r_over_r = sorted(profile["x"])
    assert r_over_r[0] == pytest.approx(0.0, abs=1e-9), "the profile has no point on the axis"
    assert r_over_r[-1] == pytest.approx(1.0, abs=1e-9), "the profile has no point on the wall"
    values = dict(zip(profile["x"], profile["value"], strict=True))
    assert values[r_over_r[-1]] == pytest.approx(0.0, abs=1e-6), "no-slip: u = 0 on the wall"
    assert values[r_over_r[0]] == pytest.approx(2.0, abs=0.05), "centreline is about 2 U_mean"
