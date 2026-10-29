"""Solver runner against the real OpenFOAM solvers (run inside the Docker image)."""

import shutil

import pytest

from cfdagent.tools.foam import parse_solver_log, run_solver

pytestmark = pytest.mark.integration


def test_run_solver_real_run_without_a_mesh_fails_closed(tmp_path):
    """An empty case directory makes the real solver stop with an error, which must be ok=False."""
    assert shutil.which("icoFoam"), "icoFoam is not on PATH; this test needs the OpenFOAM image"
    result = run_solver(tmp_path, "icoFoam", timeout_s=60)
    assert not result.ok
    assert not result.timed_out
    # The solver really started and stopped with a non-zero exit code (not "executable not found").
    assert result.returncode not in (None, 0), result.tail
    log_text = result.log_path.read_text(encoding="utf-8", errors="replace")
    assert "OpenFOAM" in log_text
    summary = parse_solver_log(log_text)
    assert not summary.completed
    assert summary.fatal_error is not None
