"""Hand-written post-processing for steady flow past a cylinder: ``post.py <case_dir> <out.json>``.

Writes the two scalars the benchmark asks for:

* ``cd``: the drag coefficient, the last row of the ``Cd`` column (total: pressure plus viscous)
  of the ``forceCoeffs`` function object. The script refuses to write it when the coefficient was
  still changing at the end of the run.
* ``recirculation_length_over_d``: length of the closed recirculation region behind the cylinder
  over the diameter. The streamwise velocity is sampled along the wake centreline from the rear
  surface (x = D / 2) downstream. The velocity is zero on the wall and negative inside the bubble,
  so the end of the bubble is the first zero crossing AFTER the most negative value (a plain
  "first crossing" would find the wall). The crossing is interpolated linearly and counted from
  the rear surface, not from the cylinder centre. If there is no reversed flow, or the sampled
  line ends before it returns to zero, the script stops with an error instead of writing a number.
"""

import re
import sys
from pathlib import Path

import numpy as np

from cfdagent.tools.foam_results import load_case, read_function_object
from cfdagent.tools.post import write_quantities

DIAMETER = 0.1  # [m]; the cylinder centre is the origin
FREE_STREAM = 1.0  # [m/s]
START = 0.5 * DIAMETER + 2e-4  # first sample point, just behind the (polygonal) rear surface
END = 0.8  # last sample point, 8 D behind the cylinder centre: far beyond the bubble
N_POINTS = 1601
MIN_REVERSED = -0.01 * FREE_STREAM  # the bubble must reach at least this reversed speed
LAST_ROWS = 20  # Cd must not change by more than TOLERANCE over this many last iterations
TOLERANCE = 1e-4


def drag_coefficient(case_dir: Path) -> float:
    """Last ``Cd`` of the force coefficients; raises if it was still moving."""
    table = read_function_object(case_dir, "forceCoeffs", file="coefficient")
    cd = table.column("Cd")
    if len(cd) < LAST_ROWS:
        raise RuntimeError(f"only {len(cd)} rows of force coefficients, need {LAST_ROWS}")
    drift = abs(float(np.mean(cd[-LAST_ROWS:])) - float(cd[-1]))
    if drift > TOLERANCE:
        raise RuntimeError(f"Cd is not converged: it moved by {drift:.2e} in the last {LAST_ROWS}")
    return float(cd[-1])


def check_reference_area(case_dir: Path, thickness: float) -> None:
    """Aref must be D * thickness: the force is computed on the slab the mesh is thick."""
    text = re.sub(r"//.*", "", (case_dir / "system" / "controlDict").read_text(encoding="utf-8"))
    values = {}
    for name in ("Aref", "lRef"):
        match = re.search(rf"\b{name}\s+([0-9.eE+-]+)\s*;", text)
        if match is None:
            raise RuntimeError(f"no {name} in the forceCoeffs entry of system/controlDict")
        values[name] = float(match.group(1))
    # the mesh points are written with six digits, so the thickness is only that exact
    if abs(values["lRef"] - DIAMETER) > 1e-9 or abs(values["Aref"] / DIAMETER - thickness) > 1e-5:
        raise RuntimeError(
            f"lRef {values['lRef']} / Aref {values['Aref']} do not match D = {DIAMETER} and "
            f"thickness {thickness}"
        )


def recirculation_length_over_d(case_dir: Path) -> float:
    """Length of the reversed-flow region on the centreline, from the rear surface, over D."""
    case = load_case(case_dir)
    low, high = case.bounds
    z_mid = 0.5 * (low[2] + high[2])
    line = case.sample_line("U", (START, 0.0, z_mid), (END, 0.0, z_mid), n_points=N_POINTS)
    line = line.drop_outside()
    x, ux = line.points[:, 0], line.value[:, 0]
    order = np.argsort(x, kind="stable")
    x, ux = x[order], ux[order]
    if ux.min() > MIN_REVERSED:
        raise RuntimeError(f"no reversed flow on the centreline (min Ux = {ux.min():.4g})")
    deepest = int(np.argmin(ux))
    after = np.nonzero(ux[deepest:] >= 0.0)[0]
    if len(after) == 0:
        raise RuntimeError(f"Ux is still negative at x = {x[-1]:g}: extend the sampled line")
    j = deepest + int(after[0])  # first sample at or after the minimum with Ux >= 0
    x0 = x[j - 1] + (x[j] - x[j - 1]) * (0.0 - ux[j - 1]) / (ux[j] - ux[j - 1])
    return float((x0 - 0.5 * DIAMETER) / DIAMETER)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: post.py <case_dir> <quantities.json>", file=sys.stderr)
        return 2
    case_dir, out_path = Path(argv[1]), Path(argv[2])
    low, high = load_case(case_dir).bounds
    check_reference_area(case_dir, float(high[2] - low[2]))
    quantities = {
        "cd": drag_coefficient(case_dir),
        "recirculation_length_over_d": recirculation_length_over_d(case_dir),
    }
    write_quantities(out_path, quantities)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
