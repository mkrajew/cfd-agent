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
        "misses it at ymin (surface -1e-07 < box 0.01, short by 0.0099"
    ) in message
    assert (
        "); a box selects a surface only when "
        "the surface's whole bounding box lies inside it, so grow the box past the surface, "
        "never shrink it"
    ) in message


def test_hint_lists_every_violated_bound(match):
    message = _error(match, FACE, [_patch("wall", _box(-1, -1, 0.002, 1, 2, 0.998))])
    assert "misses it at zmin (surface -1e-07 < box 0.002, short by 0.0019" in message
    assert ") and zmax (surface 1.0000001 > box 0.998, short by 0.0019" in message


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
    assert "at ymin (surface -1e-07 < box 5e-06, short by 4e-06)" in message


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


def test_hint_prints_enough_digits_for_coordinates_of_one_or_more(match):
    # A face at x = 2 and a box shrunk inward by 3e-6: with 6 significant digits both values
    # would print as 2 and the hint would read "surface 2 > box 2".
    surfaces = {1: (2, 0, 0, 2, 1, 1)}
    message = _error(match, surfaces, [_patch("wall", _box(1, -1, -1, 2 - 3e-6, 2, 2))])
    assert "xmax (surface 2.0000001 > box 1.999997, short by " in message
    assert "short by 2e-06)" in message


def test_hint_names_only_the_bounds_missed_beyond_the_effective_tolerance(match):
    # The box starts 1e-6 above the surface in y: inside the effective tolerance (1.1e-6 minus
    # the 1e-7 Gmsh padding), so the matcher accepts that bound. The box starts 0.01 above the
    # surface in z: a real miss. Only zmin may be named, whatever tolerance the hint uses.
    message = _error(match, FACE, [_patch("wall", _box(-1, 1e-6, 0.01, 1, 2, 2))])
    assert "misses it at zmin (surface -1e-07 < box 0.01, short by " in message
    assert "ymin" not in message
    assert " and " not in message.split("misses it at")[1].split(";")[0]


def test_hint_prefers_the_smaller_sum_over_the_smaller_maximum(match):
    patches = [
        _patch("two_bounds", _box(-1, 0.1, 0.1, 1, 2, 2)),  # 0.1 + 0.1 = 0.2, maximum 0.1
        _patch("one_bound", _box(-1, 0.15, -1, 1, 2, 2)),  # 0.15, maximum 0.15
    ]
    message = _error(match, FACE, patches)
    assert "nearest box: patch 'one_bound'" in message


def test_hint_prefers_the_smaller_sum_over_the_smaller_count(match):
    patches = [
        _patch("one_bound", _box(-1, 0.05, -1, 1, 2, 2)),  # one bound, 0.05
        _patch("two_bounds", _box(-1, 0.01, 0.01, 1, 2, 2)),  # two bounds, 0.02 in total
    ]
    message = _error(match, FACE, patches)
    assert "nearest box: patch 'two_bounds'" in message


def test_a_degenerate_box_does_not_hide_the_real_nearest_box(match):
    nan = float("nan")
    patches = [
        _patch("broken", _box(nan, nan, nan, nan, nan, nan)),
        _patch("real", _box(-1, 0.01, -1, 1, 2, 2)),
    ]
    message = _error(match, FACE, patches)
    assert "nearest box: patch 'real' locator 1" in message


# ------------------------------------------------------------------ a surface in two patches


def test_overlap_names_the_locator_of_each_patch(match):
    surfaces = {1: (0, 0, 0, 0, 1, 1), 2: (1, 0, 0, 1, 1, 1)}
    patches = [
        _patch("inlet", _plane("x", 0.0)),
        _patch("wall", _plane("y", 9.0), _box(-1, -1, -1, 2, 2, 2)),  # the box is locator 2
        _patch("outlet", _plane("x", 1.0)),
    ]
    message = _error(match, surfaces, patches)
    assert "surface tag 1, bounding box (" in message
    assert "is matched by more than one patch: 'inlet' (locator 1, plane), " in message
    assert "'wall' (locator 2, box)" in message
    assert "'wall' (locator 2, box), 'outlet' (locator 1, plane)" in message  # tag 2


def test_overlap_explains_that_a_box_selects_every_surface_inside_it(match):
    surfaces = {1: (0, 0, 0, 0, 1, 1)}
    patches = [_patch("a", _box(-1, -1, -1, 2, 2, 2)), _patch("b", _plane("x", 0.0))]
    message = _error(match, surfaces, patches)
    assert "a box selects every surface whose whole bounding box lies inside it" in message
    assert "a box that encloses the whole solid selects every face" in message
    assert "shrink that patch's box" in message
    assert message.count("a box selects every surface") == 1  # said once, however many overlaps


def test_no_overlap_sentence_without_an_overlap(match):
    message = _error(match, FACE, [_patch("wall", _plane("x", 5.0))])
    assert "every surface whose whole bounding box" not in message
