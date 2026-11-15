"""Hand-written post-processing for vortex shedding.

Usage: ``post.py [<case_dir> <out.json>]``; without arguments ``case`` and ``quantities.json`` in
the working directory.

Writes the two scalars the benchmark asks for, both from the ``forceCoeffs`` record that the
solver wrote at every time step:

* ``strouhal``: St = f D / U, with f the dominant frequency of the LIFT coefficient ``Cl``. (The
  drag oscillates at twice the shedding frequency, so the drag record would give 2 St.)
* ``cd_mean``: the mean of ``Cd`` (total: pressure plus viscous) over a whole number of shedding
  periods.

Both use the same analysis window: the second half of the record (the first half holds the
start-up and the growth of the instability). The script refuses to write numbers, and stops with
an error, when the window is not a developed periodic oscillation:

* fewer than ``MIN_PERIODS`` shedding periods in the window,
* the lift amplitude (half the peak-to-peak value) is below ``MIN_AMPLITUDE``: the wake is not
  shedding, a "frequency" of noise would be meaningless,
* the lift amplitude of the first and the second half of the window differs by more than
  ``MAX_AMPLITUDE_CHANGE``: the oscillation is still growing or decaying.

It also checks that the reference area of the force coefficients matches the thickness of the mesh.
"""

import re
import sys
from pathlib import Path

import numpy as np

from cfdagent.tools.foam_results import (
    dominant_frequency,
    load_case,
    period_average,
    read_function_object,
)
from cfdagent.tools.post import write_quantities

DIAMETER = 0.1  # [m]
FREE_STREAM = 1.0  # [m/s]
SKIP_FRACTION = 0.5  # the first half of the record is not analysed
MIN_PERIODS = 10  # shedding periods required in the analysed window
MIN_AMPLITUDE = 0.1  # lift amplitude below which the wake is not shedding
MAX_AMPLITUDE_CHANGE = 0.02  # relative change of the lift amplitude between the window halves
DEFAULT_PATHS = (Path("case"), Path("quantities.json"))  # relative to the working directory


def amplitude(values: np.ndarray) -> float:
    """Half the peak-to-peak value of an oscillation."""
    return 0.5 * float(np.max(values) - np.min(values))


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


def shedding_quantities(case_dir: Path) -> dict[str, float]:
    """Strouhal number and mean drag coefficient from the force coefficient record."""
    table = read_function_object(case_dir, "forceCoeffs", file="coefficient")
    t, cd, cl = table.column("Time"), table.column("Cd"), table.column("Cl")

    frequency = dominant_frequency(t, cl, skip_fraction=SKIP_FRACTION)  # of Cl, not of Cd
    window = t >= t[0] + SKIP_FRACTION * (t[-1] - t[0])  # the window dominant_frequency used
    t_w, cd_w, cl_w = t[window], cd[window], cl[window]

    periods = frequency * (t_w[-1] - t_w[0])
    if periods < MIN_PERIODS:
        raise RuntimeError(
            f"only {periods:.1f} shedding periods in the analysed window, need {MIN_PERIODS}: "
            "run longer"
        )
    if amplitude(cl_w) < MIN_AMPLITUDE:
        raise RuntimeError(
            f"the lift amplitude {amplitude(cl_w):.3g} is below {MIN_AMPLITUDE}: no vortex shedding"
        )
    half = len(t_w) // 2
    first, second = amplitude(cl_w[:half]), amplitude(cl_w[half:])
    change = abs(second - first) / max(first, second)
    if change > MAX_AMPLITUDE_CHANGE:
        raise RuntimeError(
            f"the lift amplitude is not stationary: {first:.4g} in the first and {second:.4g} in "
            f"the second half of the window ({100 * change:.1f} % change): run longer"
        )

    # whole shedding periods inside the window, ending at the last time
    cd_mean = period_average(t_w, cd_w, frequency)
    return {"strouhal": frequency * DIAMETER / FREE_STREAM, "cd_mean": cd_mean}


def main(argv: list[str]) -> int:
    if len(argv) not in (1, 3):
        print("usage: post.py [<case_dir> <quantities.json>]", file=sys.stderr)
        return 2
    case_dir, out_path = (Path(argv[1]), Path(argv[2])) if len(argv) == 3 else DEFAULT_PATHS
    low, high = load_case(case_dir).bounds
    check_reference_area(case_dir, float(high[2] - low[2]))
    write_quantities(out_path, shedding_quantities(case_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
