"""Solver runner against the real OpenFOAM solvers (run inside the Docker image).

The cases here are small structured 2D meshes made with ``blockMesh``; that is allowed for test
fixtures only (the pipeline itself never uses blockMesh).
"""

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from cfdagent.errors import AgentInputError
from cfdagent.tools.foam import SolverRun, parse_solver_log, run_solver, trial_run
from cfdagent.tools.process import run_tool

pytestmark = pytest.mark.integration


def _hardware_threads() -> int:
    """CPUs this process may use; OpenMPI with --use-hwthread-cpus offers one slot per CPU."""
    affinity = getattr(os, "sched_getaffinity", None)
    return len(affinity(0)) if affinity else (os.cpu_count() or 1)


needs_4_threads = pytest.mark.skipif(
    _hardware_threads() < 4,
    reason="mpirun --use-hwthread-cpus -np 4 needs 4 hardware threads (the project VM has 4)",
)

FIXTURE_CASE = Path(__file__).parents[1] / "fixtures" / "handwritten" / "cavity_re100" / "case"

BLOCK_MESH_DICT = """FoamFile
{
    version 2.0;
    format ascii;
    class dictionary;
    object blockMeshDict;
}

convertToMeters 1;

vertices
(
    (0 0 0) (1 0 0) (1 1 0) (0 1 0)
    (0 0 0.1) (1 0 0.1) (1 1 0.1) (0 1 0.1)
);

blocks ( hex (0 1 2 3 4 5 6 7) (20 20 1) simpleGrading (1 1 1) );

edges ();

boundary
(
    movingWall { type wall; faces ((3 7 6 2)); }
    fixedWalls { type wall; faces ((0 4 7 3) (2 6 5 1) (1 5 4 0)); }
    frontAndBack { type empty; faces ((0 3 2 1) (4 5 6 7)); }
);
"""

CONTROL_DICT = """FoamFile
{{
    version 2.0;
    format ascii;
    class dictionary;
    object controlDict;
}}

application icoFoam;
startFrom startTime;
startTime 0;
stopAt endTime;
endTime {end_time};
deltaT 0.005;
writeControl timeStep;
writeInterval 50;
writeFormat ascii;
writePrecision 8;
timeFormat general;
timePrecision 6;
runTimeModifiable false;
"""


def _build_case(root: Path, end_time: float = 0.25) -> Path:
    """A 20 x 20 x 1 cell case with the fixture's fields and schemes, mesh from blockMesh."""
    case = root / "case"
    for sub in ("0", "constant", "system"):
        shutil.copytree(FIXTURE_CASE / sub, case / sub)
    (case / "system" / "blockMeshDict").write_text(BLOCK_MESH_DICT, encoding="utf-8")
    (case / "system" / "controlDict").write_text(
        CONTROL_DICT.format(end_time=end_time), encoding="utf-8"
    )
    made = subprocess.run(["blockMesh"], cwd=case, capture_output=True, text=True)
    assert made.returncode == 0, made.stdout + made.stderr
    return case


def _snapshot(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        if p.is_file()
        else "dir"
        for p in sorted(root.rglob("*"))
    }


def _velocity(case: Path, time: str) -> list[tuple[float, float, float]]:
    text = (case / time / "U").read_text(encoding="utf-8")
    internal = text.split("internalField", 1)[1].split("boundaryField", 1)[0]
    number = r"[-+0-9.eE]+"
    return [
        (float(x), float(y), float(z))
        for x, y, z in re.findall(rf"\(({number}) ({number}) ({number})\)", internal)
    ]


def test_run_solver_real_run_without_a_mesh_fails_closed(tmp_path):
    """An empty case directory makes the real solver stop with an error, which must be ok=False."""
    assert shutil.which("icoFoam"), "icoFoam is not on PATH; this test needs the OpenFOAM image"
    result = run_solver(tmp_path, "icoFoam", timeout_s=60)
    assert isinstance(result, SolverRun)
    assert not result.ok
    assert not result.tool.timed_out
    # The solver really started and stopped with a non-zero exit code (not "executable not found").
    assert result.tool.returncode not in (None, 0), result.tool.tail
    log_text = result.solver_log.read_text(encoding="utf-8", errors="replace")
    assert "OpenFOAM" in log_text
    summary = parse_solver_log(log_text)
    assert not summary.completed
    assert summary.fatal_error is not None
    assert result.summary == summary
    assert result.case_end_time is None  # there is no controlDict


def test_missing_controldict_does_not_complete(tmp_path):
    """Regression: a case without controlDict must never pass (no skeleton file with deltaT 1)."""
    case = _build_case(tmp_path)
    (case / "system" / "controlDict").unlink()
    result = run_solver(case, "icoFoam", timeout_s=120)
    assert not result.ok
    assert not result.summary.completed
    assert result.case_end_time is None
    assert "controlDict" in result.solver_log.read_text(encoding="utf-8", errors="replace")
    assert not (case / "system" / "controlDict").exists()


def test_serial_run_is_ok_and_ends_at_the_case_end_time(tmp_path):
    case = _build_case(tmp_path, end_time=0.25)
    result = run_solver(case, "icoFoam", timeout_s=300)
    assert result.ok, result.tool.tail
    assert result.case_end_time == pytest.approx(0.25)
    assert result.summary.end_time == pytest.approx(0.25)
    assert (case / "0.25" / "U").is_file()


@needs_4_threads
def test_mpirun_np4_hostname(tmp_path):
    """Four ranks start with ``--use-hwthread-cpus`` (the sandbox's rank limit is 4)."""
    result = run_tool(
        ["mpirun", "--use-hwthread-cpus", "-np", "4", "hostname"],
        cwd=tmp_path,
        log_path=tmp_path / "log.hostname",
        timeout_s=120,
    )
    assert result.ok, result.tail
    lines = [line for line in result.log_path.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 4, lines


@pytest.mark.parametrize("n_procs", [2, pytest.param(4, marks=needs_4_threads)])
def test_parallel_cavity_matches_serial(tmp_path, n_procs):
    serial_case = _build_case(tmp_path / "serial")
    parallel_case = _build_case(tmp_path / "parallel")

    serial = run_solver(serial_case, "icoFoam", n_procs=1, timeout_s=300)
    parallel = run_solver(parallel_case, "icoFoam", n_procs=n_procs, timeout_s=300)

    assert serial.ok, serial.tool.tail
    assert parallel.ok, parallel.tool.tail
    assert parallel.summary.end_time == pytest.approx(serial.summary.end_time)
    assert parallel.case_end_time == pytest.approx(0.25)
    assert parallel.solver_log.name == "log.icoFoam"
    assert parallel.tool.log_path.name == "log.reconstructPar"
    u_serial = _velocity(serial_case, "0.25")
    u_parallel = _velocity(parallel_case, "0.25")
    assert len(u_serial) == len(u_parallel) == 400
    worst = max(
        abs(a - b)
        for va, vb in zip(u_serial, u_parallel, strict=True)
        for a, b in zip(va, vb, strict=True)
    )
    assert worst < 1e-4, f"max |U_serial - U_parallel| = {worst}"


def test_trial_run_does_not_touch_case_dir(tmp_path):
    case = _build_case(tmp_path, end_time=40)
    # Leftovers a trial run must neither copy nor change.
    (case / "postProcessing").mkdir()
    (case / "postProcessing" / "old.dat").write_text("old\n", encoding="utf-8")
    (case / "5").mkdir()
    (case / "5" / "U").write_text("stale\n", encoding="utf-8")
    before = _snapshot(case)

    result = trial_run(case, "icoFoam", steps=3, timeout_s=120)

    assert result.ok, result.tool.tail
    assert result.case_end_time == pytest.approx(0.015)
    assert result.summary.end_time == pytest.approx(0.015)
    assert _snapshot(case) == before
    assert not list(case.glob("log.*"))


def test_trial_run_reports_fatal_error(tmp_path):
    case = _build_case(tmp_path)
    u_file = case / "0" / "U"
    u_file.write_text(
        u_file.read_text(encoding="utf-8").replace("noSlip", "noSlipp"), encoding="utf-8"
    )
    before = _snapshot(case)
    result = trial_run(case, "icoFoam", steps=3, timeout_s=120)
    assert not result.ok
    assert result.summary.fatal_error is not None
    assert "noSlipp" in result.summary.fatal_error
    assert "noSlipp" in result.tool.tail
    assert _snapshot(case) == before


def test_trial_run_without_control_dict_is_the_agents_fault(tmp_path):
    case = _build_case(tmp_path)
    (case / "system" / "controlDict").unlink()
    with pytest.raises(AgentInputError, match="controlDict"):
        trial_run(case, "icoFoam")
