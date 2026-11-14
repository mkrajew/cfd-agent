"""The completeness part of ``check_case_consistency``: the files and entries a solver reads.

The rules were taken from running the solvers of the image with one item removed at a time
(``tests/integration/test_solver_requirements.py`` repeats that run, so the unit rules cannot drift
from OpenFOAM). The unit tests here need no OpenFOAM.
"""

from __future__ import annotations

import re

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


# ------------------------------------------------------------------ relaxationFactors placement

NESTED = (
    "    relaxationFactors\n    {\n        fields { p 0.3; }\n        equations { U 0.7; }\n    }\n"
)
ALGORITHM = {"icoFoam": "PISO", "simpleFoam": "SIMPLE", "pimpleFoam": "PIMPLE"}


def with_nested_relaxation(*, keep_top_level=False, extra=""):
    """Put ``relaxationFactors`` inside the algorithm dictionary (and drop the top-level one)."""

    def mutate(files):
        text = files["system/fvSolution"]
        if not keep_top_level and "\nrelaxationFactors" in text:
            text = text[: text.index("\nrelaxationFactors") + 1]
        text = text.replace("    pRefValue 0;\n}\n", f"    pRefValue 0;\n{NESTED}{extra}}}\n", 1)
        assert NESTED in text
        files["system/fvSolution"] = text

    return mutate


@pytest.mark.parametrize("solver", SOLVERS)
def test_relaxation_factors_inside_the_algorithm_dictionary_are_reported(tmp_path, solver):
    root = write_case(tmp_path, solver, with_nested_relaxation())
    (message,) = errors_of(root, solver)
    assert "system/fvSolution" in message and f"`{ALGORITHM[solver]}`" in message
    assert "relaxationFactors" in message and "top-level" in message
    assert "equations" in message and "ignores" in message  # the shape and why it matters
    assert '"relaxationFactors { fields { p <factor>; } equations { U <factor>; } }"' in message
    assert "0.3" not in message and "0.7" not in message  # no values: placeholders only


def test_nested_relaxation_factors_are_harmless_when_a_top_level_dictionary_exists(tmp_path):
    mutate = with_nested_relaxation(keep_top_level=True)
    assert errors_of(write_case(tmp_path, "simpleFoam", mutate), "simpleFoam") == []


def test_a_directive_in_the_algorithm_dictionary_silences_the_relaxation_check(tmp_path):
    mutate = with_nested_relaxation(extra='    #include "more"\n')
    assert errors_of(write_case(tmp_path, "simpleFoam", mutate), "simpleFoam") == []


def test_a_top_level_directive_silences_the_relaxation_check(tmp_path):
    def mutate(files):
        with_nested_relaxation()(files)
        files["system/fvSolution"] += '#include "relaxation"\n'

    assert errors_of(write_case(tmp_path, "simpleFoam", mutate), "simpleFoam") == []


def test_a_relaxation_factors_entry_outside_the_algorithm_dictionary_is_not_judged(tmp_path):
    def mutate(files):  # only the algorithm dictionary of the solver is looked at
        files["system/fvSolution"] = files["system/fvSolution"].replace(
            "    U\n    {\n", "    U\n    {\n        relaxationFactors { x 1; }\n", 1
        )

    assert errors_of(write_case(tmp_path, "icoFoam", mutate), "icoFoam") == []


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


def test_nested_relaxation_in_a_dictionary_the_solver_does_not_read_is_not_judged(tmp_path):
    def mutate(files):  # a PISO dictionary in a simpleFoam case is never read
        with_nested_relaxation()(files)
        text = files["system/fvSolution"]
        files["system/fvSolution"] = (
            text.replace("SIMPLE\n{", "PISO\n{", 1)
            + "SIMPLE\n{\n    consistent yes;\n    pRefCell 0;\n    pRefValue 0;\n}\n"
        )

    assert errors_of(write_case(tmp_path, "simpleFoam", mutate), "simpleFoam") == []


# ------------------------------------------------------------------ directive messages


SKELETON = re.compile(r'(?:add|create it with) "[^"]*;[^"]*"')


@pytest.mark.parametrize(("label", "rel", "edit", "solver"), list(removal_params()))
def test_every_missing_item_names_its_file_and_shows_a_skeleton(tmp_path, label, rel, edit, solver):
    def mutate(files):
        if edit is None:
            del files[rel]
        else:
            files[rel] = edit(files[rel])

    (error,) = errors_of(write_case(tmp_path, solver, mutate), solver)
    assert error.startswith(f"{rel}: "), error  # the file to create or edit comes first
    assert SKELETON.search(error), error  # and the minimal entry in quotes


def test_a_skeleton_is_a_generic_requirement_and_never_a_case_value(tmp_path):
    def mutate(files):
        del files["system/fvSolution"]
        del files["system/fvSchemes"]
        del files["constant/transportProperties"]

    errors = errors_of(write_case(tmp_path, "pimpleFoam", mutate), "pimpleFoam")
    text = "\n".join(errors)
    assert "<value>" in text and "<tolerance>" in text and "<convection scheme>" in text
    assert "Euler" in text and "pFinal" in text and "UFinal" in text and "PIMPLE" in text
    steady = errors_of(write_case(tmp_path / "s", "simpleFoam", mutate), "simpleFoam")
    joined = "\n".join(steady)
    assert "SIMPLE {" in joined and "PIMPLE" not in joined and "ddtSchemes" not in joined


def test_no_skeleton_carries_a_mesh_dependent_value(tmp_path):
    def mutate(files):
        del files["system/fvSolution"]

    for solver in SOLVERS:
        (error,) = errors_of(write_case(tmp_path / solver, solver, mutate), solver)
        assert "nNonOrthogonalCorrectors <" in error  # a placeholder, never a number


def test_a_missing_steady_fvsolution_skeleton_has_under_relaxation(tmp_path):
    def mutate(files):
        del files["system/fvSolution"]

    (error,) = errors_of(write_case(tmp_path / "s", "simpleFoam", mutate), "simpleFoam")
    assert "relaxationFactors { fields { p <factor>; } equations { U <factor>; } }" in error
    assert "residualControl" in error and "optional" in error
    (transient,) = errors_of(write_case(tmp_path / "t", "pimpleFoam", mutate), "pimpleFoam")
    assert "relaxationFactors" not in transient and "residualControl" not in transient
    assert len(SKELETON.findall(error)) == 1  # one quoted skeleton, not several fragments


def test_a_missing_scheme_or_solution_file_says_a_turbulence_model_needs_more(tmp_path):
    def laminar_without_files(files):
        del files["system/fvSchemes"]
        del files["system/fvSolution"]

    def turbulent_without_files(files):
        laminar_without_files(files)
        files["constant/turbulenceProperties"] = files["constant/turbulenceProperties"].replace(
            "laminar", "RAS"
        )

    def unknown_without_files(files):
        laminar_without_files(files)
        del files["constant/turbulenceProperties"]

    for label, mutate, expected in (
        ("lam", laminar_without_files, False),
        ("ras", turbulent_without_files, True),
        ("unk", unknown_without_files, True),
    ):
        errors = errors_of(write_case(tmp_path / label, "simpleFoam", mutate), "simpleFoam")
        named = [e for e in errors if e.startswith(("system/fvSchemes", "system/fvSolution"))]
        assert len(named) == 2
        for error in named:
            assert ("momentum and pressure entries only" in error) is expected, error
    ico = errors_of(write_case(tmp_path / "ico", "icoFoam", laminar_without_files), "icoFoam")
    assert not any("momentum and pressure" in e for e in ico)  # icoFoam has no turbulence model


def test_the_missing_final_solver_message_has_one_skeleton_and_no_pattern_advice(tmp_path):
    def mutate(files):
        text = files["system/fvSolution"]
        start, end = text.index("    pFinal"), text.index("    U\n")
        files["system/fvSolution"] = text[:start] + text[end:]

    (error,) = errors_of(write_case(tmp_path, "icoFoam", mutate), "icoFoam")
    assert "pFinal { $p; relTol 0; }" in error
    assert "(p|pFinal)" not in error and error.count('"') == 2
