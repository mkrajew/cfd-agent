"""Generate the reference data of the pipe-re500 benchmark from the Hagen-Poiseuille solution.

Fully developed laminar flow in a circular pipe of diameter D and radius R = D / 2, with mean
velocity U_mean:

    u(r) = 2 * U_mean * (1 - (r / R)**2)         (profile, centreline velocity 2 * U_mean)
    dp/dx = -32 * mu * U_mean / D**2             (pressure gradient)
    Delta p = 32 * mu * L * U_mean / D**2        (pressure drop over a length L)

The script writes ``reference/u_profile.csv`` (header ``x,value``; x = r / R, value = u / U_mean)
and checks that the pressure-drop reference in ``benchmark.toml`` equals the formula.

The physical parameters are constants in this file, not a table in ``benchmark.toml``: the registry
models forbid unknown keys, so an extra table would make ``load_registry`` fail. Keep them in sync
with ``task.md``.

Usage (from anywhere, standard library only):

    python benchmarks/pipe-re500/make_reference.py           # (re)write the CSV
    python benchmarks/pipe-re500/make_reference.py --check   # fail if the committed CSV differs
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
BENCHMARK_FILE = HERE / "benchmark.toml"
PROFILE_FILE = HERE / "reference" / "u_profile.csv"

# --- Physical parameters (SI units); keep in sync with task.md --------------------------------
DENSITY = 1000.0  # kg/m^3 (water)
DYNAMIC_VISCOSITY = 1.0e-3  # Pa s
DIAMETER = 0.01  # m
MEAN_VELOCITY = 0.05  # m/s, uniform inflow = bulk velocity
X_UPSTREAM = 0.35  # m from the inlet: first pressure plane of the fully developed section
X_DOWNSTREAM = 0.45  # m from the inlet: second pressure plane (also the profile plane)
REYNOLDS = 500.0  # = DENSITY * MEAN_VELOCITY * DIAMETER / DYNAMIC_VISCOSITY
# ------------------------------------------------------------------------------------------------

N_INTERVALS = 20  # reference points at r/R = 0, 0.05, ..., 1
PRESSURE_QUANTITY = "pressure_drop_fully_developed"
PROFILE_QUANTITY = "u_profile_fully_developed"


def reynolds_number() -> float:
    return DENSITY * MEAN_VELOCITY * DIAMETER / DYNAMIC_VISCOSITY


def pressure_drop() -> float:
    """Delta p in Pa between the two pressure planes: 32 * mu * L * U_mean / D**2."""
    length = X_DOWNSTREAM - X_UPSTREAM
    return 32.0 * DYNAMIC_VISCOSITY * length * MEAN_VELOCITY / DIAMETER**2


def profile_rows() -> list[tuple[float, float]]:
    """Reference profile as (r/R, u/U_mean) pairs from the axis to the wall."""
    rows = []
    for i in range(N_INTERVALS + 1):
        r = i / N_INTERVALS
        rows.append((r, 2.0 * (1.0 - r * r)))
    return rows


def render_profile_csv() -> str:
    lines = ["x,value"]
    lines += [f"{r:.2f},{u:.6f}" for r, u in profile_rows()]
    return "\n".join(lines) + "\n"


def write_reference(path: Path = PROFILE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(render_profile_csv())


def check_benchmark_toml() -> list[str]:
    """Problems found when comparing benchmark.toml with the formulas (empty list = consistent)."""
    problems = []
    if abs(reynolds_number() - REYNOLDS) > 1e-9:
        problems.append(f"Re from the parameters is {reynolds_number()}, expected {REYNOLDS}")
    with BENCHMARK_FILE.open("rb") as handle:
        criteria = {c["quantity"]: c for c in tomllib.load(handle)["criteria"]}
    expected = pressure_drop()
    given = criteria[PRESSURE_QUANTITY].get("reference_value")
    if given is None or abs(given - expected) > 1e-9 * abs(expected):
        problems.append(f"{PRESSURE_QUANTITY}: reference_value {given} != formula {expected}")
    # The profile error is in units of U_mean; dividing by 2 expresses it per centreline velocity.
    if criteria[PROFILE_QUANTITY].get("normalise_by") != 2.0:
        problems.append(f"{PROFILE_QUANTITY}: normalise_by must be 2.0 (centreline / U_mean)")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--check", action="store_true", help="do not write; fail if the committed CSV differs"
    )
    args = parser.parse_args(argv)

    problems = check_benchmark_toml()
    if args.check:
        committed = PROFILE_FILE.read_bytes() if PROFILE_FILE.is_file() else b""
        if committed != render_profile_csv().encode("utf-8"):
            problems.append(f"{PROFILE_FILE.name} differs from the regenerated profile")
    else:
        write_reference()
    for problem in problems:
        print(f"make_reference: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
