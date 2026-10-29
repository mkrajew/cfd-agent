"""Geometry stage: run a CadQuery script, then check its STEP and boundary manifest."""

from pathlib import Path

import pytest

from cfdagent.gates import GateResult
from cfdagent.manifest import MANIFEST_FILE_NAME
from cfdagent.tools.geometry import check_geometry, run_geometry_script

pytestmark = pytest.mark.integration

FIXTURE = Path(__file__).parents[1] / "fixtures" / "handwritten" / "cavity_re100" / "geometry.py"


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
    assert run_geometry_script(script, tmp_path).ok
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
        "cq.exporters.export(cq.Workplane('XY').rect(1, 1), 'geometry.step')\n",
    )
    assert run_geometry_script(script, tmp_path).ok
    (tmp_path / MANIFEST_FILE_NAME).write_text(
        '{"dimensionality": "3D", "patches": []}', encoding="utf-8"
    )
    gate = check_geometry(tmp_path, expected_bbox=(-0.5, -0.5, 0, 0.5, 0.5, 0))
    assert not gate.ok
    assert any("solid" in e for e in gate.errors)
