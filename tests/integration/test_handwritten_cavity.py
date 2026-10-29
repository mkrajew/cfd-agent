"""M0 acceptance: a hand-written lid-driven cavity (Re = 100) goes through the whole toolchain.

CadQuery geometry -> Gmsh mesh -> OpenFOAM (icoFoam) -> post-processing -> deterministic Scorer
against the benchmark reference. No LLM is involved; every input is a file under
``tests/fixtures/handwritten/cavity_re100``.
"""

import json
import os
import shutil
import time
from pathlib import Path

import pytest

import cfdagent
from cfdagent.bench.registry import load_registry
from cfdagent.bench.scoring import ScoreResult, score
from cfdagent.manifest import MANIFEST_FILE_NAME, BoundaryManifest
from cfdagent.tools.foam import parse_solver_log, run_solver
from cfdagent.tools.geometry import check_geometry, run_geometry_script
from cfdagent.tools.mesh import apply_patch_types, check_mesh, convert_to_foam, run_mesh_script
from cfdagent.tools.process import run_tool

pytestmark = pytest.mark.integration

ROOT = Path(__file__).parents[2]
CAVITY = ROOT / "tests" / "fixtures" / "handwritten" / "cavity_re100"
CAVITY_BBOX = (0, 0, 0, 1, 1, 0.1)
SOLVER = "icoFoam"


@pytest.fixture(autouse=True)
def _scripts_import_checked_out_cfdagent():
    """Scripts run in a subprocess; point it at the cfdagent under test, not the image copy."""
    src = str(Path(cfdagent.__file__).parents[1])
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PYTHONPATH", os.pathsep.join(filter(None, [src, os.environ.get("PYTHONPATH")])))
        yield


def _report(result: ScoreResult) -> str:
    return "\n".join(
        f"  {c.quantity}: passed={c.passed} error={c.error} threshold={c.threshold} ({c.reason})"
        for c in result.criteria
    )


def test_handwritten_cavity_re100_matches_ghia(tmp_path):
    # 1. geometry
    assert run_geometry_script(CAVITY / "geometry.py", tmp_path).ok
    geometry_gate = check_geometry(tmp_path, CAVITY_BBOX)
    assert geometry_gate.ok, geometry_gate.errors

    # 2. mesh
    mesh_result = run_mesh_script(CAVITY / "mesh.py", tmp_path)
    assert mesh_result.ok, mesh_result.tail
    case_dir = tmp_path / "case"
    converted = convert_to_foam(tmp_path / "mesh.msh", case_dir)
    assert converted.ok, converted.tail
    manifest = BoundaryManifest.from_file(tmp_path / MANIFEST_FILE_NAME)
    apply_patch_types(case_dir, manifest)
    mesh_gate = check_mesh(case_dir, manifest, CAVITY_BBOX)
    assert mesh_gate.ok, mesh_gate.errors

    # 3. solver (serial: 64 x 64 x 1 cells need no parallel run)
    shutil.copytree(CAVITY / "case", case_dir, dirs_exist_ok=True)
    started = time.monotonic()
    solved = run_solver(case_dir, SOLVER)
    print(f"\n{SOLVER} wall time: {time.monotonic() - started:.1f} s")
    assert solved.ok, solved.tail
    # Read the solver log by name: with n_procs > 1 the result would point at log.reconstructPar.
    summary = parse_solver_log((case_dir / f"log.{SOLVER}").read_text(encoding="utf-8"))
    assert summary.completed, summary.failure_reason
    print(f"end time {summary.end_time}, max Courant {summary.max_courant}")

    # 4. post-processing and scoring
    quantities_path = tmp_path / "quantities.json"
    post = run_tool(
        ["python", str(CAVITY / "post.py"), str(case_dir), str(quantities_path)],
        cwd=tmp_path,
        log_path=tmp_path / "post.log",
    )
    assert post.ok, post.tail
    quantities = json.loads(quantities_path.read_text(encoding="utf-8"))
    result = score(load_registry(ROOT / "benchmarks")["cavity-re100"], quantities)
    print("\nscore:\n" + _report(result))
    assert result.passed is True, "benchmark criteria failed:\n" + _report(result)
