"""Unit tests for the Geometry stage with a fake sandbox (no executor, no CadQuery)."""

import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_casespec import make_spec

from cfdagent.agents.geometry import (
    GATE_TIMEOUT_S,
    MANIFEST_MAX_BYTES,
    SCRIPT_TIMEOUT_S,
    GeometryStage,
    _last_bytes,
)
from cfdagent.agents.loop import run_stage
from cfdagent.agents.stage import StageContext
from cfdagent.agents.transcript import TranscriptWriter
from cfdagent.config import RunConfig
from cfdagent.errors import ToolFailure
from cfdagent.executor.protocol import OpResponse
from cfdagent.llm.cost import PRICING, CostTracker
from cfdagent.llm.scripted import ScriptedBackend
from cfdagent.prompts import load_prompt
from cfdagent.record import RunRecord
from cfdagent.rundir import RunDir
from cfdagent.sandbox.client import OUTER_TIMEOUT_MARGIN_S

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
ATTEMPT = "attempts/geometry-1"


def plane(axis, value):
    return {"kind": "plane", "axis": axis, "value": value}


def manifest(names=("inlet", "outlet", "walls", "frontAndBack"), **changes):
    data = {
        "dimensionality": "2D",
        "patches": [
            {"name": names[0], "type": "patch", "locators": [plane("x", 0.0)]},
            {"name": names[1], "type": "patch", "locators": [plane("x", 4.0)]},
            {"name": names[2], "type": "wall", "locators": [plane("y", -0.5), plane("y", 0.5)]},
            {
                "name": names[3],
                "type": "empty",
                "locators": [plane("z", 0.0), plane("z", 0.1)],
            },
        ],
    }
    data.update(changes)
    return data


def plan(**changes):
    """The default plan of these tests: a 2D box whose boundaries match ``manifest()``."""
    names = ("inlet", "outlet", "walls", "frontAndBack")
    boundaries = [{"name": n, "role": "x", "description": "x"} for n in names]
    return make_spec(boundaries=boundaries, **changes)


class FakeSandbox:
    """Answers ops from a table of ``OpResponse`` values (or callables); records every call."""

    def __init__(self, responses):
        self.responses = dict(responses)
        self.calls = []

    def call(self, op, args, timeout_s):
        self.calls.append((op, args, timeout_s))
        response = self.responses[op]
        return response(args) if callable(response) else response


RUN_OK = OpResponse(ok=True, data={"tail": "built a box", "returncode": 0})
GATE_OK = OpResponse(ok=True, data={"ok": True, "errors": [], "report": None})


def gate_failed(*errors):
    return OpResponse(
        ok=False,
        data={"ok": False, "errors": list(errors), "report": None},
        error="\n".join(errors),
    )


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


def make_ctx(tmp_path, sandbox, steps=(), spec=None, clock=None):
    root = tmp_path / "run"
    root.mkdir(exist_ok=True)
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
    extra = {"clock": clock} if clock is not None else {}
    return StageContext(
        run=run,
        config=RunConfig.default(),
        backend=ScriptedBackend(list(steps)),
        sandbox=sandbox,
        cost=cost,
        record=record,
        transcript=TranscriptWriter(run, cost),
        started_at=(clock or time.monotonic)(),
        casespec=plan() if spec is None else spec,
        **extra,
    )


def attempt(ctx, number=1, script=True, manifest_data=None, manifest_text=None):
    path = ctx.run.attempt_dir("geometry", number)
    if script:
        (path / "geometry.py").write_text("print('hello')\n", encoding="utf-8")
    if manifest_text is not None:
        (path / "boundary_manifest.json").write_text(manifest_text, encoding="utf-8")
    elif manifest_data is not None:
        (path / "boundary_manifest.json").write_text(json.dumps(manifest_data), encoding="utf-8")
    return path


def good_attempt(ctx, number=1):
    return attempt(ctx, number, manifest_data=manifest())


# ------------------------------------------------------------------ the gate


def test_gate_passes_and_sends_the_plan_numbers_to_the_sandbox(tmp_path):
    sandbox = FakeSandbox({"run_script": RUN_OK, "check_geometry": GATE_OK})
    ctx = make_ctx(tmp_path, sandbox)
    path = good_attempt(ctx)
    result = GeometryStage().execute_and_gate(ctx, path)
    assert result.ok and result.errors == []
    (run_op, run_args, run_timeout), (check_op, check_args, check_timeout) = sandbox.calls
    assert run_op == "run_script"
    assert run_args.script == f"{ATTEMPT}/geometry.py"
    assert run_args.cwd == ATTEMPT
    assert run_args.kind == "geometry"
    assert run_args.timeout_s == SCRIPT_TIMEOUT_S
    assert run_timeout == SCRIPT_TIMEOUT_S + OUTER_TIMEOUT_MARGIN_S
    assert check_op == "check_geometry"
    assert check_args.workdir == ATTEMPT
    assert check_args.expected_bbox == [0.0, -0.5, 0.0, 4.0, 0.5, 0.1]
    assert check_args.expected_volume == 0.4
    assert check_args.rel_tol == 1e-3
    assert check_timeout == GATE_TIMEOUT_S + OUTER_TIMEOUT_MARGIN_S


def test_gate_operation_timeouts_are_clipped_to_the_time_left(tmp_path):
    clock = Clock()
    sandbox = FakeSandbox({"run_script": RUN_OK, "check_geometry": GATE_OK})
    ctx = make_ctx(tmp_path, sandbox, clock=clock)
    clock.now += ctx.config.limits.wall_clock_s - 50  # 50 s of the run are left
    GeometryStage().execute_and_gate(ctx, good_attempt(ctx))
    assert sandbox.calls[0][1].timeout_s == pytest.approx(50)
    assert sandbox.calls[1][2] == pytest.approx(50 + OUTER_TIMEOUT_MARGIN_S)


def test_gate_errors_include_script_tail(tmp_path):
    tail = "Traceback (most recent call last):\n  File geometry.py, line 7\nValueError: boom"
    failed = OpResponse(
        ok=False,
        data={"tail": tail, "returncode": 1, "timed_out": False},
        error=f"the script 'attempts/geometry-1/geometry.py' exited with code 1\n{tail}",
    )
    sandbox = FakeSandbox({"run_script": failed})
    ctx = make_ctx(tmp_path, sandbox)
    result = GeometryStage().execute_and_gate(ctx, good_attempt(ctx))
    assert not result.ok
    text = "\n".join(result.errors)
    assert "ValueError: boom" in text
    assert "exited with code 1" in text
    assert [call[0] for call in sandbox.calls] == ["run_script"]  # nothing to check


def test_a_long_script_tail_keeps_its_end_and_stays_small(tmp_path):
    tail = "noise line\n" * 600 + "RuntimeError: the real problem"
    failed = OpResponse(
        ok=False, data={"tail": tail, "returncode": 1}, error=f"exited with code 1\n{tail}"
    )
    ctx = make_ctx(tmp_path, FakeSandbox({"run_script": failed}))
    result = GeometryStage().execute_and_gate(ctx, good_attempt(ctx))
    text = "\n".join(result.errors)
    assert "RuntimeError: the real problem" in text
    assert len(text.encode("utf-8")) <= 4000  # the loop shows the model at most 4096 bytes


def test_a_script_that_timed_out_is_the_agents_failure(tmp_path):
    failed = OpResponse(
        ok=False,
        data={"timed_out": True, "tail": "still meshing"},
        error="the script 'x' timed out after 300 s and was killed\nstill meshing",
    )
    ctx = make_ctx(tmp_path, FakeSandbox({"run_script": failed}))
    result = GeometryStage().execute_and_gate(ctx, good_attempt(ctx))
    assert not result.ok and "timed out" in result.errors[0]


def test_gate_errors_from_check_geometry_are_returned(tmp_path):
    sandbox = FakeSandbox(
        {
            "run_script": RUN_OK,
            "check_geometry": gate_failed(
                "bounding box mismatch on x: expected 4, got 5", "volume mismatch: 0.4 vs 0.5"
            ),
        }
    )
    ctx = make_ctx(tmp_path, sandbox)
    result = GeometryStage().execute_and_gate(ctx, good_attempt(ctx))
    assert not result.ok
    assert result.errors == [
        "bounding box mismatch on x: expected 4, got 5",
        "volume mismatch: 0.4 vs 0.5",
    ]


def test_an_internal_failure_is_not_blamed_on_the_agent(tmp_path):
    broken = OpResponse(ok=False, internal=True, error="the executor for run_script failed")
    ctx = make_ctx(tmp_path, FakeSandbox({"run_script": broken}))
    with pytest.raises(ToolFailure, match="executor"):
        GeometryStage().execute_and_gate(ctx, good_attempt(ctx))
    ctx = make_ctx(tmp_path, FakeSandbox({"run_script": RUN_OK, "check_geometry": broken}))
    with pytest.raises(ToolFailure, match="executor"):
        GeometryStage().execute_and_gate(ctx, good_attempt(ctx))


def test_missing_script_is_reported_without_a_sandbox_call(tmp_path):
    sandbox = FakeSandbox({})
    ctx = make_ctx(tmp_path, sandbox)
    path = attempt(ctx, script=False)
    result = GeometryStage().execute_and_gate(ctx, path)
    assert not result.ok
    assert "geometry.py" in result.errors[0] and "write_file" in result.errors[0]
    assert sandbox.calls == []


def test_gate_needs_the_casespec(tmp_path):
    ctx = make_ctx(tmp_path, FakeSandbox({}))
    ctx.casespec = None
    with pytest.raises(ToolFailure, match="CaseSpec"):
        GeometryStage().execute_and_gate(ctx, good_attempt(ctx))


# ------------------------------------------------------------------ the manifest, host side


def test_manifest_parse_is_size_limited(tmp_path):
    huge = json.dumps(manifest()) + " " * (MANIFEST_MAX_BYTES + 10)
    sandbox = FakeSandbox({"run_script": RUN_OK, "check_geometry": GATE_OK})
    ctx = make_ctx(tmp_path, sandbox)
    path = attempt(ctx, manifest_text=huge)
    result = GeometryStage().execute_and_gate(ctx, path)
    assert not result.ok
    text = "\n".join(result.errors)
    assert "boundary_manifest.json" in text and str(MANIFEST_MAX_BYTES) in text


def test_an_unreadable_manifest_is_left_to_the_sandbox_gate(tmp_path):
    sandbox = FakeSandbox(
        {"run_script": RUN_OK, "check_geometry": gate_failed("boundary_manifest.json is invalid")}
    )
    ctx = make_ctx(tmp_path, sandbox)
    for text in ("{not json", json.dumps({"patches": "none"}), "[]"):
        path = attempt(ctx, manifest_text=text)
        result = GeometryStage().execute_and_gate(ctx, path)
        assert result.errors == ["boundary_manifest.json is invalid"]


def test_manifest_names_must_be_the_planned_boundaries(tmp_path):
    sandbox = FakeSandbox({"run_script": RUN_OK, "check_geometry": GATE_OK})
    ctx = make_ctx(tmp_path, sandbox)
    path = attempt(ctx, manifest_data=manifest(names=("inlet", "exit", "walls", "frontAndBack")))
    result = GeometryStage().execute_and_gate(ctx, path)
    assert not result.ok
    text = "\n".join(result.errors)
    assert "exit" in text and "outlet" in text  # names what is unexpected and what is missing
    assert "boundaries" in text


def test_manifest_dimensionality_must_match_the_plan(tmp_path):
    sandbox = FakeSandbox({"run_script": RUN_OK, "check_geometry": GATE_OK})
    ctx = make_ctx(tmp_path, sandbox)
    data = manifest(dimensionality="3D")
    result = GeometryStage().execute_and_gate(ctx, attempt(ctx, manifest_data=data))
    assert not result.ok and "dimensionality" in result.errors[0]


def test_manifest_and_sandbox_problems_are_reported_together(tmp_path):
    sandbox = FakeSandbox({"run_script": RUN_OK, "check_geometry": gate_failed("volume mismatch")})
    ctx = make_ctx(tmp_path, sandbox)
    path = attempt(ctx, manifest_data=manifest(names=("a", "outlet", "walls", "frontAndBack")))
    result = GeometryStage().execute_and_gate(ctx, path)
    assert len(result.errors) == 2 and result.errors[-1] == "volume mismatch"


def axisymmetric_spec(names=("inlet", "outlet", "side", "wedgeFront", "wedgeBack")):
    spec = make_spec().model_dump()
    spec["dimensionality"] = "axisymmetric"
    spec["geometry"]["thickness_m"] = None
    spec["geometry"]["wedge_angle_deg"] = 5.0
    spec["boundaries"] = [{"name": n, "role": "wall", "description": "x"} for n in names]
    return make_spec(**{k: spec[k] for k in ("dimensionality", "geometry", "boundaries")})


def wedge_manifest(front="wedge", back="wedge", extra=()):
    return {
        "dimensionality": "axisymmetric",
        "patches": [
            {"name": "inlet", "type": "patch", "locators": [plane("x", 0.0)]},
            {"name": "outlet", "type": "patch", "locators": [plane("x", 4.0)]},
            {"name": "side", "type": "wall", "locators": [plane("x", 1.0)]},
            {
                "name": "wedgeFront",
                "type": front,
                "locators": [{"kind": "box", "box": [0, 0, 0, 4, 1, 0.1]}],
            },
            {
                "name": "wedgeBack",
                "type": back,
                "locators": [{"kind": "box", "box": [0, 0, -0.1, 4, 1, 0]}],
            },
            *extra,
        ],
    }


def gate(tmp_path, spec, data, number=1):
    """The stage's verdict on a manifest, with a sandbox that accepts script and geometry."""
    sandbox = FakeSandbox({"run_script": RUN_OK, "check_geometry": GATE_OK})
    ctx = make_ctx(tmp_path, sandbox, spec=spec)
    return GeometryStage().execute_and_gate(ctx, attempt(ctx, number, manifest_data=data))


def spec_with_boundaries(names, **changes):
    boundaries = [{"name": n, "role": "x", "description": "x"} for n in names]
    return make_spec(boundaries=boundaries, **changes)


def test_axisymmetric_manifest_needs_two_separate_wedge_patches(tmp_path):
    spec = axisymmetric_spec()
    assert gate(tmp_path, spec, wedge_manifest()).ok
    for bad in (wedge_manifest(back="wall"), wedge_manifest(front="empty", back="empty")):
        result = gate(tmp_path, spec, bad)
        assert not result.ok and "two patches of type `wedge`" in "\n".join(result.errors)


def test_axisymmetric_manifest_rejects_three_wedges_and_wedge_plus_empty(tmp_path):
    spec = axisymmetric_spec()
    third = {"name": "extra", "type": "wedge", "locators": [plane("x", 2.0)]}
    result = gate(tmp_path, spec, wedge_manifest(extra=[third]))
    assert not result.ok and "has 3 `wedge`" in "\n".join(result.errors)
    plus_empty = {"name": "lid2", "type": "empty", "locators": [plane("z", 0.0)]}
    result = gate(tmp_path, spec, wedge_manifest(extra=[plus_empty]))
    assert not result.ok and "1 `empty`" in "\n".join(result.errors)


def test_axisymmetric_plan_that_lists_an_axis_boundary_is_satisfiable(tmp_path):
    """Regression (scenario B): the axis of a wedge has no face, so no patch can carry its name."""
    spec = axisymmetric_spec(("inlet", "outlet", "side", "axis", "wedgeFront", "wedgeBack"))
    assert gate(tmp_path, spec, wedge_manifest()).ok


def test_wedge_patches_of_an_axisymmetric_case_may_have_any_name(tmp_path):
    spec = axisymmetric_spec(("inlet", "outlet", "side"))  # the plan lists no wedge boundary
    assert gate(tmp_path, spec, wedge_manifest()).ok


def test_a_non_structural_patch_name_the_plan_does_not_list_is_rejected_with_a_hint(tmp_path):
    spec = axisymmetric_spec()
    data = wedge_manifest()
    data["patches"][2]["name"] = "shell"  # the plan lists `side`
    result = gate(tmp_path, spec, data)
    assert not result.ok
    text = "\n".join(result.errors)
    assert "`shell`" in text and "`side`" in text  # the unknown name and the unmatched plan name
    assert "wedge" in text  # the wedge patches may have any name


def test_2d_plan_without_an_empty_boundary_is_satisfiable(tmp_path):
    """Regression (scenario A): the plan lists no front/back boundary; the manifest needs an
    `empty` patch anyway and it may have any name."""
    spec = spec_with_boundaries(("inlet", "outlet", "walls"))
    assert gate(tmp_path, spec, manifest()).ok
    other = manifest(names=("inlet", "outlet", "walls", "sides"))
    assert gate(tmp_path, spec, other, number=2).ok


def test_2d_manifest_accepts_two_separate_empty_patches(tmp_path):
    spec = spec_with_boundaries(("inlet", "outlet", "walls", "front", "back"))
    data = manifest()
    data["patches"][3] = {"name": "front", "type": "empty", "locators": [plane("z", 0.0)]}
    data["patches"].append({"name": "back", "type": "empty", "locators": [plane("z", 0.1)]})
    result = gate(tmp_path, spec, data)
    assert result.ok, result.errors


def test_2d_faces_on_the_z_planes_must_be_empty(tmp_path):
    spec = spec_with_boundaries(("inlet", "outlet", "walls", "front", "back"))
    data = manifest()
    data["patches"][3] = {"name": "front", "type": "empty", "locators": [plane("z", 0.0)]}
    data["patches"].append({"name": "back", "type": "wall", "locators": [plane("z", 0.1)]})
    result = gate(tmp_path, spec, data)
    assert not result.ok
    text = "\n".join(result.errors)
    assert "`back` (wall)" in text and "`front`" not in text


def test_2d_z_face_message_bounds_a_long_patch_name(tmp_path):
    long_name = "q" * 300
    spec = spec_with_boundaries(("inlet", "outlet", "walls", long_name))
    data = manifest(names=("inlet", "outlet", "walls", "frontAndBack"))
    data["patches"].append({"name": long_name, "type": "wall", "locators": [plane("z", 0.1)]})
    result = gate(tmp_path, spec, data)
    text = " ".join(result.errors)
    assert not result.ok and "wall" in text and long_name not in text


def test_2d_manifest_has_no_wedge_patch_and_3d_has_no_empty_or_wedge(tmp_path):
    data = manifest()
    data["patches"].append({"name": "w", "type": "wedge", "locators": [plane("x", 2.0)]})
    result = gate(tmp_path, spec_with_boundaries(("inlet", "outlet", "walls", "w")), data)
    assert not result.ok and "no `wedge` patch" in "\n".join(result.errors)
    spec3 = plan(dimensionality="3D")
    data = manifest(dimensionality="3D")  # still holds the `empty` patch
    result = gate(tmp_path, spec3, data, number=2)
    assert not result.ok and "3D case has no `empty`" in "\n".join(result.errors)
    data["patches"] = [p for p in data["patches"] if p["type"] != "empty"]
    assert gate(tmp_path, spec3, data, number=3).ok


def test_very_long_plan_names_are_shortened_in_messages(tmp_path):
    long_name = "n" * 500
    spec = spec_with_boundaries(("inlet", "outlet", "walls", long_name))
    result = gate(tmp_path, spec, manifest(names=("inlet", "exit", "walls", "frontAndBack")))
    assert not result.ok
    assert long_name not in "\n".join(result.errors)
    assert len("\n".join(result.errors)) < 1000


def test_a_missing_manifest_is_left_to_the_sandbox_gate(tmp_path):
    sandbox = FakeSandbox(
        {"run_script": RUN_OK, "check_geometry": gate_failed("boundary_manifest.json is missing")}
    )
    ctx = make_ctx(tmp_path, sandbox)
    result = GeometryStage().execute_and_gate(ctx, attempt(ctx))
    assert result.errors == ["boundary_manifest.json is missing"]


def test_last_bytes_never_splits_a_character():
    text = "x" * 10 + "é" * 20  # two bytes per character
    for limit in range(1, 40):
        shown = _last_bytes(text, limit)
        assert len(shown.split("\n", 1)[-1].encode("utf-8")) <= limit
    assert _last_bytes("short", 100) == "short"


# ------------------------------------------------------------------ promotion, scope, prompt


def test_promote_copies_step_and_manifest(tmp_path):
    ctx = make_ctx(tmp_path, FakeSandbox({}))
    path = good_attempt(ctx)
    (path / "geometry.step").write_text("ISO-10303-21;\n", encoding="utf-8")
    GeometryStage().promote(ctx, path)
    promoted = ctx.run.root / "geometry"
    assert (promoted / "geometry.step").read_text(encoding="utf-8") == "ISO-10303-21;\n"
    assert json.loads((promoted / "boundary_manifest.json").read_text(encoding="utf-8"))
    assert (promoted / "geometry.py").is_file()
    assert path.is_dir()  # the attempt itself stays


def test_scope_lets_the_agent_write_only_its_script_and_notes():
    scope = GeometryStage.scope
    for n in (1, 2, 3):
        assert scope.can_write(f"attempts/geometry-{n}/geometry.py")
        assert scope.can_write(f"attempts/geometry-{n}/notes.md")
        assert scope.can_read(f"attempts/geometry-{n}/geometry.log")
    assert not scope.can_write("attempts/geometry-1/boundary_manifest.json")
    assert not scope.can_write("attempts/geometry-1/geometry.step")
    assert not scope.can_write("geometry.py")
    assert not scope.can_write("casespec.json")
    assert not scope.can_write("attempts/mesh-1/geometry.py")
    assert not scope.can_read("attempts/mesh-1/mesh.py")
    assert not scope.can_read("benchmarks/x/reference.json")
    assert scope.can_read("casespec.json")


def test_stage_identity_and_extra_tools(tmp_path):
    stage = GeometryStage()
    ctx = make_ctx(tmp_path, FakeSandbox({}))
    assert (stage.name, stage.agent) == ("geometry", "geometry")
    assert stage.extra_tools(ctx, ctx.run.attempt_dir("geometry", 1)) == []
    assert stage.system_prompt() == load_prompt("geometry").text


def test_opening_message_states_the_plan_geometry_and_boundaries(tmp_path):
    ctx = make_ctx(tmp_path, FakeSandbox({}))
    text = GeometryStage().opening_message(ctx)
    for needle in ("inlet", "outlet", "2D", "thickness_m", "0.4", "geometry.py"):
        assert needle in text
    assert "0.1" in text  # the z extent
    assert "kinematic_viscosity" not in text  # only what geometry needs, not the whole plan
    assert "any name" in text and "no face" in text  # the exemptions of the name rule


def test_prompt_states_the_output_contract_and_the_shared_conventions():
    text = load_prompt("geometry").text
    for needle in (
        "geometry.py",
        "geometry.step",
        "boundary_manifest.json",
        "plane",
        "box",
        "empty",
        "wedge",
        "symmetryPlane",
        "metre",
        "one solid",
        "CadQuery",
        "centered=False",
        "not 360",
        "across z = 0",
        "no other face does",
    ):
        assert needle in text, needle
    # The conventions of the Planner prompt, stated the same way.
    assert re.search(r"x-y", text) and "z" in text
    assert "wedge_angle_deg" in text and "thickness_m" in text
    assert "two separate" in text
    # The example in the prompt is valid JSON of the manifest shape.
    blocks = re.findall(r"```json\n(.*?)```", text, re.S)
    assert blocks
    example = json.loads(blocks[0])
    assert {"dimensionality", "patches"} <= set(example)


# ------------------------------------------------------------------ with the stage loop


def test_fixture_script_is_the_handwritten_geometry():
    steps = json.loads((FIXTURES / "scripted" / "geometry_ok.json").read_text(encoding="utf-8"))
    written = steps[0]["tool_calls"][0]["arguments"]
    assert written["path"] == ATTEMPT + "/geometry.py"
    handwritten = FIXTURES / "handwritten" / "cavity_re100" / "geometry.py"
    assert written["content"] == handwritten.read_text(encoding="utf-8")
    assert steps[1]["tool_calls"][0]["name"] == "check_stage"


def test_stage_loop_passes_promotes_and_records_one_attempt(tmp_path):
    def run_script(args):
        # What the real script does: the files appear in the attempt directory.
        cwd = ctx.run.path(args.cwd)
        (cwd / "geometry.step").write_text("step", encoding="utf-8")
        (cwd / "boundary_manifest.json").write_text(json.dumps(manifest()), encoding="utf-8")
        return RUN_OK

    sandbox = FakeSandbox({"run_script": run_script, "check_geometry": GATE_OK})
    backend = ScriptedBackend.from_json(FIXTURES / "scripted" / "geometry_ok.json")
    ctx = make_ctx(tmp_path, sandbox)
    ctx.backend = backend
    outcome = run_stage(GeometryStage(), ctx)
    assert outcome.ok and outcome.attempts == 1
    assert (ctx.run.root / "geometry" / "geometry.step").is_file()
    assert (ctx.run.root / "geometry" / "geometry.py").is_file()
    first = backend.calls[0]
    assert first.messages[0]["content"] == load_prompt("geometry").text
    assert {t["function"]["name"] for t in first.tools} == {
        "calculator",
        "read_file",
        "list_dir",
        "write_file",
        "check_stage",
    }
