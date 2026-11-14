"""Calibration tests run inside the Docker image: hand-written solutions of the benchmarks.

They are long (minutes), not part of CI and not collected by default (``testpaths`` is
``tests/unit``). On the host (no ``WM_PROJECT_VERSION``) they are skipped instead of failing.
The run itself is ``scripts/run_calibration.py``, loaded here by path.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

import cfdagent

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts" / "run_calibration.py"
HANDWRITTEN = ROOT / "tests" / "fixtures" / "handwritten"


def _load_script():
    spec = importlib.util.spec_from_file_location("run_calibration", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_calibration"] = module
    spec.loader.exec_module(module)
    return module


_script = _load_script()
CalibrationResult = _script.CalibrationResult


def pytest_collection_modifyitems(config, items):
    if os.environ.get("WM_PROJECT_VERSION"):
        return
    skip = pytest.mark.skip(reason="OpenFOAM environment not found; run inside the Docker image")
    for item in items:
        if item.get_closest_marker("calibration"):
            item.add_marker(skip)


@pytest.fixture(scope="module", autouse=True)
def _scripts_import_checked_out_cfdagent():
    """Scripts run in subprocesses; point them at the cfdagent under test, not the image copy."""
    src = str(Path(cfdagent.__file__).parents[1])
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PYTHONPATH", os.pathsep.join(filter(None, [src, os.environ.get("PYTHONPATH")])))
        yield


@pytest.fixture
def calibrate(tmp_path):
    """``calibrate(fixture_name, benchmark_id) -> CalibrationResult``, work in pytest's tmp_path."""

    def run(fixture_name: str, benchmark_id: str, n_procs: int = 1) -> CalibrationResult:
        result = _script.calibrate(
            HANDWRITTEN / fixture_name,
            benchmark_id,
            n_procs=n_procs,
            workdir=tmp_path / "calibration",
            repo_root=ROOT,
        )
        print("\n" + _script.format_report(result))
        return result

    return run
