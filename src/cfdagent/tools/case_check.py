"""Consistency checks of an OpenFOAM case that no single-file parse can make.

``check_case_consistency`` looks at the mesh and the case files together, without running
OpenFOAM:

* every patch of ``constant/polyMesh/boundary`` has an entry in ``boundaryField`` of every field
  file in ``0/`` (an entry may be given by the patch name, by a quoted regular expression or by a
  patch group of the mesh, as OpenFOAM allows);
* constraint patch types must match on both sides: a mesh patch of type ``empty``, ``wedge``,
  ``symmetryPlane`` or ``symmetry`` needs a field entry of that same type, and a field entry of
  such a type needs a mesh patch of that type (OpenFOAM refuses an inconsistent pair);
* ``system/controlDict`` has an ``application`` equal to the solver that will run, and an
  ``endTime`` above ``startTime``;
* the files and entries the solver reads exist (``case_requirements``: transport and turbulence
  properties, scheme dictionaries, linear solvers, pressure reference), all missing ones at once;
* a uniform value has the shape of its field (a vector in a vector field, a number in a scalar one).

All problems are collected and returned together. The parser is a small, tolerant reader of the
OpenFOAM dictionary syntax, not a replacement for ``foamDictionary``: ``check_dictionaries``
judges the syntax. A file this module cannot read (too large, unbalanced braces) is left out and
named in ``data["skipped"]`` instead of being judged. A ``boundaryField`` with an ``#include`` or
an over-complicated regular expression may define patches that are not visible here, so its
missing entries are not reported.

Container-side module (an executor op calls it); host-side code does not import it.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Any

from cfdagent.gates import GateResult

MAX_FILE_BYTES = 1_000_000  # a dictionary with a few patches is a few kilobytes
MAX_FIELD_FILES = 100
MAX_NESTING = 50
MAX_REGEX_CHARS = 100
CONSTRAINT_TYPES = frozenset({"empty", "wedge", "symmetryPlane", "symmetry"})
DIRECTIVES = "#"  # key under which a dictionary records the ``#...`` directives it contains

_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|//[^\n]*|/\*.*?\*/|[{};()]|[^\s{};()"]+', re.DOTALL)
_PUNCTUATION = frozenset("{};()")
_NESTED_QUANTIFIER = re.compile(r"[)\]][*+{]")  # a repeated group: a regex that can blow up
_QUANTIFIER = re.compile(r"[*+?{]")
MAX_QUANTIFIERS = 2  # chained quantifiers (``.*.*.*``) take polynomial time on a long name


def pattern_too_complex(pattern: str) -> bool:
    """True for a regular expression not safe to try on a name: long, a repeated group, or many
    quantifiers. The caller then treats the pattern as matching (reports nothing)."""
    return (
        len(pattern) > MAX_REGEX_CHARS
        or "[:" in pattern  # a POSIX class: OpenFOAM's dialect, not Python's
        or _NESTED_QUANTIFIER.search(pattern) is not None
        or len(_QUANTIFIER.findall(pattern)) > MAX_QUANTIFIERS
    )


Dictionary = dict[str, Any]  # values: ``list[str]`` (the tokens of an entry) or a sub-dictionary


class _ParseError(Exception):
    """The text is not a dictionary this reader understands."""


def _parse_block(tokens: list[str], i: int, depth: int, top: bool) -> tuple[Dictionary, int]:
    if depth > MAX_NESTING:
        raise _ParseError("nested too deeply")
    out: Dictionary = {}
    statement: list[str] = []
    while i < len(tokens):
        token = tokens[i]
        if token == "}":
            if top:
                raise _ParseError("a closing brace without an opening one")
            return out, i + 1
        if token == "{":
            key = statement[-1] if statement else ""
            block, i = _parse_block(tokens, i + 1, depth + 1, top=False)
            if key.startswith("#") or key == ")":
                # ``nu #eval{...}``, ``div(phi,k) {...}``: not a plain ``name { ... }`` block, so
                # the entry cannot be read here. Record it as a directive: nothing is judged.
                out.setdefault(DIRECTIVES, []).append(key)
            else:
                out[key] = block
            statement = []
            continue
        if token == ";":
            if statement:
                out[statement[0]] = statement[1:]
            statement = []
            i += 1
            continue
        if token.startswith("#") and not statement:
            out.setdefault(DIRECTIVES, []).append(token)
            i += 1
            if i < len(tokens) and tokens[i] not in _PUNCTUATION:
                i += 1  # the file or function the directive names (it has no ``;``)
            continue
        statement.append(token)
        i += 1
    if not top:
        raise _ParseError("a block that is never closed")
    return out, i


def parse_dictionary(text: str) -> Dictionary:
    """The top-level entries of an OpenFOAM dictionary text; raises ``_ParseError`` if the braces
    do not balance. Comments are dropped; an entry is its list of value tokens."""
    tokens = [t for t in _TOKEN.findall(text) if not t.startswith(("//", "/*"))]
    return _parse_block(tokens, 0, 0, top=True)[0]


def _unquote(token: str) -> str:
    return token[1:-1] if len(token) >= 2 and token[0] == token[-1] == '"' else token


def _entry_type(entry: Any) -> str | None:
    """The ``type`` word of a boundary-field entry, or ``None`` if the entry gives none."""
    if not isinstance(entry, dict):
        return None
    value = entry.get("type")
    return _unquote(value[0]) if isinstance(value, list) and value else None


def _groups(entry: Any) -> list[str]:
    """The words inside the parentheses of an ``inGroups`` entry of a mesh patch."""
    value = entry.get("inGroups") if isinstance(entry, dict) else None
    if not isinstance(value, list) or "(" not in value:
        return []
    start = value.index("(") + 1
    end = value.index(")", start) if ")" in value[start:] else len(value)
    return [_unquote(t) for t in value[start:end]]


def _read(path: Path, label: str, skipped: list[str]) -> Dictionary | None:
    """Parse a regular, unlinked, small file; ``None`` (with a note) if that is not possible."""
    try:
        status = os.lstat(path)
        if not stat.S_ISREG(status.st_mode):
            skipped.append(f"{label} is not a regular file")
            return None
        if status.st_size > MAX_FILE_BYTES:
            skipped.append(f"{label} is larger than {MAX_FILE_BYTES} bytes and was not analysed")
            return None
        text = path.read_bytes().decode("utf-8", "replace")
        return parse_dictionary(text)
    except _ParseError as exc:
        skipped.append(f"{label} could not be analysed ({exc}); see the syntax check")
    except OSError as exc:
        skipped.append(f"{label} could not be read ({type(exc).__name__})")
    return None


def _mesh_patches(case_dir: Path) -> tuple[dict[str, tuple[str, list[str]]], list[str]]:
    """Patch name -> (type, groups) of ``constant/polyMesh/boundary``, and the problems."""
    path = case_dir / "constant" / "polyMesh" / "boundary"
    label = "constant/polyMesh/boundary"
    skipped: list[str] = []
    if not path.is_file():
        return {}, [f"{label} does not exist; the case has no mesh"]
    parsed = _read(path, label, skipped)
    if parsed is None:
        return {}, [f"{label} cannot be read: {'; '.join(skipped)}"]
    patches = {
        name: (_entry_type(entry) or "patch", _groups(entry))
        for name, entry in parsed.items()
        if name != "FoamFile" and isinstance(entry, dict)
    }
    if not patches:
        return {}, [f"{label} lists no patch"]
    return patches, []


def _find_entry(
    boundary_field: Dictionary, patch: str, groups: list[str]
) -> tuple[Any, str | None, bool]:
    """The entry that applies to ``patch``, how it was found (``exact``, ``group``, ``regex`` or
    ``None``) and whether a regular expression was too complex to try.

    OpenFOAM's order: the exact patch name; then the patch groups, the last written first; then
    the regular expressions, the last written first.
    """
    if isinstance(boundary_field.get(patch), dict):
        return boundary_field[patch], "exact", False
    keys = [
        key
        for key in reversed(list(boundary_field))
        if key != DIRECTIVES and isinstance(boundary_field[key], dict)
    ]
    for key in keys:
        if not key.startswith('"') and key in groups:
            return boundary_field[key], "group", False
    too_complex = False
    for key in keys:
        if not key.startswith('"'):
            continue
        pattern = _unquote(key)
        if pattern_too_complex(pattern):
            too_complex = True
            continue
        try:
            if re.fullmatch(pattern, patch):
                return boundary_field[key], "regex", too_complex
        except re.error:
            continue
    return None, None, too_complex


def _shape_problems(name: str, parsed: Dictionary, boundary_field: Dictionary) -> list[str]:
    """A uniform value of the wrong shape: a number in a vector field or a vector in a scalar
    field (``internalField`` and the ``value`` of each patch). OpenFOAM stops with a parse error
    that does not say which entry is wrong."""
    header = parsed.get("FoamFile")
    kind = _word(header.get("class")) if isinstance(header, dict) else None
    if kind not in ("volVectorField", "volScalarField"):
        return []
    vector = kind == "volVectorField"
    places = [("internalField", parsed.get("internalField"))]
    places += [
        (f"`value` of patch `{patch}`", entry.get("value"))
        for patch, entry in boundary_field.items()
        if isinstance(entry, dict)
    ]
    problems = []
    for where, tokens in places:
        if not isinstance(tokens, list) or len(tokens) < 2 or tokens[0] != "uniform":
            continue
        if _macro(tokens[1]):
            continue
        if vector and tokens[1] != "(":
            problems.append(
                f"0/{name}: {where} is `uniform {tokens[1]}` but {name} is a vector field; write "
                "`uniform (x y z)`"
            )
        elif not vector and tokens[1] == "(":
            problems.append(
                f"0/{name}: {where} is a vector but {name} is a scalar field; write "
                "`uniform <number>`"
            )
    return problems


def _field_problems(
    name: str, boundary_field: Dictionary, patches: dict[str, tuple[str, list[str]]]
) -> list[str]:
    problems = []
    opaque = DIRECTIVES in boundary_field  # an #include may supply or override entries
    for patch, (patch_type, groups) in patches.items():
        entry, how, too_complex = _find_entry(boundary_field, patch, groups)
        if entry is None:
            if not opaque and not too_complex:
                hint = (
                    f"; a {patch_type} patch is written as `type {patch_type};`"
                    if patch_type in CONSTRAINT_TYPES
                    else ""
                )
                problems.append(
                    f"0/{name} has no entry for patch `{patch}` (type {patch_type}) in "
                    f"boundaryField{hint}. This pipeline requires an entry for every patch of "
                    "constant/polyMesh/boundary in every field file"
                )
            continue
        if how == "regex" and (opaque or patch_type == "empty"):
            continue  # a wildcard: OpenFOAM fixes the type of an empty patch itself
        found = _entry_type(entry)
        if patch_type in CONSTRAINT_TYPES and found is not None and found != patch_type:
            problems.append(
                f"0/{name}: patch `{patch}` is of type {patch_type} in constant/polyMesh/boundary, "
                f"so its entry must be `type {patch_type};` (found `{found}`). OpenFOAM requires "
                "the patch field type of a constraint patch to match the mesh patch type"
            )
        elif found in CONSTRAINT_TYPES and found != patch_type:
            problems.append(
                f"0/{name}: the entry for patch `{patch}` has the constraint type `{found}`, but "
                f"the mesh patch is of type {patch_type} in constant/polyMesh/boundary (the mesh "
                "cannot be edited). Use a boundary condition that fits a "
                f"{patch_type} patch"
            )
    return problems


def _number(entry: Any) -> float | None:
    if isinstance(entry, list) and entry:
        try:
            return float(entry[0])
        except ValueError:
            return None
    return None


START_FROM = ("startTime", "firstTime", "latestTime")
STOP_AT = ("endTime", "noWriteNow", "writeNow", "nextWrite")
WRITE_CONTROLS = ("timeStep", "runTime", "adjustableRunTime", "adjustable")
_OFF = frozenset({"off", "no", "false", "none", "n", "f", "0", "uncompressed"})
_ON = frozenset({"yes", "on", "true", "y", "t", "1"})
_CONTROL = "system/controlDict"


def _word(entry: Any) -> str | None:
    return _unquote(entry[0]) if isinstance(entry, list) and entry else None


def _macro(word: str | None) -> bool:
    """A value that OpenFOAM expands (``$name``) or a directive: it cannot be judged here."""
    return word is not None and word.startswith(("$", "#"))


def _choice(control: Dictionary, key: str, allowed: tuple[str, ...]):
    """The word of ``key`` and a problem text if the word is not one of ``allowed``. A missing key
    is not a problem (OpenFOAM supplies a default), nor is a macro."""
    word = _word(control.get(key))
    if word is not None and not _macro(word) and word not in allowed:
        return word, f"{_CONTROL} has {key} `{word}`; it must be one of {', '.join(allowed)}"
    return word, None


def _write_problems(control: Dictionary, opaque: bool, start: float | None, end: float | None):
    problems = []
    write_control = _word(control.get("writeControl"))
    if write_control is None:
        write_control = "timeStep"  # OpenFOAM's default, and what an include may replace
    elif _macro(write_control):
        write_control = None
    elif write_control not in WRITE_CONTROLS:
        extra = (
            ". `writeTime` is valid only inside function objects, not as the run's writeControl"
            if write_control == "writeTime"
            else ""
        )
        _, problem = _choice(control, "writeControl", WRITE_CONTROLS)
        problems.append(f"{problem}{extra}")
        write_control = None
    interval_entry = control.get("writeInterval")
    if interval_entry is None:
        if not opaque:
            problems.append(
                f"{_CONTROL} has no `writeInterval`; this pipeline requires it (steps for "
                "timeStep, seconds for runTime and adjustableRunTime)"
            )
        return problems
    interval = _number(interval_entry)
    if interval is None or write_control is None:
        return problems
    if interval <= 0 or (write_control == "timeStep" and not float(interval).is_integer()):
        need = "an integer of at least 1" if write_control == "timeStep" else "above 0"
        problems.append(
            f"{_CONTROL} has writeInterval {interval:g}; with {write_control} it must be {need}"
        )
        return problems
    delta_t = _number(control.get("deltaT"))
    if write_control == "timeStep":
        step = interval * delta_t if delta_t and delta_t > 0 else None
    else:
        step = interval
    span = None if start is None or end is None else end - start
    if step is not None and span is not None and span > 0 and step > span * (1 + 1e-9):
        problems.append(
            f"{_CONTROL}: writeInterval {interval:g} with {write_control} gives the first write "
            f"time after {step:g}, beyond the run length {span:g}; the run would write no result. "
            "Choose a smaller writeInterval"
        )
    return problems


def _format_problems(control: Dictionary) -> list[str]:
    problems = []
    write_format = _word(control.get("writeFormat"))
    if write_format is not None and not _macro(write_format) and write_format != "ascii":
        problems.append(
            f"{_CONTROL} has writeFormat `{write_format}`; use `writeFormat ascii;` so that the "
            "results can be read by the analysis"
        )
    compression = _word(control.get("writeCompression"))
    if compression is not None and not _macro(compression) and compression.lower() not in _OFF:
        problems.append(
            f"{_CONTROL} has writeCompression `{compression}`; use `writeCompression off;`"
        )
    return problems


def _control_problems(
    case_dir: Path, solver: str, skipped: list[str]
) -> tuple[list[str], dict[str, Any]]:
    """Problems of ``system/controlDict`` and its time settings (``adjust_time_step``,
    ``delta_t``). A key that an ``#include`` or directive may supply is not reported missing."""
    settings: dict[str, Any] = {"adjust_time_step": False, "delta_t": None}
    path = case_dir / "system" / "controlDict"
    if not path.is_file():
        return [f"{_CONTROL} does not exist; write it (application, time controls)"], settings
    control = _read(path, _CONTROL, skipped)
    if control is None:
        return [], settings
    opaque = DIRECTIVES in control
    problems = []
    application = control.get("application")
    if not isinstance(application, list) or not application:
        if not opaque:
            problems.append(
                f"{_CONTROL} has no `application`; set `application {solver};`, the solver "
                "this run uses"
            )
    elif _unquote(application[0]) != solver:
        problems.append(
            f"{_CONTROL} has application `{_unquote(application[0])}` but this run uses "
            f"the solver `{solver}` (the plan's suggested solver); set `application {solver};`"
        )
    start, end = control.get("startTime"), control.get("endTime")
    if start is None and not opaque:
        problems.append(f"{_CONTROL} has no `startTime`; set `startTime 0;` or the start")
    if end is None and not opaque:
        problems.append(f"{_CONTROL} has no `endTime`; set the time the run must reach")
    start_value, end_value = _number(start), _number(end)
    if start_value is not None and end_value is not None and end_value <= start_value:
        problems.append(
            f"{_CONTROL} has endTime {end_value:g} which is not above startTime "
            f"{start_value:g}; the run would do nothing. Set endTime to the time the run must "
            "reach (iterations for a steady solver)"
        )
    for key, allowed in (("startFrom", START_FROM), ("stopAt", STOP_AT)):
        problem = _choice(control, key, allowed)[1]
        if problem:
            problems.append(problem)
    problems += _write_problems(control, opaque, start_value, end_value)
    problems += _format_problems(control)
    settings["adjust_time_step"] = (_word(control.get("adjustTimeStep")) or "").lower() in _ON
    settings["delta_t"] = _number(control.get("deltaT"))
    return problems, settings


def check_case_consistency(case_dir: Path, solver: str) -> GateResult:
    """Check the case in ``case_dir`` as described in the module docstring.

    ``data`` holds ``patches`` (name -> type from the mesh), ``fields`` (the field files judged),
    ``skipped`` (what could not be read, with the reason) and the time settings the stage needs:
    ``adjust_time_step`` (bool) and ``delta_t`` (float or ``None``).
    """
    case_dir = Path(case_dir)
    skipped: list[str] = []
    patches, problems = _mesh_patches(case_dir)
    fields: list[str] = []
    if patches:
        problems += _field_files(case_dir, patches, fields, skipped)
    control_problems, settings = _control_problems(case_dir, solver, skipped)
    problems += control_problems
    from cfdagent.tools import case_requirements  # imported here: it imports this module

    problems += case_requirements.solver_problems(case_dir, solver, skipped)
    data = {
        "patches": {name: kind for name, (kind, _groups_) in patches.items()},
        "fields": fields,
        "skipped": skipped,
        **settings,
    }
    return GateResult(not problems, problems, data=data)


def _field_files(
    case_dir: Path,
    patches: dict[str, tuple[str, list[str]]],
    fields: list[str],
    skipped: list[str],
) -> list[str]:
    folder = case_dir / "0"
    if not folder.is_dir():
        return ["the case has no 0/ directory with the initial and boundary conditions"]
    problems = []
    entries = sorted(os.scandir(folder), key=lambda e: e.name)
    for entry in entries:
        if entry.name.startswith("."):
            continue
        if entry.is_symlink():
            problems.append(f"0/{entry.name} is a symbolic link; write a regular file")
            continue
        if not entry.is_file(follow_symlinks=False):
            continue
        if len(fields) + len(skipped) >= MAX_FIELD_FILES:
            skipped.append(f"more than {MAX_FIELD_FILES} files in 0/; the rest were not analysed")
            break
        parsed = _read(Path(entry.path), f"0/{entry.name}", skipped)
        boundary_field = parsed.get("boundaryField") if parsed is not None else None
        if not isinstance(boundary_field, dict):
            continue  # not a field file (or not readable: already in ``skipped``)
        header = parsed.get("FoamFile") if parsed is not None else None
        if isinstance(header, dict) and (_word(header.get("class")) or "").startswith("point"):
            continue  # a point field has its own patch rules; the volume rules do not apply
        fields.append(entry.name)
        problems += _field_problems(entry.name, boundary_field, patches)
        problems += _shape_problems(entry.name, parsed, boundary_field)
    if not fields and not skipped:
        problems.append(
            "0/ holds no field file with a boundaryField; write the fields the solver needs"
        )
    return problems
