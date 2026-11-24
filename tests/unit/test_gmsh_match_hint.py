"""``match_surfaces`` names the nearest box locator of an unmatched surface (fake ``gmsh``)."""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

from cfdagent.manifest import BoundaryManifest, BoundaryPatch, Locator, ManifestError

HELPERS = Path(__file__).parents[2] / "src" / "cfdagent" / "tools" / "gmsh_helpers.py"
PAD = 1e-7  # Gmsh pads every bounding box by this much


def _fake_gmsh(surface_boxes):
    boxes = {
        tag: (b[0] - PAD, b[1] - PAD, b[2] - PAD, b[3] + PAD, b[4] + PAD, b[5] + PAD)
        for tag, b in surface_boxes.items()
    }
    lows = [b[:3] for b in boxes.values()]
    highs = [b[3:] for b in boxes.values()]
    whole = tuple(min(v[i] for v in lows) for i in range(3)) + tuple(
        max(v[i] for v in highs) for i in range(3)
    )
    model = types.SimpleNamespace(
        getEntities=lambda dim: [(3, 1)] if dim == 3 else [],
        getBoundary=lambda *a, **k: [(2, tag) for tag in boxes],
        getBoundingBox=lambda dim, tag: whole if dim == -1 else boxes[tag],
    )
    return types.SimpleNamespace(model=model)


@pytest.fixture
def match(monkeypatch):
    def run(surface_boxes, patches):
        monkeypatch.setitem(sys.modules, "gmsh", _fake_gmsh(surface_boxes))
        spec = importlib.util.spec_from_file_location("gmsh_helpers_hint_test", HELPERS)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.match_surfaces(BoundaryManifest(dimensionality="3D", patches=patches))

    return run


def _box(*values):
    return Locator(kind="box", box=values)


def _plane(axis, value):
    return Locator(kind="plane", axis=axis, value=value)


def _patch(name, *locators):
    return BoundaryPatch(name=name, type="wall", locators=list(locators))


# One surface: the unit face on x = 0. Gmsh pads its bounding box by 1e-7, and the matching
# tolerance (1e-6 of the model size 1 plus the padding) is 1.1e-6.
FACE = {1: (0, 0, 0, 0, 1, 1)}


def _error(match, surfaces, patches):
    with pytest.raises(ManifestError) as exc:
        match(surfaces, patches)
    return str(exc.value)


def test_hint_at_one_bound(match):
    # The box starts at y = 0.01 although the surface reaches down to y = 0.
    message = _error(match, FACE, [_patch("wall", _box(-1, 0.01, -1, 1, 2, 2))])
    assert message.startswith("surface tag 1, bounding box (")
    assert (
        "is not matched by any patch; nearest box: patch 'wall' locator 1 (1-based) "
        "misses it at ymin (surface -1e-07 < box 0.01); a box selects a surface only when "
        "the surface's whole bounding box lies inside it, so grow the box past the surface, "
        "never shrink it"
    ) in message


def test_hint_lists_every_violated_bound(match):
    message = _error(match, FACE, [_patch("wall", _box(-1, -1, 0.002, 1, 2, 0.998))])
    assert "misses it at zmin (surface -1e-07 < box 0.002) and zmax (surface 1 > box 0.998);" in (
        message
    )


def test_hint_picks_the_box_with_the_smallest_total_shortfall(match):
    patches = [
        _patch("far", _box(-1, 0.5, 0.5, 1, 2, 2)),  # misses ymin and zmin by 0.5 each
        _patch("near", _plane("z", 7), _box(-1, 0.001, -1, 1, 2, 2)),  # locator 2, ymin by 0.001
    ]
    message = _error(match, FACE, patches)
    assert "nearest box: patch 'near' locator 2 (1-based)" in message
    assert "nearest box: patch 'far'" not in message


def test_hint_takes_the_first_box_on_a_tie(match):
    patches = [
        _patch("first", _box(-1, 0.01, -1, 1, 2, 2)),
        _patch("second", _box(-1, 0.01, -1, 1, 2, 2)),
    ]
    message = _error(match, FACE, patches)
    assert "patch 'first' locator 1" in message
    assert "nearest box: patch 'second'" not in message


def test_hint_shortfall_is_measured_beyond_the_matching_tolerance(match):
    # A box 5e-7 short of the surface still matches (tolerance 1.1e-6), so the surface is matched
    # and no error is raised; 5e-6 short does not match and is reported as such.
    assert match(FACE, [_patch("wall", _box(-1, 5e-7, -1, 1, 2, 2))]) == {"wall": [1]}
    message = _error(match, FACE, [_patch("wall", _box(-1, 5e-6, -1, 1, 2, 2))])
    assert "at ymin (surface -1e-07 < box 5e-06)" in message


def test_no_hint_without_box_locators(match):
    message = _error(match, FACE, [_patch("wall", _plane("x", 5.0))])
    assert "is not matched by any patch;" in message  # the plain text: next problem follows
    assert "nearest box" not in message


def test_no_hint_for_a_surface_that_a_box_matches(match):
    surfaces = {1: (0, 0, 0, 0, 1, 1), 2: (1, 0, 0, 1, 1, 1)}
    message = _error(match, surfaces, [_patch("left", _box(-1, -1, -1, 0.5, 2, 2))])
    assert "surface tag 1" not in message
    assert message.count("nearest box") == 1
    assert message.startswith("surface tag 2, bounding box (")
