"""Geometry stage: run a CadQuery script, then check its STEP and boundary manifest."""

import json
import math
from pathlib import Path

import pytest

from cfdagent.gates import GateResult
from cfdagent.manifest import MANIFEST_FILE_NAME
from cfdagent.tools.geometry import check_geometry, run_geometry_script

pytestmark = pytest.mark.integration

HANDWRITTEN = Path(__file__).parents[1] / "fixtures" / "handwritten"
FIXTURE = HANDWRITTEN / "cavity_re100" / "geometry.py"
CHANNEL_FIXTURE = HANDWRITTEN / "cylinder_channel" / "geometry.py"

# A solid cylinder of the given diameter (height 1, axis z, base on z = 0) with a complete
# manifest: one wall patch whose box locator encloses the side and both end faces.
CYLINDER_SCRIPT = """
import json
import cadquery as cq

DIAMETER = {diameter}
solid = cq.Workplane("XY").circle(DIAMETER / 2).extrude(1.0)
cq.exporters.export(solid, "geometry.step")
r = DIAMETER / 2 + 0.01
manifest = {{
    "dimensionality": "3D",
    "patches": [
        {{"name": "wall", "type": "wall",
         "locators": [{{"kind": "box", "box": [-r, -r, -0.01, r, r, 1.01]}}]}},
    ],
}}
json.dump(manifest, open("boundary_manifest.json", "w"))
"""


def _write_script(tmp_path, body):
    script = tmp_path / "script.py"
    script.write_text(body, encoding="utf-8")
    return script


def test_handwritten_cavity_geometry_passes(tmp_path):
    result = run_geometry_script(FIXTURE, tmp_path)
    assert result.ok, result.tail
    gate = check_geometry(tmp_path, expected_bbox=(0, 0, 0, 1, 1, 0.1))
    assert isinstance(gate, GateResult)
    assert gate.ok, gate.errors
    assert gate.errors == []


def test_wrong_dimensions_fail(tmp_path):
    assert run_geometry_script(FIXTURE, tmp_path).ok
    gate = check_geometry(tmp_path, expected_bbox=(0, 0, 0, 2, 1, 0.1))
    assert not gate.ok
    assert any("bounding box" in e for e in gate.errors)


def test_script_exception_reported(tmp_path):
    script = _write_script(tmp_path, "raise ValueError('bad dimension')\n")
    result = run_geometry_script(script, tmp_path)
    assert not result.ok
    assert "ValueError" in result.tail


def test_missing_manifest_fails(tmp_path):
    script = _write_script(
        tmp_path,
        "import cadquery as cq\n"
        "cq.exporters.export(cq.Workplane('XY').box(1, 1, 0.1), 'geometry.step')\n",
    )
    result = run_geometry_script(script, tmp_path)
    assert not result.ok  # the runner already reports the missing output
    assert MANIFEST_FILE_NAME in result.tail
    gate = check_geometry(tmp_path, expected_bbox=(-0.5, -0.5, -0.05, 0.5, 0.5, 0.05))
    assert not gate.ok
    assert any(MANIFEST_FILE_NAME in e for e in gate.errors)


def test_missing_step_fails(tmp_path):
    gate = check_geometry(tmp_path, expected_bbox=(0, 0, 0, 1, 1, 1))
    assert not gate.ok
    assert any("geometry.step" in e for e in gate.errors)


def test_invalid_manifest_fails(tmp_path):
    assert run_geometry_script(FIXTURE, tmp_path).ok
    (tmp_path / MANIFEST_FILE_NAME).write_text("{not json", encoding="utf-8")
    gate = check_geometry(tmp_path, expected_bbox=(0, 0, 0, 1, 1, 0.1))
    assert not gate.ok
    assert any(MANIFEST_FILE_NAME in e for e in gate.errors)


def test_step_without_solid_fails(tmp_path):
    script = _write_script(
        tmp_path,
        "import cadquery as cq\n"
        "cq.exporters.export(cq.Workplane('XY').rect(1, 1), 'geometry.step')\n"
        "import json\n"
        "json.dump({'dimensionality': '3D', 'patches': []}, open('boundary_manifest.json', 'w'))\n",
    )
    assert run_geometry_script(script, tmp_path).ok
    gate = check_geometry(tmp_path, expected_bbox=(-0.5, -0.5, 0, 0.5, 0.5, 0))
    assert not gate.ok
    assert any("solid" in e for e in gate.errors)


def _make_cylinder(tmp_path, diameter):
    script = _write_script(tmp_path, CYLINDER_SCRIPT.format(diameter=diameter))
    result = run_geometry_script(script, tmp_path)
    assert result.ok, result.tail


def _rewrite_manifest(tmp_path, edit):
    path = tmp_path / MANIFEST_FILE_NAME
    manifest = json.loads(path.read_text(encoding="utf-8"))
    edit(manifest)
    path.write_text(json.dumps(manifest), encoding="utf-8")


def test_gate_rejects_empty_patch_list(tmp_path):
    assert run_geometry_script(FIXTURE, tmp_path).ok
    _rewrite_manifest(tmp_path, lambda m: m.update(dimensionality="3D", patches=[]))
    gate = check_geometry(tmp_path, expected_bbox=(0, 0, 0, 1, 1, 0.1))
    assert not gate.ok
    assert any("no patches" in e for e in gate.errors), gate.errors


def test_gate_reports_unmatched_surface_at_geometry_stage(tmp_path):
    assert run_geometry_script(FIXTURE, tmp_path).ok
    _rewrite_manifest(
        tmp_path, lambda m: m.update(patches=[p for p in m["patches"] if p["name"] != "movingWall"])
    )
    gate = check_geometry(tmp_path, expected_bbox=(0, 0, 0, 1, 1, 0.1))
    assert not gate.ok
    assert any("geometry stage" in e and "not matched by any patch" in e for e in gate.errors), (
        gate.errors
    )


def test_gate_reports_surface_matched_twice_and_unused_patch(tmp_path):
    assert run_geometry_script(FIXTURE, tmp_path).ok

    def edit(manifest):
        top = {"kind": "plane", "axis": "y", "value": 1.0}
        for patch in manifest["patches"]:
            if patch["name"] == "fixedWalls":
                patch["locators"].append(top)  # y = 1 now belongs to two patches
        manifest["patches"].append(
            {
                "name": "ghost",
                "type": "patch",
                "locators": [{"kind": "plane", "axis": "x", "value": 5.0}],
            }
        )

    _rewrite_manifest(tmp_path, edit)
    gate = check_geometry(tmp_path, expected_bbox=(0, 0, 0, 1, 1, 0.1))
    assert not gate.ok
    text = " ".join(gate.errors)
    assert "more than one patch" in text
    assert "'ghost' matches no boundary surface" in text


def test_gate_rejects_wrong_cylinder_diameter_via_volume(tmp_path):
    # The hole of the channel fixture is made 10 % too small. The outer box is unchanged, so the
    # bounding-box check cannot see the problem and the manifest still covers every surface; only
    # the volume check can fire.
    script = _write_script(
        tmp_path,
        CHANNEL_FIXTURE.read_text(encoding="utf-8").replace(
            "CX, CY, RADIUS = 0.2, 0.2, 0.05", "CX, CY, RADIUS = 0.2, 0.2, 0.045"
        ),
    )
    result = run_geometry_script(script, tmp_path)
    assert result.ok, result.tail
    correct_volume = (2.2 * 0.41 - math.pi * 0.05**2) * 0.1
    gate = check_geometry(
        tmp_path, expected_bbox=(0, 0, 0, 2.2, 0.41, 0.1), expected_volume=correct_volume
    )
    assert not gate.ok
    assert len(gate.errors) == 1, gate.errors
    assert "volume mismatch" in gate.errors[0]


def test_gate_rejects_multiple_solids(tmp_path):
    script = _write_script(
        tmp_path,
        "import cadquery as cq, json\n"
        "a = cq.Workplane('XY').box(1, 1, 1, centered=False)\n"
        "b = cq.Workplane('XY').box(1, 1, 1, centered=False).translate((3, 0, 0))\n"
        "cq.exporters.export(a.add(b), 'geometry.step')\n"
        "box = [-1, -1, -1, 5, 5, 5]\n"
        "m = {'dimensionality': '3D', 'patches': [{'name': 'wall', 'type': 'wall',\n"
        "     'locators': [{'kind': 'box', 'box': box}]}]}\n"
        "json.dump(m, open('boundary_manifest.json', 'w'))\n",
    )
    assert run_geometry_script(script, tmp_path).ok
    gate = check_geometry(tmp_path, expected_bbox=(0, 0, 0, 4, 1, 1))
    assert not gate.ok
    assert any("fuse the solids" in e for e in gate.errors), gate.errors


def test_gate_leaves_a_caller_owned_gmsh_session_alone(tmp_path):
    import gmsh

    assert run_geometry_script(FIXTURE, tmp_path).ok
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("caller_model")
        gate = check_geometry(tmp_path, expected_bbox=(0, 0, 0, 1, 1, 0.1))
        assert gate.ok, gate.errors
        assert gmsh.isInitialized()
        assert gmsh.model.getCurrent() == "caller_model"
        assert "geometry_gate_scratch" not in gmsh.model.list()
    finally:
        gmsh.finalize()


def test_gate_accepts_correct_cylinder_volume_and_skips_volume_when_none(tmp_path):
    _make_cylinder(tmp_path, diameter=1.0)
    expected_bbox = (-0.5, -0.5, 0.0, 0.5, 0.5, 1.0)
    assert check_geometry(tmp_path, expected_bbox, expected_volume=math.pi * 0.25).ok
    assert check_geometry(tmp_path, expected_bbox).ok  # volume check skipped


@pytest.mark.parametrize("bad", [0.0, -1.0, math.nan, math.inf])
def test_gate_rejects_unusable_expected_volume(tmp_path, bad):
    _make_cylinder(tmp_path, diameter=1.0)
    gate = check_geometry(tmp_path, (-0.5, -0.5, 0.0, 0.5, 0.5, 1.0), expected_volume=bad)
    assert not gate.ok
    assert any("volume" in e for e in gate.errors), gate.errors


def test_gate_passes_handwritten_cavity_and_cylinder(tmp_path):
    cavity_dir = tmp_path / "cavity"
    assert run_geometry_script(FIXTURE, cavity_dir).ok
    cavity = check_geometry(
        cavity_dir, expected_bbox=(0, 0, 0, 1, 1, 0.1), expected_volume=1.0 * 1.0 * 0.1
    )
    assert cavity.ok, cavity.errors

    channel_dir = tmp_path / "channel"
    assert run_geometry_script(CHANNEL_FIXTURE, channel_dir).ok
    channel = check_geometry(
        channel_dir,
        expected_bbox=(0, 0, 0, 2.2, 0.41, 0.1),
        expected_volume=(2.2 * 0.41 - math.pi * 0.05**2) * 0.1,
    )
    assert channel.ok, channel.errors
