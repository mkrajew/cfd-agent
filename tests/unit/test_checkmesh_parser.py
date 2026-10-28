"""Parsing of OpenFOAM checkMesh output, using logs captured inside the Docker image."""

import math
from pathlib import Path

import pytest

from cfdagent.tools.mesh import CheckMeshReport, parse_checkmesh

LOGS = Path(__file__).parents[1] / "fixtures" / "logs"


def _read(name):
    return (LOGS / name).read_text(encoding="utf-8")


def test_parse_ok_log():
    report = parse_checkmesh(_read("checkMesh_ok.log"))
    assert isinstance(report, CheckMeshReport)
    assert report.n_cells == 400
    assert report.failed_checks == 0
    assert report.solution_directions == 2
    assert report.patches == {"movingWall": 20, "fixedWalls": 60, "frontAndBack": 800}
    assert report.bbox == (0, 0, 0, 0.1, 0.1, 0.01)
    assert report.max_non_orthogonality == 0
    assert report.max_skewness == pytest.approx(1.66533e-14)
    assert report.max_aspect_ratio == 1


def test_parse_failed_log():
    report = parse_checkmesh(_read("checkMesh_failed.log"))
    assert report.failed_checks >= 1
    assert report.failed_checks == 3
    assert report.n_cells == 4096
    assert report.patches == {"frontAndBack": 8192, "fixedWalls": 192, "movingWall": 64}
    assert report.max_non_orthogonality == pytest.approx(76.5452)
    assert report.max_skewness == pytest.approx(2.4332)


def test_metric_not_printed_is_nan():
    # checkMesh skips the aspect-ratio check on meshes with open cells.
    report = parse_checkmesh(_read("checkMesh_failed.log"))
    assert math.isnan(report.max_aspect_ratio)


def test_default_faces_patch_is_reported_by_name():
    text = _read("checkMesh_ok.log").replace("movingWall   ", "defaultFaces ")
    report = parse_checkmesh(text)
    assert "defaultFaces" in report.patches
    assert "movingWall" not in report.patches


def test_parse_garbage_raises():
    with pytest.raises(ValueError):
        parse_checkmesh("nonsense")


def test_parse_empty_raises():
    with pytest.raises(ValueError):
        parse_checkmesh("")


def test_parse_truncated_log_raises():
    # A run that died before the verdict must not be mistaken for a pass.
    text = _read("checkMesh_ok.log").split("Checking geometry")[0]
    with pytest.raises(ValueError):
        parse_checkmesh(text)


def _with_patch_row(name: str, faces: int = 60, points: int = 122) -> str:
    """The ok log with the 'fixedWalls' table row rewritten the way checkMesh prints it."""
    text = _read("checkMesh_ok.log")
    (old,) = [ln for ln in text.splitlines() if ln.lstrip().startswith("fixedWalls")]
    new = f"    {name:<20}{faces:<9}{points:<9}ok (non-closed singly connected)  "
    return text.replace(old, new)


@pytest.mark.parametrize("length", [19, 20, 21, 48])
def test_long_patch_names_are_parsed(length):
    name = "p" * (length - 2) + "ab"  # abuts the face count from 20 characters on
    report = parse_checkmesh(_with_patch_row(name))
    assert report.patches == {"movingWall": 20, name: 60, "frontAndBack": 800}


def test_patch_rows_after_a_long_name_are_kept():
    report = parse_checkmesh(_with_patch_row("q" * 30))
    assert list(report.patches) == ["movingWall", "q" * 30, "frontAndBack"]


def test_long_name_ending_in_digit_keeps_the_row_count():
    # The digits of the name and the face count cannot be told apart in the text; the row is
    # still counted (check_mesh reads names and face counts from the boundary file).
    report = parse_checkmesh(_with_patch_row("r" * 24 + "7"))
    assert len(report.patches) == 3


def test_patch_row_count_must_match_boundary_patches_line():
    text = _read("checkMesh_ok.log")
    (row,) = [ln for ln in text.splitlines() if ln.lstrip().startswith("fixedWalls")]
    with pytest.raises(ValueError, match="patch"):
        parse_checkmesh(text.replace(row + "\n", ""))
    with pytest.raises(ValueError, match="patch"):
        parse_checkmesh(text.replace("boundary patches: 3", "boundary patches: 4"))
