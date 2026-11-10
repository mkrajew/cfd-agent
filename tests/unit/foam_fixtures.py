"""Writers of small ASCII OpenFOAM cases for the unit tests of ``cfdagent.tools.foam_results``.

``write_box_case`` makes a structured hexahedral mesh in the OpenFOAM layout (upper-triangular
face order, boundary faces last, normals pointing out of the owner) without OpenFOAM; ``warp``
turns the unit grid into a graded or sheared one. ``write_field`` writes a ``volXField`` file.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

import numpy as np

HEADER = """FoamFile
{{
    version     2.0;
    format      {fmt};
    class       {cls};
{note}    location    "{location}";
    object      {obj};
}}

"""

Warp = Callable[[np.ndarray], np.ndarray]


def _write(path: Path, header: str, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + body, encoding="utf-8", newline="\n")


def _numbers(array: np.ndarray) -> str:
    return " ".join(repr(float(v)) for v in np.atleast_1d(array))


def write_box_case(
    root: Path,
    shape: Sequence[int] = (4, 3, 1),
    size: Sequence[float] = (2.0, 1.0, 0.1),
    warp: Warp | None = None,
    patch_names: Sequence[str] = ("left", "right", "bottom", "top", "frontAndBack"),
) -> Path:
    """Write ``root/case/constant/polyMesh`` for a ``shape = (nx, ny, nz)`` box of ``size``.

    ``warp`` maps the (n, 3) array of points to new points (a graded or sheared mesh). Patches:
    ``left`` (x min), ``right`` (x max), ``bottom`` (y min), ``top`` (y max), ``frontAndBack``
    (z min and z max, type ``empty`` when ``nz == 1``, else ``patch``).
    """
    nx, ny, nz = shape
    case = Path(root) / "case"
    px, py = nx + 1, ny + 1

    def point(i: int, j: int, k: int) -> int:
        return i + px * (j + py * k)

    coords = np.array(
        [
            (i * size[0] / nx, j * size[1] / ny, k * size[2] / nz)
            for k in range(nz + 1)
            for j in range(ny + 1)
            for i in range(nx + 1)
        ]
    )
    if warp is not None:
        coords = warp(coords)

    internal: list[tuple[int, int, list[int]]] = []  # (owner, neighbour, vertices)
    boundary: dict[str, list[tuple[int, list[int]]]] = {name: [] for name in patch_names}
    left, right, bottom, top, front_back = patch_names
    for k in range(nz):
        for j in range(ny):
            for i in range(nx):
                c = i + nx * (j + ny * k)
                p = point
                if i + 1 < nx:
                    internal.append(
                        (
                            c,
                            c + 1,
                            [p(i + 1, j, k), p(i + 1, j + 1, k), p(i + 1, j + 1, k + 1)]
                            + [p(i + 1, j, k + 1)],
                        )
                    )
                if j + 1 < ny:
                    internal.append(
                        (
                            c,
                            c + nx,
                            [p(i, j + 1, k), p(i, j + 1, k + 1), p(i + 1, j + 1, k + 1)]
                            + [p(i + 1, j + 1, k)],
                        )
                    )
                if k + 1 < nz:
                    internal.append(
                        (
                            c,
                            c + nx * ny,
                            [p(i, j, k + 1), p(i + 1, j, k + 1), p(i + 1, j + 1, k + 1)]
                            + [p(i, j + 1, k + 1)],
                        )
                    )
                if i == 0:
                    boundary[left].append(
                        (c, [p(0, j, k), p(0, j, k + 1), p(0, j + 1, k + 1), p(0, j + 1, k)])
                    )
                if i == nx - 1:
                    boundary[right].append(
                        (c, [p(nx, j, k), p(nx, j + 1, k), p(nx, j + 1, k + 1), p(nx, j, k + 1)])
                    )
                if j == 0:
                    boundary[bottom].append(
                        (c, [p(i, 0, k), p(i + 1, 0, k), p(i + 1, 0, k + 1), p(i, 0, k + 1)])
                    )
                if j == ny - 1:
                    boundary[top].append(
                        (c, [p(i, ny, k), p(i, ny, k + 1), p(i + 1, ny, k + 1), p(i + 1, ny, k)])
                    )
                if k == 0:
                    boundary[front_back].append(
                        (c, [p(i, j, 0), p(i, j + 1, 0), p(i + 1, j + 1, 0), p(i + 1, j, 0)])
                    )
                if k == nz - 1:
                    boundary[front_back].append(
                        (c, [p(i, j, nz), p(i + 1, j, nz), p(i + 1, j + 1, nz), p(i, j + 1, nz)])
                    )

    faces = [v for _, _, v in internal]
    owner = [o for o, _, _ in internal]
    neighbour = [n for _, n, _ in internal]
    patch_lines: list[str] = []
    for name, items in boundary.items():
        kind = "empty" if name == front_back and nz == 1 else "patch"
        patch_lines.append(
            f"    {name}\n    {{\n        type {kind};\n"
            f"        nFaces {len(items)};\n        startFace {len(faces)};\n    }}\n"
        )
        for cell, vertices in items:
            owner.append(cell)
            faces.append(vertices)

    poly = case / "constant" / "polyMesh"
    loc = "constant/polyMesh"
    note = f'    note        "nPoints:{len(coords)}  nCells:{nx * ny * nz}";\n'

    def head(cls, obj, with_note=False):
        return HEADER.format(
            fmt="ascii", cls=cls, obj=obj, location=loc, note=note if with_note else ""
        )

    points_body = f"{len(coords)}\n(\n" + "".join(f"({_numbers(c)})\n" for c in coords) + ")\n"
    _write(poly / "points", head("vectorField", "points"), points_body)
    faces_body = (
        f"{len(faces)}\n(\n"
        + "".join(f"{len(v)}({' '.join(map(str, v))})\n" for v in faces)
        + ")\n"
    )
    _write(poly / "faces", head("faceList", "faces"), faces_body)
    owner_body = f"{len(owner)}\n(\n" + "".join(f"{o}\n" for o in owner) + ")\n"
    _write(poly / "owner", head("labelList", "owner", True), owner_body)
    if neighbour:
        neighbour_body = f"{len(neighbour)}\n(\n" + "".join(f"{n}\n" for n in neighbour) + ")\n"
    else:
        neighbour_body = "0()\n"
    _write(poly / "neighbour", head("labelList", "neighbour", True), neighbour_body)
    _write(
        poly / "boundary",
        head("polyBoundaryMesh", "boundary"),
        f"{len(boundary)}\n(\n" + "".join(patch_lines) + ")\n",
    )
    return case


def _value_text(values: np.ndarray, kind: str = "nonuniform") -> str:
    values = np.asarray(values, dtype=float)
    if kind == "uniform":
        if values.ndim == 0:
            return f"uniform {float(values)!r}"
        return f"uniform ({_numbers(values)})"
    n = len(values)
    if values.ndim == 1:
        rows = "".join(f"{v!r}\n" for v in values.tolist())
        return f"nonuniform List<scalar>\n{n}\n(\n{rows})\n"
    rows = "".join(f"({_numbers(v)})\n" for v in values)
    return f"nonuniform List<vector>\n{n}\n(\n{rows})\n"


def write_field(
    case: Path,
    time: str,
    name: str,
    internal,
    boundary: dict[str, tuple[str, object | None]],
    cls: str | None = None,
    fmt: str = "ascii",
    uniform: bool = False,
    compact: bool = False,
) -> Path:
    """Write ``case/<time>/<name>``. ``internal``: values per cell (1-D scalar, 2-D vector).

    ``boundary`` maps a patch to ``(type, value)``; a ``None`` value writes no ``value`` entry. A
    value is an array (nonuniform) or ``("uniform", number-or-vector)``. ``uniform`` writes the
    internal field as ``uniform <first value>``; ``compact`` as ``nonuniform List<..> N{v}``.
    """
    values = np.asarray(internal, dtype=float)
    cls = cls or ("volScalarField" if values.ndim == 1 else "volVectorField")
    if uniform:
        field = _value_text(values[0], "uniform")
    elif compact:
        kind = "scalar" if values.ndim == 1 else "vector"
        one = repr(float(values[0])) if values.ndim == 1 else f"({_numbers(values[0])})"
        field = f"nonuniform List<{kind}> {len(values)}{{{one}}}"
    else:
        field = _value_text(values)
    patches = ""
    for patch, (ptype, value) in boundary.items():
        entry = f"        type {ptype};\n"
        if value is not None:
            if isinstance(value, tuple) and value[0] == "uniform":
                entry += f"        value {_value_text(np.asarray(value[1]), 'uniform')};\n"
            else:
                entry += f"        value {_value_text(np.asarray(value))};\n"
        patches += f"    {patch}\n    {{\n{entry}    }}\n"
    body = f"dimensions [0 1 -1 0 0 0 0];\n\ninternalField {field};\n\n"
    body += f"boundaryField\n{{\n{patches}}}\n"
    path = Path(case) / time / name
    _write(path, HEADER.format(fmt=fmt, cls=cls, obj=name, location=time, note=""), body)
    return path
