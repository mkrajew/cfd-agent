"""Read the boundary of an ASCII OpenFOAM ``constant/polyMesh`` without OpenFOAM.

Used by the mesh gate to check that every named boundary patch sits where the boundary manifest
says it does. ``points``, ``faces`` and ``boundary`` are parsed in pure Python (with numpy): the
``FoamFile`` header, ``//`` and ``/* */`` comments, the list count prefix and the ``(`` ``)``
framing are handled, and lists may be written inline (``8((0 0 0) ...)``, as OpenFOAM does for
short lists) or one entry per line. A binary file or any malformed content raises ``MeshError``.

The ASCII ``points`` file has 6 significant digits unless the ``controlDict`` that wrote it sets
``writePrecision``; coordinate comparisons therefore carry a floor relative to the coordinate
(``REPR_EPS``, shared with the bounding-box check).
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
_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_COMMENT_BLOCK_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_COMMENT_LINE_RE = re.compile(r"//[^\n]*")
_HEADER_RE = re.compile(r"FoamFile\s*\{([^{}]*)\}")
_FORMAT_RE = re.compile(r"\bformat\s+(\w+)\s*;")
_CLASS_RE = re.compile(r"\bclass\s+(\w+)\s*;")
_POINTS_RE = re.compile(
    rf"\s*(\d+)\s*\(\s*((?:\(\s*{_NUM}\s+{_NUM}\s+{_NUM}\s*\)\s*)*)\)\s*", re.ASCII
)
_VECTOR_RE = re.compile(rf"\(\s*({_NUM})\s+({_NUM})\s+({_NUM})\s*\)")
_FACES_RE = re.compile(r"\s*(\d+)\s*\(\s*((?:\d+\s*\(\s*(?:\d+\s*)+\)\s*)*)\)\s*", re.ASCII)
_FACE_RE = re.compile(r"(\d+)\s*\(([^()]*)\)")
_LIST_START_RE = re.compile(r"\s*(\d+)\s*\(")
_PATCH_BLOCK_RE = re.compile(r"\s*([^\s{}()]+)\s*\{([^{}]*)\}")
_NFACES_RE = re.compile(r"\bnFaces\s+(\d+)\s*;")
_START_FACE_RE = re.compile(r"\bstartFace\s+(\d+)\s*;")


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
    match = _POINTS_RE.fullmatch(_read_body(path, "vectorField"))
    if match is None:
        raise MeshError(f"{path.name} is malformed: expected a count and a list of (x y z) vectors")
    values = _VECTOR_RE.findall(match.group(2))
    if len(values) != int(match.group(1)):
        raise MeshError(
            f"{path.name} declares {match.group(1)} points but contains {len(values)} vectors"
        )
    return np.array(values, dtype=float).reshape(len(values), 3)


def _read_faces(case_dir: Path, n_points: int) -> list[list[int]]:
    path = _polymesh_dir(case_dir) / "faces"
    match = _FACES_RE.fullmatch(_read_body(path, "faceList"))
    if match is None:
        raise MeshError(f"{path.name} is malformed: expected a count and a list of n(a b c ...)")
    faces: list[list[int]] = []
    for size, body in _FACE_RE.findall(match.group(2)):
        indices = [int(v) for v in body.split()]
        if len(indices) != int(size):
            raise MeshError(f"{path.name} has a face declared with {size} points but {indices}")
        if max(indices) >= n_points:
            raise MeshError(f"{path.name} refers to point {max(indices)} but there are {n_points}")
        faces.append(indices)
    if len(faces) != int(match.group(1)):
        raise MeshError(f"{path.name} declares {match.group(1)} faces but contains {len(faces)}")
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
    if re.fullmatch(r"\s*\)\s*", body[pos:]) is None:
        raise MeshError(f"{path.name} is malformed near: {body[pos : pos + 60].strip()!r}")
    if len(patches) != int(start.group(1)):
        raise MeshError(f"{path.name} declares {start.group(1)} patches but defines {len(patches)}")
    return patches


def read_patch_face_centres(case_dir: Path) -> dict[str, np.ndarray]:
    """Face centres of every boundary patch: patch name -> array of shape ``(n_faces, 3)``.

    A centre is the arithmetic mean of the face's vertices. Reads ASCII ``points``, ``faces`` and
    ``boundary`` in ``case_dir/constant/polyMesh``. A missing file raises ``ToolFailure``; a
    binary or malformed file, or a patch that reaches beyond the face list, raises ``MeshError``.
    """
    boundary = read_boundary(case_dir)
    points = _read_points(case_dir)
    faces = _read_faces(case_dir, len(points))
    centres: dict[str, np.ndarray] = {}
    for name, (start, n_faces) in boundary.items():
        if start + n_faces > len(faces):
            raise MeshError(
                f"patch '{name}' covers faces {start} to {start + n_faces - 1} but the mesh "
                f"has {len(faces)} faces"
            )
        centres[name] = np.array(
            [points[face].mean(axis=0) for face in faces[start : start + n_faces]], dtype=float
        ).reshape(n_faces, 3)
    return centres


def _slack(tol: float, bound: float) -> float:
    """Tolerance at a locator coordinate: ``tol``, but at least the 6-digit representation floor."""
    return max(tol, REPR_EPS * abs(bound))


def _contains_mask(locator: Locator, centres: np.ndarray, tol: float) -> np.ndarray:
    if locator.kind == "plane":
        assert locator.axis is not None and locator.value is not None
        axis, value = _AXES[locator.axis], locator.value
        return np.abs(centres[:, axis] - value) <= _slack(tol, value)
    assert locator.box is not None
    mask = np.ones(len(centres), dtype=bool)
    for i in range(3):
        lo, hi = locator.box[i], locator.box[3 + i]
        mask &= (centres[:, i] >= lo - _slack(tol, lo)) & (centres[:, i] <= hi + _slack(tol, hi))
    return mask


def locator_contains(locator: Locator, point: Sequence[float], tol: float) -> bool:
    """Whether ``point`` lies on a plane locator or inside a box locator.

    ``tol`` is absolute; at every locator coordinate the effective tolerance is at least
    ``REPR_EPS * abs(coordinate)``, because mesh points carry about 6 significant digits. NaN is
    never contained.
    """
    centres = np.asarray(point, dtype=float).reshape(1, 3)
    return bool(_contains_mask(locator, centres, tol)[0])


def placement_errors(case_dir: Path, manifest: BoundaryManifest, tol: float) -> list[str]:
    """Check that each manifest patch's faces lie on its locators; return one error per patch.

    Every face centre of a patch must lie on at least one of that patch's locators (within
    ``tol``, see ``locator_contains``). This catches patch names attached to the wrong faces,
    for example inlet and outlet swapped. Manifest patches that are not in the mesh, and patches
    without faces, are skipped (the mesh gate reports them separately).
    """
    centres = read_patch_face_centres(case_dir)
    errors: list[str] = []
    for patch in manifest.patches:
        found = centres.get(patch.name)
        if found is None or len(found) == 0:
            continue
        inside = np.zeros(len(found), dtype=bool)
        for locator in patch.locators:
            inside |= _contains_mask(locator, found, tol)
        outside = np.flatnonzero(~inside)
        if len(outside):
            first = ", ".join(f"{v:.6g}" for v in found[outside[0]])
            errors.append(
                f"patch '{patch.name}': {len(outside)} of {len(found)} face(s) lie on none of the "
                f"patch's locators (first offending face centre: ({first}))"
            )
    return errors
