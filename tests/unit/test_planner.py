"""Unit tests for the Planner: the gate on a CaseSpec and the two-phase planning loop."""

import json
import math
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_casespec import SPEC, make_spec

from cfdagent.agents.casespec import CaseSpec
from cfdagent.agents.planner import plan_gate, run_planner, stated_reynolds
from cfdagent.agents.routing import RouteRequest
from cfdagent.agents.stage import StageContext
from cfdagent.agents.transcript import TRANSCRIPT_FILE, TranscriptWriter
from cfdagent.config import RunConfig, RunLimits
from cfdagent.gates import GateResult
from cfdagent.llm.client import (
    ChatResult,
    EmptyChoicesError,
    LLMTransportError,
    SchemaError,
    StructuredOutputError,
    ToolCall,
    TruncatedOutputError,
    Usage,
)
from cfdagent.llm.cost import PRICING, CostTracker
from cfdagent.llm.scripted import ScriptedBackend
from cfdagent.prompts import load_prompt
from cfdagent.record import RunRecord
from cfdagent.rundir import RunDir

LIMITS = RunLimits()
TASK = "Simulate the flow. Report `centreline_u` and `drag`. Use the mesh file `case.msh`."
USAGE = Usage(100, 50, None)


# The loop tests give no volume: a volume must come from a calculator call of the run (see the
# tests of the volume rule below), and these tests are about other things.
NO_VOLUME = {**SPEC["geometry"], "volume_m3": None}


def plan_spec(**changes) -> CaseSpec:
    return make_spec(geometry=NO_VOLUME, **changes)


def spec_json(**changes) -> str:
    return json.dumps({**SPEC, "geometry": NO_VOLUME, **changes})


def reply(content=None, *calls, usage=USAGE, finish_reason=None) -> ChatResult:
    return ChatResult(
        content=content,
        tool_calls=list(calls),
        usage=usage,
        parsed=None,
        raw={},
        finish_reason=finish_reason or ("tool_calls" if calls else "stop"),
    )


def calc(expression: str, call_id: str = "c1") -> ToolCall:
    return ToolCall(id=call_id, name="calculator", arguments={"expression": expression})


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_ctx(tmp_path, steps, *, limits=None, cost_limit=1.0, clock=None, progress=None):
    root = tmp_path / "run"
    root.mkdir(exist_ok=True)
    run = RunDir(root)
    config = RunConfig.default()
    if limits is not None:
        config = config.model_copy(update={"limits": limits})
    cost = CostTracker(cost_limit, PRICING["gpt-oss-120b"])
    record = RunRecord(
        run_id="20261112-143005-demo",
        task_set="ad hoc",
        task_sha256="a" * 64,
        started_at=datetime(2026, 11, 12, 14, 30, 5, tzinfo=UTC),
        status="running",
        cfdagent_version="0.0.1",
    )
    backend = ScriptedBackend(steps)
    extra = {"started_at": (clock or time.monotonic)()}
    if clock is not None:
        extra["clock"] = clock
    if progress is not None:
        extra["on_progress"] = progress.append
    ctx = StageContext(
        run=run,
        config=config,
        backend=backend,
        sandbox=SimpleNamespace(),  # the Planner never calls the sandbox
        cost=cost,
        record=record,
        transcript=TranscriptWriter(run, cost),
        **extra,
    )
    return ctx, backend


# ---------------------------------------------------------------------------- the gate


def test_planner_gate_accepts_a_consistent_spec():
    assert plan_gate(make_spec(), TASK, LIMITS) == GateResult(True, [])


def test_planner_gate_requires_every_backticked_identifier():
    task = "Report `centreline_u`, `drag` and `wall_shear`. Mesh: `case.msh`, `x/L`, `1e-5`."
    result = plan_gate(make_spec(), task, LIMITS)
    assert not result.ok
    assert len(result.errors) == 1
    assert "`wall_shear`" in result.errors[0]
    assert "centreline_u" in result.errors[0]  # names the outputs that do exist
    # Backticked file names and expressions are not identifiers and are ignored.
    assert plan_gate(make_spec(), "Plot `centreline_u`, save `out.png`, use `x/L`.", LIMITS).ok


def test_planner_gate_identifier_match_is_exact():
    result = plan_gate(make_spec(), "Report `Drag`.", LIMITS)  # the output is named `drag`
    assert not result.ok
    assert "`Drag`" in result.errors[0]


def test_planner_gate_lists_each_missing_identifier_once():
    result = plan_gate(make_spec(), "`aaa` and `bbb` and `aaa` again", LIMITS)
    assert [("`aaa`" in e, "`bbb`" in e) for e in result.errors] == [(True, False), (False, True)]


def test_planner_gate_checks_reynolds_consistency():
    # U L / nu = 1 * 1 / 0.01 = 100
    assert plan_gate(make_spec(), TASK, LIMITS).ok
    off = make_spec(reference={"length_m": 1.0, "velocity_m_s": 1.0, "reynolds_number": 100.2})
    result = plan_gate(off, TASK, LIMITS)
    assert not result.ok
    assert "reynolds" in result.errors[0].lower()
    assert "100" in result.errors[0]
    close = make_spec(reference={"length_m": 1.0, "velocity_m_s": 1.0, "reynolds_number": 100.05})
    assert plan_gate(close, TASK, LIMITS).ok  # 0.05 % is inside the 0.1 % tolerance


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Run at Re = 100.", [100.0]),
        ("Run at Re=100 and report the profile.", [100.0]),
        ("Run at Re 100.", [100.0]),
        ("Use Re: 250 for this case.", [250.0]),
        ("Run at a Reynolds number of 100.", [100.0]),
        ("The Reynolds number is 1,000.", [1000.0]),
        ("The Reynolds number is 1000.", [1000.0]),
        ("The Reynolds number equals 2.5e4 here.", [25000.0]),
        ("reynolds number (Re) of 40", [40.0]),
        ("The Reynolds number is 100; that is, Re = 100.", [100.0]),  # the same value twice
        ("Compare Re = 100 and Re = 1000.", [100.0, 1000.0]),
        ("Run Re = 100, 400 and 1000.", [100.0, 400.0, 1000.0]),  # a list of values
        ("Sweep the Reynolds number from 100 to 400.", [100.0, 400.0]),
        ("Run Re = 100-400.", [100.0, 400.0]),
        ("The Reynolds number is based on the lid speed of 1 m/s.", []),
        ("The Reynolds number is large, about 1 million.", []),
        ("The Reynolds number is 1 x 10^5.", []),  # not a plain number: no value, no check
        ("Resolve the boundary layer. Number of cells: 100.", []),
        ("There is no flow parameter here.", []),
        # Space-separated thousands are not a plain number: no value, no check.
        ("Re = 10 000.", []),
        ("Re = 10\u202f000.", []),
        ("Re = 10\u00a0000.", []),
        # An approximate statement is not an exact value: no check.
        ("Re ≈ 3300.", []),
        ("Re ~ 3300.", []),
        ("The Reynolds number is about 3000.", []),
        # A condition that follows the words "Reynolds number at" is not the value.
        ("The Reynolds number at 300 K is 5000.", []),
        ("The Reynolds number at 20 C is 1e5.", []),
        ("Run at Re = 100 %.", []),
        # The usual wording with the convention of the Reynolds number in between.
        ("The Reynolds number based on the diameter and the mean speed is 500.", [500.0]),
        ("The Reynolds number based on the body length equals 2,000.", [2000.0]),
        ("The Reynolds number based on the diameter of 0.1 m is large.", []),
        ("The Reynolds number based on the diameter, which is 0.1 m, is 40.", []),
        # A definition of the scales (U = 1, L = 1) is not the value.
        ("The Reynolds number based on U = 1 and L = 1 is 100.", []),
        ("The Reynolds number based on the side length L = 1 is 100.", []),
        ("Reynolds number based on D = 1 equals 20", []),
        ("Re = 1e999.", []),
        # The label ends at the first "is": a later "is <number>" is another statement.
        ("The Reynolds number based on the width is low and the cell count is 2000.", []),
        ("The Reynolds number based on the width is low and the mesh is 200 cells wide.", []),
        ("The Reynolds number based on the width is 100 and the grid is 50 by 50.", [100.0]),
        # A list cut off by a number that is not taken: which values it holds is unknown.
        ("Sweep the Reynolds number from 1000 to 10 000.", []),
        ("Re = 100, 10 000.", []),
        ("Re = 100 to 2 million.", []),
        ("Re = 100 to 1 x 10^6.", []),
        ("Re = 1000 and 5 %.", []),
        ("The Reynolds number based on the diameter is 100 times the critical value.", []),
    ],
)
def test_stated_reynolds_patterns(text, expected):
    assert sorted(stated_reynolds(text)) == expected


def test_stated_reynolds_has_no_false_positive_on_words_that_start_with_re():
    assert stated_reynolds("Rerun 100 times. Report 5 results. Read 200 lines.") == []


def test_plan_gate_ignores_scales_that_define_the_reynolds_number():
    task = f"{TASK} The Reynolds number based on U = 1 and L = 1 is 100."
    assert plan_gate(make_spec(), task, LIMITS).ok


def test_plan_gate_rejects_spec_reynolds_different_from_task():
    task = f"{TASK} The Reynolds number is 1000."
    result = plan_gate(make_spec(), task, LIMITS)  # the spec says 100
    assert not result.ok
    assert len(result.errors) == 1
    assert "reynolds" in result.errors[0].lower()
    assert "1000" in result.errors[0] and "100" in result.errors[0]
    # Within 0.1 % of the stated value the spec is accepted.
    close = make_spec(
        fluid={"density_kg_m3": 1.0, "kinematic_viscosity_m2_s": 0.001},
        reference={"length_m": 1.0, "velocity_m_s": 1.0, "reynolds_number": 1000.5},
    )
    assert plan_gate(close, task, LIMITS).ok
    off = make_spec(
        fluid={"density_kg_m3": 1.0, "kinematic_viscosity_m2_s": 0.001},
        reference={"length_m": 1.0, "velocity_m_s": 1.0, "reynolds_number": 1002.0},
    )
    assert not plan_gate(off, task, LIMITS).ok


def test_plan_gate_accepts_spec_reynolds_equal_to_the_task():
    assert plan_gate(make_spec(), f"{TASK} Use Re = 100.", LIMITS).ok
    assert plan_gate(make_spec(), f"{TASK} The Reynolds number is 1e2.", LIMITS).ok


def test_plan_gate_rejects_a_spec_without_the_reynolds_number_the_task_states():
    spec = make_spec(reference={"length_m": 1.0, "velocity_m_s": 1.0, "reynolds_number": None})
    result = plan_gate(spec, f"{TASK} Use Re = 100.", LIMITS)
    assert not result.ok
    assert "reynolds" in result.errors[0].lower() and "null" in result.errors[0]


def test_plan_gate_skips_when_task_states_several_values():
    task = f"{TASK} Compare Re = 100 and Re = 1000."
    spec = make_spec(
        fluid={"density_kg_m3": 1.0, "kinematic_viscosity_m2_s": 0.002},
        reference={"length_m": 1.0, "velocity_m_s": 1.0, "reynolds_number": 500.0},
    )
    assert plan_gate(spec, task, LIMITS).ok


def test_plan_gate_skips_when_task_states_no_value():
    spec = make_spec(reference={"length_m": 1.0, "velocity_m_s": 5.0, "reynolds_number": 500.0})
    assert plan_gate(spec, TASK, LIMITS).ok  # 5 * 1 / 0.01 = 500, and the task states nothing


def test_planner_gate_skips_reynolds_check_when_not_given():
    spec = make_spec(reference={"length_m": 1.0, "velocity_m_s": 5.0, "reynolds_number": None})
    assert plan_gate(spec, TASK, LIMITS).ok


def test_planner_gate_reynolds_with_nonpositive_viscosity_is_an_error_not_a_crash():
    spec = make_spec(fluid={"density_kg_m3": 1.0, "kinematic_viscosity_m2_s": 0.0})
    result = plan_gate(spec, TASK, LIMITS)
    assert not result.ok
    assert "kinematic_viscosity_m2_s" in " ".join(result.errors)


def test_planner_gate_rejects_an_empty_bbox_axis():
    geometry = {
        **SPEC["geometry"],
        "bbox": {"xmin": 0.0, "ymin": 0.5, "zmin": 0.0, "xmax": 4.0, "ymax": 0.5, "zmax": 0.1},
    }
    result = plan_gate(make_spec(geometry=geometry), TASK, LIMITS)
    assert not result.ok
    assert len(result.errors) == 1
    assert "bbox" in result.errors[0] and "y" in result.errors[0]


def boundary(name):
    return {"name": name, "role": "wall", "description": "somewhere"}


def test_planner_gate_rejects_an_empty_boundary_list():
    result = plan_gate(make_spec(boundaries=[]), TASK, LIMITS)
    assert not result.ok and len(result.errors) == 1
    assert "boundaries" in result.errors[0] and "list every boundary" in result.errors[0]


@pytest.mark.parametrize(
    "name", ["top wall", "inlet-1", "1inlet", "_inlet", "", "région", "inlet\n"]
)
def test_planner_gate_rejects_boundary_names_a_patch_cannot_have(name):
    result = plan_gate(make_spec(boundaries=[boundary("inlet"), boundary(name)]), TASK, LIMITS)
    assert not result.ok and len(result.errors) == 1
    assert "boundaries[1].name" in result.errors[0]
    assert "letters, digits and underscores" in result.errors[0]


def test_planner_gate_accepts_plain_identifier_boundary_names():
    names = ["inlet", "Outlet_2", "a", "wall_top_1"]
    assert plan_gate(make_spec(boundaries=[boundary(n) for n in names]), TASK, LIMITS).ok


def test_planner_gate_rejects_duplicate_boundary_names_once_per_name():
    boundaries = [boundary("wall"), boundary("inlet"), boundary("wall"), boundary("wall")]
    result = plan_gate(make_spec(boundaries=boundaries), TASK, LIMITS)
    assert not result.ok and len(result.errors) == 1
    assert "`wall`" in result.errors[0] and "more than once" in result.errors[0]


def test_planner_gate_shortens_a_very_long_boundary_name_in_its_message():
    name = "x" * 400 + " bad"
    result = plan_gate(make_spec(boundaries=[boundary(name)]), TASK, LIMITS)
    assert not result.ok and name not in result.errors[0] and len(result.errors[0]) < 400


def test_boundary_name_rule_is_the_one_of_the_manifest():
    """A name that the planner accepts must be a name that a manifest patch accepts."""
    from cfdagent import manifest
    from cfdagent.agents import planner

    assert planner.PATCH_NAME_PATTERN == manifest.PATCH_NAME_PATTERN
    field = manifest.BoundaryPatch.model_fields["name"]
    assert [m.pattern for m in field.metadata if hasattr(m, "pattern")] == [
        manifest.PATCH_NAME_PATTERN
    ]


def test_planner_gate_rejects_a_nonpositive_end_time():
    for bad in (0.0, -5.0):
        spec = make_spec(solver={**SPEC["solver"], "end_time_s": bad})
        result = plan_gate(spec, TASK, LIMITS)
        assert not result.ok
        assert "end_time_s" in result.errors[0]


def test_planner_rejects_2d_without_thickness():
    for bad in (None, 0.0, -1.0):
        spec = make_spec(geometry={**SPEC["geometry"], "thickness_m": bad})
        result = plan_gate(spec, TASK, LIMITS)
        assert not result.ok
        assert any("thickness_m" in e for e in result.errors)
    spec = make_spec(dimensionality="3D", geometry={**SPEC["geometry"], "thickness_m": None})
    assert plan_gate(spec, TASK, LIMITS).ok


@pytest.mark.parametrize(
    ("estimate", "word"),
    [
        ({"cells": 2_000_001, "runtime_minutes": 5.0, "memory_gb": 0.5}, "cells"),
        ({"cells": 8000, "runtime_minutes": 90.5, "memory_gb": 0.5}, "runtime"),
        ({"cells": 8000, "runtime_minutes": 5.0, "memory_gb": 12.5}, "memory"),
    ],
)
def test_planner_gate_flags_resource_excess_with_prefix(estimate, word):
    result = plan_gate(make_spec(resource_estimate=estimate), TASK, LIMITS)
    assert not result.ok
    assert len(result.errors) == 1
    assert result.errors[0].startswith("exceeds resources:")
    assert word in result.errors[0]


def test_planner_gate_allows_resources_exactly_at_the_limits():
    estimate = {"cells": 2_000_000, "runtime_minutes": 90.0, "memory_gb": 12.0}
    assert plan_gate(make_spec(resource_estimate=estimate), TASK, LIMITS).ok


def test_planner_gate_reports_several_resources_in_one_message():
    estimate = {"cells": 9_000_000, "runtime_minutes": 500.0, "memory_gb": 50.0}
    result = plan_gate(make_spec(resource_estimate=estimate), TASK, LIMITS)
    assert len(result.errors) == 1
    for word in ("cells", "runtime", "memory"):
        assert word in result.errors[0]


def geometry(**changes) -> dict:
    return {**SPEC["geometry"], **changes}


def bbox(**changes) -> dict:
    return {**SPEC["geometry"]["bbox"], **changes}


def wedge_geometry(angle) -> dict:
    """A consistent wedge about the x axis: y from 0 to R, z from -R sin(a/2) to R sin(a/2)."""
    radius = 0.5
    shown = angle if isinstance(angle, float) and 0 < angle <= 10 else 5.0
    half = radius * math.sin(math.radians(shown) / 2)
    wedge_box = bbox(ymin=0.0, ymax=radius, zmin=-half, zmax=half)
    return geometry(bbox=wedge_box, volume_m3=None, thickness_m=None, wedge_angle_deg=angle)


def gate_errors(**changes) -> list[str]:
    return plan_gate(make_spec(**changes), TASK, LIMITS).errors


def test_planner_gate_checks_2d_thickness_against_the_bbox_z_extent():
    mismatch = geometry(thickness_m=0.1, bbox=bbox(zmin=0.0, zmax=1.0))
    errors = gate_errors(geometry=mismatch)
    assert len(errors) == 1 and "thickness_m" in errors[0] and "zmax - zmin" in errors[0]
    shifted = geometry(thickness_m=0.1, bbox=bbox(zmin=-0.05, zmax=0.05))
    assert gate_errors(geometry=shifted) == []
    tolerant = geometry(thickness_m=0.1, bbox=bbox(zmin=0.0, zmax=0.10005))  # 0.05 % off
    assert gate_errors(geometry=tolerant) == []
    thin = geometry(thickness_m=0.001, bbox=bbox(zmin=0.0, zmax=0.001), volume_m3=0.004)
    assert gate_errors(geometry=thin) == []


@pytest.mark.parametrize("angle", [None, 0.0, -5.0, 10.5, math.nan, math.inf])
def test_planner_gate_requires_a_valid_wedge_angle_for_axisymmetric_cases(angle):
    spec = make_spec(dimensionality="axisymmetric", geometry=wedge_geometry(angle))
    errors = plan_gate(spec, TASK, LIMITS).errors
    assert len(errors) == 1 and "wedge_angle_deg" in errors[0]


@pytest.mark.parametrize("angle", [0.5, 5.0, 10.0])
def test_planner_gate_accepts_a_wedge_for_axisymmetric_cases(angle):
    spec = make_spec(dimensionality="axisymmetric", geometry=wedge_geometry(angle))
    assert plan_gate(spec, TASK, LIMITS).ok


@pytest.mark.parametrize("dimensionality", ["2D", "3D"])
def test_planner_gate_rejects_a_wedge_angle_unless_axisymmetric(dimensionality):
    thickness = 0.1 if dimensionality == "2D" else None  # a 3D plan has no thickness
    wedged = geometry(thickness_m=thickness, wedge_angle_deg=5.0)
    errors = plan_gate(
        make_spec(dimensionality=dimensionality, geometry=wedged), TASK, LIMITS
    ).errors
    assert len(errors) == 1 and "wedge_angle_deg" in errors[0] and "null" in errors[0]


def test_planner_gate_rejects_a_3d_plan_with_a_slab_thickness():
    spec = make_spec(dimensionality="3D", geometry=geometry(thickness_m=0.002))
    errors = plan_gate(spec, TASK, LIMITS).errors
    assert len(errors) == 1
    assert 'dimensionality is "3D" but geometry.thickness_m is set (0.002)' in errors[0]
    assert "thickness_m null" in errors[0]
    assert 'dimensionality "2D"' in errors[0] and "empty" in errors[0]


def test_planner_gate_rejects_a_3d_plan_with_a_wedge_angle_and_names_the_alternative():
    spec = make_spec(dimensionality="3D", geometry=geometry(thickness_m=None, wedge_angle_deg=5.0))
    errors = plan_gate(spec, TASK, LIMITS).errors
    assert len(errors) == 1
    assert 'dimensionality is "3D" but geometry.wedge_angle_deg is set (5)' in errors[0]
    assert 'dimensionality "axisymmetric"' in errors[0]


def test_planner_gate_accepts_a_3d_plan_without_thickness_and_wedge_angle():
    geom = geometry(thickness_m=None, wedge_angle_deg=None)
    assert gate_errors(dimensionality="3D", geometry=geom) == []


def test_planner_gate_rejects_an_axisymmetric_plan_with_a_thickness():
    wedge = {**wedge_geometry(5.0), "thickness_m": 0.002}
    errors = gate_errors(dimensionality="axisymmetric", geometry=wedge)
    assert len(errors) == 1 and 'dimensionality is "axisymmetric"' in errors[0]
    assert "thickness_m" in errors[0] and "null" in errors[0]


def test_planner_gate_allows_a_null_volume_but_not_a_nonpositive_one():
    assert gate_errors(geometry=geometry(volume_m3=None)) == []
    for bad in (0.0, -1.0, math.nan):
        errors = gate_errors(geometry=geometry(volume_m3=bad))
        assert len(errors) == 1 and "volume_m3" in errors[0]


# The volume must come from the calculator: the geometry stage holds the solid to it within 0.1 %.

VOLUME = 0.4  # the volume of the geometry of SPEC


def volume_errors(results, volume=VOLUME) -> list[str]:
    spec = make_spec(geometry={**SPEC["geometry"], "volume_m3": volume})
    return plan_gate(spec, TASK, LIMITS, results).errors


def test_gate_rejects_a_volume_that_no_calculator_call_returned():
    errors = volume_errors([100.0, 0.001])
    assert len(errors) == 1
    assert "geometry.volume_m3 is 0.4" in errors[0]
    assert "no calculator call in this conversation returned that value" in errors[0]
    assert "outer domain minus every body removed" in errors[0]
    assert "give null if it cannot be calculated exactly" in errors[0]
    assert volume_errors([]) == errors  # no call at all is the same


def test_gate_accepts_a_volume_that_a_calculator_call_returned_within_the_tolerance():
    assert volume_errors([100.0, 0.4]) == []
    assert volume_errors([0.4 * (1 + 0.9e-3)]) == []
    assert volume_errors([0.4 * (1 - 0.9e-3)]) == []
    assert volume_errors([0.4 * (1 + 1.2e-3)]) != []


def test_gate_does_not_check_the_volume_provenance_for_a_null_volume_or_unknown_results():
    assert volume_errors([], volume=None) == []
    assert volume_errors(None) == []  # the check is opt-in: no calculator results, no check
    assert plan_gate(make_spec(), TASK, LIMITS).ok  # the default of the argument


def test_the_volume_provenance_error_is_not_a_resource_error():
    errors = volume_errors([])
    assert errors and not any(e.startswith("exceeds resources:") for e in errors)


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"fluid": {"density_kg_m3": 0.0, "kinematic_viscosity_m2_s": 0.01}}, "density_kg_m3"),
        ({"fluid": {"density_kg_m3": -1.0, "kinematic_viscosity_m2_s": 0.01}}, "density_kg_m3"),
        ({"fluid": {"density_kg_m3": 1.0, "kinematic_viscosity_m2_s": -0.01}}, "kinematic"),
        (
            {"reference": {"length_m": 0.0, "velocity_m_s": 1.0, "reynolds_number": None}},
            "length_m",
        ),
        (
            {"reference": {"length_m": 1.0, "velocity_m_s": -1.0, "reynolds_number": None}},
            "velocity_m_s",
        ),
    ],
)
def test_planner_gate_rejects_unphysical_properties_even_without_a_reynolds_number(change, field):
    errors = gate_errors(**change)
    assert errors and any(field in e for e in errors)


def test_planner_gate_allows_zero_reference_velocity_only_without_a_reynolds_number():
    ok = {"length_m": 1.0, "velocity_m_s": 0.0, "reynolds_number": None}
    assert gate_errors(reference=ok) == []
    errors = gate_errors(reference={**ok, "reynolds_number": 100.0})
    assert len(errors) == 1 and "velocity" in errors[0]


NAN = math.nan


@pytest.mark.parametrize(
    "change",
    [
        {"fluid": {"density_kg_m3": NAN, "kinematic_viscosity_m2_s": 0.01}},
        {"fluid": {"density_kg_m3": 1.0, "kinematic_viscosity_m2_s": NAN}},
        {"reference": {"length_m": NAN, "velocity_m_s": 1.0, "reynolds_number": None}},
        {"reference": {"length_m": 1.0, "velocity_m_s": NAN, "reynolds_number": None}},
        {"reference": {"length_m": 1.0, "velocity_m_s": 1.0, "reynolds_number": NAN}},
        {"geometry": geometry(bbox=bbox(xmax=NAN))},
        {"geometry": geometry(bbox=bbox(zmin=NAN))},
        {"geometry": geometry(bbox=bbox(xmax=math.inf))},
        {"geometry": geometry(thickness_m=NAN)},
        {"geometry": geometry(volume_m3=NAN)},
        {"solver": {**SPEC["solver"], "end_time_s": NAN}},
        {"solver": {**SPEC["solver"], "end_time_s": math.inf}},
        {"resource_estimate": {"cells": 8000, "runtime_minutes": NAN, "memory_gb": 0.5}},
        {"resource_estimate": {"cells": 8000, "runtime_minutes": 5.0, "memory_gb": NAN}},
        {"resource_estimate": {"cells": 8000, "runtime_minutes": math.inf, "memory_gb": 0.5}},
        {"time_dependence": "transient", "solver": {**SPEC["solver"], "time_step_s": NAN}},
    ],
)
def test_planner_gate_never_lets_a_non_finite_number_pass(change):
    assert not plan_gate(make_spec(**change), TASK, LIMITS).ok


@pytest.mark.parametrize(
    "estimate",
    [
        {"cells": 0, "runtime_minutes": 5.0, "memory_gb": 0.5},
        {"cells": -5, "runtime_minutes": -1.0, "memory_gb": -2.0},
        {"cells": 8000, "runtime_minutes": 0.0, "memory_gb": 0.5},
        {"cells": 8000, "runtime_minutes": 5.0, "memory_gb": -2.0},
    ],
)
def test_planner_gate_rejects_nonsense_resource_estimates_without_the_resource_prefix(estimate):
    errors = gate_errors(resource_estimate=estimate)
    assert errors
    assert not any(e.startswith("exceeds resources:") for e in errors)  # retried, not terminal


def test_planner_gate_rejects_a_nonpositive_target_cell_count():
    errors = gate_errors(mesh={"target_cells": 0, "rationale": "x"})
    assert any("target_cells" in e for e in errors)


def test_planner_gate_counts_the_target_mesh_against_the_cell_limit():
    mesh = {"target_cells": 50_000_000, "rationale": "fine"}
    errors = gate_errors(mesh=mesh)  # the estimate says 8000 cells: contradictory
    prefixed = [e for e in errors if e.startswith("exceeds resources:")]
    assert len(prefixed) == 1 and "50000000" in prefixed[0]
    assert any("smaller than mesh.target_cells" in e for e in errors)  # so it is retried
    both = {"cells": 50_000_000, "runtime_minutes": 5.0, "memory_gb": 0.5}
    only = gate_errors(mesh=mesh, resource_estimate=both)
    assert len(only) == 1 and only[0].startswith("exceeds resources:")


def test_planner_gate_requires_the_estimate_to_cover_the_target_mesh():
    mesh = {"target_cells": 10_000, "rationale": "x"}
    errors = gate_errors(mesh=mesh)  # estimate: 8000 cells
    assert len(errors) == 1 and "resource_estimate.cells" in errors[0]
    assert not errors[0].startswith("exceeds resources:")
    covered = {"cells": 10_000, "runtime_minutes": 5.0, "memory_gb": 0.5}
    assert gate_errors(mesh=mesh, resource_estimate=covered) == []


@pytest.mark.parametrize("step", [None, 0.0, -0.1, 1000.0, 2000.0, NAN])
def test_planner_gate_requires_a_usable_time_step_for_transient_cases(step):
    solver = {**SPEC["solver"], "time_step_s": step}  # end_time_s is 1000
    errors = gate_errors(time_dependence="transient", solver=solver)
    assert len(errors) == 1 and "time_step_s" in errors[0]


def test_planner_gate_accepts_a_good_transient_step_and_ignores_the_step_when_steady():
    solver = {**SPEC["solver"], "time_step_s": 0.5}
    assert gate_errors(time_dependence="transient", solver=solver) == []
    assert gate_errors(time_dependence="steady", solver=solver) == []


def test_planner_gate_collects_independent_errors():
    spec = make_spec(solver={**SPEC["solver"], "end_time_s": 0.0})
    result = plan_gate(spec, "Report `missing_one`.", LIMITS)
    assert len(result.errors) == 2


# ---------------------------------------------------------------------------- the loop


def test_planner_writes_casespec_json(tmp_path):
    steps = [
        reply(None, calc("1 * 1 / 0.01")),
        reply("Re = 100 gives nu = 0.01."),
        reply(spec_json()),
    ]
    ctx, _ = make_ctx(tmp_path, steps)
    outcome = run_planner(ctx, TASK)
    assert outcome.ok and outcome.attempts == 1 and outcome.errors == [] and outcome.reason is None
    written = CaseSpec.model_validate_json((tmp_path / "run" / "casespec.json").read_text("utf-8"))
    assert written == plan_spec()
    assert ctx.casespec == written


WITH_VOLUME = spec_json(geometry={**SPEC["geometry"], "volume_m3": VOLUME})


def test_planner_accepts_a_volume_the_calculator_returned(tmp_path):
    steps = [reply(None, calc("4 * 1 * 0.1")), reply("Volume 0.4."), reply(WITH_VOLUME)]
    ctx, _ = make_ctx(tmp_path, steps)
    outcome = run_planner(ctx, TASK)
    assert outcome.ok and outcome.attempts == 1
    assert ctx.casespec is not None and ctx.casespec.geometry.volume_m3 == VOLUME


def test_planner_rejects_a_volume_no_calculator_call_returned(tmp_path):
    """The stage hands the calculator results of the run to the gate: a volume written without
    a calculation is sent back, with the error in the rework message."""
    steps = [
        reply(None, calc("1 * 1 / 0.01")),
        reply("Plan ready."),
        reply(WITH_VOLUME),
        reply("Reworked plan."),
        reply(spec_json()),
    ]
    ctx, backend = make_ctx(tmp_path, steps)
    outcome = run_planner(ctx, TASK)
    assert outcome.ok and outcome.attempts == 2
    assert (
        "no calculator call in this conversation returned that value"
        in (backend.calls[3].messages[-1]["content"])
    )
    assert [a.ok for a in ctx.record.stages[-1].attempts] == [False, True]
    assert ctx.casespec is not None and ctx.casespec.geometry.volume_m3 is None


def test_planner_counts_calculator_results_of_the_rework_round(tmp_path):
    steps = [
        reply("Plan ready."),
        reply(WITH_VOLUME),
        reply(None, calc("4 * 1 * 0.1")),
        reply("Reworked plan: volume 0.4."),
        reply(WITH_VOLUME),
    ]
    ctx, _ = make_ctx(tmp_path, steps)
    outcome = run_planner(ctx, TASK)
    assert outcome.ok and outcome.attempts == 2
    assert ctx.casespec is not None and ctx.casespec.geometry.volume_m3 == VOLUME


def test_planner_does_not_count_a_failed_calculator_call_or_a_result_that_is_not_a_number(
    tmp_path,
):
    steps = [
        reply(None, ToolCall("c1", "calculator", {"expression": "import os"})),
        reply(None, ToolCall("c2", "calculator", {"expression": ""})),
        reply("Plan ready."),
        reply(WITH_VOLUME),
        reply("Reworked plan."),
        reply(WITH_VOLUME),
        reply("Reworked again."),
        reply(WITH_VOLUME),
    ]
    ctx, _ = make_ctx(tmp_path, steps)
    outcome = run_planner(ctx, TASK)
    assert not outcome.ok and outcome.attempts == 3
    assert outcome.reason and "geometry.volume_m3" in outcome.reason


def test_the_scripted_pipeline_fixture_plans_a_volume_its_calculator_calls_produce(tmp_path):
    path = Path(__file__).resolve().parents[1] / "fixtures" / "scripted" / "cavity_pipeline.json"
    steps = ScriptedBackend.from_json(path)._steps[:3]  # the Planner's three replies
    ctx, _ = make_ctx(tmp_path, steps)
    outcome = run_planner(ctx, "Simulate the flow. Report `u_vertical_centreline`. Re = 100.")
    assert outcome.ok and outcome.attempts == 1, outcome.reason
    assert ctx.casespec is not None and ctx.casespec.geometry.volume_m3 is not None


def test_planner_phase_one_uses_calculator_only_and_phase_two_uses_the_schema(tmp_path):
    steps = [
        reply(None, calc("1 * 1 / 0.01")),
        reply("Re = 100 gives nu = 0.01."),
        reply(spec_json()),
    ]
    ctx, backend = make_ctx(tmp_path, steps)
    run_planner(ctx, TASK)
    first, second, third = backend.calls
    assert [t["function"]["name"] for t in first.tools] == ["calculator"]
    assert first.response_schema is None
    assert first.messages[0] == {"role": "system", "content": load_prompt("planner").text}
    assert TASK in first.messages[1]["content"]
    # the calculator result reached the model
    assert any(m["role"] == "tool" and m["content"] == "100" for m in second.messages)
    # phase 2: schema, no tools, the phase-1 conversation, the structured token budget
    assert third.response_schema is CaseSpec
    assert not third.tools
    assert any(m["role"] == "tool" and m["content"] == "100" for m in third.messages)
    assert third.model.max_tokens == ctx.config.planner_structured_max_tokens
    assert first.model.max_tokens == ctx.config.models["planner"].max_tokens
    # the identifiers the task names are spelled out for the model
    assert "centreline_u" in third.messages[-1]["content"]
    assert "drag" in third.messages[-1]["content"]


def test_planner_receives_fix_request(tmp_path):
    steps = [reply("A plan."), reply(spec_json())]
    ctx, backend = make_ctx(tmp_path, steps)
    ctx.fix_request = RouteRequest(
        source="critic",
        target="planner",
        origin="critic",
        reason="the Reynolds number of the task was not used",
        instruction="use the stated value",
    )
    ctx.pass_no = 2
    outcome = run_planner(ctx, TASK)
    assert outcome.ok
    opening = backend.calls[0].messages[1]["content"]
    text = (
        "Fix requested by critic: the Reynolds number of the task was not used. "
        "use the stated value"
    )
    assert TASK in opening and text in opening
    assert opening.index(TASK) < opening.index(text)
    assert ctx.record.stages[-1].pass_no == 2
    lines = (tmp_path / "run" / TRANSCRIPT_FILE).read_text("utf-8").splitlines()
    assert {json.loads(line)["pass_no"] for line in lines} == {2}


def test_planner_without_a_fix_request_has_the_usual_opening(tmp_path):
    ctx, backend = make_ctx(tmp_path, [reply("A plan."), reply(spec_json())])
    assert run_planner(ctx, TASK).ok
    assert "Fix requested" not in backend.calls[0].messages[1]["content"]
    assert ctx.record.stages[-1].pass_no == 0


def test_planner_records_attempts_cost_and_transcript(tmp_path):
    progress: list[str] = []
    steps = [reply("No tool needed."), reply("not json at all"), reply(spec_json())]
    ctx, _ = make_ctx(tmp_path, steps, progress=progress)
    outcome = run_planner(ctx, TASK)
    assert outcome.ok and outcome.attempts == 2
    stage = ctx.record.stages[-1]
    assert stage.name == "planner" and stage.ok
    assert [a.ok for a in stage.attempts] == [False, True]
    assert stage.attempts[0].errors
    assert progress and set(progress) == {"planner"}
    assert ctx.cost.total_usd > 0
    lines = (tmp_path / "run" / TRANSCRIPT_FILE).read_text("utf-8").splitlines()
    entries = [json.loads(line) for line in lines]
    assert len(entries) == 3
    assert {e["stage"] for e in entries} == {"planner"}
    assert {e["agent"] for e in entries} == {"planner"}
    assert [e["attempt"] for e in entries] == [1, 1, 2]
    assert entries[1]["error"] and "StructuredOutputError" in entries[1]["error"]


def test_planner_retries_after_invalid_reply_then_succeeds(tmp_path):
    steps = [reply("Plan ready."), reply('{"title": "incomplete"}'), reply(spec_json())]
    ctx, backend = make_ctx(tmp_path, steps)
    outcome = run_planner(ctx, TASK)
    assert outcome.ok and outcome.attempts == 2
    problem = backend.calls[2].messages[-1]
    assert problem["role"] == "user"
    assert "schema" in problem["content"].lower()
    assert ctx.casespec == plan_spec()


def test_planner_retries_after_a_truncated_reply(tmp_path):
    steps = [reply("Plan ready."), TruncatedOutputError('{"title": "cut'), reply(spec_json())]
    ctx, backend = make_ctx(tmp_path, steps)
    outcome = run_planner(ctx, TASK)
    assert outcome.ok and outcome.attempts == 2
    assert "cut off" in backend.calls[2].messages[-1]["content"]


BAD = spec_json(requested_outputs=[SPEC["requested_outputs"][1]])  # lacks `centreline_u`


def test_planner_feeds_gate_errors_back_and_retries(tmp_path):
    steps = [reply("Plan ready."), reply(BAD), reply("Reworked plan."), reply(spec_json())]
    ctx, backend = make_ctx(tmp_path, steps)
    outcome = run_planner(ctx, TASK)
    assert outcome.ok and outcome.attempts == 2
    assert "`centreline_u`" in backend.calls[2].messages[-1]["content"]
    # the rejected reply stays in the conversation so the model can correct it
    assert any(m["role"] == "assistant" and m["content"] == BAD for m in backend.calls[3].messages)
    assert [a.ok for a in ctx.record.stages[-1].attempts] == [False, True]


def test_a_gate_rejection_leads_to_a_rework_round_with_the_calculator(tmp_path):
    steps = [
        reply("Plan ready."),
        reply(BAD),
        reply(None, calc("2 * 3")),
        reply("Reworked plan: 6."),
        reply(spec_json()),
    ]
    ctx, backend = make_ctx(tmp_path, steps)
    outcome = run_planner(ctx, TASK)
    assert outcome.ok and outcome.attempts == 2
    plan, structured, rework, rework_answer, final = backend.calls
    assert plan.response_schema is None and structured.response_schema is CaseSpec
    # the rework round: plain text, the calculator offered, the normal phase-1 token budget
    for call in (rework, rework_answer):
        assert call.response_schema is None
        assert [t["function"]["name"] for t in call.tools] == ["calculator"]
        assert call.model.max_tokens == ctx.config.models["planner"].max_tokens
    assert any(m["role"] == "tool" and m["content"] == "6" for m in rework_answer.messages)
    # the structured call after it: schema, no tools, the structured token budget
    assert final.response_schema is CaseSpec and not final.tools
    assert final.model.max_tokens == ctx.config.planner_structured_max_tokens
    request = final.messages[-1]
    assert request["role"] == "user"
    assert "centreline_u" in request["content"] and "drag" in request["content"]
    assert any(m["role"] == "tool" and m["content"] == "6" for m in final.messages)
    assert ctx.casespec == plan_spec()


def test_the_rework_message_lists_the_errors_and_asks_for_a_plain_text_plan(tmp_path):
    steps = [reply("Plan ready."), reply(BAD), reply("Reworked plan."), reply(spec_json())]
    ctx, backend = make_ctx(tmp_path, steps)
    assert run_planner(ctx, TASK).ok
    message = backend.calls[2].messages[-1]
    assert message["role"] == "user"
    text = message["content"]
    assert "rejected" in text and "attempt 1 of 3" in text
    assert "`centreline_u`" in text  # the gate error itself
    assert "calculator" in text and "plain text" in text
    assert "not JSON yet" in text


def test_a_very_long_error_list_is_shortened_but_the_instruction_stays(tmp_path):
    names = [f"name_{i}_{'x' * 200}" for i in range(40)]
    task = "Report " + ", ".join(f"`{n}`" for n in names) + "."
    steps = [reply("Plan ready."), reply(spec_json()), reply("Reworked."), reply("{}"), reply("{}")]
    ctx, backend = make_ctx(tmp_path, steps)
    run_planner(ctx, task)
    text = backend.calls[2].messages[-1]["content"]
    assert "calculator" in text and "not JSON yet" in text
    assert len(text) < 6000


def test_a_schema_invalid_reply_is_retried_in_phase_two_without_a_rework_round(tmp_path):
    steps = [reply("Plan ready."), reply('{"title": "incomplete"}'), reply(spec_json())]
    ctx, backend = make_ctx(tmp_path, steps)
    assert run_planner(ctx, TASK).ok
    assert len(backend.calls) == 3
    retry = backend.calls[2]
    assert retry.response_schema is CaseSpec and not retry.tools
    assert retry.model.max_tokens == ctx.config.planner_structured_max_tokens


def test_the_rework_round_has_its_own_turn_budget(tmp_path):
    steps = [reply("Plan ready."), reply(BAD)]
    steps += [reply(None, calc(f"{i} + 1", f"r{i}")) for i in range(1, 9)]  # never stops by itself
    steps += [reply(spec_json())]
    ctx, backend = make_ctx(tmp_path, steps)
    outcome = run_planner(ctx, TASK)
    assert outcome.ok and outcome.attempts == 2
    assert len(backend.calls) == 2 + 8 + 1  # the rework round is cut at eight turns
    assert all(c.tools for c in backend.calls[2:10]) and not backend.calls[10].tools
    final = backend.calls[10]
    assert final.response_schema is CaseSpec
    ids = [c["id"] for m in final.messages if m.get("tool_calls") for c in m["tool_calls"]]
    answered = [m["tool_call_id"] for m in final.messages if m["role"] == "tool"]
    assert ids == answered and len(ids) == 8


def test_every_rework_round_gets_a_fresh_turn_budget(tmp_path):
    steps = [reply("Plan ready.")]
    for n in range(2):  # two rejections, each followed by a tool loop that never stops itself
        steps.append(reply(BAD))
        steps += [reply(None, calc("1 + 1", f"c{n}_{i}")) for i in range(8)]
    steps.append(reply(spec_json()))
    ctx, backend = make_ctx(tmp_path, steps)
    outcome = run_planner(ctx, TASK)
    assert outcome.ok and outcome.attempts == 3
    assert len(backend.calls) == 1 + 2 * 9 + 1


def test_the_rework_round_stops_at_the_wall_clock(tmp_path):
    clock = FakeClock()
    steps = [reply("Plan ready."), reply(BAD), reply(None, calc("1 + 1")), reply(spec_json())]
    ctx, _ = make_ctx(tmp_path, steps, clock=clock)

    class UsesUpTheTime(ScriptedBackend):
        def chat(self, *args, **kwargs):
            result = super().chat(*args, **kwargs)
            if len(self.calls) == 3:  # the first call of the rework round
                clock.now += ctx.config.limits.wall_clock_s + 1
            return result

    backend = UsesUpTheTime(steps)
    ctx.backend = backend
    outcome = run_planner(ctx, TASK)
    assert not outcome.ok
    assert outcome.reason and "wall-clock" in outcome.reason
    assert len(backend.calls) == 3  # no structured call after the abort
    assert ctx.casespec is None


def test_a_cut_off_reply_in_the_rework_round_is_reported_in_plain_text_terms(tmp_path):
    steps = [
        reply("Plan ready."),
        reply(BAD),
        reply("a very long plan", finish_reason="length"),
        reply("Short plan."),
        reply(spec_json()),
    ]
    ctx, backend = make_ctx(tmp_path, steps)
    assert run_planner(ctx, TASK).ok
    note = backend.calls[3].messages[-1]["content"]
    assert "output limit" in note and "CaseSpec" not in note


def test_a_schema_invalid_reply_after_a_rework_round_gets_the_schema_note(tmp_path):
    steps = [
        reply("Plan ready."),
        reply(BAD),
        reply("Reworked."),
        reply('{"title": "incomplete"}'),
        reply(spec_json()),
    ]
    ctx, backend = make_ctx(tmp_path, steps)
    outcome = run_planner(ctx, TASK)
    assert outcome.ok and outcome.attempts == 3
    retry = backend.calls[4]
    assert "does not match the CaseSpec schema" in retry.messages[-1]["content"]
    assert retry.response_schema is CaseSpec and not retry.tools
    assert retry.model.max_tokens == ctx.config.planner_structured_max_tokens


def test_a_cut_off_structured_reply_after_a_rework_round_gets_the_shorter_advice(tmp_path):
    steps = [
        reply("Plan ready."),
        reply(BAD),
        reply("Reworked."),
        reply('{"title": "cut', finish_reason="length"),
        reply(spec_json()),
    ]
    ctx, backend = make_ctx(tmp_path, steps)
    assert run_planner(ctx, TASK).ok
    note = backend.calls[4].messages[-1]["content"]
    assert "complete CaseSpec as one JSON object" in note


def test_planner_gives_up_after_three_attempts(tmp_path):
    steps = [reply("Plan ready."), reply("{}"), reply("[]"), reply("nope")]
    ctx, backend = make_ctx(tmp_path, steps)
    outcome = run_planner(ctx, TASK)
    assert not outcome.ok
    assert outcome.attempts == 3
    assert outcome.errors
    assert outcome.reason and "planner" in outcome.reason and "3" in outcome.reason
    assert len(backend.calls) == 4  # one phase-1 call and three structured attempts
    assert not (tmp_path / "run" / "casespec.json").exists()
    assert ctx.casespec is None
    stage = ctx.record.stages[-1]
    assert not stage.ok and [a.ok for a in stage.attempts] == [False, False, False]


def test_planner_respects_the_attempt_limit_of_the_run(tmp_path):
    steps = [reply("Plan ready."), reply("{}"), reply("{}")]
    ctx, _ = make_ctx(tmp_path, steps, limits=RunLimits(max_attempts_per_stage=2))
    outcome = run_planner(ctx, TASK)
    assert not outcome.ok and outcome.attempts == 2


def test_planner_does_not_retry_when_only_resources_are_exceeded(tmp_path):
    big = {"cells": 5_000_000, "runtime_minutes": 5.0, "memory_gb": 0.5}
    steps = [reply("Plan ready."), reply(spec_json(resource_estimate=big))]  # no third step
    ctx, backend = make_ctx(tmp_path, steps)
    outcome = run_planner(ctx, TASK)
    assert not outcome.ok
    assert outcome.attempts == 1
    assert outcome.reason and outcome.reason.startswith("exceeds resources:")
    assert len(backend.calls) == 2
    written = json.loads((tmp_path / "run" / "casespec.json").read_text("utf-8"))
    assert written["resource_estimate"]["cells"] == 5_000_000  # the recorded estimate
    stage = ctx.record.stages[-1]
    assert not stage.ok and len(stage.attempts) == 1
    assert ctx.casespec is None  # the plan is recorded, but it is not an accepted plan


def test_planner_retries_when_resources_are_exceeded_together_with_other_errors(tmp_path):
    big = {"cells": 5_000_000, "runtime_minutes": 5.0, "memory_gb": 0.5}
    bad = spec_json(resource_estimate=big, solver={**SPEC["solver"], "end_time_s": 0.0})
    steps = [reply("Plan ready."), reply(bad), reply("Reworked plan."), reply(spec_json())]
    ctx, backend = make_ctx(tmp_path, steps)
    outcome = run_planner(ctx, TASK)
    assert outcome.ok and outcome.attempts == 2
    feedback = backend.calls[2].messages[-1]["content"]
    assert "exceeds resources:" in feedback and "end_time_s" in feedback


def test_planner_moves_on_after_eight_tool_turns(tmp_path):
    steps = [reply(None, calc(f"{i} + 1", f"c{i}")) for i in range(1, 9)]
    steps += [reply(spec_json())]  # the ninth call is the structured one
    ctx, backend = make_ctx(tmp_path, steps)
    outcome = run_planner(ctx, TASK)
    assert outcome.ok
    assert len(backend.calls) == 9
    assert backend.calls[8].response_schema is CaseSpec
    # every tool call of phase 1 was answered before phase 2 started
    final = backend.calls[8].messages
    ids = [c["id"] for m in final if m.get("tool_calls") for c in m["tool_calls"]]
    answered = [m["tool_call_id"] for m in final if m["role"] == "tool"]
    assert ids == answered and len(ids) == 8


def test_planner_survives_a_bad_tool_call_in_phase_one(tmp_path):
    steps = [
        reply(None, ToolCall("c1", "calculator", {"expression": "import os"})),
        reply(None, ToolCall("c2", "write_file", {"path": "x", "content": "y"})),
        reply("Done."),
        reply(spec_json()),
    ]
    ctx, backend = make_ctx(tmp_path, steps)
    assert run_planner(ctx, TASK).ok
    tool_texts = [m["content"] for m in backend.calls[2].messages if m["role"] == "tool"]
    assert len(tool_texts) == 2 and all(t.startswith("error") for t in tool_texts)


def test_planner_stops_at_the_cost_limit(tmp_path):
    steps = [reply("Plan ready."), reply(spec_json())]
    ctx, _ = make_ctx(tmp_path, steps, cost_limit=0.0000001)
    outcome = run_planner(ctx, TASK)
    assert not outcome.ok
    assert outcome.reason and "cost limit" in outcome.reason
    assert ctx.casespec is None


def test_planner_stops_when_the_wall_clock_is_used_up(tmp_path):
    clock = FakeClock()
    ctx, backend = make_ctx(tmp_path, [reply("never asked")], clock=clock)
    clock.now += ctx.config.limits.wall_clock_s + 1
    outcome = run_planner(ctx, TASK)
    assert not outcome.ok
    assert outcome.reason and "wall-clock" in outcome.reason
    assert backend.calls == []


def test_planner_reports_a_failing_model_service(tmp_path):
    down = LLMTransportError("boom", status_code=400, retryable=False)
    ctx, _ = make_ctx(tmp_path, [reply("Plan ready."), down])
    outcome = run_planner(ctx, TASK)
    assert not outcome.ok
    assert outcome.reason and "model service failed" in outcome.reason


def test_planner_retries_a_transient_service_error_with_a_pause(tmp_path):
    sleeps: list[float] = []
    steps = [
        LLMTransportError("busy", status_code=429, retryable=True),
        reply("Plan ready."),
        reply(spec_json()),
    ]
    ctx, _ = make_ctx(tmp_path, steps)
    ctx.sleep = sleeps.append
    assert run_planner(ctx, TASK).ok
    assert len(sleeps) == 1


def test_planner_lets_a_schema_error_propagate(tmp_path):
    ctx, _ = make_ctx(tmp_path, [reply("Plan ready."), SchemaError("programming error")])
    with pytest.raises(SchemaError):
        run_planner(ctx, TASK)


def test_scripted_backend_raises_structured_error_for_an_invalid_reply():
    """Guards the assumption of the retry tests: an invalid reply raises StructuredOutputError."""
    backend = ScriptedBackend([reply("{}")])
    with pytest.raises(StructuredOutputError):
        backend.chat([], RunConfig.default().models["planner"], response_schema=CaseSpec)


def test_planner_prompt_states_the_required_instructions():
    text = load_prompt("planner").text
    for phrase in (
        "calculator",
        "assumptions",
        "SI units",
        "Flow regime",
        "symmetry",
        "use it exactly as the `name`",
        "two separate `wedge` boundaries, one per face",
        "one `empty` boundary",
        "letters, digits and underscores",
        "axis line",
        "requested_outputs",
    ):
        assert phrase in text, phrase


def test_planner_prompt_defines_the_volume_as_the_fluid_region_only():
    text = load_prompt("planner").text
    assert "`volume_m3` is the volume of the fluid region only" in text
    assert "minus the part of every solid body, hole or obstacle that lies inside it" in text
    assert "overlapping parts counted once" in text
    assert "in-plane fluid area" in text
    assert "full fluid solid of revolution" in text
    assert "if any of them cannot, give `null`, never the outer volume" in text
    # The negative assertion guards the old wording, which let the outer area slip in.
    assert "in-plane area times" not in text


def test_geometry_prompt_agrees_that_the_checked_volume_is_the_fluid_domain():
    text = load_prompt("geometry").text
    assert "(the fluid domain, bodies cut out)" in text


def test_geometry_prompt_forbids_changing_the_solid_to_meet_the_volume():
    text = load_prompt("geometry").text
    assert "Build exactly the solid the plan describes." in text
    assert "Never add or cut material that the description does not call for" in text
    assert "just to meet `volume_m3`" in text
    assert "matches the description, the bbox and the key dimensions but not the volume" in text
    assert "say so plainly in your reply, with both numbers" in text
    assert "the plan's volume may be wrong" in text


def test_planner_prompt_explains_periodic_outputs():
    text = load_prompt("planner").text
    assert "periodic_outputs" in text
    assert "shedding" in text or "periodic" in text  # when to set it, in general words


def test_a_steady_plan_cannot_ask_for_periodic_outputs():
    errors = gate_errors(time_dependence="steady", periodic_outputs=True)
    assert len(errors) == 1 and "periodic_outputs" in errors[0] and "transient" in errors[0]
    assert gate_errors(time_dependence="steady", periodic_outputs=False) == []
    transient = {**SPEC["solver"], "time_step_s": 0.5}
    assert gate_errors(time_dependence="transient", periodic_outputs=True, solver=transient) == []


def test_planner_gives_up_when_the_gate_rejects_every_attempt(tmp_path):
    bad = spec_json(solver={**SPEC["solver"], "end_time_s": 0.0})
    steps = [reply("Plan ready."), reply(bad), reply("Rework 1."), reply(bad), reply("Rework 2.")]
    steps.append(reply(bad))
    ctx, backend = make_ctx(tmp_path, steps)
    outcome = run_planner(ctx, TASK)
    assert not outcome.ok and outcome.attempts == 3
    assert any("end_time_s" in e for e in outcome.errors)
    assert outcome.reason and "end_time_s" in outcome.reason
    assert not (tmp_path / "run" / "casespec.json").exists()
    assert ctx.casespec is None
    # a plan, three structured replies and a rework round between them; none after the last
    assert len(backend.calls) == 6


def test_planner_discards_a_valid_looking_reply_cut_off_at_the_length_limit(tmp_path):
    steps = [
        reply("Plan ready."),
        reply(spec_json(), finish_reason="length"),  # parses, but may be incomplete in meaning
        reply(spec_json()),
    ]
    ctx, backend = make_ctx(tmp_path, steps)
    outcome = run_planner(ctx, TASK)
    assert outcome.ok and outcome.attempts == 2
    retry = backend.calls[2].messages
    assert "output limit" in retry[-1]["content"] and "CaseSpec" in retry[-1]["content"]
    assert not any(m["role"] == "assistant" and m["content"] == spec_json() for m in retry)


def test_planner_retries_after_any_other_unusable_reply(tmp_path):
    steps = [reply("Plan ready."), EmptyChoicesError("no choices"), reply(spec_json())]
    ctx, backend = make_ctx(tmp_path, steps)
    outcome = run_planner(ctx, TASK)
    assert outcome.ok and outcome.attempts == 2
    text = backend.calls[2].messages[-1]["content"]
    assert "EmptyChoicesError" in text and "CaseSpec" in text
    assert "tool call" not in text  # phase 2 has no tools


def test_end_time_is_described_as_the_iteration_count_for_a_steady_case():
    from cfdagent.agents.casespec import SolverSpec

    description = SolverSpec.model_fields["end_time_s"].description
    assert "steady" in description and "iterations" in description and "never 0" in description
    text = load_prompt("planner").text
    assert "never 0" in text
