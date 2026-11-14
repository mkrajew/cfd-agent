"""What the incompressible solvers of the image read: the files and entries a case must have.

``solver_problems`` is part of ``check_case_consistency``. A trial run reveals one missing item at a
time (a missing file, then a missing entry, then the next), so a case that lacks five things needs
five attempts. This check lists every missing item at once, before the solver runs.

The rules below come from running ``icoFoam``, ``simpleFoam`` and ``pimpleFoam`` of the image on a
small laminar case with one item removed at a time; the test module
``tests/integration/test_solver_requirements.py`` repeats that experiment, so the rules cannot
drift away from OpenFOAM. Only what OpenFOAM refuses is reported, with one quality rule: a
``relaxationFactors`` entry inside the algorithm dictionary, which OpenFOAM silently ignores.
``interpolationSchemes``, ``snGradSchemes`` and the correctors of the algorithm
dictionary have defaults and are not required. The check covers this laminar core and the pressure
reference. What a turbulence model adds (its fields in ``0/``, their solvers and schemes, the
model entries) is not listed up front and is left to the trial run.

The check is conservative: a false report would reject a valid case, so it stays silent whenever
it cannot be sure. It never reports an item that an ``#include``, a ``#calc`` or an ``#eval`` may
supply; a ``default`` scheme stands in for a missing scheme name; a quoted regular expression key
covers the solver names it matches (and a key too complex to try is taken to cover them); a
pressure whose level is fixed by a boundary condition needs no reference cell; the momentum
solvers are not demanded when ``momentumPredictor`` is off; the viscous stress term is demanded
only for the laminar models that certainly have it. Anything it cannot read is left to
``check_dictionaries`` and the trial run.

Every message starts with the file to create or edit and shows the minimal skeleton of the missing
entry in double quotes, for example ``system/fvSchemes: missing laplacianSchemes; add
"laplacianSchemes { default Gauss linear corrected; }"``. A skeleton holds what the solver requires
(the keywords and the structure); where a value belongs to the case it is a ``<placeholder>``.

Container-side module (``case_check`` calls it); host-side code does not import it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from cfdagent.tools import case_check as cc

Dictionary = cc.Dictionary

# Boundary-condition types that leave the pressure level free. A pressure field made only of these
# needs a reference cell (or point); any other type (``fixedValue``, ``totalPressure``, ...) is
# assumed to fix the level.
FREE_LEVEL_TYPES = frozenset(
    {
        "zeroGradient",
        "fixedGradient",
        "fixedFluxPressure",
        "empty",
        "wedge",
        "symmetry",
        "symmetryPlane",
        "cyclic",
        "cyclicAMI",
        "cyclicSlip",
        "processor",
    }
)
# Laminar models whose momentum equation has the linear viscous stress term (the default model
# when none is named). Others (Maxwell, ...) have their own terms.
LINEAR_VISCOUS_LAMINAR = frozenset({"Stokes", "generalisedNewtonian"})
_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)
VISCOUS_DIV = "div((nuEff*dev2(T(grad(U)))))"
MOMENTUM_DIV = "div(phi,U)"
LAPLACIAN_SKELETON = "laplacianSchemes { default Gauss linear corrected; }"
GRAD_SKELETON = "gradSchemes { default Gauss linear; }"
MOMENTUM_SKELETON = f"{MOMENTUM_DIV} <convection scheme>;"
VISCOUS_SKELETON = f"{VISCOUS_DIV} Gauss linear;"
LINEAR_SOLVER_SKELETONS = {
    "p": "p { solver PCG; preconditioner DIC; tolerance <tolerance>; "
    "relTol <relative tolerance>; }",
    "pFinal": "pFinal { $p; relTol 0; }",
    "U": "U { solver smoothSolver; smoother symGaussSeidel; tolerance <tolerance>; "
    "relTol <relative tolerance>; }",
    "UFinal": "UFinal { $U; relTol 0; }",
}
PRESSURE_REFERENCE_SKELETON = "pRefCell 0; pRefValue 0;"
ALGORITHM_SKELETON = (
    "{algorithm} {{ nNonOrthogonalCorrectors <number of non-orthogonal correctors>; }}"
)
RELAXATION_SKELETON = "relaxationFactors { fields { p <factor>; } equations { U <factor>; } }"
# Said when a missing file's skeleton is incomplete because the flow may be turbulent.
TURBULENCE_NOTE = (
    " The skeleton lists the momentum and pressure entries only; the fields of a turbulence "
    "model need their own schemes and linear solvers."
)


@dataclass(frozen=True)
class _Needs:
    algorithm: str  # the dictionary of fvSolution that carries pRefCell and pRefValue
    solvers: tuple[str, ...]  # solver entries of fvSolution
    ddt_schemes: bool  # read by the laminar case (a steady turbulent case reads it too)
    transport_model: bool  # `transportModel` and `constant/turbulenceProperties`
    viscous_div: bool

    @property
    def steady(self) -> bool:
        return self.algorithm == "SIMPLE"

    @property
    def time_scheme(self) -> str:
        """A time scheme that fits the solver (the steady solver has no time derivative)."""
        return "steadyState" if self.steady else "Euler"


NEEDS = {
    "icoFoam": _Needs("PISO", ("p", "pFinal", "U"), True, False, False),
    "simpleFoam": _Needs("SIMPLE", ("p", "U"), False, True, True),
    "pimpleFoam": _Needs("PIMPLE", ("p", "pFinal", "U", "UFinal"), True, True, True),
}


def _load(case_dir: Path, rel: str, skipped: list[str]) -> tuple[Dictionary | None, bool]:
    """The parsed file and whether it exists; the dictionary is ``None`` if it cannot be judged."""
    path = case_dir / rel
    if not path.is_file():
        return None, False
    return cc._read(path, rel, skipped), True


def _transport_problems(
    case_dir: Path, needs: _Needs, solver: str, skipped: list[str]
) -> list[str]:
    rel = "constant/transportProperties"
    transport, exists = _load(case_dir, rel, skipped)
    nu_entry = "nu [0 2 -1 0 0 0 0] <value>;"
    kinematic = f"transportModel Newtonian; {nu_entry}" if needs.transport_model else nu_entry
    if not exists:
        return [
            f'{rel}: file does not exist; {solver} reads it; create it with "{kinematic}" '
            "(nu is the kinematic viscosity in m^2/s)"
        ]
    if transport is None or cc.DIRECTIVES in transport:
        return []
    problems = []
    model = cc._word(transport.get("transportModel"))
    if needs.transport_model and "transportModel" not in transport:
        problems.append(
            f'{rel}: missing transportModel; {solver} reads it; add "transportModel Newtonian;"'
        )
    # icoFoam always reads `nu`; the others read it for the Newtonian model (the default here)
    reads_nu = not needs.transport_model or (not cc._macro(model) and model in (None, "Newtonian"))
    if "nu" not in transport and reads_nu:
        problems.append(f'{rel}: missing `nu`; add "{nu_entry}" (the kinematic viscosity in m^2/s)')
    return problems


def _turbulence_problems(turbulence: Dictionary | None, exists: bool, solver: str) -> list[str]:
    rel = "constant/turbulenceProperties"
    if not exists:
        return [
            f"{rel}: file does not exist; {solver} reads it even for a laminar flow; create it "
            'with "simulationType laminar;" (for a turbulence model: "simulationType RAS;" and '
            "a `RAS` dictionary)"
        ]
    if turbulence is None or cc.DIRECTIVES in turbulence or "simulationType" in turbulence:
        return []
    return [
        f'{rel}: missing simulationType; add "simulationType laminar;" (or RAS or LES for a '
        "turbulence model)"
    ]


def _simulation_type(turbulence: Dictionary | None) -> str | None:
    if turbulence is None or cc.DIRECTIVES in turbulence:
        return None
    word = cc._word(turbulence.get("simulationType"))
    return None if cc._macro(word) else word


def _linear_viscous(turbulence: Dictionary | None) -> bool:
    """True when the flow is laminar with a model that certainly has the linear viscous stress
    term ``div((nuEff*dev2(T(grad(U)))))`` (no ``laminar`` dictionary means the default model)."""
    if _simulation_type(turbulence) != "laminar" or turbulence is None:
        return False
    laminar = turbulence.get("laminar")
    if laminar is None:
        return True
    if not isinstance(laminar, dict) or cc.DIRECTIVES in laminar:
        return False
    model = cc._word(laminar.get("model")) or cc._word(laminar.get("laminarModel"))
    return model in LINEAR_VISCOUS_LAMINAR


def _scheme_names(text: str) -> str | None:
    """The body of the ``divSchemes`` block of a scheme file, comments removed."""
    match = re.search(r"divSchemes\s*\{(.*?)\}", _COMMENT.sub("", text), re.DOTALL)
    return match.group(1) if match else None


def _has_scheme(body: str, name: str) -> bool:
    return re.search(r"(?:^|[\s;{])" + re.escape(name) + r"\s", body) is not None


def _div_skeleton(viscous: bool) -> str:
    entries = [MOMENTUM_SKELETON] + ([VISCOUS_SKELETON] if viscous else [])
    return "divSchemes { default none; " + " ".join(entries) + " }"


def _schemes_problems(
    case_dir: Path, needs: _Needs, ddt: bool, viscous: bool, skipped: list[str], note: str
) -> list[str]:
    rel = "system/fvSchemes"
    schemes, exists = _load(case_dir, rel, skipped)
    skeletons = {
        "ddtSchemes": f"ddtSchemes {{ default {needs.time_scheme}; }}",
        "gradSchemes": GRAD_SKELETON,
        "divSchemes": _div_skeleton(viscous),
        "laplacianSchemes": LAPLACIAN_SKELETON,
    }
    required = ["gradSchemes", "divSchemes", "laplacianSchemes"]
    if ddt:
        required.insert(0, "ddtSchemes")
    if not exists:
        listing = " ".join(skeletons[name] for name in required)
        return [f'{rel}: file does not exist; create it with "{listing}".{note}']
    if schemes is None or cc.DIRECTIVES in schemes:
        return []
    problems = [
        f'{rel}: missing {name}; add "{skeletons[name]}"'
        for name in required
        if not isinstance(schemes.get(name), dict)
    ]
    div = schemes.get("divSchemes")
    if not isinstance(div, dict) or cc.DIRECTIVES in div:
        return problems
    default = cc._word(div.get("default"))
    if any(key.startswith('"') for key in div) or (default is not None and default != "none"):
        return problems  # a default or a pattern stands in for the names below
    try:
        text = (case_dir / rel).read_bytes().decode("utf-8", "replace")
    except OSError:
        return problems
    body = _scheme_names(text)
    if body is None:
        return problems
    wanted = [(MOMENTUM_DIV, MOMENTUM_SKELETON)]
    if viscous:
        wanted.append((VISCOUS_DIV, VISCOUS_SKELETON))
    for name, skeleton in wanted:
        if not _has_scheme(body, name):
            problems.append(
                f"{rel}: divSchemes has no `{name}` entry and its default is `none`; add "
                f'"{skeleton}" inside divSchemes (the solver reads this term)'
            )
    return problems


def _covers(keys: list[str], name: str) -> bool:
    """True if a key of the solvers dictionary names ``name``: the word itself or a quoted
    regular expression that matches it (OpenFOAM's rule). A pattern that is too complex to try
    safely, or that Python cannot read, is taken to cover the name: nothing is reported."""
    for key in keys:
        if key == name:
            return True
        if key.startswith('"'):
            pattern = cc._unquote(key)
            if cc.pattern_too_complex(pattern):
                return True
            try:
                if re.fullmatch(pattern, name):
                    return True
            except re.error:
                return True  # OpenFOAM's regular expression dialect differs from Python's
    return False


def _pressure_needs_reference(case_dir: Path, skipped: list[str]) -> bool:
    """True when every boundary condition of ``0/p`` leaves the pressure level free."""
    field, exists = _load(case_dir, "0/p", skipped)
    boundary = field.get("boundaryField") if field is not None else None
    if not exists or not isinstance(boundary, dict) or cc.DIRECTIVES in boundary:
        return False
    entries = [value for key, value in boundary.items() if key != cc.DIRECTIVES]
    if not entries or not all(isinstance(entry, dict) for entry in entries):
        return False
    kinds = [cc._entry_type(entry) for entry in entries]
    return all(kind in FREE_LEVEL_TYPES for kind in kinds)


def _momentum_solved(solution: Dictionary, algorithm: str) -> bool:
    """False when ``momentumPredictor`` is off (the velocity equation is then not solved and has
    no linear solver entry) or when the setting cannot be judged; True for the default."""
    block = solution.get(algorithm)
    if not isinstance(block, dict):
        return True
    if cc.DIRECTIVES in block:
        return False
    word = cc._word(block.get("momentumPredictor"))
    if word is None:
        return True
    if cc._macro(word):
        return False
    return word.lower() not in cc._OFF


def _solution_problems(case_dir: Path, needs: _Needs, skipped: list[str], note: str) -> list[str]:
    rel = "system/fvSolution"
    solution, exists = _load(case_dir, rel, skipped)
    if not exists:
        skeleton = f"{_solvers_skeleton(needs.solvers)} {_algorithm_skeleton(needs.algorithm)}"
        advice = ""
        if needs.steady:
            skeleton += f" {RELAXATION_SKELETON}"
            advice = (
                " A steady run is also best given a `residualControl` dictionary inside "
                f"`{needs.algorithm}` (optional, but it stops the run when the residuals are "
                "small enough)."
            )
        return [f'{rel}: file does not exist; create it with "{skeleton}".{advice}{note}']
    if solution is None or cc.DIRECTIVES in solution:
        return []
    problems = []
    wanted = needs.solvers
    if not _momentum_solved(solution, needs.algorithm):
        wanted = tuple(name for name in wanted if not name.startswith("U"))
    solvers = solution.get("solvers")
    if not isinstance(solvers, dict):
        problems.append(f'{rel}: missing `solvers`; add "{_solvers_skeleton(wanted)}"')
    elif cc.DIRECTIVES not in solvers:
        keys = [key for key in solvers if key != cc.DIRECTIVES]
        missing = [name for name in wanted if not _covers(keys, name)]
        if missing:
            skeleton = " ".join(LINEAR_SOLVER_SKELETONS[name] for name in missing)
            problems.append(
                f"{rel}: `solvers` has no entry for: {', '.join(missing)}; add "
                f'"{skeleton}" inside `solvers`. {_final_hint(missing)}'
            )
    if _pressure_needs_reference(case_dir, skipped):
        problems += _reference_problems(rel, solution, needs.algorithm)
    problems += _relaxation_problems(rel, solution, needs.algorithm)
    return problems


def _algorithm_skeleton(algorithm: str) -> str:
    return ALGORITHM_SKELETON.format(algorithm=algorithm)


def _solvers_skeleton(names: tuple[str, ...]) -> str:
    return "solvers { " + " ".join(LINEAR_SOLVER_SKELETONS[name] for name in names) + " }"


RELAXATION = "relaxationFactors"


def _relaxation_problems(rel: str, solution: Dictionary, algorithm: str) -> list[str]:
    """``relaxationFactors`` written inside the solver's algorithm dictionary (SIMPLE, PISO or
    PIMPLE, ``algorithm``).

    OpenFOAM reads the relaxation factors only from the top-level ``relaxationFactors`` dictionary
    of fvSolution; inside the algorithm dictionary the entry is silently ignored, no relaxation is
    applied and a steady solver may diverge without any message. Reported only when no
    top-level dictionary exists (then the nested one is the only attempt to set the factors) and
    when no directive in the file or in the algorithm dictionary could supply or move entries.
    """
    if RELAXATION in solution or cc.DIRECTIVES in solution:
        return []
    block = solution.get(algorithm)
    if not isinstance(block, dict) or cc.DIRECTIVES in block or RELAXATION not in block:
        return []
    value = block[RELAXATION]
    if isinstance(value, list) and cc._macro(cc._word(value)):
        return []
    return [
        f"{rel}: `{RELAXATION}` is written inside the `{algorithm}` dictionary, where "
        "OpenFOAM ignores it, so no under-relaxation is applied. It must be a top-level "
        f'dictionary of {rel}, beside `{algorithm}` and not inside it: "{RELAXATION_SKELETON}" '
        "(a steady solver under-relaxes with it; choose the factors for the case)"
    ]


def _final_hint(missing: list[str]) -> str:
    if any(name.endswith("Final") for name in missing):
        return "The final solves read `pFinal` (and `UFinal` for pimpleFoam)"
    return "Add a linear solver entry for each field the solver solves"


def _reference_problems(rel: str, solution: Dictionary, algorithm: str) -> list[str]:
    block = solution.get(algorithm)
    if isinstance(block, dict) and cc.DIRECTIVES in block:
        return []
    block = block if isinstance(block, dict) else {}
    missing = []
    if "pRefCell" not in block and "pRefPoint" not in block:
        missing.append("pRefCell")
    if "pRefValue" not in block:
        missing.append("pRefValue")
    if not missing:
        return []
    return [
        f"{rel}: missing {', '.join(missing)}; the pressure level is not fixed by any boundary "
        f'condition of 0/p, so add "{PRESSURE_REFERENCE_SKELETON}" inside `{algorithm}` '
        "(`pRefPoint (x y z);` may replace pRefCell)"
    ]


def solver_problems(case_dir: Path, solver: str, skipped: list[str]) -> list[str]:
    """All missing files and entries for ``solver`` in ``case_dir`` (empty for a solver outside
    ``NEEDS``). Notes about unreadable files are appended to ``skipped``."""
    needs = NEEDS.get(solver)
    if needs is None:
        return []
    case_dir = Path(case_dir)
    problems = _transport_problems(case_dir, needs, solver, skipped)
    turbulence: Dictionary | None = None
    ddt, viscous = needs.ddt_schemes, False
    if needs.transport_model:
        turbulence, exists = _load(case_dir, "constant/turbulenceProperties", skipped)
        problems += _turbulence_problems(turbulence, exists, solver)
        # a steady turbulent case reads ddtSchemes too (the turbulence equations have a time term)
        ddt = ddt or _simulation_type(turbulence) in ("RAS", "LES")
        viscous = needs.viscous_div and _linear_viscous(turbulence)
    # The skeletons of a missing file are the laminar core; a turbulence model, or a flow whose
    # model is not known, needs more than they list.
    incomplete = needs.transport_model and _simulation_type(turbulence) != "laminar"
    note = TURBULENCE_NOTE if incomplete else ""
    problems += _schemes_problems(case_dir, needs, ddt, viscous, skipped, note)
    problems += _solution_problems(case_dir, needs, skipped, note)
    return problems
