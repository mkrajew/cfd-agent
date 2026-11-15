"""Calibration of ``cavity-re1000``: the hand-written steady solution passes the Scorer.

Run in the image (minutes): ``pytest -m calibration tests/calibration/test_cavity_re1000.py -s``.
The hand-written case is ``tests/fixtures/handwritten/cavity_re1000``.
"""

import pytest

pytestmark = pytest.mark.calibration


def test_cavity_re1000_handwritten_passes_scorer(calibrate):
    result = calibrate("cavity_re1000", "cavity-re1000")
    assert result.failure is None, result.failure
    assert result.passed, f"criteria failed: {result.criteria}"
    # The run ended because residualControl was met, not because the iteration cap was reached.
    assert result.converged_at is not None, "the solver log has no 'solution converged' line"
    assert result.ended_by_residual_control, (
        f"converged at {result.converged_at} but the cap is endTime {result.case_end_time}"
    )
    # The profiles were sampled at the end of the run that was just checked, not at a stale time.
    assert result.sample_times, "no function-object output found"
    for name, sampled in result.sample_times.items():
        assert sampled == pytest.approx(result.end_time), f"{name} sampled at {sampled}"
