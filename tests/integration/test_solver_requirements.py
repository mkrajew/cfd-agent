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


END_OF_SIMPLE = "    pRefValue 0;\n}\n"


def residual_lines(root: Path, *, must_run: bool = True) -> list[str]:
    """The linear-solver lines of a run of simpleFoam (they carry the residuals). A case without
    any relaxation may blow up in the first iteration: ``must_run=False`` compares what was
    printed before that."""
    done = subprocess.run(["simpleFoam", "-case", str(root)], capture_output=True, text=True)
    assert done.returncode == 0 or not must_run, done.stdout[-500:]
    return [line for line in done.stdout.splitlines() if "Initial residual" in line]


def test_relaxation_factors_inside_the_algorithm_dictionary_are_accepted_but_ignored(tmp_path):
    """OpenFOAM does not refuse the case and does not use the nested factors (the run is the same
    as without them), while the same factors at the top level change the run. The check reports
    the placement because the solver then runs with other relaxation than the case asked for."""
    top = "relaxationFactors\n{\n    fields { p 0.1; }\n    equations { U 0.1; }\n}\n"
    nested = (
        "    relaxationFactors\n    {\n        fields { p 0.1; }\n"
        "        equations { U 0.1; }\n    }\n"
    )

    def case(name, edit):
        root = tmp_path / name
        root.mkdir()
        build(root, "simpleFoam", "system/fvSolution", edit)
        return root

    def without_top_level(text):
        return text[: text.index("relaxationFactors")]

    def with_nested(text):
        return text.replace(END_OF_SIMPLE, END_OF_SIMPLE[:-2] + nested + "}\n", 1)

    base = case("base", lambda text: text)
    ignored = case("ignored", lambda text: with_nested(text))
    strong = case("strong", lambda text: without_top_level(text) + top)
    reference = residual_lines(base)
    assert reference and residual_lines(ignored) == reference  # the nested factors change nothing
    assert residual_lines(strong) != reference  # the same factors at the top level do
    assert check_case_consistency(base, "simpleFoam").ok
    misplaced = case("misplaced", lambda text: with_nested(without_top_level(text)))
    bare = case("bare", without_top_level)
    # ignored also where the check reports it: the run is the same as with no relaxation at all
    without = residual_lines(bare, must_run=False)
    assert without and residual_lines(misplaced, must_run=False) == without
    reported = check_case_consistency(misplaced, "simpleFoam").errors
    assert len(reported) == 1 and "relaxationFactors" in reported[0]
