"""The Geometry stage end to end inside the image: a scripted model writes a CadQuery script, the
local-mode executor runs it and the real geometry gate judges it. Local mode imports the
``cfdagent`` under test (the checked-out sources), see ``SandboxClient._command``.
"""

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cfdagent.agents.casespec import CaseSpec
from cfdagent.agents.geometry import GeometryStage
from cfdagent.agents.loop import run_stage
from cfdagent.agents.stage import StageContext
from cfdagent.agents.transcript import TranscriptWriter
from cfdagent.config import RunConfig
from cfdagent.llm.client import ChatResult, ToolCall, Usage
from cfdagent.llm.cost import PRICING, CostTracker
from cfdagent.llm.scripted import ScriptedBackend
from cfdagent.record import RunRecord
from cfdagent.rundir import RunDir
from cfdagent.sandbox.client import SandboxClient
from cfdagent.sandbox.config import SandboxConfig

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CAVITY_SCRIPT = (FIXTURES / "handwritten" / "cavity_re100" / "geometry.py").read_text(
    encoding="utf-8"
)

CAVITY_SPEC = {
    "title": "Square domain",
    "summary": "A unit square domain, one cell thick.",
    "dimensionality": "2D",
    "flow_regime": "laminar",
    "time_dependence": "steady",
    "fluid": {"density_kg_m3": 1.0, "kinematic_viscosity_m2_s": 0.01},
    "reference": {"length_m": 1.0, "velocity_m_s": 1.0, "reynolds_number": 100.0},
    "geometry": {
        "description": "A unit square in the x-y plane, 0.1 thick in z.",
        "bbox": {"xmin": 0.0, "ymin": 0.0, "zmin": 0.0, "xmax": 1.0, "ymax": 1.0, "zmax": 0.1},
        "volume_m3": 0.1,
        "key_dimensions": [{"name": "side", "value_m": 1.0}],
        "thickness_m": 0.1,
        "wedge_angle_deg": None,
    },
    "boundaries": [
        {"name": "movingWall", "role": "wall", "description": "top, y = 1"},
        {"name": "fixedWalls", "role": "wall", "description": "left, right and bottom"},
        {"name": "frontAndBack", "role": "empty", "description": "z = 0 and z = 0.1"},
    ],
    "mesh": {"target_cells": 400, "rationale": "Uniform cells."},
    "solver": {"suggested_solver": "icoFoam", "end_time_s": 1.0, "time_step_s": 0.005},
    "requested_outputs": [{"name": "profile", "kind": "profile", "description": "u on a line"}],
    "assumptions": [],
    "resource_estimate": {"cells": 400, "runtime_minutes": 1.0, "memory_gb": 0.1},
}


def call(call_id, name, **arguments):
    return ToolCall(id=call_id, name=name, arguments=arguments)


def reply(*calls):
    return ChatResult(
        content=None,
        tool_calls=list(calls),
        usage=Usage(100, 50, None),
        parsed=None,
        raw={},
        finish_reason="tool_calls",
    )


def write_script(number, text):
    return reply(
        call(
            f"w{number}", "write_file", path=f"attempts/geometry-{number}/geometry.py", content=text
        )
    )


def check(number):
    return reply(call(f"k{number}", "check_stage"))


def make_ctx(tmp_path, backend):
    root = tmp_path / "run"
    root.mkdir()
    run = RunDir(root)
    cost = CostTracker(1.0, PRICING["gpt-oss-120b"])
    record = RunRecord(
        run_id="20261112-143005-demo",
        task_set="ad hoc",
        task_sha256="a" * 64,
        started_at=datetime(2026, 11, 12, 14, 30, 5, tzinfo=UTC),
        status="running",
        cfdagent_version="0.0.1",
    )
    sandbox = SandboxClient(
        SandboxConfig(mode="local", allow_local=True, image="local-unused"),
        run,
        "20261112-143005-geom",
    )
    return StageContext(
        run=run,
        config=RunConfig.default(),
        backend=backend,
        sandbox=sandbox,
        cost=cost,
        record=record,
        transcript=TranscriptWriter(run, cost),
        started_at=time.monotonic(),
        casespec=CaseSpec.model_validate(CAVITY_SPEC),
    )


def test_geometry_stage_passes_scripted_cavity(tmp_path):
    backend = ScriptedBackend.from_json(FIXTURES / "scripted" / "geometry_ok.json")
    ctx = make_ctx(tmp_path, backend)
    outcome = run_stage(GeometryStage(), ctx)
    assert outcome.ok, outcome
    assert outcome.attempts == 1
    promoted = ctx.run.root / "geometry"
    assert (promoted / "geometry.step").stat().st_size > 0
    manifest = json.loads((promoted / "boundary_manifest.json").read_text(encoding="utf-8"))
    assert {p["name"] for p in manifest["patches"]} == {"movingWall", "fixedWalls", "frontAndBack"}
    assert (promoted / "geometry.py").is_file()
    assert [(a.n, a.ok) for a in ctx.record.stages[0].attempts] == [(1, True)]


def test_geometry_stage_recovers_after_wrong_size(tmp_path):
    wrong = CAVITY_SCRIPT.replace("box(1.0, 1.0, THICKNESS", "box(1.2, 1.0, THICKNESS")
    assert wrong != CAVITY_SCRIPT
    backend = ScriptedBackend(
        [write_script(1, wrong), check(1), write_script(2, CAVITY_SCRIPT), check(2)]
    )
    ctx = make_ctx(tmp_path, backend)
    outcome = run_stage(GeometryStage(), ctx)
    assert outcome.ok, outcome
    assert outcome.attempts == 2
    attempts = ctx.record.stages[0].attempts
    assert [(a.n, a.ok) for a in attempts] == [(1, False), (2, True)]
    assert "x" in attempts[0].errors[0] and "bounding box" in attempts[0].errors[0].lower()
    # The model was told what was wrong, in the tool result of its first check.
    feedback = [m for m in backend.calls[2].messages if m["role"] == "tool"][-1]["content"]
    assert "checks failed" in feedback and "attempt 2" in feedback
    assert (ctx.run.root / "geometry" / "geometry.step").is_file()


def test_geometry_stage_reports_a_script_that_crashes(tmp_path):
    crash = "import cadquery as cq\nraise ValueError('side length not set')\n"
    backend = ScriptedBackend(
        [write_script(1, crash), check(1), write_script(2, CAVITY_SCRIPT), check(2)]
    )
    ctx = make_ctx(tmp_path, backend)
    outcome = run_stage(GeometryStage(), ctx)
    assert outcome.ok and outcome.attempts == 2
    first_errors = "\n".join(ctx.record.stages[0].attempts[0].errors)
    assert "ValueError: side length not set" in first_errors
