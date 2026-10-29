"""The pipe task text and the script that generates its reference data must agree.

``benchmarks/pipe-re500/task.md`` is what the Planner reads; ``make_reference.py`` holds the same
physical parameters as constants (``benchmark.toml`` cannot hold them). A drift between the two
would make the reference data answer a different question than the one asked.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PIPE = Path(__file__).resolve().parents[2] / "benchmarks" / "pipe-re500"
NUMBER = r"([0-9]+(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?)"
REL_TOL = 1e-9


def _find(pattern: str, text: str, what: str, flags: int = 0) -> float:
    match = re.search(pattern, text, flags)
    if match is None:
        raise AssertionError(f"cannot find {what} with /{pattern}/")
    return float(match.group(1))


def task_parameters(text: str) -> dict[str, float]:
    """Physical parameters stated in task.md, in SI units."""
    return {
        "diameter": _find(rf"inner diameter D = {NUMBER} mm", text, "diameter") / 1000.0,
        "radius": _find(rf"radius R = {NUMBER} mm", text, "radius") / 1000.0,
        "length": _find(rf"and a length of {NUMBER} m", text, "length"),
        "density": _find(rf"density {NUMBER} kg/m\^3", text, "density"),
        "viscosity": _find(rf"dynamic\s+viscosity {NUMBER} Pa s", text, "viscosity"),
        "velocity": _find(rf"uniform axial velocity of {NUMBER} m/s", text, "inlet velocity"),
        "reynolds": _find(rf"Re = {NUMBER}", text, "Reynolds number"),
        "plane_downstream": _find(rf"cross-section at x = {NUMBER} m", text, "profile plane"),
        "plane_a": _find(rf"between the cross-sections at\s+x = {NUMBER} m", text, "plane a"),
        "plane_b": _find(rf"and x = {NUMBER} m, i\.e\.", text, "plane b"),
    }


def script_parameters(text: str) -> dict[str, float]:
    """Constants of make_reference.py."""

    def constant(name: str) -> float:
        return _find(rf"^{name} = {NUMBER}", text, name, re.MULTILINE)

    return {
        "diameter": constant("DIAMETER"),
        "density": constant("DENSITY"),
        "viscosity": constant("DYNAMIC_VISCOSITY"),
        "velocity": constant("MEAN_VELOCITY"),
        "reynolds": constant("REYNOLDS"),
        "plane_upstream": constant("X_UPSTREAM"),
        "plane_downstream": constant("X_DOWNSTREAM"),
    }


def _differs(a: float, b: float) -> bool:
    return abs(a - b) > REL_TOL * max(abs(a), abs(b))


def mismatches(task_text: str, script_text: str) -> list[str]:
    """Describe every disagreement between the task and the script (empty list = in sync)."""
    task = task_parameters(task_text)
    script = script_parameters(script_text)
    problems = []
    for name in ("diameter", "density", "viscosity", "velocity", "reynolds"):
        if _differs(task[name], script[name]):
            problems.append(f"{name}: task {task[name]} != script {script[name]}")
    if _differs(task["radius"], script["diameter"] / 2.0):
        problems.append(f"radius: task {task['radius']} != script D/2 {script['diameter'] / 2.0}")
    if _differs(task["radius"], task["diameter"] / 2.0):
        problems.append("radius in the task is not half of its diameter")
    if _differs(task["plane_downstream"], script["plane_downstream"]):
        problems.append("profile plane: task and script differ")
    if _differs(task["plane_a"], script["plane_upstream"]) or _differs(
        task["plane_b"], script["plane_downstream"]
    ):
        problems.append("pressure planes: task and script differ")
    if task["plane_downstream"] > task["length"]:
        problems.append("the planes lie beyond the pipe length stated in the task")
    # the Reynolds number must follow from the stated fluid and flow
    stated = task["density"] * task["velocity"] * task["diameter"] / task["viscosity"]
    if _differs(stated, task["reynolds"]):
        problems.append(f"task Re {task['reynolds']} does not follow from its data ({stated})")
    return problems


TASK_TEXT = (PIPE / "task.md").read_text(encoding="utf-8")
SCRIPT_TEXT = (PIPE / "make_reference.py").read_text(encoding="utf-8")


def test_pipe_task_matches_make_reference_parameters():
    assert mismatches(TASK_TEXT, SCRIPT_TEXT) == []


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("inner diameter D = 10 mm", "inner diameter D = 12 mm"),
        ("radius R = 5 mm", "radius R = 6 mm"),
        ("a length of 0.5 m", "a length of 0.4 m"),
        ("density 1000 kg/m^3", "density 998 kg/m^3"),
        ("viscosity 1.0e-3 Pa s", "viscosity 1.1e-3 Pa s"),
        ("uniform axial velocity of 0.05 m/s", "uniform axial velocity of 0.06 m/s"),
        ("Re = 500", "Re = 600"),
        ("cross-section at x = 0.45 m,", "cross-section at x = 0.46 m,"),
        ("x = 0.35 m and x = 0.45 m, i.e.", "x = 0.30 m and x = 0.45 m, i.e."),
    ],
)
def test_task_drift_is_detected(old, new):
    assert old in TASK_TEXT, f"test needs updating: {old!r} not in task.md"
    assert mismatches(TASK_TEXT.replace(old, new, 1), SCRIPT_TEXT) != []


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("DENSITY = 1000.0", "DENSITY = 998.0"),
        ("DYNAMIC_VISCOSITY = 1.0e-3", "DYNAMIC_VISCOSITY = 2.0e-3"),
        ("DIAMETER = 0.01", "DIAMETER = 0.02"),
        ("MEAN_VELOCITY = 0.05", "MEAN_VELOCITY = 0.1"),
        ("X_UPSTREAM = 0.35", "X_UPSTREAM = 0.3"),
        ("X_DOWNSTREAM = 0.45", "X_DOWNSTREAM = 0.4"),
        ("REYNOLDS = 500.0", "REYNOLDS = 400.0"),
    ],
)
def test_script_drift_is_detected(old, new):
    assert old in SCRIPT_TEXT, f"test needs updating: {old!r} not in make_reference.py"
    assert mismatches(TASK_TEXT, SCRIPT_TEXT.replace(old, new, 1)) != []
