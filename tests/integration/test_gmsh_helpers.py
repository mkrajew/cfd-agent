"""Boundary-face matching and physical groups on Gmsh models imported from STEP."""

import cadquery as cq
import gmsh
import pytest

from cfdagent.manifest import BoundaryManifest, BoundaryPatch, Locator, ManifestError
from cfdagent.tools.gmsh_helpers import assign_physical_groups, match_surfaces

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
