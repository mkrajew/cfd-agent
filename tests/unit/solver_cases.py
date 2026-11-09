"""Minimal, complete cases of the three incompressible solvers, as text, for the completeness check.

``complete_case(solver)`` maps a path inside the case to its text. The cases are what OpenFOAM ESI
v2512 needs to take time steps on a one-cell-thick square with a moving lid (checked by running
the solvers in the image: ``tests/integration/test_solver_requirements.py``). They contain only
what the solver requires, so removing any one item must be reported by the check.
"""

from __future__ import annotations

SOLVERS = ("icoFoam", "simpleFoam", "pimpleFoam")

BOUNDARY = """FoamFile { version 2.0; format ascii; class polyBoundaryMesh; object boundary; }
3
(
    movingWall { type wall; inGroups List<word> 1(wall); nFaces 10; startFace 280; }
    fixedWalls { type wall; inGroups List<word> 1(wall); nFaces 30; startFace 290; }
    frontAndBack { type empty; inGroups List<word> 1(empty); nFaces 200; startFace 320; }
)
"""


def _head(kind: str, name: str) -> str:
    lines = ["FoamFile", "{", "    version 2.0;", "    format ascii;"]
    lines += [f"    class {kind};", f"    object {name};", "}"]
    return "\n".join(lines) + "\n"


def complete_case(solver: str) -> dict[str, str]:
    transient = solver != "simpleFoam"
    files: dict[str, str] = {}
    steps = "endTime 0.015;\ndeltaT 0.005;\n" if transient else "endTime 3;\ndeltaT 1;\n"
    files["system/controlDict"] = _head("dictionary", "controlDict") + (
        f"application {solver};\nstartFrom startTime;\nstartTime 0;\nstopAt endTime;\n{steps}"
        "writeControl timeStep;\nwriteInterval 3;\n"
    )
    divphi = {
        "icoFoam": "Gauss linear",
        "simpleFoam": "bounded Gauss linearUpwind grad(U)",
        "pimpleFoam": "Gauss linearUpwind grad(U)",
    }[solver]
    viscous = "" if solver == "icoFoam" else "    div((nuEff*dev2(T(grad(U))))) Gauss linear;\n"
    ddt = "steadyState" if solver == "simpleFoam" else "Euler"
    files["system/fvSchemes"] = _head("dictionary", "fvSchemes") + (
        f"ddtSchemes\n{{\n    default {ddt};\n}}\n"
        "gradSchemes\n{\n    default Gauss linear;\n}\n"
        f"divSchemes\n{{\n    default none;\n    div(phi,U) {divphi};\n{viscous}}}\n"
        "laplacianSchemes\n{\n    default Gauss linear corrected;\n}\n"
    )
    algorithm = {"icoFoam": "PISO", "simpleFoam": "SIMPLE", "pimpleFoam": "PIMPLE"}[solver]
    final_p = "    pFinal\n    {\n        $p;\n        relTol 0;\n    }\n" if transient else ""
    final_u = (
        "    UFinal\n    {\n        $U;\n        relTol 0;\n    }\n"
        if solver == "pimpleFoam"
        else ""
    )
    relax = (
        "relaxationFactors\n{\n    equations\n    {\n        U 0.9;\n    }\n}\n"
        if solver == "simpleFoam"
        else ""
    )
    consistent = "    consistent yes;\n" if solver == "simpleFoam" else ""
    files["system/fvSolution"] = _head("dictionary", "fvSolution") + (
        "solvers\n{\n    p\n    {\n        solver PCG;\n        preconditioner DIC;\n"
        "        tolerance 1e-6;\n        relTol 0.05;\n    }\n"
        f"{final_p}"
        "    U\n    {\n        solver smoothSolver;\n        smoother symGaussSeidel;\n"
        "        tolerance 1e-5;\n        relTol 0;\n    }\n"
        f"{final_u}"
        "}\n"
        f"{algorithm}\n{{\n    nNonOrthogonalCorrectors 0;\n{consistent}"
        "    pRefCell 0;\n    pRefValue 0;\n}\n"
        f"{relax}"
    )
    if solver == "icoFoam":
        files["constant/transportProperties"] = _head("dictionary", "transportProperties") + (
            "nu [0 2 -1 0 0 0 0] 0.01;\n"
        )
    else:
        files["constant/transportProperties"] = _head("dictionary", "transportProperties") + (
            "transportModel Newtonian;\nnu [0 2 -1 0 0 0 0] 0.01;\n"
        )
        files["constant/turbulenceProperties"] = _head("dictionary", "turbulenceProperties") + (
            "simulationType laminar;\n"
        )
    files["0/U"] = _head("volVectorField", "U") + (
        "dimensions [0 1 -1 0 0 0 0];\ninternalField uniform (0 0 0);\nboundaryField\n{\n"
        "    movingWall { type fixedValue; value uniform (1 0 0); }\n"
        "    fixedWalls { type noSlip; }\n    frontAndBack { type empty; }\n}\n"
    )
    files["0/p"] = _head("volScalarField", "p") + (
        "dimensions [0 2 -2 0 0 0 0];\ninternalField uniform 0;\nboundaryField\n{\n"
        "    movingWall { type zeroGradient; }\n    fixedWalls { type zeroGradient; }\n"
        "    frontAndBack { type empty; }\n}\n"
    )
    return files


# What removing one item from ``complete_case(solver)`` does. Each entry: label, file, the edit
# (``None`` deletes the file) and the solvers for which OpenFOAM refuses to run the case without
# the item (checked by ``tests/integration/test_solver_requirements.py``).
def remove_lines(marker: str):
    return lambda text: "".join(
        line + "\n" for line in text.split("\n") if marker not in line and line != ""
    )


def remove_block(name: str, indent: str = ""):
    """Remove ``name`` and its braces; ``indent`` is the leading space of the block."""
    import re

    pattern = re.compile(rf"^{indent}{re.escape(name)}\n{indent}\{{.*?\n{indent}\}}\n", re.S | re.M)
    return lambda text: pattern.sub("", text, count=1)


ALL = frozenset(SOLVERS)
SIMPLE_PIMPLE = frozenset({"simpleFoam", "pimpleFoam"})
TRANSIENT = frozenset({"icoFoam", "pimpleFoam"})
REMOVALS = [
    ("constant/transportProperties missing", "constant/transportProperties", None, ALL),
    ("nu missing", "constant/transportProperties", remove_lines("nu "), ALL),
    (
        "transportModel missing",
        "constant/transportProperties",
        remove_lines("transportModel"),
        SIMPLE_PIMPLE,
    ),
    ("constant/turbulenceProperties missing", "constant/turbulenceProperties", None, SIMPLE_PIMPLE),
    (
        "simulationType missing",
        "constant/turbulenceProperties",
        remove_lines("simulationType"),
        SIMPLE_PIMPLE,
    ),
    ("system/fvSchemes missing", "system/fvSchemes", None, ALL),
    ("system/fvSolution missing", "system/fvSolution", None, ALL),
    ("ddtSchemes missing", "system/fvSchemes", remove_block("ddtSchemes"), TRANSIENT),
    ("gradSchemes missing", "system/fvSchemes", remove_block("gradSchemes"), ALL),
    ("divSchemes missing", "system/fvSchemes", remove_block("divSchemes"), ALL),
    ("laplacianSchemes missing", "system/fvSchemes", remove_block("laplacianSchemes"), ALL),
    ("div(phi,U) missing", "system/fvSchemes", remove_lines("div(phi,U)"), ALL),
    ("viscous div missing", "system/fvSchemes", remove_lines("nuEff"), SIMPLE_PIMPLE),
    ("solvers missing", "system/fvSolution", remove_block("solvers"), ALL),
    ("solver p missing", "system/fvSolution", remove_block("p", "    "), ALL),
    ("solver pFinal missing", "system/fvSolution", remove_block("pFinal", "    "), TRANSIENT),
    ("solver U missing", "system/fvSolution", remove_block("U", "    "), ALL),
    ("solver UFinal missing", "system/fvSolution", remove_block("UFinal", "    "), {"pimpleFoam"}),
    ("pRefCell missing", "system/fvSolution", remove_lines("pRefCell"), ALL),
    ("pRefValue missing", "system/fvSolution", remove_lines("pRefValue"), ALL),
]
