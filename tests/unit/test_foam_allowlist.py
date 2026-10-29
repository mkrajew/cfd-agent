"""The set of solvers and the process allowlist must never drift apart."""

import pytest

from cfdagent.tools.foam import NON_SOLVERS, SOLVERS, run_solver
from cfdagent.tools.process import ALLOWED_EXECUTABLES, ToolNotAllowed


def test_allowlist_is_solvers_plus_non_solvers():
    # Adding an executable to the allowlist forces a decision: is it a solver or not?
    assert ALLOWED_EXECUTABLES == SOLVERS | NON_SOLVERS


def test_solvers_and_non_solvers_do_not_overlap():
    assert not SOLVERS & NON_SOLVERS


def test_solver_set_is_the_explicit_list():
    assert SOLVERS == frozenset({"icoFoam", "simpleFoam", "pimpleFoam"})


@pytest.mark.parametrize(
    "name", ["bash", "icoFoam2", "potentialFoam", "/usr/bin/icoFoam", "", "foamDictionary"]
)
def test_unknown_executable_is_not_a_solver(name, tmp_path):
    assert name not in SOLVERS
    with pytest.raises(ToolNotAllowed):
        run_solver(tmp_path, name)
    assert list(tmp_path.iterdir()) == []  # nothing ran, nothing written
