"""Post-processing helpers: OpenFOAM raw set files, time directories, quantities JSON."""

import json
import math
from pathlib import Path

import numpy as np
import pytest

from cfdagent.tools.post import (
    PostProcessingError,
    latest_time_dir,
    read_line_sample,
    write_quantities,
)

SAMPLES = Path(__file__).parents[1] / "fixtures" / "samples"


def test_read_line_sample_columns():
    data = read_line_sample(SAMPLES / "vertical_U.xy", columns=("y", "Ux", "Uy", "Uz"))
    assert set(data) == {"y", "Ux", "Uy", "Uz"}
    assert len({len(v) for v in data.values()}) == 1
    assert len(data["y"]) == 11
    assert np.all(np.diff(data["y"]) > 0)
    assert data["y"][0] == 0.0
    assert data["y"][-1] == pytest.approx(0.1)
    assert data["Ux"][-1] == pytest.approx(1.0)  # moving lid
    assert all(v.dtype == np.float64 for v in data.values())


def test_read_line_sample_ignores_comments_and_blank_lines(tmp_path):
    path = tmp_path / "s.xy"
    path.write_text("# x  T\n\n0 1\n  # note\n0.5\t2\n1   3\n", encoding="utf-8")
    data = read_line_sample(path, ["x", "T"])
    assert data["x"].tolist() == [0.0, 0.5, 1.0]
    assert data["T"].tolist() == [1.0, 2.0, 3.0]


def test_read_line_sample_column_count_mismatch():
    with pytest.raises(ValueError, match="columns"):
        read_line_sample(SAMPLES / "vertical_U.xy", columns=("y", "Ux"))
    with pytest.raises(ValueError, match="columns"):
        read_line_sample(SAMPLES / "vertical_U.xy", columns=("y", "Ux", "Uy", "Uz", "p"))


def test_read_line_sample_ragged_row(tmp_path):
    path = tmp_path / "s.xy"
    path.write_text("0 1\n1 2 3\n", encoding="utf-8")
    with pytest.raises(ValueError, match="line 2"):
        read_line_sample(path, ["x", "T"])


def test_read_line_sample_non_numeric(tmp_path):
    path = tmp_path / "s.xy"
    path.write_text("0 1\n1 oops\n", encoding="utf-8")
    with pytest.raises(ValueError, match="line 2"):
        read_line_sample(path, ["x", "T"])


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf"])
def test_read_line_sample_rejects_non_finite(tmp_path, bad):
    path = tmp_path / "s.xy"
    path.write_text(f"0 1\n1 {bad}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        read_line_sample(path, ["x", "T"])


def test_read_line_sample_empty_file(tmp_path):
    path = tmp_path / "s.xy"
    path.write_text("# only a comment\n\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no data"):
        read_line_sample(path, ["x", "T"])


def test_read_line_sample_bad_column_names(tmp_path):
    path = tmp_path / "s.xy"
    path.write_text("0 1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        read_line_sample(path, ["x", "x"])
    with pytest.raises(ValueError):
        read_line_sample(path, [])


def test_latest_time_dir_is_numerically_largest(tmp_path):
    for name in ["0", "0.5", "1e-05", "100", "20", "9"]:
        (tmp_path / name).mkdir()
    assert latest_time_dir(tmp_path) == tmp_path / "100"


def test_latest_time_dir_handles_small_and_scientific_names(tmp_path):
    for name in ["0", "1e-05", "0.0001", "5e-05"]:
        (tmp_path / name).mkdir()
    assert latest_time_dir(tmp_path) == tmp_path / "0.0001"


def test_latest_time_dir_ignores_files_and_non_numeric_names(tmp_path):
    (tmp_path / "0.1").mkdir()
    (tmp_path / "constant").mkdir()
    (tmp_path / "nan").mkdir()
    (tmp_path / "inf").mkdir()
    (tmp_path / "7").write_text("a file, not a time directory")
    assert latest_time_dir(tmp_path) == tmp_path / "0.1"


def test_latest_time_dir_none_found(tmp_path):
    (tmp_path / "constant").mkdir()
    with pytest.raises(PostProcessingError):
        latest_time_dir(tmp_path)
    with pytest.raises(PostProcessingError):
        latest_time_dir(tmp_path / "missing")


def test_write_quantities_is_sorted_lf_json(tmp_path):
    path = tmp_path / "out" / "q.json"
    write_quantities(
        path,
        {"b": 2.0, "a": 1, "profile": {"x": np.array([0.0, 1.0]), "value": [0.5, 0.25]}},
    )
    raw = path.read_bytes()
    assert b"\r" not in raw
    assert raw.endswith(b"\n")
    data = json.loads(raw)
    assert list(data) == ["a", "b", "profile"]
    assert data == {"a": 1.0, "b": 2.0, "profile": {"value": [0.5, 0.25], "x": [0.0, 1.0]}}
    assert isinstance(data["a"], float)
    assert list(data["profile"]) == ["value", "x"]


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_write_quantities_rejects_non_finite_scalar(tmp_path, bad):
    path = tmp_path / "q.json"
    with pytest.raises(ValueError):
        write_quantities(path, {"a": bad})
    assert not path.exists()


def test_write_quantities_rejects_non_finite_profile_value(tmp_path):
    path = tmp_path / "q.json"
    with pytest.raises(ValueError):
        write_quantities(path, {"p": {"x": [0.0, 1.0], "value": [1.0, math.nan]}})
    assert not path.exists()


def test_write_quantities_rejects_mismatched_profile_lengths(tmp_path):
    with pytest.raises(ValueError):
        write_quantities(tmp_path / "q.json", {"p": {"x": [0.0, 1.0], "value": [1.0]}})
