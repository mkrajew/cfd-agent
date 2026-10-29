"""Parsing of OpenFOAM checkMesh output, using logs captured inside the Docker image."""

import dataclasses
import math
from pathlib import Path

import pytest

from cfdagent.tools.mesh import (
    CheckMeshReport,
    MeshThresholds,
    parse_checkmesh,
    quality_errors,
)

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


def test_patch_names_with_shared_long_prefix_are_not_collapsed():
    """Real checkMesh output: names of 20+ characters ending in a digit abut the face count.

    'shared_long_prefix_in_1' and '..._in_2' print as '..._in_11' and '..._in_21'; the two rows
    must both be counted, not merged into one dict key (which looked like a missing patch).
    """
    report = parse_checkmesh(_read("checkMesh_long_names.log"))
    assert report.n_patch_rows == 4
    assert report.n_cells == 1
    assert report.patches["walls"] == 2
    assert report.patches["frontAndBack"] == 2


def test_aspect_ratio_with_colon_is_parsed():
    text = _read("checkMesh_ok.log").replace(
        "Max aspect ratio = 1 OK.", "Max aspect ratio: 12.5 OK."
    )
    assert parse_checkmesh(text).max_aspect_ratio == 12.5


def test_skewness_with_colon_is_parsed():
    text = _read("checkMesh_ok.log").replace("Max skewness = 1.66533e-14", "Max skewness: 0.25")
    assert parse_checkmesh(text).max_skewness == 0.25


def _report(**changes):
    return dataclasses.replace(parse_checkmesh(_read("checkMesh_ok.log")), **changes)


def test_quality_within_defaults_passes():
    assert quality_errors(_report(), MeshThresholds()) == []


def test_default_thresholds():
    t = MeshThresholds()
    assert (t.max_non_orthogonality, t.max_skewness, t.max_aspect_ratio) == (70.0, 4.0, 1000.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        t.max_skewness = 5.0  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value", "word"),
    [
        ("max_non_orthogonality", 70.5, "non-orthogonality"),
        ("max_skewness", 4.5, "skewness"),
        ("max_aspect_ratio", 1500.0, "aspect ratio"),
    ],
)
def test_quality_threshold_exceeded_fails(field, value, word):
    errors = quality_errors(_report(**{field: value}), MeshThresholds())
    assert len(errors) == 1 and word in errors[0]


def test_quality_exactly_at_threshold_passes():
    report = _report(max_non_orthogonality=70.0, max_skewness=4.0, max_aspect_ratio=1000.0)
    assert quality_errors(report, MeshThresholds()) == []


def test_custom_thresholds_are_used():
    errors = quality_errors(_report(max_aspect_ratio=2.0), MeshThresholds(max_aspect_ratio=1.5))
    assert len(errors) == 1


def test_nan_aspect_ratio_fails():
    # checkMesh skips the aspect-ratio check after open cells; an unknown value never passes.
    errors = quality_errors(_report(max_aspect_ratio=math.nan), MeshThresholds())
    assert len(errors) == 1 and "aspect ratio" in errors[0] and "not reported" in errors[0]


def test_nan_everything_reports_every_metric():
    nan = math.nan
    report = _report(max_non_orthogonality=nan, max_skewness=nan, max_aspect_ratio=nan)
    assert len(quality_errors(report, MeshThresholds())) == 3
