"""Function-object tables and time-series helpers of ``cfdagent.tools.foam_results``.

The file layouts below follow what OpenFOAM ESI v2512 writes (checked against a real run in
``tests/integration/test_foam_results_openfoam.py``); the legacy header forms are what older
ESI versions wrote.
"""

import numpy as np
import pytest

from cfdagent.tools.foam_results import (
    FoamResultsError,
    FunctionObjectTable,
    dominant_frequency,
    period_average,
    read_function_object,
)

FORCE_HEADER = (
    "# Force\n"
    "# CofR          : (5.00000000e-01 5.00000000e-01 5.00000000e-02)\n"
    "#\n"
    "# Time          \ttotal_x total_y total_z\tpressure_x pressure_y pressure_z"
    "\tviscous_x viscous_y viscous_z\n"
)


def write_dat(case, function_object, start, name, text):
    directory = case / "postProcessing" / function_object / start
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(text, encoding="utf-8")


def force_rows(times, offset=0.0):
    return "".join(f"{t:g}   {offset + t:g} {2 * t:g} 0 1 2 3 4 5 6\n" for t in times)


def irregular_times(duration, seed=3):
    rng = np.random.default_rng(seed)
    steps = rng.uniform(0.004, 0.02, size=int(duration / 0.004))
    t = np.cumsum(steps)
    return t[t <= duration]


# ------------------------------------------------------------------------ dominant_frequency


def test_dominant_frequency_of_sine_with_transient():
    t = irregular_times(30.0)
    y = np.exp(-t) + np.sin(2 * np.pi * 1.7 * t)
    assert dominant_frequency(t, y) == pytest.approx(1.7, rel=0.005)


def test_dominant_frequency_ignores_offset_and_amplitude():
    t = irregular_times(20.0)
    y = 1000.0 + 0.01 * np.sin(2 * np.pi * 0.45 * t + 1.0)
    assert dominant_frequency(t, y) == pytest.approx(0.45, rel=0.005)


def test_dominant_frequency_needs_three_periods():
    t = np.linspace(0.0, 3.0, 600)  # 1.5 s remain after skipping half: 2.55 periods
    y = np.sin(2 * np.pi * 1.7 * t)
    with pytest.raises(FoamResultsError, match="3 periods"):
        dominant_frequency(t, y)
    # the whole record has 5.1 periods; without skipping it is enough
    assert dominant_frequency(t, y, skip_fraction=0.0) == pytest.approx(1.7, rel=0.02)


@pytest.mark.parametrize(("duration", "ok"), [(2.8, False), (3.4, True)])
def test_dominant_frequency_period_criterion_is_the_estimated_period(duration, ok):
    t = np.linspace(0.0, duration, 800)
    y = np.sin(2 * np.pi * 1.0 * t)  # 1 Hz: the period count equals the duration
    if ok:
        assert dominant_frequency(t, y, skip_fraction=0.0) == pytest.approx(1.0, rel=0.02)
    else:
        with pytest.raises(FoamResultsError, match="3 periods"):
            dominant_frequency(t, y, skip_fraction=0.0)


def test_dominant_frequency_refuses_a_constant_signal():
    t = np.linspace(0.0, 10.0, 500)
    with pytest.raises(FoamResultsError, match="constant"):
        dominant_frequency(t, np.full_like(t, 2.5))


@pytest.mark.parametrize(
    ("t", "y", "skip", "message"),
    [
        (np.arange(10.0), np.arange(9.0), 0.5, "same length"),
        (np.arange(10.0), np.r_[np.arange(9.0), np.nan], 0.5, "finite"),
        (np.r_[np.arange(9.0), np.inf], np.arange(10.0), 0.5, "finite"),
        (np.array([0.0, 1.0, 1.0, 2.0]), np.arange(4.0), 0.5, "increasing"),
        (np.array([0.0, 2.0, 1.0, 3.0]), np.arange(4.0), 0.5, "increasing"),
        (np.arange(100.0), np.arange(100.0), 1.0, "skip_fraction"),
        (np.arange(100.0), np.arange(100.0), -0.1, "skip_fraction"),
        (np.arange(100.0).reshape(10, 10), np.arange(100.0), 0.5, "one-dimensional"),
        (np.arange(3.0), np.arange(3.0), 0.0, "at least"),
    ],
)
def test_dominant_frequency_validates_its_input(t, y, skip, message):
    with pytest.raises(FoamResultsError, match=message):
        dominant_frequency(t, y, skip_fraction=skip)


# ---------------------------------------------------------------------------- period_average


def test_period_average_of_offset_sine():
    t = irregular_times(20.3, seed=5)
    frequency = 0.8
    y = 1.33 + 0.4 * np.sin(2 * np.pi * frequency * t + 0.7)
    assert period_average(t, y, frequency) == pytest.approx(1.33, abs=1e-3)
    assert period_average(t, y, frequency, n_periods=3) == pytest.approx(1.33, abs=1e-3)
    # the plain mean over the partial last period is visibly biased: that is what this is for
    assert abs(float(np.trapezoid(y, t)) / (t[-1] - t[0]) - 1.33) > 1e-3


def test_period_average_uses_the_last_periods():
    t = np.linspace(0.0, 10.0, 4001)
    y = np.where(t < 4.0, 100.0, 5.0 + np.sin(2 * np.pi * t))  # a start-up level, then period 1 s
    assert period_average(t, y, 1.0, n_periods=5) == pytest.approx(5.0, abs=1e-3)
    assert period_average(t, y, 1.0) > 5.5  # all 10 whole periods include the start-up level


def test_period_average_needs_one_whole_period():
    t = np.linspace(0.0, 0.9, 100)
    with pytest.raises(FoamResultsError, match="one period"):
        period_average(t, np.sin(t), 1.0)


def test_period_average_refuses_more_periods_than_available():
    t = np.linspace(0.0, 3.5, 350)
    with pytest.raises(FoamResultsError, match="3 whole periods"):
        period_average(t, np.sin(t), 1.0, n_periods=4)


@pytest.mark.parametrize("frequency", [0.0, -1.0, float("nan"), float("inf")])
def test_period_average_validates_the_frequency(frequency):
    t = np.linspace(0.0, 10.0, 100)
    with pytest.raises(FoamResultsError, match="frequency"):
        period_average(t, np.sin(t), frequency)


@pytest.mark.parametrize("n_periods", [0, -2, 1.5])
def test_period_average_validates_n_periods(n_periods):
    t = np.linspace(0.0, 10.0, 100)
    with pytest.raises(FoamResultsError, match="n_periods"):
        period_average(t, np.sin(t), 1.0, n_periods=n_periods)


def test_period_average_of_whole_record_that_is_exactly_whole_periods():
    t = np.linspace(0.0, 4.0, 4001)
    assert period_average(t, 2.0 + np.sin(2 * np.pi * 0.5 * t), 0.5) == pytest.approx(2.0, abs=1e-4)


# ----------------------------------------------------------------------- read_function_object


def test_read_function_object_concatenates_restarts(tmp_path):
    write_dat(tmp_path, "wallForces", "0", "force.dat", FORCE_HEADER + force_rows([0.1, 0.2, 0.3]))
    # the restart at 0.3 writes the time 0.3 again, with a different value
    write_dat(
        tmp_path,
        "wallForces",
        "0.3",
        "force.dat",
        FORCE_HEADER + force_rows([0.3, 0.4, 0.5], offset=100.0),
    )
    table = read_function_object(tmp_path, "wallForces", file="force.dat")
    assert table.column("Time") == pytest.approx([0.1, 0.2, 0.3, 0.4, 0.5])
    # later restart wins at the overlapping time
    assert table.column("total_x") == pytest.approx([0.1, 0.2, 100.3, 100.4, 100.5])


def test_a_restart_replaces_what_the_earlier_run_wrote_after_the_restart_time(tmp_path):
    write_dat(tmp_path, "f", "0", "force.dat", FORCE_HEADER + force_rows([0.1, 0.2, 0.3, 0.4]))
    write_dat(tmp_path, "f", "0.2", "force.dat", FORCE_HEADER + force_rows([0.2, 0.3], offset=50.0))
    table = read_function_object(tmp_path, "f")
    assert table.column("Time") == pytest.approx([0.1, 0.2, 0.3])
    assert table.column("total_x") == pytest.approx([0.1, 50.2, 50.3])


def test_start_time_directories_are_ordered_numerically(tmp_path):
    for start, times in (("10", [10.0, 11.0]), ("9", [8.0, 9.0]), ("100", [12.0, 13.0])):
        write_dat(tmp_path, "f", start, "force.dat", FORCE_HEADER + force_rows(times))
    table = read_function_object(tmp_path, "f")
    assert table.column("Time") == pytest.approx([8.0, 9.0, 10.0, 11.0, 12.0, 13.0])


def test_read_function_object_lists_files_when_ambiguous(tmp_path):
    write_dat(tmp_path, "wallForces", "0", "force.dat", FORCE_HEADER + force_rows([0.1]))
    write_dat(tmp_path, "wallForces", "0", "moment.dat", FORCE_HEADER + force_rows([0.1]))
    with pytest.raises(FoamResultsError) as excinfo:
        read_function_object(tmp_path, "wallForces")
    assert "force.dat" in str(excinfo.value) and "moment.dat" in str(excinfo.value)
    # the name with or without the extension selects a file
    assert read_function_object(tmp_path, "wallForces", file="moment").column("Time").size == 1
    assert read_function_object(tmp_path, "wallForces", file="moment.dat").column("Time").size == 1
    with pytest.raises(FoamResultsError, match=r"no file 'lift\.dat'.*force\.dat.*moment\.dat"):
        read_function_object(tmp_path, "wallForces", file="lift.dat")


def test_a_single_file_needs_no_name(tmp_path):
    write_dat(tmp_path, "f", "0", "coefficient.dat", FORCE_HEADER + force_rows([0.1, 0.2]))
    assert read_function_object(tmp_path, "f").column("Time") == pytest.approx([0.1, 0.2])


def test_missing_function_object_lists_what_exists(tmp_path):
    write_dat(tmp_path, "wallForces", "0", "force.dat", FORCE_HEADER + force_rows([0.1]))
    with pytest.raises(FoamResultsError, match="'drag'.*wallForces"):
        read_function_object(tmp_path, "drag")


def test_missing_postprocessing_directory(tmp_path):
    with pytest.raises(FoamResultsError, match="postProcessing"):
        read_function_object(tmp_path, "wallForces")


def test_missing_case_directory(tmp_path):
    with pytest.raises(FoamResultsError, match="does not exist"):
        read_function_object(tmp_path / "nope", "wallForces")


@pytest.mark.parametrize("name", ["", "..", "a/b", "a\\b", ".hidden"])
def test_function_object_name_must_be_a_plain_name(tmp_path, name):
    (tmp_path / "postProcessing").mkdir()
    with pytest.raises(FoamResultsError, match="function object name"):
        read_function_object(tmp_path, name)


def test_unknown_column_lists_the_available_ones(tmp_path):
    write_dat(tmp_path, "f", "0", "force.dat", FORCE_HEADER + force_rows([0.1, 0.2]))
    table = read_function_object(tmp_path, "f")
    assert table.columns[:4] == ["Time", "total_x", "total_y", "total_z"]
    assert table.data.shape == (2, 10)
    with pytest.raises(FoamResultsError, match=r"no column 'Fx'.*Time.*total_x.*viscous_z"):
        table.column("Fx")
    # a lookup differing only in case is accepted when it is unambiguous
    assert table.column("time") == pytest.approx([0.1, 0.2])


def test_column_returns_a_copy(tmp_path):
    write_dat(tmp_path, "f", "0", "force.dat", FORCE_HEADER + force_rows([0.1, 0.2]))
    table = read_function_object(tmp_path, "f")
    table.column("total_x")[:] = -1.0
    assert table.column("total_x") == pytest.approx([0.1, 0.2])


def test_force_coefficients_header_names_with_parentheses_stay_whole(tmp_path):
    text = (
        "# Force and moment coefficients\n"
        "# dragDir       : (1.00000000e+00 0.00000000e+00 0.00000000e+00)\n"
        "#\n"
        "# Time          \tCd              \tCd(f)           \tCd(r)           \tCl\n"
        "0.005           \t-3.5e-02\t-1.7e-02\t-1.8e-02\t1.4e-05\n"
        "0.01            \t-3.4e-02\t-1.6e-02\t-1.8e-02\t2.4e-05\n"
    )
    write_dat(tmp_path, "coeffs", "0", "coefficient.dat", text)
    table = read_function_object(tmp_path, "coeffs")
    assert table.columns == ["Time", "Cd", "Cd(f)", "Cd(r)", "Cl"]
    assert table.column("Cd(f)") == pytest.approx([-1.7e-2, -1.6e-2])


def test_group_headers_and_parenthesised_values_of_older_versions(tmp_path):
    text = (
        "# Forces\n"
        "# Time          forces(pressure viscous porous)   moments(pressure viscous porous)\n"
        "0.1   ((1 2 3) (4 5 6) (7 8 9)) ((10 11 12) (13 14 15) (16 17 18))\n"
        "0.2   ((2 3 4) (5 6 7) (8 9 10)) ((11 12 13) (14 15 16) (17 18 19))\n"
    )
    write_dat(tmp_path, "f", "0", "forces.dat", text)
    table = read_function_object(tmp_path, "f")
    assert table.columns[:4] == [
        "Time",
        "forces_pressure_x",
        "forces_pressure_y",
        "forces_pressure_z",
    ]
    assert len(table.columns) == 19
    assert table.column("forces_viscous_y") == pytest.approx([5.0, 6.0])
    assert table.column("moments_porous_z") == pytest.approx([18.0, 19.0])


def test_flat_parenthesised_name_group(tmp_path):
    text = (
        "# Time   (total_x total_y total_z)   (pressure_x pressure_y pressure_z)\n"
        "0.1   1 2 3 4 5 6\n"
        "0.2   2 3 4 5 6 7\n"
    )
    write_dat(tmp_path, "f", "0", "force.dat", text)
    table = read_function_object(tmp_path, "f")
    assert table.columns == [
        "Time",
        "total_x",
        "total_y",
        "total_z",
        "pressure_x",
        "pressure_y",
        "pressure_z",
    ]
    assert table.column("pressure_z") == pytest.approx([6.0, 7.0])


def test_probe_files_expand_vector_values_per_probe(tmp_path):
    text = (
        "# Probe 0 (0.5 0.5 0.05)\n"
        "# Probe 1 (0.2 0.2 0.05)\n"
        "# Time          0               1\n"
        "0.005           (-0.0015 9.8e-05 0) (-0.0004 0.00033 0)\n"
        "0.01            (-0.0030 1.9e-04 0) (-0.0008 0.00065 0)\n"
    )
    write_dat(tmp_path, "probes", "0", "U", text)
    write_dat(
        tmp_path,
        "probes",
        "0",
        "p",
        "# Probe 0 (0.5 0.5 0.05)\n# Probe 1 (0.2 0.2 0.05)\n# Time  0  1\n0.005 0.035 0.0007\n",
    )
    with pytest.raises(FoamResultsError, match="'U'.*'p'"):
        read_function_object(tmp_path, "probes")
    vectors = read_function_object(tmp_path, "probes", file="U")
    assert vectors.columns == ["Time", "0_x", "0_y", "0_z", "1_x", "1_y", "1_z"]
    assert vectors.column("1_y") == pytest.approx([3.3e-4, 6.5e-4])
    scalars = read_function_object(tmp_path, "probes", file="p")
    assert scalars.columns == ["Time", "0", "1"]
    assert scalars.column("1") == pytest.approx([7e-4])


def test_file_without_a_header_gets_numbered_columns(tmp_path):
    write_dat(tmp_path, "f", "0", "x.dat", "# some text\n0.1 1 2\n0.2 3 4\n")
    assert read_function_object(tmp_path, "f").columns == ["Time", "col1", "col2"]


def test_blank_lines_and_nan_are_tolerated(tmp_path):
    text = "# Time a\n\n0.1 1\n\n0.2 nan\n0.3 -nan\n"
    write_dat(tmp_path, "f", "0", "x.dat", text)
    table = read_function_object(tmp_path, "f")
    assert table.column("Time") == pytest.approx([0.1, 0.2, 0.3])
    assert np.isnan(table.column("a")[1:]).all()


def test_ragged_rows_name_the_line(tmp_path):
    write_dat(tmp_path, "f", "0", "x.dat", "# Time a b\n0.1 1 2\n0.2 3\n")
    with pytest.raises(FoamResultsError, match=r"x\.dat.*line 3"):
        read_function_object(tmp_path, "f")


def test_values_that_do_not_fit_the_header_are_reported(tmp_path):
    write_dat(tmp_path, "f", "0", "x.dat", "# Time a b\n0.1 1 2 3 4\n")
    with pytest.raises(FoamResultsError, match="2 column names"):
        read_function_object(tmp_path, "f")


def test_non_numeric_value_names_the_line(tmp_path):
    write_dat(tmp_path, "f", "0", "x.dat", "# Time a\n0.1 1\n0.2 oops\n")
    with pytest.raises(FoamResultsError, match=r"x\.dat.*line 3"):
        read_function_object(tmp_path, "f")


def test_file_without_data_rows(tmp_path):
    write_dat(tmp_path, "f", "0", "x.dat", "# Time a\n")
    with pytest.raises(FoamResultsError, match="no data rows"):
        read_function_object(tmp_path, "f")


def test_restart_files_with_different_columns_are_refused(tmp_path):
    write_dat(tmp_path, "f", "0", "x.dat", "# Time a b\n0.1 1 2\n")
    write_dat(tmp_path, "f", "1", "x.dat", "# Time a c\n1.1 1 2\n")
    with pytest.raises(FoamResultsError, match="different columns"):
        read_function_object(tmp_path, "f")


def test_time_directories_without_the_file_are_skipped(tmp_path):
    write_dat(tmp_path, "f", "0", "x.dat", "# Time a\n0.1 1\n")
    (tmp_path / "postProcessing" / "f" / "5").mkdir()
    assert read_function_object(tmp_path, "f").column("a") == pytest.approx([1.0])


def test_table_validates_its_shape():
    with pytest.raises(FoamResultsError, match="columns"):
        FunctionObjectTable(["Time", "a"], np.zeros((3, 3)))
    table = FunctionObjectTable(["Time", "a"], np.zeros((3, 2)))
    assert table.column("a").shape == (3,)


# ------------------------------------------------------------------------- fix round 1


def test_dominant_frequency_drops_the_first_part_not_the_last():
    t = irregular_times(30.0)
    # the first half is a stronger, slower oscillation; only the second half counts
    y = np.where(t < 15, 3 * np.sin(2 * np.pi * 0.3 * t), np.sin(2 * np.pi * 1.7 * t))
    assert dominant_frequency(t, y) == pytest.approx(1.7, rel=0.005)


def test_period_average_accepts_exactly_whole_periods_despite_rounding():
    t = np.linspace(0.0, 0.3, 301)  # 0.3 / 0.1 is 2.9999999999999996 in floating point
    y = 2.0 + np.sin(2 * np.pi * 10.0 * t)
    assert period_average(t, y, frequency=10.0, n_periods=3) == pytest.approx(2.0, abs=1e-4)


def test_a_header_only_restart_file_is_skipped(tmp_path):
    write_dat(tmp_path, "f", "0", "x.dat", "# Time a\n0.1 1\n0.2 2\n0.3 3\n")
    write_dat(tmp_path, "f", "0.3", "x.dat", "# Time a\n")
    table = read_function_object(tmp_path, "f")
    assert table.column("Time") == pytest.approx([0.1, 0.2, 0.3])
    # only when every file is empty is that an error
    write_dat(tmp_path, "f", "0", "x.dat", "# Time a\n")
    with pytest.raises(FoamResultsError, match="no data rows"):
        read_function_object(tmp_path, "f")


def test_time_going_backwards_inside_a_file_is_a_restart(tmp_path):
    text = "# Time a\n0.1 1\n0.2 2\n0.3 3\n0.1 10\n0.2 20\n"
    write_dat(tmp_path, "f", "0", "x.dat", text)
    table = read_function_object(tmp_path, "f")
    assert table.column("Time") == pytest.approx([0.1, 0.2])
    assert table.column("a") == pytest.approx([10.0, 20.0])


def test_scalar_and_vector_values_in_one_file(tmp_path):
    text = (
        "# Time          areaAverage(p) areaAverage(U)\n"
        "0.005           0.0427 (1 0 0)\n"
        "0.01            0.0431 (0.9 0.1 0)\n"
    )
    write_dat(tmp_path, "inlet", "0", "surfaceFieldValue.dat", text)
    table = read_function_object(tmp_path, "inlet")
    assert table.columns == [
        "Time",
        "areaAverage(p)",
        "areaAverage(U)_x",
        "areaAverage(U)_y",
        "areaAverage(U)_z",
    ]
    assert table.column("areaAverage(p)") == pytest.approx([0.0427, 0.0431])
    assert table.column("areaAverage(U)_y") == pytest.approx([0.0, 0.1])


def test_mixed_widths_that_do_not_fit_the_names_are_refused(tmp_path):
    write_dat(tmp_path, "f", "0", "x.dat", "# Time a b\n0.1 1 (1 2)\n")
    with pytest.raises(FoamResultsError, match="2 column names"):
        read_function_object(tmp_path, "f")
