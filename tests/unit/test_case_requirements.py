"""The completeness part of ``check_case_consistency``: the files and entries a solver reads.

The rules were taken from running the solvers of the image with one item removed at a time
(``tests/integration/test_solver_requirements.py`` repeats that run, so the unit rules cannot drift
from OpenFOAM). The unit tests here need no OpenFOAM.
"""

from __future__ import annotations

import pytest
from solver_cases import BOUNDARY, REMOVALS, SOLVERS, complete_case

from cfdagent.tools.case_check import check_case_consistency

# What the message must name for each removal (a substring).
KEYWORD = {
    "constant/transportProperties missing": "constant/transportProperties",
    "nu missing": "`nu`",
    "transportModel missing": "transportModel",
    "constant/turbulenceProperties missing": "constant/turbulenceProperties",
    "simulationType missing": "simulationType",
    "system/fvSchemes missing": "system/fvSchemes",
    "system/fvSolution missing": "system/fvSolution",
    "ddtSchemes missing": "ddtSchemes",
    "gradSchemes missing": "gradSchemes",
    "divSchemes missing": "divSchemes",
    "laplacianSchemes missing": "laplacianSchemes",
    "div(phi,U) missing": "div(phi,U)",
    "viscous div missing": "nuEff",
    "solvers missing": "`solvers`",
    "solver p missing": "entry for: p",
    "solver pFinal missing": "pFinal",
    "solver U missing": "entry for: U",
    "solver UFinal missing": "UFinal",
    "pRefCell missing": "pRefCell",
    "pRefValue missing": "pRefValue",
}


def write_case(root, solver, mutate=None):
    """A complete case of ``solver`` in ``root``; ``mutate`` may change the files (dict)."""
    files = complete_case(solver)
    if mutate is not None:
        mutate(files)
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    boundary = root / "constant" / "polyMesh" / "boundary"
    boundary.parent.mkdir(parents=True, exist_ok=True)
    boundary.write_text(BOUNDARY, encoding="utf-8")
    return root


def errors_of(root, solver):
    result = check_case_consistency(root, solver)
    assert result.ok == (result.errors == [])
    return result.errors


@pytest.mark.parametrize("solver", SOLVERS)
def test_a_complete_case_has_no_problem(tmp_path, solver):
    assert errors_of(write_case(tmp_path, solver), solver) == []


def removal_params():
    for label, rel, edit, solvers in REMOVALS:
        for solver in SOLVERS:
            if solver in solvers:
                yield pytest.param(label, rel, edit, solver, id=f"{solver}-{label}")


@pytest.mark.parametrize(("label", "rel", "edit", "solver"), list(removal_params()))
def test_each_missing_item_is_reported_by_name(tmp_path, label, rel, edit, solver):
    def mutate(files):
        if edit is None:
            del files[rel]
        else:
            files[rel] = edit(files[rel])

    errors = errors_of(write_case(tmp_path, solver, mutate), solver)
    assert any(KEYWORD[label] in e for e in errors), (label, errors)
    assert len(errors) == 1, errors  # one cause, one message


def test_items_a_solver_does_not_read_are_not_demanded(tmp_path):
    # simpleFoam is steady: no ddtSchemes; icoFoam has no turbulence model and no viscous divergence
    def steady(files):
        files["system/fvSchemes"] = files["system/fvSchemes"].replace(
            "ddtSchemes\n{\n    default steadyState;\n}\n", ""
        )

    assert errors_of(write_case(tmp_path / "a", "simpleFoam", steady), "simpleFoam") == []
    ico = write_case(tmp_path / "b", "icoFoam")
    assert not (ico / "constant" / "turbulenceProperties").exists()
    assert errors_of(ico, "icoFoam") == []


def test_all_missing_items_are_reported_together(tmp_path):
    def mutate(files):
        del files["constant/turbulenceProperties"]
        transport = files["constant/transportProperties"]
        files["constant/transportProperties"] = transport.replace("transportModel Newtonian;\n", "")
        schemes = files["system/fvSchemes"]
        divergence = "    div(phi,U) bounded Gauss linearUpwind grad(U);\n"
        files["system/fvSchemes"] = schemes.replace(divergence, "")
        solution = files["system/fvSolution"]
        files["system/fvSolution"] = solution.replace("    pRefCell 0;\n", "")

    errors = errors_of(write_case(tmp_path, "simpleFoam", mutate), "simpleFoam")
    joined = "\n".join(errors)
    for word in ("turbulenceProperties", "transportModel", "div(phi,U)", "pRefCell"):
        assert word in joined, (word, errors)


# ------------------------------------------------------------------ what is not reported


def test_a_default_scheme_stands_in_for_a_missing_divergence_entry(tmp_path):
    def mutate(files):
        files["system/fvSchemes"] = (
            files["system/fvSchemes"]
            .replace("    default none;\n", "    default Gauss linear;\n")
            .replace("    div(phi,U) Gauss linear;\n", "")
        )

    assert errors_of(write_case(tmp_path, "icoFoam", mutate), "icoFoam") == []


def test_a_commented_out_divergence_entry_does_not_count(tmp_path):
    def mutate(files):
        files["system/fvSchemes"] = files["system/fvSchemes"].replace(
            "    div(phi,U) Gauss linear;\n", "    // div(phi,U) Gauss linear;\n"
        )

    errors = errors_of(write_case(tmp_path, "icoFoam", mutate), "icoFoam")
    assert any("div(phi,U)" in e for e in errors)


def test_a_regular_expression_key_covers_the_final_solvers(tmp_path):
    def mutate(files):
        text = files["system/fvSolution"]
        start, end = text.index("    pFinal"), text.index("    U\n")
        text = text[:start] + text[end:]
        files["system/fvSolution"] = text.replace("    p\n", '    "(p|pFinal)"\n', 1)

    assert errors_of(write_case(tmp_path, "icoFoam", mutate), "icoFoam") == []


def test_an_include_may_supply_what_is_missing(tmp_path):
    def mutate(files):
        files["system/fvSolution"] = files["system/fvSolution"].replace(
            "solvers\n{", '#include "solverSettings"\nsolvers\n{', 1
        )
        files["system/fvSolution"] = files["system/fvSolution"].replace("    pRefCell 0;\n", "")

    assert errors_of(write_case(tmp_path, "icoFoam", mutate), "icoFoam") == []


def test_a_pressure_level_fixed_by_a_boundary_condition_needs_no_reference_cell(tmp_path):
    def mutate(files):
        files["0/p"] = files["0/p"].replace(
            "fixedWalls { type zeroGradient; }", "fixedWalls { type fixedValue; value uniform 0; }"
        )
        files["system/fvSolution"] = files["system/fvSolution"].replace(
            "    pRefCell 0;\n    pRefValue 0;\n", ""
        )

    assert errors_of(write_case(tmp_path, "icoFoam", mutate), "icoFoam") == []


def test_a_reference_point_replaces_the_reference_cell(tmp_path):
    def mutate(files):
        files["system/fvSolution"] = files["system/fvSolution"].replace(
            "    pRefCell 0;\n", "    pRefPoint (0.5 0.5 0.05);\n"
        )

    assert errors_of(write_case(tmp_path, "icoFoam", mutate), "icoFoam") == []


def test_a_transport_model_other_than_newtonian_needs_no_plain_nu(tmp_path):
    def mutate(files):
        files["constant/transportProperties"] = files["constant/transportProperties"].replace(
            "transportModel Newtonian;\nnu [0 2 -1 0 0 0 0] 0.01;\n",
            "transportModel CrossPowerLaw;\nCrossPowerLawCoeffs { nu0 1; nuInf 1; m 1; n 1; }\n",
        )

    assert errors_of(write_case(tmp_path, "simpleFoam", mutate), "simpleFoam") == []


def test_a_solver_outside_the_known_three_gets_no_completeness_demands(tmp_path):
    root = write_case(tmp_path, "icoFoam")
    (root / "system" / "fvSchemes").unlink()
    errors = errors_of(root, "rhoPimpleFoam")
    assert not any("fvSchemes" in e for e in errors)  # only the `application` mismatch remains


# ------------------------------------------------------------------ the shape of a uniform value


def test_a_scalar_where_a_vector_is_needed_is_reported(tmp_path):
    def mutate(files):
        files["0/U"] = files["0/U"].replace("value uniform (1 0 0)", "value uniform 1")

    errors = errors_of(write_case(tmp_path, "icoFoam", mutate), "icoFoam")
    assert len(errors) == 1 and "0/U" in errors[0] and "vector" in errors[0]


def test_a_vector_internal_field_given_as_a_number_is_reported(tmp_path):
    def mutate(files):
        files["0/U"] = files["0/U"].replace(
            "internalField uniform (0 0 0)", "internalField uniform 0"
        )

    errors = errors_of(write_case(tmp_path, "icoFoam", mutate), "icoFoam")
    assert len(errors) == 1 and "0/U" in errors[0] and "internalField" in errors[0]


def test_a_vector_where_a_scalar_is_needed_is_reported(tmp_path):
    def mutate(files):
        files["0/p"] = files["0/p"].replace(
            "internalField uniform 0", "internalField uniform (0 0 0)"
        )

    errors = errors_of(write_case(tmp_path, "icoFoam", mutate), "icoFoam")
    assert len(errors) == 1 and "0/p" in errors[0] and "scalar" in errors[0]


def test_a_macro_in_place_of_a_value_is_not_judged(tmp_path):
    def mutate(files):
        files["0/U"] = files["0/U"].replace("value uniform (1 0 0)", "value $internalField")
        files["0/p"] = files["0/p"].replace(
            "internalField uniform 0", "internalField uniform $pInit"
        )

    assert errors_of(write_case(tmp_path, "icoFoam", mutate), "icoFoam") == []
