"""Read the results of a solved OpenFOAM case from a post-processing script, without OpenFOAM.

The Analyst's ``post.py`` imports this module instead of writing its own parser::

    from cfdagent.tools.foam_results import load_case, read_solver_log

    case = load_case("case")                  # the case directory (the one with system/, constant/)
    print(case.times, case.latest_time)       # numeric time directories of the case
    u = case.read_field("U")                  # latest time: (n_cells, 3) array
    xc = case.cell_centres                    # (n_cells, 3), same order as the fields
    wall = case.read_patch_field("p", "<patch>")        # values on the faces of a boundary patch
    line = case.sample_line("U", (x0, y0), (x1, y1), n_points=200)   # along a straight line
    line.s, line.value                        # distance from the start, values (NaN outside)
    log = read_solver_log("case")             # residuals, iteration count, convergence line

What it reads: the ASCII files that the pipeline's cases are written in (``writeFormat ascii``,
optionally ``.gz``) in the root time directories; ``processor*/`` directories are ignored. The mesh
geometry (cell centres, volumes, face centres and areas) is rebuilt from
``constant/polyMesh/{points,faces,owner,neighbour,boundary}`` with the same formulas OpenFOAM uses,
so it matches ``postProcess -func writeCellCentres`` and works for the one-cell-thick cases of 2D
runs. Nothing is written, nothing outside the case directory is opened and symbolic links are
never followed. Binary files are refused with a message (the case setup requires ASCII).

Arrays: a scalar field is ``(n,)``, a vector field ``(n, 3)``, a symmetric tensor ``(n, 6)`` and a
tensor ``(n, 9)``, in OpenFOAM's component order. Cell values are ordered like the cells of the
mesh. Surface fields (``phi``) are read on the internal faces, and their patch values per patch
face.
"""

from __future__ import annotations

import gzip
import math
import os
import re
import zlib
from collections import OrderedDict
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any

import numpy as np

from cfdagent.errors import AgentInputError, ToolFailure
from cfdagent.tools import polymesh
from cfdagent.tools.foam import parse_solver_log_file

__all__ = [
    "FoamCase",
    "FoamResultsError",
    "LineSample",
    "Patch",
    "SolverLog",
    "latest_time",
    "list_times",
    "load_case",
    "read_solver_log",
]

_TIME_NAME_RE = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_COMMENT_BLOCK_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_COMMENT_LINE_RE = re.compile(r"//[^\n]*")
_HEADER_RE = re.compile(r"FoamFile\s*\{([^{}]*)\}")
_FORMAT_RE = re.compile(r"\bformat\s+(\w+)\s*;")
_CLASS_RE = re.compile(r"\bclass\s+(\w+)\s*;")
_FIELD_CLASS_RE = re.compile(
    r"^(vol|surface|point)(Scalar|Vector|SphericalTensor|SymmTensor|Tensor)Field$"
)
_COMPONENTS = {"Scalar": 1, "Vector": 3, "SphericalTensor": 1, "SymmTensor": 6, "Tensor": 9}
_INTERNAL_RE = re.compile(r"\binternalField\s")
_BOUNDARY_RE = re.compile(r"\bboundaryField\s*\{")
_PATCH_START_RE = re.compile(r'\s*("[^"]*"|[^\s{};]+)\s*\{')
_BRACE_RE = re.compile(r"[{}]")
_TYPE_RE = re.compile(r"(?<![\w])type\s+([^\s;]+)\s*;")
_VALUE_RE = re.compile(r"(?<![\w])value(?=\s)")
_UNIFORM_RE = re.compile(r"\s*uniform\b")
_NONUNIFORM_RE = re.compile(r"\s*nonuniform\b\s*(?:List<\w+>\s*)?(\d+)\s*([({])")
_LIST_END_RE = re.compile(r"\)\s*;")
_EXEC_RE = re.compile(r"^Exec\s*:\s*(\S+)", re.MULTILINE)
_STEP_RE = re.compile(r"^Time = ", re.MULTILINE)
_METHODS = ("linear", "nearest")
_NO_FLUX_TYPES = ("symmetry", "symmetryPlane", "wedge")
_CACHE_SIZE = 8
_TOLERANCE = 1e-9  # relative to the size of the mesh: "on the boundary" counts as inside


class FoamResultsError(AgentInputError):
    """The case, a field or a request cannot be used; the message says what to do."""


# --------------------------------------------------------------------------------------------
# Time directories
# --------------------------------------------------------------------------------------------


def _time_entries(case_dir: Path) -> list[tuple[float, str]]:
    """``(value, directory name)`` of every numeric root directory, ascending. Links are skipped."""
    found: list[tuple[float, str]] = []
    try:
        entries = list(os.scandir(case_dir))
    except OSError as exc:
        raise FoamResultsError(f"cannot list the case directory {case_dir}: {exc}") from exc
    for entry in entries:
        if entry.is_symlink() or not entry.is_dir() or not _TIME_NAME_RE.fullmatch(entry.name):
            continue
        value = float(entry.name)
        if math.isfinite(value):
            found.append((value, entry.name))
    return sorted(found)


def _check_case_dir(case_dir: Path | str) -> Path:
    path = Path(case_dir)
    if not path.is_dir():
        raise FoamResultsError(
            f"the case directory {str(path)!r} does not exist or is not a directory; pass the "
            "directory that holds constant/ and system/ (a path relative to the working directory "
            "is fine)"
        )
    return path


def list_times(case_dir: Path | str) -> list[float]:
    """The numeric time directories of ``case_dir`` (``0``, ``0.5``, ``100``, ...), ascending.

    ``processor*/``, ``constant/``, ``system/``, ``postProcessing/`` and symbolic links are
    ignored. Does not need a mesh.
    """
    return [value for value, _ in _time_entries(_check_case_dir(case_dir))]


def latest_time(case_dir: Path | str) -> float:
    """The largest time of ``list_times``; ``FoamResultsError`` if the case has none."""
    times = list_times(case_dir)
    if not times:
        raise FoamResultsError(
            f"no time directory in {case_dir}; the solver wrote nothing to read (look at "
            "log.* and logs/solver_run.json)"
        )
    return times[-1]


# --------------------------------------------------------------------------------------------
# Reading and parsing one field file
# --------------------------------------------------------------------------------------------


@dataclass
class _Block:
    """One patch entry of a field file: its ``type`` and its ``value`` (``None`` when absent)."""

    type: str | None
    value: np.ndarray | None  # (c,) for a uniform value, (n_faces, c) for a list


@dataclass
class _Field:
    cls: str
    ncomp: int | None
    internal: np.ndarray  # (c,) uniform or (n, c)
    boundary: dict[str, _Block]
    location: str  # "vol", "surface", "point" or "" for a class that is not a field class
    time_name: str
    name: str


def _numbers(text: str, what: str) -> np.ndarray:
    try:
        return np.array(text.replace("(", " ").replace(")", " ").split(), dtype=float)
    except ValueError as exc:
        raise FoamResultsError(f"{what} contains a value that is not a number ({exc})") from exc


def _parse_value(text: str, pos: int, ncomp: int | None, what: str) -> np.ndarray:
    """The ``uniform ...;`` or ``nonuniform List<..> N(...)`` value that starts at ``pos``.

    Returns ``(c,)`` for a uniform value and ``(N, c)`` for a list.
    """
    uniform = _UNIFORM_RE.match(text, pos)
    if uniform:
        start = uniform.end()
        end = text.find(";", start)
        if end < 0:
            raise FoamResultsError(f"{what}: a uniform value without ';'")
        values = _numbers(text[start:end], what)
        if len(values) == 0 or (ncomp is not None and len(values) != ncomp):
            raise FoamResultsError(f"{what}: a uniform value with {len(values)} numbers")
        return values
    listed = _NONUNIFORM_RE.match(text, pos)
    if listed is None:
        raise FoamResultsError(f"{what}: expected 'uniform ...' or 'nonuniform List<..> N(...)'")
    count = int(listed.group(1))
    if listed.group(2) == "{":
        end = text.find("}", listed.end())
        if end < 0:
            raise FoamResultsError(f"{what}: a compact list without '}}'")
        one = _numbers(text[listed.end() : end], what)
        return np.tile(one, (count, 1)) if count else np.empty((0, ncomp or 1))
    end_match = _LIST_END_RE.search(text, listed.end())
    if end_match is None:
        raise FoamResultsError(f"{what}: a list that does not end with ');'")
    values = _numbers(text[listed.end() : end_match.start()], what)
    if count == 0:
        return np.empty((0, ncomp or 1))
    if len(values) % count:
        raise FoamResultsError(
            f"{what}: {len(values)} numbers cannot make {count} entries of equal size"
        )
    width = len(values) // count
    if ncomp is not None and width != ncomp:
        raise FoamResultsError(f"{what}: entries of {width} numbers, expected {ncomp}")
    return values.reshape(count, width)


def _patch_blocks(body: str, pos: int, what: str) -> dict[str, str]:
    """Patch name -> text between the braces, for the entries of ``boundaryField { ... }``."""
    blocks: dict[str, str] = {}
    while opening := _PATCH_START_RE.match(body, pos):
        depth, i = 1, opening.end()
        while depth:
            brace = _BRACE_RE.search(body, i)
            if brace is None:
                raise FoamResultsError(f"{what}: unbalanced braces in boundaryField")
            depth += 1 if brace.group() == "{" else -1
            i = brace.end()
        blocks[opening.group(1).strip('"')] = body[opening.end() : i - 1]
        pos = i
    return blocks


def _parse_field_text(raw: bytes, name: str, time_name: str, source: Path) -> _Field:
    what = f"{source.parent.name}/{source.name}"
    text = raw.decode("utf-8", errors="replace")
    text = _COMMENT_LINE_RE.sub(" ", _COMMENT_BLOCK_RE.sub(" ", text))
    header = _HEADER_RE.search(text)
    if header is None:
        raise FoamResultsError(f"{what} has no FoamFile header, so it is not an OpenFOAM field")
    fmt = _FORMAT_RE.search(header.group(1))
    if fmt is None or fmt.group(1) != "ascii":
        raise FoamResultsError(
            f"{what} is in {fmt.group(1) if fmt else 'an unknown'} format; only ASCII files can "
            "be read (binary or compressed-binary fields are not supported)"
        )
    cls_match = _CLASS_RE.search(header.group(1))
    cls = cls_match.group(1) if cls_match else ""
    known = _FIELD_CLASS_RE.match(cls)
    ncomp = _COMPONENTS[known.group(2)] if known else None
    body = text[header.end() :]
    internal = _INTERNAL_RE.search(body)
    if internal is None:
        raise FoamResultsError(f"{what} has no internalField entry (class {cls or 'unknown'})")
    internal_value = _parse_value(body, internal.end(), ncomp, f"{what} internalField")
    boundary: dict[str, _Block] = {}
    opening = _BOUNDARY_RE.search(body)
    if opening is not None:
        for patch, content in _patch_blocks(body, opening.end(), what).items():
            kind = _TYPE_RE.search(content)
            value_match = _VALUE_RE.search(content)
            value = None
            if value_match is not None:
                try:
                    value = _parse_value(content, value_match.end(), ncomp, f"{what} patch {patch}")
                except FoamResultsError:
                    value = None  # e.g. "value $internalField;": no usable value, see the fallbacks
            boundary[patch] = _Block(kind.group(1) if kind else None, value)
    return _Field(
        cls, ncomp, internal_value, boundary, known.group(1) if known else "", time_name, name
    )


# --------------------------------------------------------------------------------------------
# Mesh geometry
# --------------------------------------------------------------------------------------------


def _face_geometry(corners: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Centre and area vector of ``m`` faces with ``k`` corners each, ``corners`` is (m, k, 3).

    The formulas of OpenFOAM's ``primitiveMesh`` (triangle decomposition around the mean point).
    """
    k = corners.shape[1]
    if k == 3:
        a, b, c = corners[:, 0], corners[:, 1], corners[:, 2]
        return corners.mean(axis=1), 0.5 * np.cross(b - a, c - a)
    estimate = corners.mean(axis=1)
    following = np.roll(corners, -1, axis=1)
    normals = np.cross(following - corners, estimate[:, None, :] - corners)  # (m, k, 3)
    centres = corners + following + estimate[:, None, :]
    total = normals.sum(axis=1)
    length = np.linalg.norm(total, axis=1)
    unit = total / np.where(length > 0, length, 1.0)[:, None]
    weight = np.einsum("mkj,mj->mk", normals, unit)  # (m, k)
    weight_sum = weight.sum(axis=1)
    weighted = (weight[:, :, None] * centres).sum(axis=1)
    safe = np.where(weight_sum > 1e-300, weight_sum, 1.0)
    centre = np.where((weight_sum > 1e-300)[:, None], weighted / (3.0 * safe)[:, None], estimate)
    return centre, 0.5 * total


@dataclass
class Patch:
    """A boundary patch: the faces ``start_face`` ... ``start_face + n_faces - 1`` of the mesh.

    ``face_centres`` (n_faces, 3), ``face_areas`` (n_faces, 3: area vectors pointing out of the
    domain) and ``owner_cells`` (n_faces: the adjacent cell of every face) are in the order of
    the patch values returned by ``FoamCase.read_patch_field``.
    """

    name: str
    type: str
    start_face: int
    n_faces: int
    owner_cells: np.ndarray = field(repr=False)
    face_centres: np.ndarray = field(repr=False)
    face_areas: np.ndarray = field(repr=False)


@dataclass
class _Geometry:
    owner: np.ndarray
    neighbour: np.ndarray
    n_internal: int
    n_cells: int
    face_centres: np.ndarray
    face_areas: np.ndarray
    cell_centres: np.ndarray
    cell_volumes: np.ndarray
    cell_face_start: np.ndarray  # CSR: faces of cell c are cell_faces[start[c] : start[c + 1]]
    cell_faces: np.ndarray
    cell_face_sign: np.ndarray  # +1 where the face normal points out of the cell, else -1
    cell_min: np.ndarray
    cell_max: np.ndarray
    bounds: tuple[np.ndarray, np.ndarray]
    patches: dict[str, Patch]


def _build_geometry(case_dir: Path) -> _Geometry:
    for name in ("points", "faces", "owner", "neighbour", "boundary"):
        if (case_dir / "constant" / "polyMesh" / name).is_symlink():
            raise FoamResultsError(
                f"constant/polyMesh/{name} is a symbolic link; links are not followed"
            )
    try:
        points = polymesh.read_points(case_dir)
        faces = polymesh.read_faces(case_dir)
        owner, neighbour = polymesh.read_owner_neighbour(case_dir)
        ranges = polymesh.read_boundary(case_dir)
        types = polymesh.read_patch_types(case_dir)
    except (polymesh.MeshError, ToolFailure) as exc:
        raise FoamResultsError(f"the mesh of {case_dir} cannot be read: {exc}") from exc
    n_faces, n_internal = len(faces), len(neighbour)
    if len(owner) != n_faces or n_internal > n_faces:
        raise FoamResultsError(
            f"inconsistent mesh: {n_faces} faces, {len(owner)} owners, {n_internal} neighbours"
        )
    n_cells = int(max(owner.max(initial=-1), neighbour.max(initial=-1))) + 1

    sizes = np.fromiter((len(f) for f in faces), dtype=np.int64, count=n_faces)
    centres = np.empty((n_faces, 3))
    areas = np.empty((n_faces, 3))
    low = np.empty((n_faces, 3))
    high = np.empty((n_faces, 3))
    for k in np.unique(sizes):
        ids = np.nonzero(sizes == k)[0]
        corners = points[np.array([faces[i] for i in ids], dtype=np.int64)]
        centres[ids], areas[ids] = _face_geometry(corners)
        low[ids], high[ids] = corners.min(axis=1), corners.max(axis=1)

    cell_of = np.concatenate([owner, neighbour])
    face_of = np.concatenate([np.arange(n_faces), np.arange(n_internal)])
    sign = np.concatenate([np.ones(n_faces), -np.ones(n_internal)])
    count = np.bincount(cell_of, minlength=n_cells)
    if (count == 0).any():
        raise FoamResultsError("inconsistent mesh: a cell without faces")
    estimate = (
        np.stack(
            [
                np.bincount(cell_of, weights=centres[face_of][:, d], minlength=n_cells)
                for d in range(3)
            ],
            axis=1,
        )
        / count[:, None]
    )
    pyramid = sign * np.einsum("ij,ij->i", areas[face_of], centres[face_of] - estimate[cell_of])
    pyramid_centre = 0.75 * centres[face_of] + 0.25 * estimate[cell_of]
    volume3 = np.bincount(cell_of, weights=pyramid, minlength=n_cells)
    weighted = np.stack(
        [
            np.bincount(cell_of, weights=pyramid * pyramid_centre[:, d], minlength=n_cells)
            for d in range(3)
        ],
        axis=1,
    )
    good = np.abs(volume3) > 1e-300
    cell_centres = np.where(
        good[:, None], weighted / np.where(good, volume3, 1.0)[:, None], estimate
    )

    order = np.argsort(cell_of, kind="stable")
    start = np.concatenate([[0], np.cumsum(count)])
    cell_min = np.full((n_cells, 3), np.inf)
    cell_max = np.full((n_cells, 3), -np.inf)
    for d in range(3):
        np.minimum.at(cell_min[:, d], cell_of, low[face_of][:, d])
        np.maximum.at(cell_max[:, d], cell_of, high[face_of][:, d])

    patches: dict[str, Patch] = {}
    for name, (first, number) in ranges.items():
        ids = np.arange(first, first + number)
        if number and ids[-1] >= n_faces:
            raise FoamResultsError(f"patch {name!r} reaches beyond the {n_faces} faces of the mesh")
        patches[name] = Patch(
            name, types.get(name, "patch"), first, number, owner[ids], centres[ids], areas[ids]
        )
    return _Geometry(
        owner=owner,
        neighbour=neighbour,
        n_internal=n_internal,
        n_cells=n_cells,
        face_centres=centres,
        face_areas=areas,
        cell_centres=cell_centres,
        cell_volumes=volume3 / 3.0,
        cell_face_start=start,
        cell_faces=face_of[order],
        cell_face_sign=sign[order],
        cell_min=cell_min,
        cell_max=cell_max,
        bounds=(points.min(axis=0), points.max(axis=0)),
        patches=patches,
    )


# --------------------------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------------------------


@dataclass
class LineSample:
    """Values along a straight line. ``s``: distance from the start, ``points`` (n, 3),
    ``value`` (n,) or (n, c), ``inside`` (n,) bool, ``cell`` (n,) the containing cell or -1.

    Points that lie outside the mesh have ``inside`` False, ``cell`` -1 and NaN values.
    """

    s: np.ndarray
    points: np.ndarray
    value: np.ndarray
    inside: np.ndarray
    cell: np.ndarray

    def drop_outside(self) -> LineSample:
        """The same sample without the points outside the mesh."""
        keep = self.inside
        return LineSample(
            self.s[keep], self.points[keep], self.value[keep], keep[keep], self.cell[keep]
        )


@dataclass(frozen=True)
class SolverLog:
    """What a solver log says. Residuals are per field (a vector gives ``Ux``, ``Uy``, ``Uz``).

    ``n_time_steps``: the number of ``Time = ...`` lines (for a steady solver, the iterations).
    ``last_time``: the last ``Time`` value; ``time_step``: the difference of the last two
    (``None`` with fewer than two). ``initial_residuals`` / ``first_initial_residuals``: the
    initial residuals of the last / first solve; ``final_residuals``: the final residuals of the
    last solve. ``converged_at``: the iteration of the ``SIMPLE solution converged`` line, else
    ``None`` (a run without ``residualControl`` never prints it). ``completed``: the log ends
    with ``End`` and shows no fatal error or divergence.
    """

    path: Path
    solver: str | None
    n_time_steps: int
    last_time: float | None
    time_step: float | None
    completed: bool
    converged_at: float | None
    initial_residuals: dict[str, float]
    first_initial_residuals: dict[str, float]
    final_residuals: dict[str, float]
    max_linear_iterations: int | None

    def to_dict(self) -> dict[str, Any]:
        """Plain Python values (the path as text), for printing or JSON."""
        data = {name: getattr(self, name) for name in self.__dataclass_fields__}
        data["path"] = str(self.path)
        return data


class FoamCase:
    """A solved case directory. See the module documentation; build it with ``load_case``."""

    def __init__(self, case_dir: Path | str):
        self.path = _check_case_dir(case_dir)
        self._cache: OrderedDict[tuple[str, int, int], _Field] = OrderedDict()

    def __repr__(self) -> str:
        return f"FoamCase({str(self.path)!r})"

    # ------------------------------------------------------------------ times and fields

    @property
    def times(self) -> list[float]:
        """The numeric time directories, ascending (read afresh on every access)."""
        return list_times(self.path)

    @property
    def latest_time(self) -> float:
        return latest_time(self.path)

    def _resolve_time(self, time: float | int | str | None) -> tuple[float, str]:
        entries = _time_entries(self.path)
        if not entries:
            raise FoamResultsError(
                f"no time directory in {self.path}; the solver wrote nothing to read (look at "
                "log.* and logs/solver_run.json)"
            )
        if time is None:
            return entries[-1]
        try:
            wanted = float(time)
        except (TypeError, ValueError) as exc:
            raise FoamResultsError(f"time must be a number or None, not {time!r}") from exc
        for value, name in entries:
            if math.isclose(value, wanted, rel_tol=1e-9, abs_tol=1e-12):
                return value, name
        shown = ", ".join(f"{value:g}" for value, _ in entries)
        raise FoamResultsError(f"no time {time} in {self.path}; available times: {shown}")

    def list_fields(self, time: float | int | str | None = None) -> list[str]:
        """The field files of a time directory (``.gz`` stripped), sorted; default: the latest."""
        _, dirname = self._resolve_time(time)
        names = []
        for entry in os.scandir(self.path / dirname):
            if entry.is_symlink() or not entry.is_file() or entry.name.startswith("."):
                continue
            names.append(entry.name.removesuffix(".gz"))
        return sorted(set(names))

    def _load(self, name: str, time: float | int | str | None) -> _Field:
        _, dirname = self._resolve_time(time)
        if not name or "/" in name or "\\" in name or name.startswith("."):
            raise FoamResultsError(f"{name!r} is not a field name (use a name like 'p' or 'U')")
        directory = self.path / dirname
        for candidate in (directory / name, directory / f"{name}.gz"):
            if candidate.is_symlink():
                raise FoamResultsError(f"{candidate} is a symbolic link; links are not followed")
            if candidate.is_file():
                break
        else:
            available = ", ".join(self.list_fields(time)) or "none"
            raise FoamResultsError(
                f"no field {name!r} at time {time if time is not None else dirname} in "
                f"{directory}; available fields: {available}"
            )
        stat = candidate.stat()
        key = (str(candidate), stat.st_mtime_ns, stat.st_size)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached
        try:
            raw = (
                gzip.decompress(candidate.read_bytes())
                if candidate.suffix == ".gz"
                else candidate.read_bytes()
            )
        except (OSError, EOFError, zlib.error) as exc:
            raise FoamResultsError(f"{candidate} cannot be read: {exc}") from exc
        parsed = _parse_field_text(raw, name, dirname, candidate)
        self._cache[key] = parsed
        while len(self._cache) > _CACHE_SIZE:
            self._cache.popitem(last=False)
        return parsed

    @staticmethod
    def _squeeze(values: np.ndarray) -> np.ndarray:
        return values[:, 0] if values.shape[1] == 1 else values

    def read_field(self, name: str, time: float | int | str | None = None) -> np.ndarray:
        """The internal field ``name`` at ``time`` (default: the latest) as a new array.

        ``(n_cells,)`` for a scalar, ``(n_cells, 3)`` for a vector, ``(n_cells, 6)`` for a
        symmetric tensor. A ``uniform`` field is expanded to all cells. A surface field such as
        ``phi`` has one value per internal face.
        """
        loaded = self._load(name, time)
        geometry = self._geometry
        expected = geometry.n_internal if loaded.location == "surface" else geometry.n_cells
        values = loaded.internal
        if values.ndim == 1:  # uniform
            values = np.tile(values, (expected, 1))
        if len(values) != expected:
            kind = "internal faces" if loaded.location == "surface" else "cells"
            raise FoamResultsError(
                f"field {name!r} has {len(values)} values but the mesh has {expected} {kind}; "
                "the field and the mesh in constant/polyMesh do not belong together"
            )
        return self._squeeze(values.copy())

    def read_patch_field(
        self, name: str, patch: str, time: float | int | str | None = None
    ) -> np.ndarray:
        """The values of field ``name`` on the faces of ``patch`` at ``time`` (default: latest).

        Shape ``(n_faces,)`` / ``(n_faces, 3)``, in the order of ``case.patches[patch]``'s
        ``face_centres``. Uses the ``value`` entry of the patch. A ``zeroGradient`` patch (it
        writes no value) gives the value of the adjacent cell, ``noSlip`` zero. Any other patch
        type without a ``value`` entry, and ``empty`` patches (no values), raise an error; the
        exception is a surface field such as ``phi`` on a ``symmetry``, ``symmetryPlane`` or
        ``wedge`` patch: zero. A ``value`` that is not a literal list (``$internalField``) counts
        as absent.
        """
        patches = self.patches
        if patch not in patches:
            raise FoamResultsError(
                f"no patch {patch!r} in the mesh; the patches are: {', '.join(patches)}"
            )
        loaded = self._load(name, time)
        return self._squeeze(self._patch_values(loaded, patches[patch]))

    def _patch_values(self, loaded: _Field, patch: Patch) -> np.ndarray:
        block = loaded.boundary.get(patch.name)
        kind = block.type if block and block.type else patch.type
        if patch.type == "empty" or kind == "empty":
            raise FoamResultsError(
                f"patch {patch.name!r} is of type empty (the unused direction of a 2D case): it "
                "holds no values"
            )
        if block is None:
            raise FoamResultsError(
                f"the file of field {loaded.name!r} has no entry for patch {patch.name!r}"
            )
        if block.value is None and loaded.location == "surface" and kind in _NO_FLUX_TYPES:
            return np.zeros((patch.n_faces, 1))  # nothing flows through a symmetry-type patch
        if block.value is not None:
            values = block.value
            if values.ndim == 1:
                values = np.tile(values, (patch.n_faces, 1))
            if len(values) != patch.n_faces:
                raise FoamResultsError(
                    f"patch {patch.name!r} of field {loaded.name!r} has {len(values)} values "
                    f"for {patch.n_faces} faces"
                )
            return values.copy()
        if kind == "zeroGradient" and loaded.location == "vol":
            cells = self.read_field(loaded.name, loaded.time_name)
            cells = cells.reshape(len(cells), -1)
            return cells[patch.owner_cells].copy()
        if kind == "noSlip":
            return np.zeros((patch.n_faces, loaded.ncomp or 3))
        raise FoamResultsError(
            f"patch {patch.name!r} of field {loaded.name!r} is of type {kind!r}, which writes no "
            "value; take the value of the adjacent cells instead: read_field(name)"
            f"[case.patches[{patch.name!r}].owner_cells] (for a vector that keeps a normal "
            "component, which a symmetry or slip boundary does not have)"
        )

    # ------------------------------------------------------------------ mesh

    @cached_property
    def _geometry(self) -> _Geometry:
        return _build_geometry(self.path)

    @property
    def n_cells(self) -> int:
        return self._geometry.n_cells

    @property
    def cell_centres(self) -> np.ndarray:
        """Cell centres, ``(n_cells, 3)``, in the order of the field values."""
        return self._geometry.cell_centres.copy()

    @property
    def cell_volumes(self) -> np.ndarray:
        """Cell volumes, ``(n_cells,)``. For a one-cell-thick 2D case: area times the thickness."""
        return self._geometry.cell_volumes.copy()

    @property
    def bounds(self) -> tuple[np.ndarray, np.ndarray]:
        """``(lowest, highest)`` corner of the mesh points, each of shape (3,)."""
        low, high = self._geometry.bounds
        return low.copy(), high.copy()

    @property
    def patches(self) -> dict[str, Patch]:
        """Boundary patches by name, in the order of ``constant/polyMesh/boundary``."""
        return dict(self._geometry.patches)

    @property
    def is_2d(self) -> bool:
        """True when the mesh has an ``empty`` patch (a one-cell-thick 2D case)."""
        return any(patch.type == "empty" for patch in self._geometry.patches.values())

    # ------------------------------------------------------------------ sampling

    def _point(self, point: Any, label: str) -> np.ndarray:
        try:
            values = np.asarray(point, dtype=float).reshape(-1)
        except (TypeError, ValueError) as exc:
            raise FoamResultsError(f"{label} must be a sequence of numbers, got {point!r}") from exc
        if len(values) == 2 and self.is_2d:
            low, high = self.bounds
            values = np.append(values, 0.5 * (low[2] + high[2]))  # the middle of the thickness
        if len(values) != 3 or not np.isfinite(values).all():
            raise FoamResultsError(
                f"{label} must have three coordinates (x, y, z) of finite numbers (two are "
                f"accepted only for a 2D case), got {point!r}"
            )
        return values

    def _locate(self, points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """``(cell index or -1, inside)`` for every point; a point on a face counts as inside."""
        g = self._geometry
        low, high = g.bounds
        tol = _TOLERANCE * max(float(np.linalg.norm(high - low)), 1e-300)
        magnitude = np.maximum(np.linalg.norm(g.face_areas, axis=1), 1e-300)
        cells = np.full(len(points), -1, dtype=np.int64)
        for i, x in enumerate(points):
            candidates = np.nonzero(
                np.all((g.cell_min - tol <= x) & (x <= g.cell_max + tol), axis=1)
            )[0]
            best = math.inf
            for c in candidates:
                faces = g.cell_faces[g.cell_face_start[c] : g.cell_face_start[c + 1]]
                sign = g.cell_face_sign[g.cell_face_start[c] : g.cell_face_start[c + 1]]
                outward = sign * np.einsum(
                    "fj,fj->f", x - g.face_centres[faces], g.face_areas[faces]
                )
                if np.all(outward / magnitude[faces] <= tol):
                    distance = float(np.sum((x - g.cell_centres[c]) ** 2))
                    if distance < best:
                        best, cells[i] = distance, c
        return cells, cells >= 0

    def _boundary_values(self, loaded: _Field, width: int) -> np.ndarray:
        """Patch values per boundary face (NaN where a patch has none), for the gradients."""
        g = self._geometry
        values = np.full((len(g.face_centres) - g.n_internal, width), np.nan)
        for patch in g.patches.values():
            try:
                found = self._patch_values(loaded, patch)
            except FoamResultsError:
                continue
            first = patch.start_face - g.n_internal
            values[first : first + patch.n_faces] = found
        return values

    def _reconstruct(
        self, cells: np.ndarray, points: np.ndarray, values: np.ndarray, boundary: np.ndarray
    ) -> np.ndarray:
        """``value + gradient . (x - centre)`` in each cell; least-squares gradient, exact for
        linear fields. Neighbouring cell centres and the patch values are the data."""
        g = self._geometry
        out = np.empty((len(cells), values.shape[1]))
        for i, (c, x) in enumerate(zip(cells, points, strict=True)):
            first, last = g.cell_face_start[c], g.cell_face_start[c + 1]
            offsets, differences = [], []
            for f, sign in zip(g.cell_faces[first:last], g.cell_face_sign[first:last], strict=True):
                if f < g.n_internal:
                    other = g.neighbour[f] if sign > 0 else g.owner[f]
                    offsets.append(g.cell_centres[other] - g.cell_centres[c])
                    differences.append(values[other] - values[c])
                else:
                    wall = boundary[f - g.n_internal]
                    if np.isnan(wall).any():
                        continue
                    offsets.append(g.face_centres[f] - g.cell_centres[c])
                    differences.append(wall - values[c])
            gradient = np.zeros((3, values.shape[1]))
            if offsets:
                d = np.array(offsets)
                w = d / np.einsum("ij,ij->i", d, d)[:, None]  # weight 1 / |d|^2
                gradient = np.linalg.pinv(w.T @ d, rcond=1e-10) @ (w.T @ np.array(differences))
            out[i] = values[c] + (x - g.cell_centres[c]) @ gradient
        return out

    def _apply_boundary_values(
        self,
        loaded: _Field,
        cells: np.ndarray,
        points: np.ndarray,
        inside: np.ndarray,
        result: np.ndarray,
    ) -> None:
        """A point on a boundary face gets the patch value of that face (when the patch has one)."""
        g = self._geometry
        low, high = g.bounds
        tol = _TOLERANCE * max(float(np.linalg.norm(high - low)), 1e-300)
        patches = list(g.patches.values())
        cache: dict[str, np.ndarray | None] = {}
        for i in np.nonzero(inside)[0]:
            c = cells[i]
            for f in g.cell_faces[g.cell_face_start[c] : g.cell_face_start[c + 1]]:
                if f < g.n_internal:
                    continue
                area = g.face_areas[f]
                if abs(float((points[i] - g.face_centres[f]) @ area)) > tol * np.linalg.norm(area):
                    continue
                patch = next(
                    (p for p in patches if p.start_face <= f < p.start_face + p.n_faces), None
                )
                if patch is None:
                    continue
                if patch.name not in cache:
                    try:
                        cache[patch.name] = self._patch_values(loaded, patch)
                    except FoamResultsError:
                        cache[patch.name] = None
                wall = cache[patch.name]
                if wall is not None:
                    result[i] = wall[f - patch.start_face]
                    break

    def sample_line(
        self,
        name: str,
        start: Any,
        end: Any,
        n_points: int = 101,
        time: float | int | str | None = None,
        method: str = "linear",
    ) -> LineSample:
        """Sample field ``name`` at ``n_points`` equally spaced points from ``start`` to ``end``.

        ``start`` and ``end`` are (x, y, z); (x, y) is accepted for a 2D case and uses the middle
        of the thickness. ``method="linear"`` (default) evaluates the cell value plus a
        least-squares gradient times the offset from the cell centre (exact for linear fields;
        the gradient uses the neighbouring cells and the patch values); ``"nearest"`` returns the
        value of the cell that contains the point. Both are piecewise: the linear result jumps
        across cell faces (each cell has its own gradient) and is not bounded by the cell values.
        A point on a boundary face counts as inside and gets the patch value of that face (its
        ``value`` entry, a ``zeroGradient`` patch's adjacent cell) when the patch has one. Cells
        next to a patch without values (``empty``, symmetry types) have a one-sided stencil there.
        Points outside the mesh get NaN (see ``LineSample.drop_outside``); an end point on a
        boundary that the mesh only approximates may fall outside: take such a value from
        ``read_patch_field`` instead. Raises if no point is inside the mesh.
        """
        if method not in _METHODS:
            raise FoamResultsError(f"method must be one of {_METHODS}, got {method!r}")
        if not isinstance(n_points, int | np.integer) or n_points < 2:
            raise FoamResultsError(f"n_points must be an integer of at least 2, got {n_points!r}")
        p0, p1 = self._point(start, "start"), self._point(end, "end")
        length = float(np.linalg.norm(p1 - p0))
        if length == 0.0:
            raise FoamResultsError("start and end must differ")
        loaded = self._load(name, time)
        if loaded.location != "vol":
            raise FoamResultsError(
                f"field {name!r} is of class {loaded.cls}; only volume fields can be sampled"
            )
        values = self.read_field(name, time)
        flat = values.reshape(len(values), -1)
        fractions = np.linspace(0.0, 1.0, int(n_points))
        points = p0 + fractions[:, None] * (p1 - p0)
        cells, inside = self._locate(points)
        if not inside.any():
            low, high = self.bounds
            raise FoamResultsError(
                f"no point of the line from {tuple(p0)} to {tuple(p1)} lies inside the mesh; "
                f"the mesh bounds are x {low[0]:g}..{high[0]:g}, y {low[1]:g}..{high[1]:g}, "
                f"z {low[2]:g}..{high[2]:g} (a one-cell-thick case is only that thick in z)"
            )
        result = np.full((len(points), flat.shape[1]), np.nan)
        if method == "nearest":
            result[inside] = flat[cells[inside]]
        else:
            boundary = self._boundary_values(loaded, flat.shape[1])
            result[inside] = self._reconstruct(cells[inside], points[inside], flat, boundary)
        self._apply_boundary_values(loaded, cells, points, inside, result)
        shaped = result.reshape((len(points),) + values.shape[1:])
        return LineSample(fractions * length, points, shaped, inside, cells)


def load_case(case_dir: Path | str) -> FoamCase:
    """Open the case directory (the one with ``constant/`` and ``system/``); see ``FoamCase``."""
    return FoamCase(case_dir)


# --------------------------------------------------------------------------------------------
# Solver log
# --------------------------------------------------------------------------------------------


def _summarise_log(path: Path) -> SolverLog:
    summary = parse_solver_log_file(path)
    text = path.read_text(encoding="utf-8", errors="replace")
    exec_match = _EXEC_RE.search(text)
    return SolverLog(
        path=path,
        solver=Path(exec_match.group(1)).name if exec_match else None,
        n_time_steps=len(_STEP_RE.findall(text)),
        last_time=summary.end_time,
        time_step=summary.last_step,
        completed=summary.completed,
        converged_at=summary.converged_at,
        initial_residuals=dict(summary.initial_residuals),
        first_initial_residuals=dict(summary.first_initial_residuals),
        final_residuals=dict(summary.final_residuals),
        max_linear_iterations=summary.max_linear_iterations,
    )


def read_solver_log(path: Path | str) -> SolverLog:
    """Summarise a solver log: a log file, or a case directory (its newest ``log.*`` file with
    linear-solver output and, of those, the one with most time steps; logs of
    utilities such as ``postProcess`` have no linear-solver output).

    Raises ``FoamResultsError`` for a missing file or a directory without a solver log.
    """
    target = Path(path)
    if target.is_symlink():
        raise FoamResultsError(f"{target} is a symbolic link; links are not followed")
    if target.is_file():
        return _summarise_log(target)
    if not target.is_dir():
        raise FoamResultsError(f"{str(target)!r} does not exist")
    logs = sorted(
        (entry for entry in target.glob("log.*") if entry.is_file() and not entry.is_symlink()),
        key=lambda entry: entry.stat().st_mtime_ns,
        reverse=True,
    )
    solver_logs = [log for log in map(_summarise_log, logs) if log.initial_residuals]
    if solver_logs:  # the run with most time steps; of equal ones the newest (the sort is stable)
        return max(solver_logs, key=lambda log: log.n_time_steps)
    found = ", ".join(entry.name for entry in logs) or "no log.* file"
    raise FoamResultsError(
        f"no solver log (a log.* file with 'Solving for' lines) in {target}; found: {found}"
    )
