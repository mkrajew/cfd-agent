"""The convergence checks of the Solver Runner gate (ADR-0006, decision 6): steadiness and
periodicity. Pure numpy and text files; no OpenFOAM, no Docker."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from cfdagent.tools import convergence
from cfdagent.tools.convergence import (
    ConvergenceThresholds,
    check_convergence,
    periodicity,
    read_monitors,
    steadiness,
)
from cfdagent.tools.foam import SolverLogSummary
from cfdagent.tools.foam_results import FunctionObjectTable

T = ConvergenceThresholds()


def summary(converged_at=None, initial=None, completed=True, **changes) -> SolverLogSummary:
    data = {
        "completed": completed,
        "diverged": False,
        "fatal_error": None,
        "end_time": 1000.0,
        "final_residuals": {"p": 1e-9},
        "max_courant": None,
        "converged_at": converged_at,
        "initial_residuals": {"Ux": 3e-8, "p": 8e-9} if initial is None else initial,
    }
    data.update(changes)
    return SolverLogSummary(**data)


def table(**columns) -> FunctionObjectTable:
    """A function-object table; the time runs 1..n and every column has n values."""
    n = len(next(iter(columns.values())))
    names = ["Time", *columns]
    data = np.column_stack(
        [np.arange(1.0, n + 1), *[np.asarray(v, float) for v in columns.values()]]
    )
    return FunctionObjectTable(names, data)


def converging(n=1000, final=2.0, scale=1.0, rate=0.05):
    """A history that settles to ``final``: exponential decay of the initial offset."""
    t = np.arange(n)
    return final + scale * np.exp(-rate * t)


def wave(t, frequency=1.0, amplitude=1.0, mean=0.0, growth=0.0):
    return mean + amplitude * (1.0 + growth * t) * np.sin(2 * math.pi * frequency * t)


# ------------------------------------------------------------------ thresholds


def test_provisional_thresholds_are_the_values_of_the_adr():
    assert T.final_residual_max == 1e-4
    assert T.steady_window == 0.1
    assert T.steady_rel_change == 1e-3
    assert T.min_periods == 10
    assert T.periodic_rel_change == 0.02
    with pytest.raises(AttributeError):  # frozen
        T.min_periods = 3  # type: ignore[misc]


# ------------------------------------------------------------------ steadiness


def test_residual_control_met_is_steady():
    report = steadiness(summary(converged_at=500.0), {}, T)
    assert report.ok and report.criterion == "residual_control"
    assert "500" in report.details


def test_residual_control_does_not_count_for_a_run_that_did_not_complete():
    assert not steadiness(summary(converged_at=500.0, completed=False), {}, T).ok


def test_monitored_change_makes_time_marched_case_steady():
    monitors = {"coeffs": table(Cd=converging(final=2.0, rate=0.05))}
    report = steadiness(summary(), monitors, T)
    assert report.ok and report.criterion == "monitored_change"


def test_a_drifting_monitor_is_not_steady():
    # 5 % growth over the last tenth of the record: far above 1e-3
    monitors = {"coeffs": table(Cd=np.linspace(1.0, 2.0, 1000))}
    report = steadiness(summary(), monitors, T)
    assert not report.ok and report.criterion == "none"
    assert "Cd" in report.details and "coeffs" in report.details


def test_the_change_is_judged_over_the_last_window_only():
    # a long transient that has died out before the last tenth of the record
    y = converging(n=1000, final=3.0, scale=5.0, rate=0.2)
    assert steadiness(summary(), {"m": table(Cd=y)}, T).ok
    # the same decay, still going on in the window: the record is too short for it
    slow = converging(n=1000, final=3.0, scale=5.0, rate=0.002)
    assert not steadiness(summary(), {"m": table(Cd=slow)}, T).ok


def test_every_monitored_quantity_must_be_steady():
    monitors = {"a": table(Cd=converging()), "b": table(Cl=np.linspace(0.0, 1.0, 1000))}
    report = steadiness(summary(), monitors, T)
    assert not report.ok and "Cl" in report.details and "Cd" not in report.details


def test_high_final_initial_residuals_block_monitored_steadiness():
    monitors = {"coeffs": table(Cd=converging())}
    report = steadiness(summary(initial={"Ux": 3e-8, "p": 2e-3}), monitors, T)
    assert not report.ok
    assert "p" in report.details and "residual" in report.details.lower()


def test_a_run_that_reached_end_time_gets_the_residuals_and_the_rule_up_front():
    # residuals below the limit but levelled off, monitors still drifting: the message must give
    # the levelled-off values and say to raise endTime, early enough to survive clipping
    monitors = {"coeffs": table(Cd=np.linspace(1.0, 2.0, 1000))}
    initial = {"Ux": 1.2e-5, "Uy": 1.4e-5, "p": 3e-6}
    report = steadiness(summary(initial=initial), monitors, T)
    assert not report.ok
    details = report.details
    assert details.startswith("the run was not ended by residualControl; ")
    assert "final initial residuals Ux 1.2e-05, Uy 1.4e-05, p 3e-06" in details
    assert "raise endTime" in details
    assert "Cd" in details  # the monitored change is still reported
    gate_error = f"steady state not reached: {details}"
    assert gate_error.index("raise endTime") + len("raise endTime") < 400
    # the rule comes before the residual list, so it does not depend on the number of fields
    assert details.index("raise endTime") < details.index("final initial residuals")
    assert details.count("1.2e-05") == 1  # the residuals are listed once


def test_the_rule_guards_against_loosening_and_names_the_threshold_in_force():
    monitors = {"coeffs": table(Cd=np.linspace(1.0, 2.0, 1000))}
    for limit, shown in ((1e-4, "0.0001"), (5e-3, "0.005")):
        thresholds = ConvergenceThresholds(final_residual_max=limit)
        report = steadiness(summary(initial={"Ux": 1e-6}), monitors, thresholds)
        assert f"loosen only up to {shown}" in report.details
        assert "only when the monitored quantities have stopped changing" in report.details
        assert "tightening it alone cannot make the run converge" in report.details
        assert "otherwise look for the cause" in report.details


def test_the_rule_reads_sensibly_without_any_residuals():
    report = steadiness(summary(initial={}), {}, T)
    assert report.details.startswith("the run was not ended by residualControl; ")
    assert "raise endTime" in report.details and "loosen only up to 0.0001" in report.details
    assert "final initial residuals" not in report.details
    assert "the log has no initial residuals to judge" in report.details


def test_high_residuals_of_a_run_that_reached_end_time_are_listed_once_with_the_limit():
    report = steadiness(summary(initial={"Ux": 3e-8, "p": 2e-3}), {}, T)
    assert "final initial residuals Ux 3e-08, p 0.002 (not below 0.0001: p)" in report.details
    assert "raise endTime" in report.details
    assert report.details.count("0.002") == 1


def test_the_residual_clause_respects_ignore_residuals_and_is_left_out_without_residuals():
    initial = {"Ux": 3e-8, "Uz": 5e-2}
    report = steadiness(summary(initial=initial), {}, T, ignore_residuals=("Uz",))
    assert "final initial residuals Ux 3e-08" in report.details
    assert "Uz" not in report.details
    empty = steadiness(summary(initial={}), {}, T)
    assert "final initial residuals" not in empty.details
    assert "raise endTime" in empty.details


def test_a_run_ended_by_residual_control_has_no_rule():
    report = steadiness(summary(converged_at=500.0, completed=False), {}, T)
    assert not report.ok
    assert "raise endTime" not in report.details
    assert "not ended by residualControl" not in report.details


def test_a_log_without_initial_residuals_cannot_show_steadiness():
    monitors = {"coeffs": table(Cd=converging())}
    assert not steadiness(summary(initial={}), monitors, T).ok


def test_no_criterion_and_no_monitor_is_not_steady():
    report = steadiness(summary(), {}, T)
    assert not report.ok and report.criterion == "none"
    assert "residualControl" in report.details and "monitor" in report.details


def test_a_non_finite_monitor_value_is_not_steady():
    y = converging()
    y[-3] = float("nan")
    assert not steadiness(summary(), {"m": table(Cd=y)}, T).ok


def test_a_quantity_that_tends_to_zero_is_judged_against_its_table():
    # Cl is round-off noise around zero beside Cd = 2: relative to its own size the change is
    # 100 %, relative to the table it is far below 1e-3.
    rng = np.random.default_rng(1)
    monitors = {"c": table(Cd=converging(final=2.0), Cl=1e-7 * rng.standard_normal(1000))}
    assert steadiness(summary(), monitors, T).ok


def test_a_small_quantity_that_is_still_moving_is_not_hidden_by_a_large_one():
    # Cl is 1 % of Cd (above the floor of a thousandth) and still drifts by half of itself
    monitors = {"c": table(Cd=np.full(1000, 2.0), Cl=np.linspace(0.01, 0.02, 1000))}
    assert not steadiness(summary(), monitors, T).ok


def test_a_record_too_short_for_the_window_cannot_show_steadiness():
    report = steadiness(summary(), {"m": table(Cd=[1.0, 1.0, 1.0])}, T)
    assert not report.ok and "samples" in report.details


def test_the_window_follows_the_threshold():
    y = np.concatenate([np.linspace(0.0, 1.0, 500), np.ones(500)])  # flat in the last half
    monitors = {"m": table(Cd=y)}
    assert steadiness(summary(), monitors, ConvergenceThresholds(steady_window=0.4)).ok
    assert not steadiness(summary(), monitors, ConvergenceThresholds(steady_window=0.6)).ok


# ------------------------------------------------------------------ periodicity


def test_clean_periodic_signal_is_periodic():
    t = np.linspace(0.0, 30.0, 6001)  # 30 periods, 200 samples per period
    report = periodicity(t, wave(t, frequency=1.0), T)
    assert report.ok, report.details
    assert report.frequency == pytest.approx(1.0, rel=1e-2)
    assert report.n_periods == pytest.approx(15.0, rel=0.05)  # the second half of the record
    assert report.mean_change < 0.02 and report.amplitude_change < 0.02


def test_a_periodic_signal_with_an_offset_keeps_its_mean_and_amplitude():
    t = np.linspace(0.0, 30.0, 6001)
    report = periodicity(t, wave(t, amplitude=0.3, mean=1.3), T)
    assert report.ok, report.details


def test_too_few_periods_is_not_periodic():
    t = np.linspace(0.0, 12.0, 2401)  # 12 periods: 6 remain after the transient half
    report = periodicity(t, wave(t), T)
    assert not report.ok
    assert report.n_periods == pytest.approx(6.0, rel=0.1)
    assert "periods" in report.details and "10" in report.details


def test_a_record_with_one_period_is_not_periodic_and_says_so():
    t = np.linspace(0.0, 1.5, 301)
    report = periodicity(t, wave(t), T)
    assert not report.ok and report.n_periods < 3
    assert report.details


def test_growing_amplitude_is_not_periodic():
    t = np.linspace(0.0, 40.0, 8001)
    report = periodicity(t, wave(t, growth=0.05), T)  # the amplitude triples over the record
    assert not report.ok
    assert report.amplitude_change > 0.02
    assert "amplitude" in report.details


def test_a_drifting_mean_is_not_periodic():
    t = np.linspace(0.0, 40.0, 8001)
    report = periodicity(t, wave(t, amplitude=0.5) + 0.05 * t, T)
    assert not report.ok
    assert report.mean_change > 0.02
    assert "mean" in report.details


def test_a_constant_signal_is_not_periodic():
    t = np.linspace(0.0, 30.0, 3001)
    report = periodicity(t, np.full_like(t, 1.3), T)
    assert not report.ok and report.frequency is None and report.n_periods == 0.0
    assert "constant" in report.details or "no oscillation" in report.details


def test_the_period_limit_follows_the_threshold():
    t = np.linspace(0.0, 12.0, 2401)
    assert periodicity(t, wave(t), ConvergenceThresholds(min_periods=5)).ok
    assert not periodicity(t, wave(t), ConvergenceThresholds(min_periods=7)).ok


def test_unusable_input_is_a_failed_report_not_an_exception():
    report = periodicity([0.0, 1.0, 2.0], [1.0, float("nan"), 2.0], T)
    assert not report.ok and "finite" in report.details


# ------------------------------------------------------------------ the case on disk

LOG_STEP = (
    "Time = {t}\n\n"
    "{solver}:  Solving for Ux, Initial residual = {r}, Final residual = {r}, No Iterations 1\n"
    "{solver}:  Solving for p, Initial residual = {r}, Final residual = {r}, No Iterations 1\n"
)


def write_log(case: Path, solver="simpleFoam", residual=1e-8, converged_at=None, end=1000):
    """A solver log whose last time is ``end`` (the monitors are judged against the run's span)."""
    text = "".join(
        LOG_STEP.format(t=t, r=f"{residual:g}", solver="smoothSolver") for t in (1, end // 2, end)
    )
    if converged_at is not None:
        text += f"\nSIMPLE solution converged in {converged_at:g} iterations\n"
    text += "\nEnd\n"
    case.mkdir(parents=True, exist_ok=True)
    (case / f"log.{solver}").write_text(text, encoding="utf-8")


def write_table(case: Path, name: str, file: str, columns: list[str], rows: np.ndarray, start="0"):
    folder = case / "postProcessing" / name / start
    folder.mkdir(parents=True, exist_ok=True)
    lines = ["# Time    " + "    ".join(columns)]
    lines += ["    ".join(f"{v:.10g}" for v in row) for row in rows]
    (folder / file).write_text("\n".join(lines) + "\n", encoding="utf-8")


def coefficient_rows(t, cd, cl):
    return np.column_stack([t, cd, cl])


def test_a_steady_case_that_stopped_by_residual_control_passes(tmp_path):
    write_log(tmp_path, converged_at=412)
    result = check_convergence(tmp_path, "simpleFoam", "steady", False)
    assert result.ok and result.errors == []
    assert result.data["steadiness"]["criterion"] == "residual_control"
    assert result.data["periodicity"] is None


def test_a_steady_case_without_a_criterion_or_a_monitor_fails(tmp_path):
    write_log(tmp_path)
    result = check_convergence(tmp_path, "simpleFoam", "steady", False)
    assert not result.ok
    assert len(result.errors) == 1 and "steady" in result.errors[0]
    assert result.data["steadiness"]["ok"] is False


def test_a_steady_case_is_judged_on_the_monitors_in_postprocessing(tmp_path):
    write_log(tmp_path, solver="icoFoam")
    t = np.arange(1.0, 1001.0)
    write_table(
        tmp_path, "forceCoeffs1", "coefficient.dat", ["Cd", "Cl"],
        coefficient_rows(t, converging(), converging(final=0.0, scale=0.2)),
    )  # fmt: skip
    result = check_convergence(tmp_path, "icoFoam", "steady", False)
    assert result.ok, result.errors
    assert result.data["steadiness"]["criterion"] == "monitored_change"
    drifting = coefficient_rows(t, np.linspace(1, 2, 1000), np.zeros(1000))
    write_table(tmp_path, "forceCoeffs1", "coefficient.dat", ["Cd", "Cl"], drifting)
    assert not check_convergence(tmp_path, "icoFoam", "steady", False).ok


def test_solver_info_and_residual_tables_are_not_monitored_quantities(tmp_path):
    write_log(tmp_path)
    t = np.arange(1.0, 101.0)
    noisy = np.column_stack([t, np.abs(np.sin(t))])  # residual histories are not steady by nature
    write_table(tmp_path, "solverInfo", "solverInfo.dat", ["Ux_initial"], noisy)
    write_table(tmp_path, "residuals", "residuals.dat", ["Ux"], noisy)
    assert read_monitors(tmp_path) == {}
    result = check_convergence(tmp_path, "simpleFoam", "steady", False)
    assert not result.ok and "monitor" in result.errors[0]


def test_sampled_sets_are_not_monitored_quantities(tmp_path):
    write_log(tmp_path)
    sets = tmp_path / "postProcessing" / "sets" / "1000"
    sets.mkdir(parents=True)
    (sets / "line_U.xy").write_text("0 1 0 0\n0.5 2 0 0\n1 1 0 0\n", encoding="utf-8")
    assert read_monitors(tmp_path) == {}


def test_probe_tables_and_force_tables_are_monitors(tmp_path):
    t = np.arange(1.0, 101.0)
    write_table(tmp_path, "probes", "p", ["0"], np.column_stack([t, converging(100)]))
    write_table(tmp_path, "forces", "force.dat", ["total_x"], np.column_stack([t, t * 0 + 1.0]))
    names = set(read_monitors(tmp_path))
    assert any("probes" in n for n in names) and any("forces" in n for n in names)


def test_an_unreadable_function_object_file_is_skipped(tmp_path):
    folder = tmp_path / "postProcessing" / "broken" / "0"
    folder.mkdir(parents=True)
    (folder / "x.dat").write_text("# Time a\n1 not-a-number\n", encoding="utf-8")
    assert read_monitors(tmp_path) == {}


def test_a_periodic_case_is_judged_on_its_force_coefficients(tmp_path):
    write_log(tmp_path, solver="pimpleFoam")
    t = np.linspace(0.0, 30.0, 6001)
    rows = coefficient_rows(t, 1.3 + 0.004 * np.sin(4 * math.pi * t), wave(t, amplitude=0.3))
    write_table(tmp_path, "forceCoeffs1", "coefficient.dat", ["Cd", "Cl"], rows)
    result = check_convergence(tmp_path, "pimpleFoam", "transient", True)
    assert result.ok, result.errors
    assert result.data["steadiness"] is None
    report = result.data["periodicity"]
    assert report["ok"] and report["column"] == "Cl"  # the strongest oscillation
    assert report["frequency"] == pytest.approx(1.0, rel=1e-2)


def test_a_periodic_case_with_too_few_periods_fails(tmp_path):
    write_log(tmp_path, solver="pimpleFoam")
    t = np.linspace(0.0, 12.0, 2401)
    write_table(
        tmp_path, "forceCoeffs1", "coefficient.dat", ["Cd", "Cl"],
        coefficient_rows(t, np.full_like(t, 1.3), wave(t, amplitude=0.3)),
    )  # fmt: skip
    result = check_convergence(tmp_path, "pimpleFoam", "transient", True)
    assert not result.ok and "periodic" in result.errors[0] and "periods" in result.errors[0]


def test_a_periodic_case_without_force_coefficients_says_what_to_add(tmp_path):
    write_log(tmp_path, solver="pimpleFoam")
    t = np.arange(1.0, 101.0)
    write_table(tmp_path, "probes", "p", ["0"], np.column_stack([t, np.sin(t)]))
    result = check_convergence(tmp_path, "pimpleFoam", "transient", True)
    assert not result.ok and "forceCoeffs" in result.errors[0]


def test_aperiodic_transient_is_not_checked_for_periodicity(tmp_path):
    # no postProcessing at all: nothing is read, nothing can fail
    write_log(tmp_path, solver="pimpleFoam")
    result = check_convergence(tmp_path, "pimpleFoam", "transient", False)
    assert result.ok and result.errors == []
    assert result.data["steadiness"] is None and result.data["periodicity"] is None


def test_a_steady_case_that_is_also_periodic_needs_both(tmp_path):
    write_log(tmp_path, solver="pimpleFoam", converged_at=900)
    result = check_convergence(tmp_path, "pimpleFoam", "steady", True)
    assert not result.ok  # steady by residualControl, but no force-coefficient history
    assert result.data["steadiness"]["ok"] is True


def test_a_missing_solver_log_is_a_failure_with_a_message(tmp_path):
    result = check_convergence(tmp_path, "simpleFoam", "steady", False)
    assert not result.ok and "log.simpleFoam" in result.errors[0]


def test_the_report_is_plain_json_data(tmp_path):
    import json

    write_log(tmp_path, converged_at=10)
    result = check_convergence(tmp_path, "simpleFoam", "steady", False)
    json.dumps(result.data, allow_nan=False)  # no numpy types, no NaN
    assert convergence.THRESHOLDS == T
    # a failed periodicity report has NaN changes inside the check; they leave as null
    t = np.arange(0.0, 40.0, 0.35)
    write_log(tmp_path, solver="pimpleFoam")
    rows = coefficient_rows(t, np.full_like(t, 1.3), wave(t, frequency=1.6))
    write_table(tmp_path, "forceCoeffs1", "coefficient.dat", ["Cd", "Cl"], rows)
    failed = check_convergence(tmp_path, "pimpleFoam", "transient", True)
    assert not failed.ok
    json.dumps(failed.data, allow_nan=False)
    assert failed.data["periodicity"]["mean_change"] is None


def test_a_monitor_that_stopped_writing_early_cannot_show_steadiness(tmp_path):
    # the run goes on to 1000 but the monitor ends at 700: nothing of it lies in the last tenth
    write_log(tmp_path, solver="icoFoam")
    t = np.arange(1.0, 701.0)
    write_table(tmp_path, "forces", "force.dat", ["total_x"], np.column_stack([t, 1.0 + 0 * t]))
    result = check_convergence(tmp_path, "icoFoam", "steady", False)
    assert not result.ok and "sample" in result.errors[0] and "700" in result.errors[0]


def test_the_window_is_a_fraction_of_the_run_in_the_log_not_of_the_table():
    table_early = table(Cd=np.concatenate([np.linspace(0.0, 1.0, 400), np.ones(100)]))
    log_longer = summary(end_time=1000.0, first_time=1.0)
    # the table covers 1..500 and is flat in its last 100 samples, but the last tenth of the run
    # (900..1000) holds none of them
    assert not steadiness(log_longer, {"m": table_early}, T).ok
    log_same = summary(end_time=500.0, first_time=1.0)
    assert steadiness(log_same, {"m": table_early}, T).ok


# ------------------------------------------------------------------ review round 2


def test_white_noise_hardly_ever_passes_as_a_periodic_regime():
    """The dominant "frequency" of noise written at every step sits near Nyquist: the sampling
    guard rejects it. A few trials may still pass by chance (about 1 %), so the bound is not 0."""
    rng = np.random.default_rng(3)
    t = np.linspace(0.0, 30.0, 6001)
    passed = sum(periodicity(t, rng.standard_normal(len(t)), T).ok for _ in range(100))
    assert passed <= 6


def test_a_history_written_too_rarely_is_not_periodic_even_if_a_peak_is_found():
    t = np.arange(0.0, 40.0, 0.35)  # a 1.6 Hz signal sampled every 0.35 s aliases to 1.26 Hz
    report = periodicity(t, wave(t, frequency=1.6), T)
    assert not report.ok and "too rarely" in report.details
    assert "every time step" in report.details


def test_twenty_samples_per_period_are_enough():
    t = np.linspace(0.0, 30.0, 601)  # 20 samples per period of a 1 Hz signal
    assert periodicity(t, wave(t), T).ok
    t = np.linspace(0.0, 30.0, 421)  # about 14 samples per period
    assert not periodicity(t, wave(t), T).ok


def test_a_decaying_oscillation_says_it_is_decaying_and_a_growing_one_says_growing():
    t = np.linspace(0.0, 40.0, 8001)
    decaying = periodicity(t, wave(t, growth=-0.02), T)
    assert not decaying.ok and "decaying" in decaying.details
    growing = periodicity(t, wave(t, growth=0.05), T)
    assert not growing.ok and "growing" in growing.details


def test_the_period_count_is_stated_against_the_second_half():
    t = np.linspace(0.0, 12.0, 2401)
    report = periodicity(t, wave(t), T)
    assert "second half" in report.details


def test_the_swirl_residual_of_a_wedge_case_is_not_judged():
    monitors = {"c": table(Cd=converging())}
    swirl = summary(initial={"Ux": 3e-8, "p": 8e-9, "Uz": 3e-2})
    assert not steadiness(swirl, monitors, T).ok  # judged by default
    report = steadiness(swirl, monitors, T, ignore_residuals=("Uz",))
    assert report.ok and "Uz" in report.details  # and it says what was left out
    # leaving Uz out does not hide another component
    other = summary(initial={"Ux": 3e-2, "Uz": 3e-2})
    assert not steadiness(other, monitors, T, ignore_residuals=("Uz",)).ok


def test_check_convergence_ignores_uz_only_for_an_axisymmetric_case(tmp_path):
    case = tmp_path
    solve = (
        "smoothSolver:  Solving for {f}, Initial residual = {r}, Final residual = 1e-08, "
        "No Iterations 1\n"
    )
    text = "".join(
        f"Time = {t}\n\n" + solve.format(f="Ux", r="1e-08") + solve.format(f="Uz", r="0.03")
        for t in (1, 500, 1000)
    )
    (case / "log.icoFoam").write_text(text + "\nEnd\n", encoding="utf-8")
    t = np.arange(1.0, 1001.0)
    write_table(case, "forces", "force.dat", ["total_x"], np.column_stack([t, converging()]))
    wedge = check_convergence(case, "icoFoam", "steady", False, dimensionality="axisymmetric")
    assert wedge.ok, wedge.errors
    flat = check_convergence(case, "icoFoam", "steady", False, dimensionality="2D")
    assert not flat.ok and "Uz" in flat.errors[0]
    assert wedge.data["dimensionality"] == "axisymmetric"


def test_probe_files_in_the_openfoam_format_are_monitors(tmp_path):
    """The header of a probes file of OpenFOAM v2512 (a point per comment line, then the times)."""
    folder = tmp_path / "postProcessing" / "probes" / "0"
    folder.mkdir(parents=True)
    head = "# Probe 0 (0.5 0.5 0.05)\n# Probe 1 (0.25 0.75 0.05)\n# Time          0       1\n"
    rows = "".join(f"{0.005 * i:g}  {1.0 + 0.001 * i:.8g}  {-0.25:.8g}\n" for i in range(1, 60))
    (folder / "p").write_text(head + rows, encoding="utf-8")
    vector = "".join(f"{0.005 * i:g}  (0.1 {0.01 * i:.8g} 0)  (0.2 0.3 0)\n" for i in range(1, 60))
    (folder / "U").write_text(head + vector, encoding="utf-8")
    monitors = read_monitors(tmp_path)
    assert set(monitors) == {"probes/p", "probes/U"}
    assert monitors["probes/p"].columns == ["Time", "0", "1"]
    assert monitors["probes/U"].columns[:4] == ["Time", "0_x", "0_y", "0_z"]
