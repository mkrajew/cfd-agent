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
