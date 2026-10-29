"""The geometry and mesh gates must fail closed on NaN, inf and a non-positive tolerance.

External tools are replaced by stubs, so these tests run without OpenFOAM or CadQuery.
"""

import math
import sys
import types
from pathlib import Path

import pytest

from cfdagent.manifest import MANIFEST_FILE_NAME, BoundaryManifest, BoundaryPatch, Locator
from cfdagent.tools import geometry, mesh
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
    def __init__(self, bbox, volume=1.0, valid=True):
        self._bbox = bbox
        self._volume = volume
        self._valid = valid

    def isValid(self):  # noqa: N802 - mirrors the CadQuery API
        return self._valid

    def BoundingBox(self):  # noqa: N802
        return _FakeBox(self._bbox)

    def Volume(self):  # noqa: N802
        return self._volume


def _fake_cadquery(monkeypatch, bbox=None, solids=None, coverage=None):
    """Stub CadQuery's STEP import and the Gmsh coverage check; return the coverage call log.

    The coverage check needs a real STEP file and Gmsh, so it is covered in integration tests.
    ``coverage`` is the error message the stub returns (``None`` means the manifest covers it).
    """
    if solids is None:
        solids = [_FakeSolid(bbox)]
    vals = types.SimpleNamespace(vals=lambda: list(solids))
    shape = types.SimpleNamespace(solids=lambda: vals)
    importers = types.SimpleNamespace(importStep=lambda _path: shape)
    monkeypatch.setitem(sys.modules, "cadquery", types.SimpleNamespace(importers=importers))
    calls = []

    def fake_coverage(step_path, manifest):
        calls.append((step_path, manifest))
        return coverage

    monkeypatch.setattr(geometry, "_manifest_coverage_error", fake_coverage)
    return calls


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


def test_geometry_gate_accepts_matching_volume(tmp_path, monkeypatch):
    _fake_cadquery(monkeypatch, solids=[_FakeSolid(BOX, volume=1.0)])
    assert check_geometry(_geometry_dir(tmp_path), BOX, expected_volume=1.0005).ok


def test_geometry_gate_reports_volume_mismatch(tmp_path, monkeypatch):
    _fake_cadquery(monkeypatch, solids=[_FakeSolid(BOX, volume=0.9)])
    gate = check_geometry(_geometry_dir(tmp_path), BOX, expected_volume=1.0)
    assert not gate.ok
    assert any("volume mismatch" in e for e in gate.errors), gate.errors
    assert not any("bounding box" in e for e in gate.errors), gate.errors


def test_geometry_gate_volume_check_is_skipped_when_none(tmp_path, monkeypatch):
    _fake_cadquery(monkeypatch, solids=[_FakeSolid(BOX, volume=123.0)])
    assert check_geometry(_geometry_dir(tmp_path), BOX).ok


def test_geometry_gate_rejects_nan_actual_volume(tmp_path, monkeypatch):
    _fake_cadquery(monkeypatch, solids=[_FakeSolid(BOX, volume=NAN)])
    gate = check_geometry(_geometry_dir(tmp_path), BOX, expected_volume=1.0)
    assert not gate.ok
    assert any("volume" in e for e in gate.errors)


@pytest.mark.parametrize("bad", [0.0, -1.0, NAN, INF, "1.0", [1.0], object()])
def test_geometry_gate_rejects_unusable_expected_volume(tmp_path, bad):
    gate = check_geometry(tmp_path, BOX, expected_volume=bad)
    assert not gate.ok
    assert any("volume" in e for e in gate.errors)


def test_geometry_gate_rejects_multiple_solids(tmp_path, monkeypatch):
    solids = [_FakeSolid(BOX, volume=0.5), _FakeSolid(BOX, volume=0.5)]
    calls = _fake_cadquery(monkeypatch, solids=solids)
    gate = check_geometry(_geometry_dir(tmp_path), BOX, expected_volume=1.0)
    assert not gate.ok
    assert any("fuse the solids" in e for e in gate.errors), gate.errors
    assert calls == []


def test_geometry_gate_propagates_coverage_error(tmp_path, monkeypatch):
    calls = _fake_cadquery(monkeypatch, BOX, coverage="geometry stage: surface 3 is unmatched")
    gate = check_geometry(_geometry_dir(tmp_path), BOX)
    assert not gate.ok
    assert "geometry stage: surface 3 is unmatched" in gate.errors
    assert len(calls) == 1


def test_geometry_gate_skips_coverage_when_manifest_is_missing(tmp_path, monkeypatch):
    calls = _fake_cadquery(monkeypatch, BOX)
    (tmp_path / STEP_FILE_NAME).write_text("stub", encoding="utf-8")
    gate = check_geometry(tmp_path, BOX)
    assert not gate.ok
    assert any(MANIFEST_FILE_NAME in e for e in gate.errors)
    assert calls == []


def test_geometry_gate_skips_coverage_when_manifest_is_invalid(tmp_path, monkeypatch):
    calls = _fake_cadquery(monkeypatch, BOX)
    (tmp_path / STEP_FILE_NAME).write_text("stub", encoding="utf-8")
    (tmp_path / MANIFEST_FILE_NAME).write_text("{not json", encoding="utf-8")
    gate = check_geometry(tmp_path, BOX)
    assert not gate.ok
    assert any(MANIFEST_FILE_NAME in e for e in gate.errors)
    assert calls == []


def test_geometry_gate_skips_coverage_when_a_solid_is_invalid(tmp_path, monkeypatch):
    calls = _fake_cadquery(monkeypatch, solids=[_FakeSolid(BOX, valid=False)])
    gate = check_geometry(_geometry_dir(tmp_path), BOX)
    assert not gate.ok
    assert any("not valid" in e for e in gate.errors)
    assert calls == []


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


def test_mesh_gate_default_tolerance_accepts_an_inscribed_polygon(tmp_path, monkeypatch):
    # A 30-gon mesh of a 5 mm radius circle (axis x) is 2.7e-5 m short in z of the CAD box.
    line = "Overall domain bounding box (0 -0.005 -0.00497261) (0.5 0.005 0.00497261)"
    _stub_checkmesh(monkeypatch, tmp_path, bbox_line=line)
    expected = (0, -0.005, -0.005, 0.5, 0.005, 0.005)
    assert check_mesh(tmp_path, _manifest(), expected).ok
    strict = check_mesh(tmp_path, _manifest(), expected, rel_tol=1e-3)
    assert not strict.ok
    assert any("bounding box" in e for e in strict.errors)
