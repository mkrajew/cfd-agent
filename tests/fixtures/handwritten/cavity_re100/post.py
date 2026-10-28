"""Hand-written post-processing for the lid-driven cavity: ``post.py <case_dir> <quantities.json>``.

Reads the ``sets`` function-object output of the finished run (the latest time directory under
``postProcessing/centrelines``) and writes the two profiles the benchmark asks for:

* ``u_vertical_centreline``: x = y / L along the vertical centreline, value = u / U_lid
* ``v_horizontal_centreline``: x = x / L along the horizontal centreline, value = v / U_lid

Both profiles are sorted by position. Nothing is added to the sampled data: the end points on the
walls come from the sampled field itself (see ``system/controlDict``).
"""

import sys
from pathlib import Path

import numpy as np

from cfdagent.tools.post import latest_time_dir, read_line_sample, write_quantities

LENGTH = 1.0  # side of the cavity [m]
LID_SPEED = 1.0  # speed of the moving wall [m/s]
FUNCTION_OBJECT = "centrelines"  # name of the sets function object in system/controlDict


def centreline_profile(
    case_dir: Path, set_name: str, coordinate: str, component: str
) -> dict[str, list[float]]:
    """Profile of velocity ``component`` along ``coordinate`` (``x`` or ``y``), normalised."""
    set_file = latest_time_dir(case_dir / "postProcessing" / FUNCTION_OBJECT) / f"{set_name}_U.xy"
    columns = read_line_sample(set_file, (coordinate, "Ux", "Uy", "Uz"))
    order = np.argsort(columns[coordinate], kind="stable")
    return {
        "x": (columns[coordinate][order] / LENGTH).tolist(),
        "value": (columns[component][order] / LID_SPEED).tolist(),
    }


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: post.py <case_dir> <quantities.json>", file=sys.stderr)
        return 2
    case_dir, out_path = Path(argv[1]), Path(argv[2])
    quantities = {
        "u_vertical_centreline": centreline_profile(case_dir, "verticalCentreline", "y", "Ux"),
        "v_horizontal_centreline": centreline_profile(case_dir, "horizontalCentreline", "x", "Uy"),
    }
    write_quantities(out_path, quantities)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
