"""What the incompressible solvers of the image read: the files and entries a case must have.

``solver_problems`` is part of ``check_case_consistency``. A trial run reveals one missing item at a
time (a missing file, then a missing entry, then the next), so a case that lacks five things needs
five attempts. This check lists every missing item at once, before the solver runs.

The rules below come from running ``icoFoam``, ``simpleFoam`` and ``pimpleFoam`` of the image on a
small laminar case with one item removed at a time; the test module
``tests/integration/test_solver_requirements.py`` repeats that experiment, so the rules cannot
drift away from OpenFOAM. Only what OpenFOAM refuses
is reported: ``interpolationSchemes``, ``snGradSchemes`` and the correctors of the algorithm
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


@dataclass(frozen=True)
class _Needs:
    algorithm: str  # the dictionary of fvSolution that carries pRefCell and pRefValue
    solvers: tuple[str, ...]  # solver entries of fvSolution
    ddt_schemes: bool  # read by the laminar case (a steady turbulent case reads it too)
    transport_model: bool  # `transportModel` and `constant/turbulenceProperties`
    viscous_div: bool


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
    kinematic = (
        "`transportModel Newtonian;` and `nu [0 2 -1 0 0 0 0] <value>;`"
        if needs.transport_model
        else "`nu [0 2 -1 0 0 0 0] <value>;` (kinematic viscosity)"
    )
    if not exists:
        return [f"{rel} does not exist; {solver} reads it. Write it with {kinematic}"]
    if transport is None or cc.DIRECTIVES in transport:
        return []
    problems = []
    model = cc._word(transport.get("transportModel"))
    if needs.transport_model and "transportModel" not in transport:
        problems.append(
            f"{rel} has no `transportModel`; {solver} reads it. Add `transportModel Newtonian;`"
        )
    # icoFoam always reads `nu`; the others read it for the Newtonian model (the default here)
    reads_nu = not needs.transport_model or (not cc._macro(model) and model in (None, "Newtonian"))
    if "nu" not in transport and reads_nu:
        problems.append(f"{rel} has no `nu`; add `nu [0 2 -1 0 0 0 0] <value>;` (m^2/s)")
    return problems


def _turbulence_problems(turbulence: Dictionary | None, exists: bool, solver: str) -> list[str]:
    rel = "constant/turbulenceProperties"
    if not exists:
        return [
            f"{rel} does not exist; {solver} reads it even for a laminar flow. Write it with "
            "`simulationType laminar;` (or `RAS { ... }` for a turbulence model)"
        ]
    if turbulence is None or cc.DIRECTIVES in turbulence or "simulationType" in turbulence:
        return []
    return [f"{rel} has no `simulationType`; add `simulationType laminar;` or `RAS`/`LES`"]


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


def _schemes_problems(
    case_dir: Path, needs: _Needs, ddt: bool, viscous: bool, skipped: list[str]
) -> list[str]:
    rel = "system/fvSchemes"
    schemes, exists = _load(case_dir, rel, skipped)
    if not exists:
        return [
            f"{rel} does not exist; write it with the scheme dictionaries gradSchemes, "
            "divSchemes and laplacianSchemes" + (" and ddtSchemes" if ddt else "")
        ]
    if schemes is None or cc.DIRECTIVES in schemes:
        return []
    required = ["gradSchemes", "divSchemes", "laplacianSchemes"]
    if ddt:
        required.insert(0, "ddtSchemes")
    problems = [
        f"{rel} has no `{name}` dictionary; add `{name} {{ default ...; }}`"
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
    wanted = [MOMENTUM_DIV] + ([VISCOUS_DIV] if viscous else [])
    for name in wanted:
        if not _has_scheme(body, name):
            problems.append(
                f"{rel}: divSchemes has no `{name}` entry and its default is `none`; add "
                f"`{name} <scheme>;` (the solver reads this term)"
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


def _solution_problems(case_dir: Path, needs: _Needs, skipped: list[str]) -> list[str]:
    rel = "system/fvSolution"
    solution, exists = _load(case_dir, rel, skipped)
    if not exists:
        return [
            f"{rel} does not exist; write it with `solvers` ({', '.join(needs.solvers)}) and the "
            f"`{needs.algorithm}` dictionary"
        ]
    if solution is None or cc.DIRECTIVES in solution:
        return []
    problems = []
    wanted = needs.solvers
    if not _momentum_solved(solution, needs.algorithm):
        wanted = tuple(name for name in wanted if not name.startswith("U"))
    solvers = solution.get("solvers")
    if not isinstance(solvers, dict):
        problems.append(
            f"{rel} has no `solvers` dictionary; add an entry for each of {', '.join(wanted)}"
        )
    elif cc.DIRECTIVES not in solvers:
        keys = [key for key in solvers if key != cc.DIRECTIVES]
        missing = [name for name in wanted if not _covers(keys, name)]
        if missing:
            problems.append(
                f"{rel}: `solvers` has no entry for: {', '.join(missing)}. {_final_hint(missing)}"
            )
    if _pressure_needs_reference(case_dir, skipped):
        problems += _reference_problems(rel, solution, needs.algorithm)
    return problems


def _final_hint(missing: list[str]) -> str:
    if any(name.endswith("Final") for name in missing):
        return (
            "The final solves read `pFinal` (and `UFinal` for pimpleFoam); a quoted pattern such "
            'as `"(p|pFinal)"` covers both'
        )
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
        f"{rel}: the pressure level is not fixed by any boundary condition of 0/p, so "
        f"`{algorithm}` needs `pRefCell 0;` (or `pRefPoint (x y z);`) and `pRefValue 0;`; "
        f"missing: {', '.join(missing)}"
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
    problems += _schemes_problems(case_dir, needs, ddt, viscous, skipped)
    problems += _solution_problems(case_dir, needs, skipped)
    return problems
