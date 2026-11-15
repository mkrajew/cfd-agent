"""Calibration of the cylinder benchmarks: the hand-written solutions pass the Scorer.

Run in the image (minutes): ``pytest -m calibration tests/calibration/test_cylinder.py -s``.
The hand-written cases are ``tests/fixtures/handwritten/cylinder_re20`` (steady, ``simpleFoam``)
and ``cylinder_re100`` (transient, ``pimpleFoam``; 8 to 11 minutes on 4 ranks).
"""

import pytest

pytestmark = pytest.mark.calibration


def test_cylinder_re20_handwritten_passes_scorer(calibrate):
    result = calibrate("cylinder_re20", "cylinder-re20")
    assert result.failure is None, result.failure
    assert result.passed, f"criteria failed: {result.criteria}"
    # The run ended because residualControl was met, not because the iteration cap was reached.
    assert result.converged_at is not None, "the solver log has no 'solution converged' line"
    assert result.ended_by_residual_control, (
        f"converged at {result.converged_at} but the cap is endTime {result.case_end_time}"
    )


def test_cylinder_re100_handwritten_passes_scorer(calibrate):
    result = calibrate("cylinder_re100", "cylinder-re100", n_procs=4)
    assert result.failure is None, result.failure
    assert result.passed, f"criteria failed: {result.criteria}"
    # The run reached the intended end time (it was not cut short).
    assert result.end_time is not None and result.case_end_time is not None
    assert result.end_time >= result.case_end_time - 1e-6, (
        f"the run stopped at {result.end_time} but the endTime is {result.case_end_time}"
    )
