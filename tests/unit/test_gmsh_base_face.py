"""``base_face`` of the Gmsh helpers, tested against a fake ``gmsh`` module (no Gmsh needed)."""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

from cfdagent.manifest import ManifestError

HELPERS = Path(__file__).parents[2] / "src" / "cfdagent" / "tools" / "gmsh_helpers.py"
PAD = 1e-7  # Gmsh pads every bounding box by this much


def _padded(box):
    xmin, ymin, zmin, xmax, ymax, zmax = box
    return (xmin - PAD, ymin - PAD, zmin - PAD, xmax + PAD, ymax + PAD, zmax + PAD)


def _fake_gmsh(surface_boxes, volumes=((3, 1),)):
    """A fake module whose model has the given surfaces (tag -> unpadded box) and volumes."""
    boxes = {tag: _padded(box) for tag, box in surface_boxes.items()}
    lows = [b[:3] for b in boxes.values()]
    highs = [b[3:] for b in boxes.values()]
    whole = tuple(min(v[i] for v in lows) for i in range(3)) + tuple(
        max(v[i] for v in highs) for i in range(3)
    )
    model = types.SimpleNamespace(
        getEntities=lambda dim: list(volumes) if dim == 3 else [],
        getBoundary=lambda *a, **k: [(2, tag) for tag in boxes],
        getBoundingBox=lambda dim, tag: whole if dim == -1 else boxes[tag],
    )
    return types.SimpleNamespace(model=model)


@pytest.fixture
def helpers(monkeypatch):
    def load(surface_boxes, volumes=((3, 1),)):
        monkeypatch.setitem(sys.modules, "gmsh", _fake_gmsh(surface_boxes, volumes))
        spec = importlib.util.spec_from_file_location("gmsh_helpers_under_test", HELPERS)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    return load


# A slab 2 x 1 x 0.1 whose z range does not start at zero.
SLAB = {
    1: (0, 0, 0.5, 2, 1, 0.5),  # on z = zmin
    2: (0, 0, 0.6, 2, 1, 0.6),  # far face
    3: (0, 0, 0.5, 0, 1, 0.6),
    4: (2, 0, 0.5, 2, 1, 0.6),
    5: (0, 0, 0.5, 2, 0, 0.6),
    6: (0, 1, 0.5, 2, 1, 0.6),
}

# A wedge of opening angle 5 degrees, length 2, radius 1, symmetric about the x-y plane:
# z = -/+ sin(2.5 deg) at the largest radius, the -z face is tilted.
S = 0.0436193874
WEDGE = {
    1: (0, 0, -S, 2, 0.999, 0),  # the -z face: contains the axis, tilted, zmax = 0
    2: (0, 0, 0, 2, 0.999, S),  # the +z face
    3: (0, 0, -S, 0, 0.999, S),  # inlet end
    4: (2, 0, -S, 2, 0.999, S),  # outlet end
    5: (0, 0, -S, 2, 1, S),  # curved outer wall
}


def test_2d_returns_the_face_on_the_lowest_z_plane(helpers):
    assert helpers(SLAB).base_face("2D") == 1


def test_2d_when_the_slab_starts_at_zero(helpers):
    slab = {
        1: (0, 0, 0, 2, 1, 0),
        2: (0, 0, 0.1, 2, 1, 0.1),
        3: (0, 0, 0, 0, 1, 0.1),
    }
    assert helpers(slab).base_face("2D") == 1


def test_axisymmetric_returns_the_tilted_minus_z_face(helpers):
    assert helpers(WEDGE).base_face("axisymmetric") == 1


def test_axisymmetric_wedge_that_does_not_reach_the_axis(helpers):
    # The inner radius is 0.2, so the -z face has zmax = -0.2 sin(2.5 deg), below zero.
    wedge = dict(WEDGE)
    wedge[1] = (0, 0.1996, -S, 2, 0.999, -0.0087)
    wedge[2] = (0, 0.1996, 0.0087, 2, 0.999, S)
    wedge[3] = (0, 0.1996, -S, 0, 0.999, S)
    assert helpers(wedge).base_face("axisymmetric") == 1


def test_2d_is_not_confused_by_a_tilted_face(helpers):
    # The wedge has no face on the lowest z plane: its -z face spans z.
    with pytest.raises(ManifestError, match="no surface"):
        helpers(WEDGE).base_face("2D")


def test_axisymmetric_does_not_accept_a_flat_slab_face(helpers):
    with pytest.raises(ManifestError, match="no surface"):
        helpers(SLAB).base_face("axisymmetric")


def test_none_found_lists_the_candidates_with_their_boxes(helpers):
    with pytest.raises(ManifestError) as exc:
        helpers(WEDGE).base_face("2D")
    message = str(exc.value)
    assert "tag 1" in message and "tag 5" in message and "bounding box" in message


def test_two_found_lists_the_matching_surfaces(helpers):
    slab = dict(SLAB)
    slab[7] = (2, 0, 0.5, 3, 1, 0.5)  # a second face on the lowest plane
    with pytest.raises(ManifestError, match="more than one") as exc:
        helpers(slab).base_face("2D")
    assert "tag 1" in str(exc.value) and "tag 7" in str(exc.value)


def test_other_dimensionality_has_no_base_face(helpers):
    with pytest.raises(ManifestError, match="no base face"):
        helpers(SLAB).base_face("3D")


def test_no_volume_raises(helpers):
    with pytest.raises(ManifestError, match="no volume"):
        helpers(SLAB, volumes=()).base_face("2D")


def test_tolerance_is_relative_to_the_model_size(helpers):
    # A face 1e-4 above the lowest plane in a model of size 2 is outside tol = 1e-6 ...
    slab = dict(SLAB)
    slab[7] = (0, 0, 0.5001, 2, 1, 0.5001)
    assert helpers(slab).base_face("2D") == 1
    # ... and inside a tolerance of 1e-3.
    with pytest.raises(ManifestError, match="more than one"):
        helpers(slab).base_face("2D", tol=1e-3)
