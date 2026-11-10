"""Reading the results of an ASCII OpenFOAM case (``cfdagent.tools.foam_results``), no OpenFOAM."""

import shutil
from pathlib import Path

import numpy as np
import pytest
from foam_fixtures import write_box_case, write_field

from cfdagent.errors import AgentInputError
from cfdagent.tools.foam_results import (
    FoamResultsError,
    _face_geometry,
    latest_time,
    list_times,
    load_case,
    read_solver_log,
)

LOGS = Path(__file__).parents[1] / "fixtures" / "logs"


def graded(points: np.ndarray) -> np.ndarray:
    """A non-uniform but axis-aligned grid: spacing grows with x, shrinks with y."""
    out = points.copy()
    out[:, 0] = 2.0 * (points[:, 0] / 2.0) ** 2
    out[:, 1] = np.sqrt(points[:, 1])
    return out


def sheared(points: np.ndarray) -> np.ndarray:
    """An affine map: parallelogram cells, so cell centres and volumes follow in closed form."""
    out = points.copy()
    out[:, 0] = points[:, 0] + 0.3 * points[:, 1]
    return out


def linear(x: np.ndarray) -> np.ndarray:
    return 1.5 + 2.0 * x[..., 0] - 3.0 * x[..., 1] + 0.5 * x[..., 2]


@pytest.fixture
def box(tmp_path):
    return write_box_case(tmp_path, shape=(4, 3, 1), size=(2.0, 1.0, 0.1))


# --------------------------------------------------------------------------------------- times


def test_list_times_sorts_by_value_and_ignores_other_entries(tmp_path):
    case = tmp_path / "case"
    for name in (
        "0",
        "0.5",
        "1e-05",
        "100",
        "9",
        "constant",
        "system",
        "processor0",
        "postProcessing",
    ):
        (case / name).mkdir(parents=True)
    (case / "12").write_text("a file, not a time directory")
    assert list_times(case) == [0.0, 1e-05, 0.5, 9.0, 100.0]
    assert latest_time(case) == 100.0


def test_latest_time_without_time_directories_is_an_error(tmp_path):
    (tmp_path / "case" / "constant").mkdir(parents=True)
    with pytest.raises(FoamResultsError, match="no time directory"):
        latest_time(tmp_path / "case")


def test_a_missing_case_is_an_error_naming_the_path(tmp_path):
    with pytest.raises(FoamResultsError, match="does-not-exist"):
        load_case(tmp_path / "does-not-exist")
    assert issubclass(FoamResultsError, AgentInputError)


# --------------------------------------------------------------------------------------- mesh


def test_cell_centres_and_volumes_of_a_uniform_box(box):
    case = load_case(box)
    assert case.n_cells == 12
    assert case.cell_volumes == pytest.approx(np.full(12, 0.5 * (1 / 3) * 0.1))
    expected = [
        ((i + 0.5) * 0.5, (j + 0.5) / 3, 0.05) for j in range(3) for i in range(4)
    ]  # cell index = i + 4 j
    assert case.cell_centres == pytest.approx(np.array(expected))
    assert case.bounds[0] == pytest.approx([0, 0, 0])
    assert case.bounds[1] == pytest.approx([2, 1, 0.1])
    assert case.is_2d


def test_cell_geometry_of_a_sheared_mesh_follows_the_affine_map(tmp_path):
    base = load_case(write_box_case(tmp_path / "a", warp=None))
    case = load_case(write_box_case(tmp_path / "b", warp=sheared))
    mapped = base.cell_centres.copy()
    mapped[:, 0] += 0.3 * base.cell_centres[:, 1]
    assert case.cell_centres == pytest.approx(mapped)
    assert case.cell_volumes == pytest.approx(base.cell_volumes)  # shear keeps the volume


def test_cell_centre_of_a_trapezoidal_cell_is_its_centroid(tmp_path):
    """One cell, x from 0 to 1 + y: area 1.5, centroid (7/9, 5/9) (times the thickness 0.1)."""

    def taper(points):
        out = points.copy()
        out[:, 0] = points[:, 0] * (1.0 + points[:, 1])
        return out

    case = load_case(write_box_case(tmp_path, shape=(1, 1, 1), size=(1.0, 1.0, 0.1), warp=taper))
    assert case.cell_volumes == pytest.approx(np.array([0.15]))
    assert case.cell_centres == pytest.approx(np.array([[7 / 9, 5 / 9, 0.05]]))


def test_face_geometry_of_triangles_and_polygons():
    triangle = np.array([[[0.0, 0, 0], [2, 0, 0], [0, 2, 0]]])
    centre, area = _face_geometry(triangle)
    assert centre == pytest.approx(np.array([[2 / 3, 2 / 3, 0]]))
    assert area == pytest.approx(np.array([[0, 0, 2.0]]))  # counter-clockwise: normal +z, area 2
    pentagon = np.array([[[0.0, 0, 1], [2, 0, 1], [2, 2, 1], [1, 3, 1], [0, 2, 1]]])
    centre, area = _face_geometry(pentagon)
    assert area == pytest.approx(
        np.array([[0, 0, 5.0]])
    )  # a 2 x 2 square plus a triangle of area 1
    assert centre[0, 2] == pytest.approx(1.0)
    assert centre[0, 0] == pytest.approx(1.0)  # symmetric about x = 1
    assert centre[0, 1] == pytest.approx((4 * 1.0 + 1 * (2 + 1 / 3)) / 5)


def test_patches_are_listed_with_type_and_faces(box):
    case = load_case(box)
    assert list(case.patches) == ["left", "right", "bottom", "top", "frontAndBack"]
    left = case.patches["left"]
    assert left.type == "patch"
    assert left.n_faces == 3
    assert case.patches["frontAndBack"].type == "empty"
    assert case.patches["frontAndBack"].n_faces == 24
    assert left.face_centres == pytest.approx(
        np.array([[0, 1 / 6, 0.05], [0, 0.5, 0.05], [0, 5 / 6, 0.05]])
    )
    assert left.owner_cells.tolist() == [0, 4, 8]
    assert left.face_areas == pytest.approx(np.tile([-(1 / 3) * 0.1, 0, 0], (3, 1)))  # outward


def test_a_three_dimensional_mesh_is_not_2d(tmp_path):
    case = load_case(write_box_case(tmp_path, shape=(2, 2, 2), size=(1, 1, 1)))
    assert not case.is_2d
    assert case.n_cells == 8
    assert case.cell_volumes == pytest.approx(np.full(8, 0.125))
    assert case.cell_centres[7] == pytest.approx([0.75, 0.75, 0.75])


# --------------------------------------------------------------------------------------- fields


def test_read_field_scalar_vector_and_the_latest_time_by_default(box):
    case = load_case(box)
    p = np.arange(12, dtype=float)
    u = np.stack([p, -p, 2 * p], axis=1)
    write_field(box, "0", "p", p * 0, {})
    write_field(box, "0.5", "p", p, {})
    write_field(box, "0.5", "U", u, {})
    assert case.times == [0.0, 0.5]
    assert case.latest_time == 0.5
    assert case.read_field("p") == pytest.approx(p)
    assert case.read_field("p", time=0).tolist() == [0.0] * 12
    assert case.read_field("U", time="0.5").shape == (12, 3)
    assert case.read_field("U") == pytest.approx(u)
    assert case.list_fields() == ["U", "p"]
    assert case.list_fields(time=0) == ["p"]


def test_read_field_uniform_and_compact_internal_fields(box):
    case = load_case(box)
    write_field(box, "1", "p", np.full(12, 2.5), {}, uniform=True)
    write_field(box, "1", "U", np.tile([1.0, 0.0, 0.0], (12, 1)), {}, uniform=True)
    write_field(box, "2", "T", np.full(12, 300.0), {}, compact=True)
    write_field(box, "2", "V", np.tile([0.0, 2.0, 0.0], (12, 1)), {}, compact=True)
    assert case.read_field("p", time=1).tolist() == [2.5] * 12
    assert case.read_field("U", time=1) == pytest.approx(np.tile([1.0, 0, 0], (12, 1)))
    assert case.read_field("T", time=2).tolist() == [300.0] * 12
    assert case.read_field("V", time=2).shape == (12, 3)


def test_read_field_comments_and_gz_are_handled(box):
    import gzip

    case = load_case(box)
    path = write_field(box, "3", "p", np.arange(12.0), {})
    text = path.read_text(encoding="utf-8")
    path.write_text("/* banner */\n// a line\n" + text, encoding="utf-8")
    assert case.read_field("p", time=3).tolist() == list(range(12))
    path.with_name("q.gz").write_bytes(
        gzip.compress(text.replace("object      p", "object      q").encode())
    )
    assert case.read_field("q", time=3).tolist() == list(range(12))


def test_read_field_errors_are_clear(box):
    case = load_case(box)
    write_field(box, "1", "p", np.arange(12.0), {})
    write_field(box, "1", "bin", np.arange(12.0), {}, fmt="binary")
    write_field(box, "1", "short", np.arange(5.0), {})
    with pytest.raises(FoamResultsError, match=r"no field 'T' at time 1.*available.*p"):
        case.read_field("T", time=1)
    with pytest.raises(FoamResultsError, match="binary"):
        case.read_field("bin", time=1)
    with pytest.raises(FoamResultsError, match=r"5 values.*12 cells"):
        case.read_field("short", time=1)
    with pytest.raises(FoamResultsError, match=r"no time 7.*available times: 1"):
        case.read_field("p", time=7)


def test_patch_field_values_and_fallbacks(box):
    case = load_case(box)
    p = np.arange(12, dtype=float) * 10
    write_field(
        box,
        "1",
        "p",
        p,
        {
            "left": ("fixedValue", ("uniform", 7.0)),
            "right": ("fixedValue", np.array([1.0, 2.0, 3.0])),
            "bottom": ("zeroGradient", None),
            "top": ("zeroGradient", None),
            "frontAndBack": ("empty", None),
        },
    )
    write_field(
        box,
        "1",
        "U",
        np.zeros((12, 3)),
        {
            "left": ("fixedValue", ("uniform", [1.0, 0.0, 0.0])),
            "right": ("noSlip", None),
            "bottom": ("slip", None),
            "top": ("fixedValue", np.tile([0.0, 2.0, 0.0], (4, 1))),
            "frontAndBack": ("empty", None),
        },
    )
    assert case.read_patch_field("p", "left").tolist() == [7.0, 7.0, 7.0]
    assert case.read_patch_field("p", "right").tolist() == [1.0, 2.0, 3.0]
    # zeroGradient writes no value: the value is that of the adjacent cell
    assert case.read_patch_field("p", "bottom").tolist() == [0.0, 10.0, 20.0, 30.0]
    assert case.read_patch_field("p", "top").tolist() == [80.0, 90.0, 100.0, 110.0]
    assert case.read_patch_field("U", "left") == pytest.approx(np.tile([1.0, 0, 0], (3, 1)))
    assert case.read_patch_field("U", "right") == pytest.approx(np.zeros((3, 3)))
    assert case.read_patch_field("U", "top").shape == (4, 3)
    with pytest.raises(FoamResultsError, match="empty"):
        case.read_patch_field("p", "frontAndBack")
    with pytest.raises(FoamResultsError, match=r"'slip'.*no value.*owner_cells"):
        case.read_patch_field("U", "bottom")
    with pytest.raises(FoamResultsError, match=r"no patch 'inlet'.*left, right, bottom, top"):
        case.read_patch_field("p", "inlet")


def test_a_surface_field_is_read_on_internal_faces_and_patch_faces(tmp_path):
    root = write_box_case(tmp_path, shape=(2, 1, 1), size=(2, 1, 1))
    case = load_case(root)
    write_field(
        root,
        "1",
        "phi",
        np.array([0.25]),
        {"left": ("calculated", np.array([-0.25])), "right": ("calculated", np.array([0.25]))},
        cls="surfaceScalarField",
    )
    assert case.read_field("phi", time=1).tolist() == [0.25]
    assert case.read_patch_field("phi", "left").tolist() == [-0.25]


# --------------------------------------------------------------------------------------- sampling


def _case_with_linear_field(tmp_path, warp):
    root = write_box_case(tmp_path, shape=(8, 6, 1), size=(2.0, 1.0, 0.1), warp=warp)
    case = load_case(root)
    p = linear(case.cell_centres)
    u = np.stack([p, 2 * p + 1, np.zeros_like(p)], axis=1)
    boundary = {}
    for name, patch in case.patches.items():
        if patch.type == "empty":
            boundary[name] = ("empty", None)
        else:
            boundary[name] = ("fixedValue", linear(patch.face_centres))
    write_field(root, "1", "p", p, boundary)
    write_field(root, "1", "U", u, {n: (t, None) for n, (t, _) in boundary.items()})
    return case


@pytest.mark.parametrize("warp", [None, graded, sheared])
def test_linear_sampling_reproduces_a_linear_field_exactly(tmp_path, warp):
    case = _case_with_linear_field(tmp_path, warp)
    start, end = (0.05, 0.1, 0.05), (1.9, 0.9, 0.05)
    line = case.sample_line("p", start, end, n_points=40, method="linear")
    assert line.points.shape == (40, 3)
    assert line.s[0] == 0.0
    assert line.s[-1] == pytest.approx(np.linalg.norm(np.subtract(end, start)))
    assert line.inside.all()
    assert line.value == pytest.approx(linear(line.points), abs=1e-9)
    vec = case.sample_line("U", start, end, n_points=40)
    assert vec.value.shape == (40, 3)
    assert vec.value[:, 0] == pytest.approx(linear(vec.points), abs=1e-9)
    assert vec.value[:, 1] == pytest.approx(2 * linear(vec.points) + 1, abs=1e-9)


def test_linear_sampling_uses_the_patch_values_where_neighbours_are_missing(tmp_path):
    """A single column of cells has no neighbour in x: the x gradient needs the patch values."""
    root = write_box_case(tmp_path, shape=(1, 4, 1), size=(2.0, 1.0, 0.1))
    case = load_case(root)
    boundary = {
        name: ("empty", None)
        if patch.type == "empty"
        else ("fixedValue", linear(patch.face_centres))
        for name, patch in case.patches.items()
    }
    write_field(root, "1", "p", linear(case.cell_centres), boundary)
    line = case.sample_line("p", (0.1, 0.4), (1.9, 0.4), n_points=7)
    assert line.value == pytest.approx(linear(line.points), abs=1e-9)


def test_nearest_sampling_returns_the_cell_value_of_the_containing_cell(box):
    case = load_case(box)
    p = np.arange(12, dtype=float)
    write_field(box, "1", "p", p, {})
    # points inside cells 0, 5 (i=1, j=1) and 11 (i=3, j=2)
    line = case.sample_line("p", (0.1, 0.1, 0.05), (1.9, 0.9, 0.05), n_points=2, method="nearest")
    assert line.cell.tolist() == [0, 11]
    assert line.value.tolist() == [0.0, 11.0]
    on_axis = case.sample_line(
        "p", (0.75, 0.0, 0.05), (0.75, 1.0, 0.05), n_points=3, method="nearest"
    )
    assert on_axis.cell.tolist() == [1, 5, 9]


def test_points_outside_the_mesh_are_nan_and_can_be_dropped(box):
    case = load_case(box)
    write_field(box, "1", "p", np.arange(12.0), {})
    line = case.sample_line("p", (-1.0, 0.5, 0.05), (1.0, 0.5, 0.05), n_points=5)
    # x = -1, -0.5 outside; 0, 0.5, 1 inside (x = 0 lies on the left boundary)
    assert line.inside.tolist() == [False, False, True, True, True]
    assert np.isnan(line.value[:2]).all()
    assert line.cell[:2].tolist() == [-1, -1]
    kept = line.drop_outside()
    assert len(kept.value) == 3
    assert kept.s[0] == pytest.approx(1.0)
    assert not np.isnan(kept.value).any()


def test_a_line_that_misses_the_mesh_is_an_error_naming_the_bounds(box):
    case = load_case(box)
    write_field(box, "1", "p", np.arange(12.0), {})
    with pytest.raises(FoamResultsError, match=r"no point.*inside the mesh.*bounds.*z"):
        case.sample_line("p", (0.0, 0.5, 5.0), (1.0, 0.5, 5.0), n_points=5)


def test_two_component_points_use_the_mid_plane_of_a_2d_case(box, tmp_path):
    case = load_case(box)
    write_field(box, "1", "p", np.arange(12.0), {})
    line = case.sample_line("p", (0.1, 0.1), (1.9, 0.9), n_points=2, method="nearest")
    assert line.points[:, 2].tolist() == pytest.approx([0.05, 0.05])
    case3d = load_case(write_box_case(tmp_path / "d3", shape=(2, 2, 2), size=(1, 1, 1)))
    write_field(case3d.path.parent / "case", "1", "p", np.arange(8.0), {})
    with pytest.raises(FoamResultsError, match="three coordinates"):
        case3d.sample_line("p", (0.1, 0.1), (0.9, 0.9))


def test_sample_line_argument_errors(box):
    case = load_case(box)
    write_field(box, "1", "p", np.arange(12.0), {})
    with pytest.raises(FoamResultsError, match="method"):
        case.sample_line("p", (0, 0, 0.05), (1, 1, 0.05), method="cubic")
    with pytest.raises(FoamResultsError, match="n_points"):
        case.sample_line("p", (0, 0, 0.05), (1, 1, 0.05), n_points=1)
    with pytest.raises(FoamResultsError, match="differ"):
        case.sample_line("p", (0, 0, 0.05), (0, 0, 0.05))


# --------------------------------------------------------------------------------------- safety


def test_symlinked_time_directories_and_fields_are_not_followed(box, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    write_field(outside, "0", "p", np.arange(12.0), {})
    try:
        (box / "5").symlink_to(outside / "0", target_is_directory=True)
        (box / "1").mkdir()
        (box / "1" / "p").symlink_to(outside / "0" / "p")
    except OSError:
        pytest.skip("symbolic links are not available")
    case = load_case(box)
    assert case.times == [1.0]  # "5" is a link to a directory: not a time
    with pytest.raises(FoamResultsError, match="symbolic link"):
        case.read_field("p", time=1)


# --------------------------------------------------------------------------------------- log


def test_read_solver_log_of_a_steady_run(tmp_path):
    case = tmp_path / "case"
    case.mkdir()
    shutil.copy(LOGS / "simpleFoam_converged.log", case / "log.simpleFoam")
    (case / "log.postProcess").write_text("Time = 12\nEnd\n")  # no solver, ignored
    log = read_solver_log(case)
    assert log.path.name == "log.simpleFoam"
    assert log.solver == "simpleFoam"
    assert log.converged_at == 47
    assert log.completed
    assert set(log.initial_residuals) >= {"p"}
    assert set(log.final_residuals) >= {"p"}
    assert log.n_time_steps == 47
    assert log.last_time == 47  # a steady run: the time is the iteration number
    assert log.to_dict()["converged_at"] == log.converged_at


def test_read_solver_log_of_a_transient_run_by_file(tmp_path):
    log = read_solver_log(LOGS / "icoFoam_ok.log")
    assert log.solver == "icoFoam"
    assert (log.n_time_steps, log.last_time) == (7, 0.5)
    assert log.completed
    assert log.converged_at is None
    assert "Ux" in log.initial_residuals
    assert log.first_initial_residuals["Ux"] != log.initial_residuals["Ux"]


def test_read_solver_log_without_a_solver_log_lists_what_there_is(tmp_path):
    (tmp_path / "log.checkMesh").write_text("Mesh OK.\nEnd\n")
    with pytest.raises(FoamResultsError, match=r"no solver log.*log.checkMesh"):
        read_solver_log(tmp_path)
    with pytest.raises(FoamResultsError, match="does not exist"):
        read_solver_log(tmp_path / "log.missing")


def test_read_solver_log_of_a_case_takes_the_newest_solver_log(tmp_path):
    import os

    old, new = tmp_path / "log.icoFoam", tmp_path / "log.simpleFoam"
    shutil.copy(LOGS / "icoFoam_ok.log", old)
    shutil.copy(LOGS / "simpleFoam_converged.log", new)
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(new, (2_000_000, 2_000_000))
    assert read_solver_log(tmp_path).solver == "simpleFoam"
    os.utime(old, (3_000_000, 3_000_000))
    assert read_solver_log(tmp_path).solver == "icoFoam"


@pytest.mark.parametrize("name", ["../p", "../../outside/p", "sub/p", "..\\p", ".hidden", ""])
def test_a_field_name_cannot_leave_the_time_directory(box, name):
    write_field(box, "1", "p", np.arange(12.0), {})
    case = load_case(box)
    with pytest.raises(FoamResultsError, match="not a field name"):
        case.read_field(name, time=1)
    with pytest.raises(FoamResultsError, match="not a field name"):
        case.read_patch_field(name, "left", time=1)
