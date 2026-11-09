"""More cases of the completeness check: switches that remove a demand, directives, odd input."""

from __future__ import annotations

import time

import pytest
from solver_cases import SOLVERS, remove_block
from test_case_requirements import errors_of, write_case

from cfdagent.tools.case_check import check_case_consistency

HEADER = (
    "FoamFile\n{\n    version 2.0;\n    format ascii;\n    class dictionary;\n    object x;\n}\n"
)
VISCOUS = "    div((nuEff*dev2(T(grad(U))))) Gauss linear;\n"
STEADY_DDT = "ddtSchemes\n{\n    default steadyState;\n}\n"


def without_velocity_solvers(files, extra):
    text = remove_block("U", "    ")(files["system/fvSolution"])
    text = remove_block("UFinal", "    ")(text)
    files["system/fvSolution"] = text.replace(
        "    nNonOrthogonalCorrectors 0;\n",
        "    nNonOrthogonalCorrectors 0;\n" + extra,
        1,
    )


@pytest.mark.parametrize("solver", SOLVERS)
@pytest.mark.parametrize("off", ["no", "off", "false"])
def test_velocity_solvers_are_not_demanded_when_the_momentum_predictor_is_off(
    tmp_path, solver, off
):
    def mutate(files):
        without_velocity_solvers(files, f"    momentumPredictor {off};\n")

    assert errors_of(write_case(tmp_path, solver, mutate), solver) == []


def test_velocity_solvers_are_still_demanded_when_the_predictor_is_on(tmp_path):
    def mutate(files):
        without_velocity_solvers(files, "    momentumPredictor yes;\n")

    errors = errors_of(write_case(tmp_path, "icoFoam", mutate), "icoFoam")
    assert any("entry for: U" in e for e in errors)


def test_a_macro_momentum_predictor_is_not_judged(tmp_path):
    def mutate(files):
        without_velocity_solvers(files, "    momentumPredictor $mp;\n")

    assert errors_of(write_case(tmp_path, "icoFoam", mutate), "icoFoam") == []


def laminar_model(model):
    def mutate(files):
        files["constant/turbulenceProperties"] = files["constant/turbulenceProperties"].replace(
            "simulationType laminar;\n",
            f"simulationType laminar;\nlaminar\n{{\n    model {model};\n}}\n",
        )
        files["system/fvSchemes"] = files["system/fvSchemes"].replace(VISCOUS, "")

    return mutate


@pytest.mark.parametrize("model", ["Stokes", "generalisedNewtonian"])
def test_the_viscous_stress_term_is_demanded_for_linear_viscous_laminar_models(tmp_path, model):
    errors = errors_of(write_case(tmp_path, "pimpleFoam", laminar_model(model)), "pimpleFoam")
    assert any("nuEff" in e for e in errors)


@pytest.mark.parametrize("model", ["Maxwell", "$model"])
def test_the_viscous_stress_term_is_not_demanded_for_other_laminar_models(tmp_path, model):
    assert errors_of(write_case(tmp_path, "pimpleFoam", laminar_model(model)), "pimpleFoam") == []


def turbulent(files):
    files["constant/turbulenceProperties"] = files["constant/turbulenceProperties"].replace(
        "simulationType laminar;", "simulationType RAS;\nRAS\n{\n    model kEpsilon;\n}"
    )


def test_the_viscous_stress_term_is_not_demanded_when_the_flow_is_turbulent(tmp_path):
    def mutate(files):
        turbulent(files)
        files["system/fvSchemes"] = files["system/fvSchemes"].replace(VISCOUS, "")

    errors = errors_of(write_case(tmp_path, "simpleFoam", mutate), "simpleFoam")
    assert not any("nuEff" in e for e in errors)  # turbulence models are not judged here


def test_a_steady_turbulent_case_needs_ddt_schemes_and_a_laminar_one_does_not(tmp_path):
    def mutate(files):
        turbulent(files)
        files["system/fvSchemes"] = files["system/fvSchemes"].replace(STEADY_DDT, "")

    errors = errors_of(write_case(tmp_path / "ras", "simpleFoam", mutate), "simpleFoam")
    assert any("ddtSchemes" in e for e in errors)

    def laminar(files):
        files["system/fvSchemes"] = files["system/fvSchemes"].replace(STEADY_DDT, "")

    assert errors_of(write_case(tmp_path / "lam", "simpleFoam", laminar), "simpleFoam") == []


@pytest.mark.parametrize("where", ["transportProperties", "pRefCell", "div"])
def test_an_eval_directive_hides_an_entry_from_the_check(tmp_path, where):
    def mutate(files):
        if where == "transportProperties":
            text = files["constant/transportProperties"]
            files["constant/transportProperties"] = text.replace(
                "nu [0 2 -1 0 0 0 0] 0.01;", "nu #eval{ 1.0/100.0 };"
            )
        elif where == "pRefCell":
            text = files["system/fvSolution"].replace("    pRefValue 0;\n", "")
            files["system/fvSolution"] = text.replace(
                "    pRefCell 0;\n", "    pRefCell #eval{ 0 };\n"
            )
        else:
            text = files["system/fvSchemes"]
            files["system/fvSchemes"] = text.replace(
                "    div(phi,U) Gauss linear;\n", "    div(phi,U) #eval{ 1 };\n"
            )

    assert errors_of(write_case(tmp_path, "icoFoam", mutate), "icoFoam") == []


def test_a_block_valued_scheme_in_the_divergence_dictionary_hides_the_names(tmp_path):
    def mutate(files):
        files["system/fvSchemes"] = files["system/fvSchemes"].replace(
            "    div(phi,U) Gauss linear;\n", "    div(phi,k)\n    {\n        type x;\n    }\n", 1
        )

    assert errors_of(write_case(tmp_path, "icoFoam", mutate), "icoFoam") == []


def test_a_macro_transport_model_is_not_judged_but_icofoam_always_needs_nu(tmp_path):
    def macro(files):
        text = files["constant/transportProperties"]
        text = text.replace("transportModel Newtonian;\n", "transportModel $tm;\n")
        files["constant/transportProperties"] = text.replace("nu [0 2 -1 0 0 0 0] 0.01;\n", "")

    assert errors_of(write_case(tmp_path / "a", "simpleFoam", macro), "simpleFoam") == []

    def ico(files):
        files["constant/transportProperties"] = HEADER + "transportModel CrossPowerLaw;\n"

    errors = errors_of(write_case(tmp_path / "b", "icoFoam", ico), "icoFoam")
    assert any("`nu`" in e for e in errors)


def test_a_chain_of_quantifiers_in_a_solver_key_is_neither_tried_nor_reported(tmp_path):
    pattern = ".*" * 45

    def mutate(files):
        keys = "".join(
            f'    "{pattern}X{i}"\n    {{\n        solver PCG;\n    }}\n' for i in range(20)
        )
        text = remove_block("p", "    ")(files["system/fvSolution"])
        files["system/fvSolution"] = text.replace("solvers\n{\n", "solvers\n{\n" + keys, 1)

    root = write_case(tmp_path, "icoFoam", mutate)
    started = time.monotonic()
    errors = errors_of(root, "icoFoam")
    assert time.monotonic() - started < 5
    assert errors == []  # the pattern is too complex to try: it is taken to cover the names


def test_a_posix_class_in_a_solver_key_does_not_cause_a_report(tmp_path):
    def mutate(files):
        text = remove_block("p", "    ")(files["system/fvSolution"])
        text = remove_block("pFinal", "    ")(text)
        key = '    "[[:alpha:]]"\n    {\n        solver PCG;\n    }\n'
        files["system/fvSolution"] = text.replace("solvers\n{\n", "solvers\n{\n" + key, 1)

    assert errors_of(write_case(tmp_path, "icoFoam", mutate), "icoFoam") == []


def break_file(path, kind):
    if kind == "unparseable":
        path.write_text("a { b {\n", encoding="utf-8")
    elif kind == "oversized":
        path.write_text("x 1;\n" * 400_000, encoding="utf-8")
    elif kind == "binary":
        path.write_bytes(bytes(range(256)) * 20)
    elif kind == "empty":
        path.write_text("", encoding="utf-8")
    else:
        target = path.with_name(path.name + ".real")
        path.rename(target)
        try:
            path.symlink_to(target)
        except OSError:
            pytest.skip("symbolic links are not available")


@pytest.mark.parametrize("kind", ["unparseable", "oversized", "symlink", "binary", "empty"])
@pytest.mark.parametrize(
    "rel", ["system/fvSchemes", "system/fvSolution", "constant/transportProperties"]
)
@pytest.mark.parametrize("solver", SOLVERS)
def test_a_file_that_cannot_be_judged_is_skipped_without_a_report(tmp_path, solver, rel, kind):
    root = write_case(tmp_path, solver)
    break_file(root / rel, kind)
    result = check_case_consistency(root, solver)  # must not raise
    if kind in ("empty", "binary"):
        return  # a readable file with nothing in it: its items are really missing
    named = [e for e in result.errors if rel in e and "does not exist" not in e]
    assert named == [], named


def test_an_include_at_the_top_of_a_scheme_file_hides_its_entries(tmp_path):
    def mutate(files):
        text = files["system/fvSchemes"].replace("laplacianSchemes", "laplacianSchemesX", 1)
        files["system/fvSchemes"] = '#include "extra"\n' + text

    assert errors_of(write_case(tmp_path, "icoFoam", mutate), "icoFoam") == []


def test_odd_scheme_shapes_do_not_raise(tmp_path):
    def mutate(files):
        files["system/fvSchemes"] = (
            HEADER + "gradSchemes (1 2);\ndivSchemes 7;\nlaplacianSchemes;\n"
        )
        files["0/p"] = files["0/p"].replace("internalField uniform 0;", "internalField { a 1; }")

    errors = errors_of(write_case(tmp_path, "icoFoam", mutate), "icoFoam")
    assert any("gradSchemes" in e for e in errors)  # reported, not an exception
