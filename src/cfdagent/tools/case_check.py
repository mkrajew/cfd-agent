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
  ``endTime`` above ``startTime``.

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
            out[key], i = _parse_block(tokens, i + 1, depth + 1, top=False)
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


def _find_entry(boundary_field: Dictionary, patch: str, groups: list[str]) -> tuple[Any, bool]:
    """The entry that applies to ``patch`` and whether a regular expression was too complex to
    try. OpenFOAM tries the exact name first, then the patterns and groups, last written first."""
    if isinstance(boundary_field.get(patch), dict):
        return boundary_field[patch], False
    too_complex = False
    for key in reversed(list(boundary_field)):
        entry = boundary_field[key]
        if key == DIRECTIVES or not isinstance(entry, dict):
            continue
        if key.startswith('"'):
            pattern = _unquote(key)
            if len(pattern) > MAX_REGEX_CHARS or _NESTED_QUANTIFIER.search(pattern):
                too_complex = True
                continue
            try:
                if re.fullmatch(pattern, patch):
                    return entry, too_complex
            except re.error:
                continue
        elif key in groups:
            return entry, too_complex
    return None, too_complex


def _field_problems(
    name: str, boundary_field: Dictionary, patches: dict[str, tuple[str, list[str]]]
) -> list[str]:
    problems = []
    opaque = DIRECTIVES in boundary_field  # an #include may supply entries we cannot see
    for patch, (patch_type, groups) in patches.items():
        entry, too_complex = _find_entry(boundary_field, patch, groups)
        if entry is None:
            if not opaque and not too_complex:
                hint = (
                    f"; a {patch_type} patch needs `type {patch_type};`"
                    if patch_type in CONSTRAINT_TYPES
                    else ""
                )
                problems.append(
                    f"0/{name} has no entry for patch `{patch}` (type {patch_type}) in "
                    f"boundaryField{hint}. Every patch of constant/polyMesh/boundary needs an "
                    "entry in every field file"
                )
            continue
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


def _control_problems(case_dir: Path, solver: str, skipped: list[str]) -> list[str]:
    path = case_dir / "system" / "controlDict"
    if not path.is_file():
        return ["system/controlDict does not exist; write it (application, time controls)"]
    control = _read(path, "system/controlDict", skipped)
    if control is None:
        return []
    problems = []
    application = control.get("application")
    if not isinstance(application, list) or not application:
        problems.append(
            f"system/controlDict has no `application`; set `application {solver};`, the solver "
            "this run uses"
        )
    elif _unquote(application[0]) != solver:
        problems.append(
            f"system/controlDict has application `{_unquote(application[0])}` but this run uses "
            f"the solver `{solver}` (the plan's suggested solver); set `application {solver};`"
        )
    start, end = control.get("startTime"), control.get("endTime")
    if start is None:
        problems.append("system/controlDict has no `startTime`; set `startTime 0;` or the start")
    if end is None:
        problems.append("system/controlDict has no `endTime`; set the time the run must reach")
    start_value, end_value = _number(start), _number(end)
    if start_value is not None and end_value is not None and end_value <= start_value:
        problems.append(
            f"system/controlDict has endTime {end_value:g} which is not above startTime "
            f"{start_value:g}; the run would do nothing. Set endTime to the time the run must "
            "reach (iterations for a steady solver)"
        )
    return problems


def check_case_consistency(case_dir: Path, solver: str) -> GateResult:
    """Check the case in ``case_dir`` as described in the module docstring.

    ``data`` holds ``patches`` (name -> type from the mesh), ``fields`` (the field files judged)
    and ``skipped`` (what could not be read, with the reason).
    """
    case_dir = Path(case_dir)
    skipped: list[str] = []
    patches, problems = _mesh_patches(case_dir)
    fields: list[str] = []
    if patches:
        problems += _field_files(case_dir, patches, fields, skipped)
    problems += _control_problems(case_dir, solver, skipped)
    data = {
        "patches": {name: kind for name, (kind, _groups_) in patches.items()},
        "fields": fields,
        "skipped": skipped,
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
        fields.append(entry.name)
        problems += _field_problems(entry.name, boundary_field, patches)
    if not fields and not skipped:
        problems.append(
            "0/ holds no field file with a boundaryField; write the fields the solver needs"
        )
    return problems
