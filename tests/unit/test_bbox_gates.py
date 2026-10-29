"""The geometry and mesh gates must fail closed on NaN, inf and a non-positive tolerance.

External tools are replaced by stubs, so these tests run without OpenFOAM or CadQuery.
"""

import math
import sys
import types
from pathlib import Path

import pytest

from cfdagent.manifest import MANIFEST_FILE_NAME, BoundaryManifest, BoundaryPatch, Locator
from cfdagent.tools import mesh
from cfdagent.tools.geometry import STEP_FILE_NAME, check_geometry
from cfdagent.tools.mesh import CheckMeshReport, check_mesh
from cfdagent.tools.process import ToolResult

LOGS = Path(__file__).parents[1] / "fixtures" / "logs"
BOX = (0.0, 0.0, 0.0, 0.1, 0.1, 0.01)
OK_BBOX_LINE = "Overall domain bounding box (0 0 0) (0.1 0.1 0.01)"
NAN = math.nan
INF = math.inf


def _manifest():
    def plane(axis, value):
        return Locator(kind="plane", axis=axis, value=value)

    return BoundaryManifest(
        dimensionality="2D",
        patches=[
            BoundaryPatch(name="movingWall", type="wall", locators=[plane("y", 0.1)]),
            BoundaryPatch(name="fixedWalls", type="wall", locators=[plane("x", 0.0)]),
            BoundaryPatch(name="frontAndBack", type="empty", locators=[plane("z", 0.0)]),
        ],
    )


# --------------------------------------------------------------------------------------------
# Geometry gate
# --------------------------------------------------------------------------------------------


class _FakeBox:
    def __init__(self, bbox):
        self.xmin, self.ymin, self.zmin, self.xmax, self.ymax, self.zmax = bbox


class _FakeSolid:
    def __init__(self, bbox):
        self._bbox = bbox

    def isValid(self):  # noqa: N802 - mirrors the CadQuery API
        return True

    def BoundingBox(self):  # noqa: N802
        return _FakeBox(self._bbox)


def _fake_cadquery(monkeypatch, bbox):
    solids = types.SimpleNamespace(vals=lambda: [_FakeSolid(bbox)])
    shape = types.SimpleNamespace(solids=lambda: solids)
    importers = types.SimpleNamespace(importStep=lambda _path: shape)
    monkeypatch.setitem(sys.modules, "cadquery", types.SimpleNamespace(importers=importers))


def _geometry_dir(tmp_path):
    _manifest().to_file(tmp_path / MANIFEST_FILE_NAME)
    (tmp_path / STEP_FILE_NAME).write_text("stub", encoding="utf-8")
    return tmp_path


def test_geometry_gate_accepts_matching_box_with_stub(tmp_path, monkeypatch):
    _fake_cadquery(monkeypatch, BOX)
    gate = check_geometry(_geometry_dir(tmp_path), BOX)
    assert gate.ok, gate.errors


@pytest.mark.parametrize("bad", [NAN, INF, -INF])
def test_geometry_gate_rejects_non_finite_actual(tmp_path, monkeypatch, bad):
    _fake_cadquery(monkeypatch, (0, 0, 0, bad, 0.1, 0.01))
    gate = check_geometry(_geometry_dir(tmp_path), BOX)
    assert not gate.ok
    assert any("bounding box" in e for e in gate.errors)


def test_geometry_gate_rejects_all_nan_actual(tmp_path, monkeypatch):
    _fake_cadquery(monkeypatch, (NAN,) * 6)
    gate = check_geometry(_geometry_dir(tmp_path), BOX)
    assert not gate.ok
    assert any("bounding box" in e for e in gate.errors)


@pytest.mark.parametrize("bad", [NAN, INF])
def test_geometry_gate_rejects_non_finite_expected(tmp_path, bad):
    gate = check_geometry(tmp_path, (0, 0, 0, bad, 0.1, 0.01))
    assert not gate.ok
    assert any("bounding box" in e for e in gate.errors)


@pytest.mark.parametrize("rel_tol", [0.0, -1e-3, NAN])
def test_geometry_gate_rejects_non_positive_tolerance(tmp_path, monkeypatch, rel_tol):
    _fake_cadquery(monkeypatch, BOX)
    gate = check_geometry(_geometry_dir(tmp_path), BOX, rel_tol=rel_tol)
    assert not gate.ok
    assert any("bounding box" in e for e in gate.errors)


# --------------------------------------------------------------------------------------------
# Mesh gate
# --------------------------------------------------------------------------------------------


def _stub_checkmesh(monkeypatch, tmp_path, bbox_line=OK_BBOX_LINE):
    log_text = (LOGS / "checkMesh_ok.log").read_text(encoding="utf-8")
    assert OK_BBOX_LINE in log_text
    log = tmp_path / "checkMesh.log"
    log.write_text(log_text.replace(OK_BBOX_LINE, bbox_line), encoding="utf-8")

    def fake_run_tool(cmd, cwd, log_path, timeout_s=0.0):
        return ToolResult(ok=True, returncode=0, timed_out=False, log_path=log, tail="")

    patch_types = {"movingWall": "wall", "fixedWalls": "wall", "frontAndBack": "empty"}
    monkeypatch.setattr(mesh, "run_tool", fake_run_tool)
    monkeypatch.setattr(
        mesh,
        "_read_boundary_patches",
        lambda _case: {"movingWall": 20, "fixedWalls": 60, "frontAndBack": 800},
    )
    monkeypatch.setattr(mesh, "_read_entry", lambda _case, entry: patch_types[entry.split("/")[1]])


def test_mesh_gate_accepts_matching_box_with_stub(tmp_path, monkeypatch):
    _stub_checkmesh(monkeypatch, tmp_path)
    gate = check_mesh(tmp_path, _manifest(), BOX)
    assert gate.ok, gate.errors
    assert isinstance(gate.data, CheckMeshReport)
    assert gate.data.patches == {"movingWall": 20, "fixedWalls": 60, "frontAndBack": 800}


@pytest.mark.parametrize(
    "bbox_line",
    [
        "Overall domain bounding box (nan nan nan) (nan nan nan)",
        "Overall domain bounding box (0 0 0) (inf 0.1 0.01)",
        "Overall domain bounding box (0 0 0) (0.1 -inf 0.01)",
    ],
)
def test_mesh_gate_rejects_non_finite_actual(tmp_path, monkeypatch, bbox_line):
    _stub_checkmesh(monkeypatch, tmp_path, bbox_line)
    gate = check_mesh(tmp_path, _manifest(), BOX)
    assert not gate.ok
    assert any("bounding box" in e for e in gate.errors)


@pytest.mark.parametrize("bad", [NAN, INF])
def test_mesh_gate_rejects_non_finite_expected(tmp_path, monkeypatch, bad):
    _stub_checkmesh(monkeypatch, tmp_path)
    gate = check_mesh(tmp_path, _manifest(), (0, 0, 0, bad, 0.1, 0.01))
    assert not gate.ok
    assert gate.data is None
    assert any("bounding box" in e for e in gate.errors)


@pytest.mark.parametrize("rel_tol", [0.0, -1e-3, NAN])
def test_mesh_gate_rejects_non_positive_tolerance(tmp_path, monkeypatch, rel_tol):
    _stub_checkmesh(monkeypatch, tmp_path)
    gate = check_mesh(tmp_path, _manifest(), BOX, rel_tol=rel_tol)
    assert not gate.ok
    assert any("bounding box" in e for e in gate.errors)


# --------------------------------------------------------------------------------------------
# Manifest errors in the geometry gate
# --------------------------------------------------------------------------------------------


def test_geometry_gate_reports_an_invalid_manifest(tmp_path, monkeypatch):
    _fake_cadquery(monkeypatch, BOX)
    (tmp_path / STEP_FILE_NAME).write_text("stub", encoding="utf-8")
    (tmp_path / MANIFEST_FILE_NAME).write_bytes(bytes([0xFF, 0xFE]) + b" not a manifest")
    gate = check_geometry(tmp_path, BOX)
    assert not gate.ok
    assert any(MANIFEST_FILE_NAME in e and "invalid" in e for e in gate.errors)


def test_geometry_gate_does_not_hide_programmer_errors(tmp_path, monkeypatch):
    _fake_cadquery(monkeypatch, BOX)

    def broken(_path):
        raise KeyError("bug in our code")

    monkeypatch.setattr(BoundaryManifest, "from_file", staticmethod(broken))
    with pytest.raises(KeyError):
        check_geometry(_geometry_dir(tmp_path), BOX)
