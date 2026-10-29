"""Pure-Python reading of ASCII polyMesh files and the patch placement check."""

import shutil
import time
from pathlib import Path

import numpy as np
import pytest

from cfdagent.errors import AgentInputError, ToolFailure
from cfdagent.manifest import BoundaryManifest, BoundaryPatch, Locator
from cfdagent.tools.polymesh import (
    MeshError,
    locator_contains,
    placement_errors,
    read_patch_face_centres,
)

BOX = Path(__file__).parents[1] / "fixtures" / "polymesh" / "box_one_cell"
BOX_LENGTH = 2.0  # the largest extent of the box fixture (2 x 1 x 0.5)


def _plane(axis, value):
    return Locator(kind="plane", axis=axis, value=value)


def _manifest(inlet_x=0.0, outlet_x=2.0):
    return BoundaryManifest(
        dimensionality="2D",
        patches=[
            BoundaryPatch(name="inlet", type="patch", locators=[_plane("x", inlet_x)]),
            BoundaryPatch(name="outlet", type="patch", locators=[_plane("x", outlet_x)]),
            BoundaryPatch(name="walls", type="wall", locators=[_plane("y", 0.0), _plane("y", 1.0)]),
            BoundaryPatch(
                name="frontAndBack", type="empty", locators=[_plane("z", 0.0), _plane("z", 0.5)]
            ),
        ],
    )


def _case(tmp_path: Path) -> Path:
    """A copy of the box fixture laid out as a case directory."""
    poly = tmp_path / "case" / "constant" / "polyMesh"
    shutil.copytree(BOX, poly)
    return tmp_path / "case"


def _edit(case: Path, name: str, old: str, new: str) -> None:
    path = case / "constant" / "polyMesh" / name
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8", newline="\n")


def test_face_centres_of_box_fixture(tmp_path):
    centres = read_patch_face_centres(_case(tmp_path))
    assert set(centres) == {"inlet", "outlet", "walls", "frontAndBack"}
    assert all(isinstance(c, np.ndarray) and c.shape[1] == 3 for c in centres.values())
    assert centres["inlet"] == pytest.approx(np.array([[0.0, 0.5, 0.25]]))
    assert centres["outlet"] == pytest.approx(np.array([[2.0, 0.5, 0.25]]))
    walls = centres["walls"][np.argsort(centres["walls"][:, 1])]
    assert walls == pytest.approx(np.array([[1.0, 0.0, 0.25], [1.0, 1.0, 0.25]]))
    fb = centres["frontAndBack"][np.argsort(centres["frontAndBack"][:, 2])]
    assert fb == pytest.approx(np.array([[1.0, 0.5, 0.0], [1.0, 0.5, 0.5]]))


def test_multi_line_lists_comments_and_header_are_handled(tmp_path):
    """Large meshes are written one entry per line; headers and comments may sit anywhere."""
    case = _case(tmp_path)
    _edit(
        case,
        "points",
        "8((0 0 0.5) (0 0 0) (0 1 0.5) (0 1 0) (2 0 0.5) (2 0 0) (2 1 0.5) (2 1 0))",
        "// a comment (with parentheses) and 3 numbers\n8\n(\n/* block ( 1 2 3 ) */\n"
        "(0 0 0.5)\n(0 0 0)\n(0 1 0.5)\n(0 1 0)\n(2 0 0.5)\n(2 0 0)\n(2 1 0.5)\n(2 1 0)\n)",
    )
    _edit(
        case,
        "faces",
        "6\n(\n4(2 3 1 0)",
        "6\n(\n4 (2 3 1 0)",
    )
    centres = read_patch_face_centres(case)
    assert centres["inlet"] == pytest.approx(np.array([[0.0, 0.5, 0.25]]))
    assert centres["outlet"].shape == (1, 3)


def test_scientific_notation_and_negative_coordinates(tmp_path):
    case = _case(tmp_path)
    _edit(case, "points", "(0 0 0.5)", "(-1e-3 0 5e-1)")
    centres = read_patch_face_centres(case)
    assert centres["inlet"][0, 0] == pytest.approx((-1e-3 + 0 + 0 + 0) / 4)


def test_missing_polymesh_is_a_tool_failure(tmp_path):
    with pytest.raises(ToolFailure) as exc:
        read_patch_face_centres(tmp_path / "no_case")
    assert not isinstance(exc.value, AgentInputError)


def test_binary_points_are_rejected(tmp_path):
    case = _case(tmp_path)
    _edit(case, "points", "format      ascii;", "format      binary;")
    with pytest.raises(MeshError, match="binary"):
        read_patch_face_centres(case)


@pytest.mark.parametrize(
    ("name", "old", "new"),
    [
        ("points", "8((0 0 0.5)", "9((0 0 0.5)"),  # wrong count
        ("points", "(2 1 0))", "(2 1 0)"),  # unbalanced
        ("points", "(0 0 0.5)", "(0 0)"),  # not a vector
        ("points", "(0 0 0.5)", "(0 0 abc)"),
        ("faces", "6\n(", "7\n("),  # wrong count
        ("faces", "4(2 3 1 0)", "3(2 3 1 0)"),  # vertex count does not match
        ("faces", "4(2 3 1 0)", "4(2 3 1 99)"),  # point index out of range
        ("boundary", "nFaces          2;\n        startFace       4;", "nFaces 9; startFace 4;"),
        ("boundary", "startFace       0;", ""),  # no startFace
        ("faces", "class       faceList;", "class       faceCompactList;"),
    ],
)
def test_malformed_files_raise_mesh_error(tmp_path, name, old, new):
    case = _case(tmp_path)
    _edit(case, name, old, new)
    with pytest.raises(MeshError):
        read_patch_face_centres(case)


def test_truncated_and_empty_files_raise_mesh_error(tmp_path):
    case = _case(tmp_path)
    path = case / "constant" / "polyMesh" / "faces"
    path.write_text(path.read_text(encoding="utf-8")[:-200], encoding="utf-8", newline="\n")
    with pytest.raises(MeshError):
        read_patch_face_centres(case)
    path.write_text("", encoding="utf-8")
    with pytest.raises(MeshError):
        read_patch_face_centres(case)


def test_mesh_error_is_an_agent_input_error():
    assert issubclass(MeshError, AgentInputError)


def test_correct_manifest_places_every_patch(tmp_path):
    assert placement_errors(_case(tmp_path), _manifest(), tol=1e-5 * BOX_LENGTH) == []


def test_inlet_outlet_swap_is_rejected(tmp_path):
    """Patch names swapped between the two end faces: the faces are not where the manifest says."""
    case = _case(tmp_path)
    errors = placement_errors(case, _manifest(inlet_x=2.0, outlet_x=0.0), tol=1e-5 * BOX_LENGTH)
    assert any("inlet" in e for e in errors)
    assert any("outlet" in e for e in errors)
    assert not any("walls" in e or "frontAndBack" in e for e in errors)


def test_placement_reports_a_patch_with_one_stray_face(tmp_path):
    manifest = _manifest()
    manifest.patches[2].locators = [_plane("y", 0.0)]  # the y = 1 wall is no longer covered
    errors = placement_errors(_case(tmp_path), manifest, tol=1e-5 * BOX_LENGTH)
    assert len(errors) == 1
    assert "walls" in errors[0] and "1 of 2" in errors[0]


def test_placement_skips_patches_missing_from_the_mesh(tmp_path):
    manifest = _manifest()
    manifest.patches.append(BoundaryPatch(name="ghost", type="patch", locators=[_plane("x", 7.0)]))
    assert placement_errors(_case(tmp_path), manifest, tol=1e-5 * BOX_LENGTH) == []


@pytest.mark.parametrize(
    ("locator", "point", "tol", "expected"),
    [
        (_plane("x", 1.0), (1.0, 5.0, 5.0), 1e-6, True),
        (_plane("x", 1.0), (1.001, 5.0, 5.0), 1e-6, False),
        (_plane("x", 1.0), (1.001, 5.0, 5.0), 2e-3, True),
        # polyMesh points have 6 significant digits: far from the origin the representation
        # floor (relative to the coordinate) applies even for a tiny tolerance.
        (_plane("x", 1000.0), (1000.004, 0.0, 0.0), 1e-9, True),
        (_plane("x", 1000.0), (1000.1, 0.0, 0.0), 1e-9, False),
        (Locator(kind="box", box=(0, 0, 0, 1, 1, 1)), (0.5, 0.5, 0.5), 1e-6, True),
        (Locator(kind="box", box=(0, 0, 0, 1, 1, 1)), (1.0000005, 0.5, 0.5), 1e-6, True),
        (Locator(kind="box", box=(0, 0, 0, 1, 1, 1)), (1.1, 0.5, 0.5), 1e-6, False),
        (Locator(kind="box", box=(0, 0, 0, 1, 1, 1)), (0.5, -0.1, 0.5), 1e-6, False),
        (_plane("x", 1.0), (float("nan"), 0.0, 0.0), 1e-3, False),
    ],
)
def test_locator_contains(locator, point, tol, expected):
    assert locator_contains(locator, point, tol) is expected


# --- malformed large files must be rejected quickly (no regex backtracking) ---------------------


def _write_list_file(case: Path, name: str, body: str, count: int) -> None:
    """Replace the list of a polyMesh file, keeping its header; ``body`` is the list contents."""
    path = case / "constant" / "polyMesh" / name
    text = path.read_text(encoding="utf-8")
    header = text[: text.index("// * * *")]
    path.write_text(f"{header}\n{count}\n(\n{body}", encoding="utf-8", newline="\n")


def _assert_rejected_quickly(case: Path) -> None:
    start = time.perf_counter()
    with pytest.raises(MeshError):
        read_patch_face_centres(case)
    assert time.perf_counter() - start < 2.0


def test_truncated_faces_with_large_indices_are_rejected_quickly(tmp_path):
    case = _case(tmp_path)
    faces = "\n".join(f"4({10000 + i} {20000 + i} {30000 + i} {40000 + i})" for i in range(3000))
    _write_list_file(case, "faces", faces + "\n", 3000)  # the closing ')' is missing
    _assert_rejected_quickly(case)


def test_garbled_faces_with_large_indices_are_rejected_quickly(tmp_path):
    case = _case(tmp_path)
    faces = "\n".join(f"4({10000 + i} {20000 + i} {30000 + i} {40000 + i})" for i in range(2000))
    _write_list_file(case, "faces", faces + "\n4(12345 23456 3456\n)\n", 2001)
    _assert_rejected_quickly(case)


def test_truncated_points_with_integer_coordinates_are_rejected_quickly(tmp_path):
    case = _case(tmp_path)
    points = "\n".join(f"({100 + i % 800} {200 + i % 700} {1000 + i % 9000})" for i in range(70000))
    _write_list_file(case, "points", points + "\n", 70000)  # the closing ')' is missing
    _assert_rejected_quickly(case)


def test_points_with_a_broken_vector_in_the_middle_are_rejected_quickly(tmp_path):
    case = _case(tmp_path)
    good = "\n".join(f"({100 + i % 800} {200 + i % 700} {1000 + i % 9000})" for i in range(50000))
    _write_list_file(case, "points", good + "\n(1234 5678)\n)\n", 50001)
    _assert_rejected_quickly(case)


def test_large_valid_lists_are_read(tmp_path):
    """A well-formed list of 70 000 integer-valued points is not mistaken for garbage."""
    case = _case(tmp_path)
    points = "\n".join(f"({100 + i % 800} {200 + i % 700} {1000 + i % 9000})" for i in range(70000))
    _write_list_file(case, "points", points + "\n)\n", 70000)
    start = time.perf_counter()
    read_patch_face_centres(case)
    assert time.perf_counter() - start < 5.0


# --- placement follows the manifest's locator semantics (face bounding box) --------------------


def test_face_with_centre_on_the_plane_but_vertices_off_it_is_rejected(tmp_path):
    case = _case(tmp_path)
    _edit(case, "points", "(0 0 0.5) (0 0 0) (0 1 0.5)", "(-0.4 0 0.5) (0 0 0) (0.4 1 0.5)")
    assert read_patch_face_centres(case)["inlet"] == pytest.approx(np.array([[0.0, 0.5, 0.25]]))
    errors = placement_errors(case, _manifest(), tol=1e-5 * BOX_LENGTH)
    assert any("inlet" in e for e in errors)


def test_box_locator_uses_the_face_bounding_box(tmp_path):
    case = _case(tmp_path)
    inlet_box = Locator(kind="box", box=(0.0, 0.0, 0.0, 0.0, 0.4, 0.5))  # covers y <= 0.4 only
    manifest = _manifest()
    manifest.patches[0].locators = [inlet_box]  # the inlet face spans y 0..1, its centre y = 0.5
    errors = placement_errors(case, manifest, tol=1e-5 * BOX_LENGTH)
    assert len(errors) == 1 and "inlet" in errors[0]
    manifest.patches[0].locators = [Locator(kind="box", box=(-0.1, -0.1, -0.1, 0.1, 1.1, 0.6))]
    assert placement_errors(case, manifest, tol=1e-5 * BOX_LENGTH) == []


def test_placement_error_lists_locators_and_gives_a_remedy(tmp_path):
    errors = placement_errors(
        _case(tmp_path), _manifest(inlet_x=2.0, outlet_x=0.0), tol=1e-5 * BOX_LENGTH
    )
    inlet = next(e for e in errors if "'inlet'" in e)
    assert "plane x=2" in inlet
    assert "physical group" in inlet and "box" in inlet
