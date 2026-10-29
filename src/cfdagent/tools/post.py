"""Post-processing helpers: OpenFOAM time directories, "raw" set files and the quantities JSON."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from pathlib import Path

import numpy as np

from cfdagent.quantities import Quantities


def latest_time_dir(func_dir: Path) -> Path:
    """Return the numerically largest time directory inside ``func_dir``.

    ``func_dir`` is typically ``<case>/postProcessing/<functionObject>``. Names such as ``0``,
    ``0.5``, ``1e-05`` and ``100`` are compared by value; files and directories whose name is not a
    finite number are ignored. Raises FileNotFoundError if there is none.
    """
    func_dir = Path(func_dir)
    if not func_dir.is_dir():
        raise FileNotFoundError(f"{func_dir} is not a directory")
    best: tuple[float, Path] | None = None
    for entry in func_dir.iterdir():
        if not entry.is_dir():
            continue
        try:
            value = float(entry.name)
        except ValueError:
            continue
        if not math.isfinite(value):
            continue
        if best is None or value > best[0]:
            best = (value, entry)
    if best is None:
        raise FileNotFoundError(f"no time directory in {func_dir}")
    return best[1]


def read_line_sample(path: Path, columns: Sequence[str]) -> dict[str, np.ndarray]:
    """Read an OpenFOAM "raw" set file (whitespace-separated numbers, ``#`` comment lines).

    ``columns`` names the columns in file order, e.g. ``("y", "Ux", "Uy", "Uz")`` for a vector
    field sampled along a line. Returns one float64 array per name. Raises ValueError for empty or
    duplicate column names, a row whose column count differs from ``len(columns)``, non-numeric or
    non-finite values, or a file without data rows (the line number is in the message).
    """
    names = list(columns)
    if not names or len(set(names)) != len(names):
        raise ValueError(f"columns must be a non-empty list of distinct names, got {names!r}")

    rows: list[list[float]] = []
    text = Path(path).read_text(encoding="utf-8")
    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split()
        if len(fields) != len(names):
            raise ValueError(
                f"{path}, line {line_no}: {len(fields)} columns in the file but "
                f"{len(names)} column names given ({names})"
            )
        try:
            values = [float(f) for f in fields]
        except ValueError as exc:
            raise ValueError(f"{path}, line {line_no}: non-numeric value ({exc})") from exc
        if not all(math.isfinite(v) for v in values):
            raise ValueError(f"{path}, line {line_no}: non-finite value")
        rows.append(values)

    if not rows:
        raise ValueError(f"{path}: no data rows")
    table = np.array(rows, dtype=np.float64)
    return {name: table[:, i].copy() for i, name in enumerate(names)}


def _finite_floats(values, what: str) -> list[float]:
    out = [float(v) for v in values]
    if not all(math.isfinite(v) for v in out):
        raise ValueError(f"{what} contains a non-finite value")
    return out


def write_quantities(path: Path, quantities: Quantities) -> None:
    """Write ``quantities`` as JSON: LF line endings, sorted keys, floats as numbers.

    Scalars are written as floats; a profile (``{"x": [...], "value": [...]}``) as two float lists
    (numpy arrays are accepted). Raises ValueError, writing nothing, for a non-finite value or a
    profile whose lists differ in length.
    """
    out: dict[str, float | dict[str, list[float]]] = {}
    for name, q in quantities.items():
        if isinstance(q, dict):
            profile = {key: _finite_floats(vals, f"{name}.{key}") for key, vals in q.items()}
            if len({len(v) for v in profile.values()}) > 1:
                raise ValueError(f"profile {name!r} has lists of different lengths")
            out[name] = profile
        else:
            (value,) = _finite_floats([q], name)
            out[name] = value

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, indent=2, sort_keys=True, allow_nan=False)
        f.write("\n")
