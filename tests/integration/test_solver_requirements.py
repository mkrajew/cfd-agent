"""The completeness rules of ``check_case_consistency`` against the real solvers of the image.

For each solver a minimal complete case runs three steps; then one item at a time is removed. The
solver must refuse the case exactly when the check reports the item, and must run when the item is
one the check does not demand. This keeps the unit rules (``tests/unit/test_case_requirements.py``)
tied to what OpenFOAM does. ``blockMesh`` makes the mesh: allowed for test fixtures only.
"""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "unit"))
from solver_cases import REMOVALS, SOLVERS, complete_case  # noqa: E402

from cfdagent.tools.case_check import check_case_consistency  # noqa: E402

pytestmark = pytest.mark.integration

BLOCK_MESH = """FoamFile { version 2.0; format ascii; class dictionary; object blockMeshDict; }
convertToMeters 1;
vertices ((0 0 0) (1 0 0) (1 1 0) (0 1 0) (0 0 0.1) (1 0 0.1) (1 1 0.1) (0 1 0.1));
blocks (hex (0 1 2 3 4 5 6 7) (10 10 1) simpleGrading (1 1 1));
edges ();
boundary
(
    movingWall { type wall; faces ((3 7 6 2)); }
    fixedWalls { type wall; faces ((0 4 7 3) (2 6 5 1) (1 5 4 0)); }
    frontAndBack { type empty; faces ((0 3 2 1) (4 5 6 7)); }
);
"""


def build(root: Path, solver: str, rel=None, edit=None) -> bool:
    """Write the case (one item removed if ``rel`` is given) and its mesh; True if it changed."""
    files = complete_case(solver)
    changed = False
    if rel is not None and rel in files:
        if edit is None:
            del files[rel]
            changed = True
        else:
            edited = edit(files[rel])
            changed = edited != files[rel]
            files[rel] = edited
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    (root / "system" / "blockMeshDict").write_text(BLOCK_MESH, encoding="utf-8")
    done = subprocess.run(["blockMesh", "-case", str(root)], capture_output=True, text=True)
    assert done.returncode == 0, done.stdout[-500:]
    return changed


def solver_runs(root: Path, solver: str) -> bool:
    done = subprocess.run([solver, "-case", str(root)], capture_output=True, text=True)
    return done.returncode == 0


@pytest.mark.parametrize("solver", SOLVERS)
def test_the_complete_case_runs_and_passes_the_check(tmp_path, solver):
    build(tmp_path, solver)
    assert solver_runs(tmp_path, solver)
    result = check_case_consistency(tmp_path, solver)
    assert result.ok, result.errors


def cases():
    for label, rel, edit, solvers in REMOVALS:
        for solver in SOLVERS:
            yield pytest.param(label, rel, edit, solver, solver in solvers, id=f"{solver}-{label}")


@pytest.mark.parametrize(("label", "rel", "edit", "solver", "needed"), list(cases()))
def test_the_check_demands_what_the_solver_demands(tmp_path, label, rel, edit, solver, needed):
    changed = build(tmp_path, solver, rel, edit)
    runs = solver_runs(tmp_path, solver)
    reported = not check_case_consistency(tmp_path, solver).ok
    if needed:
        assert changed, "the removal changed nothing: the row proves nothing"
        assert not runs, f"{solver} ran without the item: the rule is too strict"
        assert reported, f"{solver} refuses the case but the check says nothing"
    else:
        assert runs, f"{solver} refused a case the check does not call incomplete"
        assert not reported, f"the check demands an item {solver} does not need"


@pytest.mark.parametrize("solver", SOLVERS)
def test_solvers_run_without_velocity_solvers_when_the_momentum_predictor_is_off(tmp_path, solver):
    row = next(r for r in REMOVALS if r[0].startswith("no momentum predictor"))
    assert build(tmp_path, solver, row[1], row[2]), "the edit changed nothing"
    assert solver_runs(tmp_path, solver)
    assert check_case_consistency(tmp_path, solver).ok
