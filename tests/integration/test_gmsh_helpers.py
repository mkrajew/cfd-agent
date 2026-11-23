"""Boundary-face matching and physical groups on Gmsh models imported from STEP."""

import cadquery as cq
import gmsh
import pytest

from cfdagent.manifest import BoundaryManifest, BoundaryPatch, Locator, ManifestError
from cfdagent.tools.gmsh_helpers import assign_physical_groups, base_face, match_surfaces

pytestmark = pytest.mark.integration


def _plane(axis, value):
    return Locator(kind="plane", axis=axis, value=value)


def _patches(include_moving_wall=True):
    patches = []
    if include_moving_wall:
        patches.append(BoundaryPatch(name="movingWall", type="wall", locators=[_plane("y", 1.0)]))
    patches += [
        BoundaryPatch(
            name="fixedWalls",
            type="wall",
            locators=[_plane("x", 0.0), _plane("x", 1.0), _plane("y", 0.0)],
        ),
        BoundaryPatch(
            name="frontAndBack", type="empty", locators=[_plane("z", 0.0), _plane("z", 0.1)]
        ),
    ]
    return patches


def _manifest(patches=None):
    return BoundaryManifest(dimensionality="2D", patches=patches or _patches())


def _box():
    return cq.Workplane("XY").box(1, 1, 0.1, centered=False)


@pytest.fixture
def load_step(tmp_path):
    """Export a CadQuery workplane to STEP and import it into a fresh Gmsh model."""

    def _load(workplane):
        path = tmp_path / "geometry.step"
        cq.exporters.export(workplane, str(path))
        gmsh.initialize()
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("test")
        gmsh.model.occ.importShapes(str(path))
        gmsh.model.occ.synchronize()

    yield _load
    gmsh.finalize()


def test_assign_physical_groups_on_box(load_step):
    load_step(_box())
    groups = assign_physical_groups(_manifest())
    assert {k: len(v) for k, v in groups.items()} == {
        "movingWall": 1,
        "fixedWalls": 3,
        "frontAndBack": 2,
    }
    names = {gmsh.model.getPhysicalName(d, t) for d, t in gmsh.model.getPhysicalGroups()}
    assert names == {"movingWall", "fixedWalls", "frontAndBack", "internal"}
    assert [d for d, _ in gmsh.model.getPhysicalGroups()].count(3) == 1
    (volume_group,) = gmsh.model.getPhysicalGroups(3)
    assert len(gmsh.model.getEntitiesForPhysicalGroup(*volume_group)) == 1


def test_volume_group_name_is_configurable(load_step):
    load_step(_box())
    assign_physical_groups(_manifest(), volume_name="fluid")
    names = {gmsh.model.getPhysicalName(*g) for g in gmsh.model.getPhysicalGroups(3)}
    assert names == {"fluid"}


def test_match_surfaces_does_not_create_groups(load_step):
    load_step(_box())
    assert match_surfaces(_manifest()).keys() == {"movingWall", "fixedWalls", "frontAndBack"}
    assert gmsh.model.getPhysicalGroups() == []


def test_unmatched_surface_raises(load_step):
    load_step(_box())
    with pytest.raises(ManifestError, match="not matched") as exc:
        assign_physical_groups(_manifest(_patches(include_moving_wall=False)))
    assert "tag" in str(exc.value) and "bounding box" in str(exc.value)
    # Nothing is created when the manifest does not cover the boundary.
    assert gmsh.model.getPhysicalGroups() == []


def test_surface_matched_twice_raises(load_step):
    load_step(_box())
    everything = Locator(kind="box", box=(-1, -1, -1, 2, 2, 2))
    patches = _patches()
    patches[0] = BoundaryPatch(
        name="movingWall", type="wall", locators=[_plane("y", 1.0), everything]
    )
    with pytest.raises(ManifestError, match="more than one"):
        assign_physical_groups(_manifest(patches))


def test_patch_matching_no_surface_raises(load_step):
    load_step(_box())
    patches = _patches() + [BoundaryPatch(name="ghost", type="patch", locators=[_plane("x", 5.0)])]
    with pytest.raises(ManifestError, match="ghost"):
        assign_physical_groups(_manifest(patches))


def test_box_locator_matches_curved_surface(load_step):
    hole = cq.Workplane("XY").workplane(offset=-0.05).center(0.5, 0.5).circle(0.05).extrude(0.2)
    load_step(_box().cut(hole))
    patches = _patches() + [
        BoundaryPatch(
            name="hole",
            type="wall",
            locators=[Locator(kind="box", box=(0.44, 0.44, -0.01, 0.56, 0.56, 0.11))],
        )
    ]
    groups = assign_physical_groups(_manifest(patches))
    assert len(groups["hole"]) == 1
    assert len(groups["frontAndBack"]) == 2


def test_no_volume_raises(load_step):
    load_step(cq.Workplane("XY").rect(1, 1))  # a face only, no solid
    with pytest.raises(ManifestError, match="volume"):
        assign_physical_groups(_manifest())


def test_matching_tolerance_is_relative_to_the_model_size(load_step):
    # A micro-scale domain of 5e-7 m: an absolute tolerance of 1e-6 would let every surface match
    # every plane, so each surface would be matched by several patches.
    gmsh.initialize()
    gmsh.option.setNumber("General.Terminal", 0)
    gmsh.model.add("micro")
    gmsh.model.occ.addBox(0, 0, 0, 5e-7, 5e-7, 5e-7)
    gmsh.model.occ.synchronize()
    patches = [
        BoundaryPatch(name="top", type="wall", locators=[_plane("y", 5e-7)]),
        BoundaryPatch(
            name="sides",
            type="wall",
            locators=[_plane("x", 0.0), _plane("x", 5e-7), _plane("y", 0.0)],
        ),
        BoundaryPatch(
            name="frontAndBack", type="empty", locators=[_plane("z", 0.0), _plane("z", 5e-7)]
        ),
    ]
    groups = match_surfaces(_manifest(patches))
    assert {k: len(v) for k, v in groups.items()} == {"top": 1, "sides": 3, "frontAndBack": 2}


def _wedge(angle_deg=5.0, length=2.0, radius=1.0):
    """The wedge convention of the pipeline: axis = x axis, symmetric about the x-y plane."""
    return (
        cq.Workplane("XY")
        .moveTo(0, 0)
        .lineTo(length, 0)
        .lineTo(length, radius)
        .lineTo(0, radius)
        .close()
        .revolve(angle_deg, (0, 0, 0), (1, 0, 0))
        .rotate((0, 0, 0), (1, 0, 0), -angle_deg / 2)
    )


def _bbox(tag):
    return gmsh.model.getBoundingBox(2, tag)


def test_base_face_2d_is_the_face_on_the_lowest_z_plane(load_step):
    load_step(cq.Workplane("XY").workplane(offset=0.3).rect(2, 1, centered=False).extrude(0.1))
    face = base_face("2D")
    assert _bbox(face)[2] == pytest.approx(0.3, abs=1e-6)
    assert _bbox(face)[5] == pytest.approx(0.3, abs=1e-6)


def test_base_face_axisymmetric_is_the_tilted_minus_z_wedge_face(load_step):
    load_step(_wedge())
    face = base_face("axisymmetric")
    xmin, ymin, zmin, xmax, ymax, zmax = _bbox(face)
    assert zmax == pytest.approx(0.0, abs=1e-6)
    assert zmin < -0.01  # tilted: its z extent is not small
    assert (xmin, xmax) == pytest.approx((0.0, 2.0), abs=1e-6)


def test_base_face_2d_finds_nothing_on_a_wedge(load_step):
    load_step(_wedge())
    with pytest.raises(ManifestError, match="no surface"):
        base_face("2D")


def test_base_face_result_builds_a_one_cell_layer(load_step):
    load_step(_wedge())
    face = base_face("axisymmetric")
    copy = gmsh.model.occ.copy([(2, face)])
    gmsh.model.occ.remove(gmsh.model.getEntities(3), recursive=True)
    gmsh.model.occ.synchronize()
    gmsh.model.mesh.setRecombine(2, copy[0][1])
    gmsh.model.occ.revolve(copy, 0, 0, 0, 1, 0, 0, 0.0872664626, numElements=[1], recombine=True)
    gmsh.model.occ.synchronize()
    gmsh.model.mesh.generate(3)
    assert gmsh.model.mesh.getElements(3)[1]  # volume cells exist


@pytest.mark.parametrize(
    ("size", "z0"),
    [((0.05, 0.01, 0.001), 0.0), ((0.05, 0.01, 0.001), 0.3), ((0.001, 0.001, 0.0001), 0.0)],
)
def test_base_face_2d_on_small_offset_slabs(load_step, size, z0):
    length, width, thickness = size
    slab = (
        cq.Workplane("XY")
        .workplane(offset=z0)
        .rect(length, width, centered=False)
        .extrude(thickness)
    )
    load_step(slab)
    assert _bbox(base_face("2D"))[2] == pytest.approx(z0, abs=2e-7)  # Gmsh pads boxes by 1e-7


def test_base_face_axisymmetric_wedge_off_the_axis(load_step):
    inner = 0.2
    wedge = (
        cq.Workplane("XY")
        .moveTo(0, inner)
        .lineTo(2, inner)
        .lineTo(2, 1)
        .lineTo(0, 1)
        .close()
        .revolve(5.0, (0, 0, 0), (1, 0, 0))
        .rotate((0, 0, 0), (1, 0, 0), -2.5)
    )
    load_step(wedge)
    zmax = _bbox(base_face("axisymmetric"))[5]
    assert zmax < 0  # the face stays off the axis


def test_base_face_axisymmetric_tiny_wedge(load_step):
    load_step(_wedge(angle_deg=5.0, length=0.05, radius=0.005))
    face = base_face("axisymmetric")
    assert _bbox(face)[2] < 0
    assert _bbox(face)[5] == pytest.approx(0.0, abs=1e-6)
