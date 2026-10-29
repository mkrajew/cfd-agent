"""Shared helpers of the integration tests that need a small OpenFOAM case (run in the image).

The cases are small structured 2D meshes made with ``blockMesh``; that is allowed for test
fixtures only (the pipeline itself never uses blockMesh).
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest


def hardware_threads() -> int:
    """CPUs this process may use; OpenMPI with --use-hwthread-cpus offers one slot per CPU."""
    affinity = getattr(os, "sched_getaffinity", None)
    return len(affinity(0)) if affinity else (os.cpu_count() or 1)


needs_4_threads = pytest.mark.skipif(
    hardware_threads() < 4,
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


def build_case(root: Path, end_time: float = 0.25) -> Path:
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
