"""Hand-written post-processing for the pipe: ``post.py [<case_dir> <out.json>]``.

Without arguments it reads ``case`` and writes ``quantities.json`` in the working directory, which
is how the Analyst stage runs a post-processing script.

Reads the fields of the last written time with ``cfdagent.tools.foam_results`` (no OpenFOAM
needed) and writes the two quantities the benchmark asks for:

* ``u_profile_fully_developed``: x = r / R, value = u_axial / U_mean at x = 0.45 m
* ``pressure_drop_fully_developed``: area-averaged pressure at x = 0.35 m minus that at
  x = 0.45 m, in Pa

Two details of the wedge. The mesh is one flat-faced cell through the wedge angle, so on the
mid-plane its wall is at y = R cos(angle / 2), not at R (0.095 % less for 5 degrees); r / R is
formed with that radius, so the profile runs from exactly 0 on the axis to exactly 1 on the wall.
And the solver's pressure is the kinematic pressure p / rho in m^2/s^2: the pressure in Pa is
that value times the density of the task (1000 kg/m^3), which is not in the OpenFOAM case.
"""

import math
import sys
from pathlib import Path

import numpy as np

from cfdagent.tools.foam_results import load_case
from cfdagent.tools.post import write_quantities

RADIUS = 0.005  # pipe radius R [m]
WEDGE_ANGLE_DEG = 5.0  # as in geometry.py
MEAN_VELOCITY = 0.05  # [m/s]
DENSITY = 1000.0  # [kg/m^3], from the task statement
X_PROFILE = 0.45  # cross-section of the profile [m]
X_UPSTREAM, X_DOWNSTREAM = 0.35, 0.45  # cross-sections of the pressure drop [m]
N_POINTS = 201
DEFAULT_PATHS = (Path("case"), Path("quantities.json"))  # relative to the working directory

# Wall of the mesh on the mid-plane (z = 0), where the radial lines below run.
WALL_Y = RADIUS * math.cos(math.radians(WEDGE_ANGLE_DEG) / 2)


def radial_line(case, field: str, x: float):
    """``field`` along the mid-plane line from the axis to the wall at axial position ``x``."""
    sample = case.sample_line(field, (x, 0.0, 0.0), (x, WALL_Y, 0.0), n_points=N_POINTS)
    if not sample.inside.all():
        raise RuntimeError(f"{field} at x = {x}: points of the radial line lie outside the mesh")
    return sample.points[:, 1], sample.value


def area_average(radius: np.ndarray, values: np.ndarray) -> float:
    """Average over the cross-section of a wedge: the weight of a radius is r (dA = r dr dtheta)."""
    weight = radius * values
    integral = np.sum(0.5 * (weight[1:] + weight[:-1]) * np.diff(radius))
    return float(integral / (0.5 * (radius[-1] ** 2 - radius[0] ** 2)))


def main(argv: list[str]) -> int:
    if len(argv) not in (1, 3):
        print("usage: post.py [<case_dir> <quantities.json>]", file=sys.stderr)
        return 2
    case_dir, out_path = (Path(argv[1]), Path(argv[2])) if len(argv) == 3 else DEFAULT_PATHS
    case = load_case(case_dir)

    radius, velocity = radial_line(case, "U", X_PROFILE)
    profile = {"x": (radius / WALL_Y).tolist(), "value": (velocity[:, 0] / MEAN_VELOCITY).tolist()}

    mean_p = {}
    for x in (X_UPSTREAM, X_DOWNSTREAM):
        radius, kinematic_p = radial_line(case, "p", x)
        mean_p[x] = area_average(radius, kinematic_p) * DENSITY  # kinematic pressure -> Pa
    quantities = {
        "u_profile_fully_developed": profile,
        "pressure_drop_fully_developed": mean_p[X_UPSTREAM] - mean_p[X_DOWNSTREAM],
    }
    write_quantities(out_path, quantities)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
