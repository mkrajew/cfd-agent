"""Mesh stage and mesh gate: Gmsh script -> MSH 2.2 -> OpenFOAM polyMesh -> checkMesh gate."""

import os
import shutil
from pathlib import Path

import gmsh
import pytest

import cfdagent
from cfdagent.manifest import MANIFEST_FILE_NAME, BoundaryManifest, BoundaryPatch, Locator
from cfdagent.tools.geometry import GateResult, check_geometry, run_geometry_script
from cfdagent.tools.mesh import (
    CheckMeshReport,
    MeshError,
    apply_patch_types,
    check_mesh,
    convert_to_foam,
    run_mesh_script,
)
from cfdagent.tools.process import ToolResult, run_tool

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parents[1] / "fixtures" / "handwritten"
CAVITY = FIXTURES / "cavity_re100"
CYLINDER = FIXTURES / "cylinder_channel"
CAVITY_BBOX = (0, 0, 0, 1, 1, 0.1)
CYLINDER_BBOX = (0, 0, 0, 2.2, 0.41, 0.1)


@pytest.fixture(scope="module", autouse=True)
def _scripts_import_checked_out_cfdagent():
    """Mesh scripts run in a subprocess; point it at the cfdagent under test, not the image copy."""
    src = str(Path(cfdagent.__file__).parents[1])
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PYTHONPATH", os.pathsep.join(filter(None, [src, os.environ.get("PYTHONPATH")])))
        yield


def _plane(axis, value):
    return Locator(kind="plane", axis=axis, value=value)


def _make_case(fixture: Path, workdir: Path, expected_bbox, apply_types: bool = True):
    """geometry -> mesh -> convert -> (apply types); returns (manifest, case_dir)."""
    assert run_geometry_script(fixture / "geometry.py", workdir).ok
    assert check_geometry(workdir, expected_bbox).ok
    result = run_mesh_script(fixture / "mesh.py", workdir)
    assert isinstance(result, ToolResult)
    assert result.ok, result.tail
    case_dir = workdir / "case"
    converted = convert_to_foam(workdir / "mesh.msh", case_dir)
    assert converted.ok, converted.tail
    manifest = BoundaryManifest.from_file(workdir / MANIFEST_FILE_NAME)
    if apply_types:
        apply_patch_types(case_dir, manifest)
    return manifest, case_dir


def _patch_value(case_dir: Path, patch: str, key: str) -> str:
    result = run_tool(
        [
            "foamDictionary",
            "constant/polyMesh/boundary",
            "-entry",
            f"entry0/{patch}/{key}",
            "-value",
        ],
        cwd=case_dir,
        log_path=case_dir / "readback.log",
    )
    assert result.ok, result.tail
    return result.log_path.read_text(encoding="utf-8").strip()


@pytest.fixture(scope="module")
def cavity(tmp_path_factory):
    workdir = tmp_path_factory.mktemp("cavity")
    manifest, case_dir = _make_case(CAVITY, workdir, CAVITY_BBOX)
    return manifest, case_dir


def test_cavity_mesh_passes_gate(cavity):
    manifest, case_dir = cavity
    gate, report = check_mesh(case_dir, manifest, CAVITY_BBOX)
    assert isinstance(gate, GateResult)
    assert gate.ok, gate.errors
    assert gate.errors == []
    assert isinstance(report, CheckMeshReport)
    assert report.n_cells == 64 * 64
    assert report.solution_directions == 2
    assert set(report.patches) == {"movingWall", "fixedWalls", "frontAndBack"}
    assert report.patches == {"movingWall": 64, "fixedWalls": 192, "frontAndBack": 2 * 64 * 64}
    assert report.bbox == pytest.approx(CAVITY_BBOX)
    assert report.failed_checks == 0


def test_patch_types_applied(cavity):
    _, case_dir = cavity
    assert _patch_value(case_dir, "frontAndBack", "type") == "empty"
    assert _patch_value(case_dir, "movingWall", "type") == "wall"
    assert _patch_value(case_dir, "fixedWalls", "type") == "wall"
    assert "wall" in _patch_value(case_dir, "movingWall", "inGroups")
    assert "empty" in _patch_value(case_dir, "frontAndBack", "inGroups")


def test_check_mesh_rejects_scaled_mesh(cavity):
    """The same mesh checked against an expected box 1000x larger (a mm/m mix-up) is rejected."""
    manifest, case_dir = cavity
    scaled = tuple(1000 * v for v in CAVITY_BBOX)
    gate, report = check_mesh(case_dir, manifest, scaled)
    assert not gate.ok
    assert any("bounding box" in e for e in gate.errors)
    assert any("unit" in e for e in gate.errors)
    assert report is not None


def test_check_mesh_bbox_tolerance(cavity):
    manifest, case_dir = cavity
    gate, _ = check_mesh(case_dir, manifest, (0, 0, 0, 1.0005, 1, 0.1), rel_tol=1e-3)
    assert gate.ok, gate.errors
    gate, _ = check_mesh(case_dir, manifest, (0, 0, 0, 1.01, 1, 0.1), rel_tol=1e-3)
    assert not gate.ok


def test_check_mesh_rejects_unapplied_patch_types(tmp_path):
    """A raw gmshToFoam mesh has all patches of type 'patch' and no empty direction."""
    manifest, case_dir = _make_case(CAVITY, tmp_path, CAVITY_BBOX, apply_types=False)
    gate, report = check_mesh(case_dir, manifest, CAVITY_BBOX)
    assert not gate.ok
    assert any("movingWall" in e and "'wall'" in e for e in gate.errors)
    assert any("frontAndBack" in e and "'empty'" in e for e in gate.errors)
    assert any("solution directions" in e for e in gate.errors)
    assert report is not None and report.solution_directions == 3


def test_check_mesh_rejects_default_faces(tmp_path):
    """One boundary surface without a physical group lands in defaultFaces and is rejected."""
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("unnamed_face")
        gmsh.model.occ.addBox(0, 0, 0, 1, 1, 0.1)
        gmsh.model.occ.synchronize()

        def on_plane(tag, axis, value):
            bb = gmsh.model.getBoundingBox(2, tag)
            return abs(bb[axis] - value) < 1e-6 and abs(bb[axis + 3] - value) < 1e-6

        tags = [t for _, t in gmsh.model.getEntities(2)]
        moving = [t for t in tags if on_plane(t, 1, 1.0)]
        fixed = [t for t in tags if on_plane(t, 0, 1.0) or on_plane(t, 1, 0.0)]  # x=0 left out
        front_back = [t for t in tags if on_plane(t, 2, 0.0) or on_plane(t, 2, 0.1)]
        assert len(moving) + len(fixed) + len(front_back) == 5
        for name, group in (
            ("movingWall", moving),
            ("fixedWalls", fixed),
            ("frontAndBack", front_back),
        ):
            gmsh.model.addPhysicalGroup(2, group, name=name)
        gmsh.model.addPhysicalGroup(3, [t for _, t in gmsh.model.getEntities(3)], name="internal")
        gmsh.model.mesh.setTransfiniteAutomatic()
        gmsh.option.setNumber("Mesh.RecombineAll", 1)
        gmsh.option.setNumber("Mesh.MshFileVersion", 2.2)
        gmsh.model.mesh.generate(3)
        gmsh.write(str(tmp_path / "mesh.msh"))
    finally:
        gmsh.finalize()

    case_dir = tmp_path / "case"
    assert convert_to_foam(tmp_path / "mesh.msh", case_dir).ok
    manifest = BoundaryManifest(
        dimensionality="2D",
        patches=[
            BoundaryPatch(name="movingWall", type="wall", locators=[_plane("y", 1.0)]),
            BoundaryPatch(
                name="fixedWalls",
                type="wall",
                locators=[_plane("x", 0.0), _plane("x", 1.0), _plane("y", 0.0)],
            ),
            BoundaryPatch(
                name="frontAndBack", type="empty", locators=[_plane("z", 0.0), _plane("z", 0.1)]
            ),
        ],
    )
    apply_patch_types(case_dir, manifest)
    gate, report = check_mesh(case_dir, manifest, CAVITY_BBOX)
    assert not gate.ok
    assert any("defaultFaces" in e for e in gate.errors)
    assert report is not None
    assert report.patches["defaultFaces"] > 0


def test_cylinder_channel_mesh_passes_gate(tmp_path):
    manifest, case_dir = _make_case(CYLINDER, tmp_path, CYLINDER_BBOX)
    gate, report = check_mesh(case_dir, manifest, CYLINDER_BBOX)
    assert gate.ok, gate.errors
    assert report is not None
    assert set(report.patches) == {"inlet", "outlet", "walls", "cylinder", "frontAndBack"}
    assert report.solution_directions == 2
    assert report.failed_checks == 0
    assert report.n_cells > 500
    assert _patch_value(case_dir, "cylinder", "type") == "wall"
    assert _patch_value(case_dir, "inlet", "type") == "patch"


@pytest.mark.parametrize(
    "patch_type", ["patch", "wall", "empty", "symmetryPlane", "symmetry", "wedge"]
)
def test_apply_patch_types_all_types(cavity, tmp_path, patch_type):
    _, source = cavity
    case_dir = tmp_path / "case"
    shutil.copytree(source, case_dir)
    manifest = BoundaryManifest(
        dimensionality="3D",
        patches=[BoundaryPatch(name="fixedWalls", type=patch_type, locators=[_plane("x", 0.0)])],
    )
    apply_patch_types(case_dir, manifest)
    assert _patch_value(case_dir, "fixedWalls", "type") == patch_type
    text = (case_dir / "constant" / "polyMesh" / "boundary").read_text(encoding="utf-8")
    block = text.split("fixedWalls")[1].split("}")[0]
    if patch_type == "patch":
        # The cavity setup gave fixedWalls inGroups (wall); switching to 'patch' must drop it.
        assert "inGroups" not in block
    else:
        assert f"({patch_type})" in _patch_value(case_dir, "fixedWalls", "inGroups")


def test_apply_patch_types_unknown_patch_raises(cavity, tmp_path):
    _, source = cavity
    case_dir = tmp_path / "case"
    shutil.copytree(source, case_dir)
    manifest = BoundaryManifest(
        dimensionality="3D",
        patches=[BoundaryPatch(name="ghost", type="wall", locators=[_plane("x", 0.0)])],
    )
    with pytest.raises(MeshError, match="ghost"):
        apply_patch_types(case_dir, manifest)


def test_apply_patch_types_requires_converted_mesh(tmp_path):
    manifest = BoundaryManifest(
        dimensionality="3D",
        patches=[BoundaryPatch(name="a", type="wall", locators=[_plane("x", 0.0)])],
    )
    with pytest.raises(MeshError, match="convert_to_foam"):
        apply_patch_types(tmp_path / "no_case", manifest)


def test_mesh_script_failure_is_reported(tmp_path):
    script = tmp_path / "bad_mesh.py"
    script.write_text("raise RuntimeError('meshing blew up')\n", encoding="utf-8")
    result = run_mesh_script(script, tmp_path / "w")
    assert not result.ok
    assert "RuntimeError" in result.tail


def test_mesh_script_without_output_is_reported(tmp_path):
    script = tmp_path / "noop_mesh.py"
    script.write_text("pass\n", encoding="utf-8")
    result = run_mesh_script(script, tmp_path / "w")
    assert not result.ok
    assert "mesh.msh" in result.tail


def test_mesh_script_wrong_format_is_reported(tmp_path):
    script = tmp_path / "v4_mesh.py"
    script.write_text(
        "import gmsh\n"
        "gmsh.initialize()\n"
        "gmsh.model.add('m')\n"
        "gmsh.model.occ.addBox(0, 0, 0, 1, 1, 1)\n"
        "gmsh.model.occ.synchronize()\n"
        "gmsh.model.mesh.generate(3)\n"
        "gmsh.write('mesh.msh')\n",  # the default format is MSH 4.1
        encoding="utf-8",
    )
    result = run_mesh_script(script, tmp_path / "w")
    assert not result.ok
    assert "2.2" in result.tail


def test_stale_mesh_is_not_mistaken_for_output(tmp_path):
    workdir = tmp_path / "w"
    workdir.mkdir()
    (workdir / "mesh.msh").write_text("$MeshFormat\n2.2 0 8\n$EndMeshFormat\n", encoding="utf-8")
    script = tmp_path / "noop_mesh.py"
    script.write_text("pass\n", encoding="utf-8")
    assert not run_mesh_script(script, workdir).ok
