"""Read the boundary of an ASCII OpenFOAM ``constant/polyMesh`` without OpenFOAM.

Used by the mesh gate to check that every named boundary patch sits where the boundary manifest
says it does. ``points``, ``faces`` and ``boundary`` are parsed in pure Python (with numpy): the
``FoamFile`` header, ``//`` and ``/* */`` comments, the list count prefix and the ``(`` ``)``
framing are handled, and lists may be written inline (``8((0 0 0) ...)``, as OpenFOAM does for
short lists) or one entry per line. A binary file or any malformed content raises ``MeshError``.

Parsing is linear in the file size: the lists are scanned entry by entry with patterns that cannot
backtrack (possessive quantifiers), never with one pattern over the whole file, so a truncated or
garbled file is rejected immediately instead of hanging the gate.

Precision: gmshToFoam writes points with far more than 6 significant digits, but the bounding box
that checkMesh prints has 6 (see ``REPR_EPS`` in ``tools/bbox.py``). Coordinate comparisons
against locators keep the same floor relative to the coordinate, which is conservative for the
points themselves.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

import numpy as np

from cfdagent.errors import AgentInputError, ToolFailure
from cfdagent.manifest import BoundaryManifest, Locator
from cfdagent.tools.bbox import REPR_EPS

_AXES = {"x": 0, "y": 1, "z": 2}
# Possessive quantifiers (``++``, ``*+``, ``?+``) make every pattern below match without
# backtracking, so scanning is linear even on malformed input.
_NUM = r"[-+]?+(?:\d++(?:\.\d*+)?+|\.\d++)(?:[eE][-+]?+\d++)?+"
_COMMENT_BLOCK_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_COMMENT_LINE_RE = re.compile(r"//[^\n]*+")
_HEADER_RE = re.compile(r"FoamFile\s*+\{([^{}]*+)\}")
_FORMAT_RE = re.compile(r"\bformat\s++(\w++)\s*+;")
_CLASS_RE = re.compile(r"\bclass\s++(\w++)\s*+;")
_LIST_START_RE = re.compile(r"\s*+(\d++)\s*+\(")
_VECTOR_RE = re.compile(rf"\s*+\(\s*+({_NUM})\s++({_NUM})\s++({_NUM})\s*+\)")
_FACE_RE = re.compile(r"\s*+(\d++)\s*+\(\s*+((?:\d++\s*+)++)\)")
_LIST_END_RE = re.compile(r"\s*+\)\s*+")
_PATCH_BLOCK_RE = re.compile(r"\s*+([^\s{}()]++)\s*+\{([^{}]*+)\}")
_NFACES_RE = re.compile(r"\bnFaces\s++(\d++)\s*+;")
_START_FACE_RE = re.compile(r"\bstartFace\s++(\d++)\s*+;")


class MeshError(AgentInputError):
    """A mesh-stage step could not be carried out (not a gate verdict)."""


class PatchRange(NamedTuple):
    """The faces of one boundary patch: ``faces[start_face : start_face + n_faces]``."""

    start_face: int
    n_faces: int


def _polymesh_dir(case_dir: Path) -> Path:
    return Path(case_dir) / "constant" / "polyMesh"


def _read_body(path: Path, expected_class: str) -> str:
    """Return the text of a polyMesh file after its ``FoamFile`` header, comments removed.

    A missing or unreadable file raises ``ToolFailure`` (our own conversion step writes it); a
    binary, wrongly typed or headerless file raises ``MeshError``.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ToolFailure(f"{path} does not exist; run convert_to_foam first") from exc
    except UnicodeDecodeError as exc:
        raise MeshError(f"{path.name} is not an ASCII OpenFOAM file (binary content)") from exc
    except OSError as exc:
        raise ToolFailure(f"{path} could not be read: {exc}") from exc
    text = _COMMENT_LINE_RE.sub(" ", _COMMENT_BLOCK_RE.sub(" ", raw))
    header = _HEADER_RE.search(text)
    if header is None:
        raise MeshError(f"{path.name} has no FoamFile header")
    fmt = _FORMAT_RE.search(header.group(1))
    if fmt is None or fmt.group(1) != "ascii":
        shown = fmt.group(1) if fmt else "unknown"
        raise MeshError(
            f"{path.name} is in {shown} format (binary files are not supported); "
            "the mesh must be written in ASCII"
        )
    cls = _CLASS_RE.search(header.group(1))
    if cls is None or cls.group(1) != expected_class:
        shown = cls.group(1) if cls else "none"
        raise MeshError(f"{path.name} has class {shown}, expected {expected_class}")
    return text[header.end() :]


def _read_points(case_dir: Path) -> np.ndarray:
    path = _polymesh_dir(case_dir) / "points"
    body = _read_body(path, "vectorField")
    start = _LIST_START_RE.match(body)
    if start is None:
        raise MeshError(f"{path.name} is malformed: expected a point count and '('")
    values: list[tuple[str, str, str]] = []
    pos = start.end()
    while vector := _VECTOR_RE.match(body, pos):
        values.append(vector.groups())  # type: ignore[arg-type]
        pos = vector.end()
    if _LIST_END_RE.fullmatch(body, pos) is None:
        raise MeshError(
            f"{path.name} is malformed after {len(values)} points: expected '(x y z)' or ')' "
            f"near {body[pos : pos + 40].strip()!r}"
        )
    if len(values) != int(start.group(1)):
        raise MeshError(f"{path.name} declares {start.group(1)} points but contains {len(values)}")
    return np.array(values, dtype=float).reshape(len(values), 3)


def _read_faces(case_dir: Path, n_points: int) -> list[list[int]]:
    path = _polymesh_dir(case_dir) / "faces"
    body = _read_body(path, "faceList")
    start = _LIST_START_RE.match(body)
    if start is None:
        raise MeshError(f"{path.name} is malformed: expected a face count and '('")
    faces: list[list[int]] = []
    pos = start.end()
    while face := _FACE_RE.match(body, pos):
        indices = [int(v) for v in face.group(2).split()]
        if len(indices) != int(face.group(1)):
            raise MeshError(
                f"{path.name} has a face declared with {face.group(1)} points but {indices}"
            )
        if max(indices) >= n_points:
            raise MeshError(f"{path.name} refers to point {max(indices)} but there are {n_points}")
        faces.append(indices)
        pos = face.end()
    if _LIST_END_RE.fullmatch(body, pos) is None:
        raise MeshError(
            f"{path.name} is malformed after {len(faces)} faces: expected 'n(a b c ...)' or ')' "
            f"near {body[pos : pos + 40].strip()!r}"
        )
    if len(faces) != int(start.group(1)):
        raise MeshError(f"{path.name} declares {start.group(1)} faces but contains {len(faces)}")
    return faces


def read_boundary(case_dir: Path) -> dict[str, PatchRange]:
    """Patch name -> face range from ``constant/polyMesh/boundary``, in file order.

    Raises ``MeshError`` for a malformed file or a patch without ``nFaces`` or ``startFace``.
    """
    path = _polymesh_dir(case_dir) / "boundary"
    body = _read_body(path, "polyBoundaryMesh")
    start = _LIST_START_RE.match(body)
    if start is None:
        raise MeshError(f"{path.name} is malformed: expected a patch count and '('")
    patches: dict[str, PatchRange] = {}
    pos = start.end()
    while block := _PATCH_BLOCK_RE.match(body, pos):
        name, content = block.groups()
        n_faces = _NFACES_RE.search(content)
        if n_faces is None:
            raise MeshError(f"patch '{name}' in {path} has no nFaces entry")
        start_face = _START_FACE_RE.search(content)
        if start_face is None:
            raise MeshError(f"patch '{name}' in {path} has no startFace entry")
        if name in patches:
            raise MeshError(f"patch '{name}' is defined twice in {path}")
        patches[name] = PatchRange(int(start_face.group(1)), int(n_faces.group(1)))
        pos = block.end()
    if _LIST_END_RE.fullmatch(body, pos) is None:
        raise MeshError(f"{path.name} is malformed near: {body[pos : pos + 60].strip()!r}")
    if len(patches) != int(start.group(1)):
        raise MeshError(f"{path.name} declares {start.group(1)} patches but defines {len(patches)}")
    return patches


class _FaceGeometry(NamedTuple):
    """Per face of a patch: centre, and the smallest and largest vertex coordinate per axis."""

    centres: np.ndarray  # (n_faces, 3)
    mins: np.ndarray  # (n_faces, 3)
    maxs: np.ndarray  # (n_faces, 3)


def _patch_geometry(case_dir: Path) -> dict[str, _FaceGeometry]:
    boundary = read_boundary(case_dir)
    points = _read_points(case_dir)
    faces = _read_faces(case_dir, len(points))
    result: dict[str, _FaceGeometry] = {}
    for name, (start, n_faces) in boundary.items():
        if start + n_faces > len(faces):
            raise MeshError(
                f"patch '{name}' covers faces {start} to {start + n_faces - 1} but the mesh "
                f"has {len(faces)} faces"
            )
        vertices = [points[face] for face in faces[start : start + n_faces]]
        result[name] = _FaceGeometry(
            np.array([v.mean(axis=0) for v in vertices], dtype=float).reshape(n_faces, 3),
            np.array([v.min(axis=0) for v in vertices], dtype=float).reshape(n_faces, 3),
            np.array([v.max(axis=0) for v in vertices], dtype=float).reshape(n_faces, 3),
        )
    return result


def read_patch_face_centres(case_dir: Path) -> dict[str, np.ndarray]:
    """Face centres of every boundary patch: patch name -> array of shape ``(n_faces, 3)``.

    A centre is the arithmetic mean of the face's vertices. Reads ASCII ``points``, ``faces`` and
    ``boundary`` in ``case_dir/constant/polyMesh``. A missing file raises ``ToolFailure``; a
    binary or malformed file, or a patch that reaches beyond the face list, raises ``MeshError``.
    """
    return {name: geometry.centres for name, geometry in _patch_geometry(case_dir).items()}


def _slack(tol: float, bound: float) -> float:
    """Tolerance at a locator coordinate: ``tol``, but at least the representation floor."""
    return max(tol, REPR_EPS * abs(bound))


def _contains_mask(locator: Locator, mins: np.ndarray, maxs: np.ndarray, tol: float) -> np.ndarray:
    """Which faces (given by their per-axis minimum and maximum vertex coordinates) fit."""
    if locator.kind == "plane":
        assert locator.axis is not None and locator.value is not None
        axis, value = _AXES[locator.axis], locator.value
        slack = _slack(tol, value)
        return (np.abs(mins[:, axis] - value) <= slack) & (np.abs(maxs[:, axis] - value) <= slack)
    assert locator.box is not None
    mask = np.ones(len(mins), dtype=bool)
    for i in range(3):
        lo, hi = locator.box[i], locator.box[3 + i]
        mask &= (mins[:, i] >= lo - _slack(tol, lo)) & (maxs[:, i] <= hi + _slack(tol, hi))
    return mask


def locator_contains(locator: Locator, point: Sequence[float], tol: float) -> bool:
    """Whether ``point`` lies on a plane locator or inside a box locator.

    ``tol`` is absolute; at every locator coordinate the effective tolerance is at least
    ``REPR_EPS * abs(coordinate)``, a conservative allowance for 6-digit printed coordinates.
    NaN is never contained.
    """
    p = np.asarray(point, dtype=float).reshape(1, 3)
    return bool(_contains_mask(locator, p, p, tol)[0])


def describe_locator(locator: Locator) -> str:
    """Short text for an error message, e.g. ``plane x=0`` or ``box (0, 0, 0, 1, 1, 1)``."""
    if locator.kind == "plane":
        return f"plane {locator.axis}={locator.value:g}"
    assert locator.box is not None
    return "box (" + ", ".join(f"{v:g}" for v in locator.box) + ")"


def placement_errors(case_dir: Path, manifest: BoundaryManifest, tol: float) -> list[str]:
    """Check that each manifest patch's faces lie on its locators; return one error per patch.

    Locator semantics are those of the manifest: a face belongs to a ``plane`` locator if its
    bounding box lies on the plane, and to a ``box`` locator if its bounding box lies inside the
    box (within ``tol``, see ``locator_contains``). Every face of a patch must satisfy at least
    one of the patch's locators. This catches patch names attached to the wrong faces, for
    example inlet and outlet swapped. Manifest patches that are not in the mesh, and patches
    without faces, are skipped (the mesh gate reports them separately).
    """
    geometry = _patch_geometry(case_dir)
    errors: list[str] = []
    for patch in manifest.patches:
        found = geometry.get(patch.name)
        if found is None or len(found.centres) == 0:
            continue
        inside = np.zeros(len(found.centres), dtype=bool)
        for locator in patch.locators:
            inside |= _contains_mask(locator, found.mins, found.maxs, tol)
        outside = np.flatnonzero(~inside)
        if len(outside):
            first = ", ".join(f"{v:.6g}" for v in found.centres[outside[0]])
            locators = "; ".join(describe_locator(loc) for loc in patch.locators)
            errors.append(
                f"patch '{patch.name}': {len(outside)} of {len(found.centres)} face(s) do not lie "
                f"on any of the patch's locators [{locators}] (first offending face centre: "
                f"({first})). The faces named '{patch.name}' are not where the manifest places "
                "them: patch names swapped or wrong surfaces in the physical group. Assign "
                "physical groups from the manifest locators; a curved patch needs a 'box' locator."
            )
    return errors
