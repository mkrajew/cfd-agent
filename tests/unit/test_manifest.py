import pytest
from pydantic import ValidationError

from cfdagent.manifest import BoundaryManifest, BoundaryPatch, Locator, ManifestError


def _plane(axis, value):
    return Locator(kind="plane", axis=axis, value=value)


def _manifest():
    return BoundaryManifest(
        dimensionality="2D",
        patches=[
            BoundaryPatch(name="movingWall", type="wall", locators=[_plane("y", 1.0)]),
            BoundaryPatch(
                name="fixedWalls",
                type="wall",
                locators=[_plane("x", 0.0), _plane("x", 1.0), _plane("y", 0.0)],
            ),
            BoundaryPatch(
                name="frontAndBack",
                type="empty",
                locators=[
                    _plane("z", 0.0),
                    Locator(kind="box", box=(0, 0, 0.1, 1, 1, 0.1)),
                ],
            ),
        ],
    )


def test_roundtrip_json(tmp_path):
    path = tmp_path / "boundary_manifest.json"
    manifest = _manifest()
    manifest.to_file(path)
    assert BoundaryManifest.from_file(path) == manifest
    raw = path.read_bytes()
    assert b"\r" not in raw  # LF on every platform
    assert raw.endswith(b"}\n")


def test_manifest_error_is_an_exception():
    assert issubclass(ManifestError, Exception)


def test_rejects_duplicate_patch_names():
    patch = BoundaryPatch(name="a", type="wall", locators=[_plane("x", 0.0)])
    with pytest.raises(ValidationError):
        BoundaryManifest(dimensionality="3D", patches=[patch, patch])


@pytest.mark.parametrize("name", ["inlet 1", "1inlet", "", "in-let", "_x"])
def test_rejects_invalid_openfoam_name(name):
    with pytest.raises(ValidationError):
        BoundaryPatch(name=name, type="wall", locators=[_plane("x", 0.0)])


def test_plane_locator_requires_axis_and_value():
    with pytest.raises(ValidationError):
        Locator(kind="plane", value=1.0)
    with pytest.raises(ValidationError):
        Locator(kind="plane", axis="x")
    with pytest.raises(ValidationError):
        Locator(kind="plane", axis="x", value=1.0, box=(0, 0, 0, 1, 1, 1))


def test_box_locator_requires_box():
    with pytest.raises(ValidationError):
        Locator(kind="box")
    with pytest.raises(ValidationError):
        Locator(kind="box", box=(0, 0, 0, 1, 1, 1), axis="x")
    with pytest.raises(ValidationError):
        Locator(kind="box", box=(1, 0, 0, 0, 1, 1))  # xmin > xmax


def test_2d_manifest_requires_an_empty_patch():
    patch = BoundaryPatch(name="walls", type="wall", locators=[_plane("x", 0.0)])
    with pytest.raises(ValidationError):
        BoundaryManifest(dimensionality="2D", patches=[patch])
    BoundaryManifest(dimensionality="3D", patches=[patch])


def test_patch_requires_at_least_one_locator():
    with pytest.raises(ValidationError):
        BoundaryPatch(name="a", type="wall", locators=[])
