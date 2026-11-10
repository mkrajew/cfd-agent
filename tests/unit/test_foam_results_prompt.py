"""The Analyst prompt documents ``cfdagent.tools.foam_results``; that text must not drift."""

import json
import re
import shutil
from pathlib import Path

import numpy as np
import pytest
from foam_fixtures import write_box_case, write_field

from cfdagent.prompts import load_prompt
from cfdagent.quantities import validate_quantities
from cfdagent.tools import foam_results

LOGS = Path(__file__).parents[1] / "fixtures" / "logs"


def _section() -> str:
    text = load_prompt("analyst").text
    start = text.index("## Reading the case: `cfdagent.tools.foam_results`")
    return text[start : text.index("\n## ", start + 5)]


def test_the_prompt_names_only_things_the_module_has():
    section = _section()
    attributes = set(re.findall(r"`case\.(\w+)", section))
    assert attributes >= {
        "times",
        "latest_time",
        "list_fields",
        "read_field",
        "cell_centres",
        "cell_volumes",
        "n_cells",
        "bounds",
        "is_2d",
        "patches",
        "read_patch_field",
        "sample_line",
    }
    for name in attributes:
        assert hasattr(foam_results.FoamCase, name), name
    for name in re.findall(r"`(\w+)\(\"case\"\)", section):
        assert hasattr(foam_results, name), name
    for name in ("load_case", "read_solver_log", "list_times", "latest_time", "LineSample"):
        assert name in section or name == "LineSample", name
    for name in ("solver", "n_time_steps", "last_time", "completed", "converged_at"):
        assert f".{name}" in section and name in foam_results.SolverLog.__dataclass_fields__
    for name in ("drop_outside", "inside", "cell"):
        assert name in section
    for name in ("type", "n_faces", "face_centres", "face_areas", "owner_cells"):
        assert name in foam_results.Patch.__dataclass_fields__ and f".{name}" in section
    assert "PyVista" in section  # the warning against opening the case with it


def test_the_prompt_skeleton_runs_on_a_case(tmp_path, monkeypatch):
    section = _section()
    code = re.search(r"```python\n(.*?)```", section, re.DOTALL).group(1)
    root = write_box_case(tmp_path, shape=(8, 6, 1), size=(2.0, 1.0, 0.1))
    case = foam_results.load_case(root)
    patches = {n: p for n, p in case.patches.items() if p.type != "empty"}
    empty = {n: ("empty", None) for n, p in case.patches.items() if p.type == "empty"}
    for time, scale in (("0.5", 0.9), ("1", 1.0)):
        x = case.cell_centres
        write_field(
            root,
            time,
            "U",
            scale * np.stack([x[:, 0], x[:, 1], 0 * x[:, 0]], axis=1),
            {**{n: ("fixedValue", ("uniform", [0.0, 0.0, 0.0])) for n in patches}, **empty},
        )
        write_field(
            root,
            time,
            "phi",
            np.zeros(82),
            {**{n: ("calculated", np.zeros(p.n_faces)) for n, p in patches.items()}, **empty},
            cls="surfaceScalarField",
        )
    shutil.copy(LOGS / "simpleFoam_converged.log", root / "log.simpleFoam")
    monkeypatch.chdir(root.parent)
    namespace = {
        "FIELD": "U",
        "START": (0.1, 0.1),
        "END": (1.9, 0.9),
        "L_REF": 1.0,
        "U_REF": 2.0,
        "OUTPUT_NAME": "profile",
    }
    exec(compile(code, "<prompt skeleton>", "exec"), namespace)  # noqa: S102
    data = validate_quantities(json.loads((root.parent / "quantities.json").read_text()))
    assert len(data["profile"]["x"]) == 200
    assert data["profile"]["x"] == sorted(data["profile"]["x"])
    assert np.isfinite(namespace["change"]) and namespace["change"] > 0
    assert namespace["log"].converged_at == 47
    assert sum(namespace["flux"].values()) == pytest.approx(0.0)
