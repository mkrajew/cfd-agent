"""The convergence checks of the Solver Runner gate (ADR-0006, decision 6; design 6.2, row 5).

A solver run that reached its end time says nothing about whether the flow it computed is the one
the task asks for. Two checks answer that, from the solver log and the function-object tables of
the finished case:

* ``steadiness``: for a steady case (also one marched in time with a transient solver). It holds
  when ``residualControl`` stopped the run, or when every monitored quantity changes by less than
  ``steady_rel_change`` over the last ``steady_window`` fraction of the run and the final initial
  residuals are below ``final_residual_max``.
* ``periodicity``: for a case whose outputs are statistics of a periodic regime (the Planner's
  ``periodic_outputs``). It holds when at least ``min_periods`` periods of the dominant oscillation
  of a force-coefficient history remain after the start-up transient and the mean and the amplitude
  of the last two halves of that record differ by less than ``periodic_rel_change``.

An aperiodic transient case gets neither check. ``check_convergence`` applies the checks to a case
directory and returns a ``GateResult``; its errors are written for the agent that has to act on
them (what was measured, what is needed).

How the numbers are defined
---------------------------
*Monitored quantities* are the columns of the function-object tables under ``postProcessing/``
that hold a time history: forces, force coefficients, probes, volume and surface averages, or any
other table with a ``# Time`` header (``case_requirements.MONITOR_TYPES`` lists the types the
completeness check accepts). Residual histories (``solverInfo``, ``residuals``) are not quantities
of the flow and are left out, and so is sampled output (``sets``, ``surfaces``) that has no
``# Time`` header.
The change of a column is its peak-to-peak range over the window divided by its own largest
magnitude in the window, but by not less than a thousandth of the largest magnitude in its table.
The floor keeps a quantity that tends to zero (the transverse force of a symmetric flow) from being
judged against round-off noise, while a quantity that is small yet still moving is still caught.

*The wedge.* The residual of the component normal to the wedge planes of an axisymmetric case
(``Uz``) is round-off noise that never falls, so ``check_convergence`` does not judge it.

*Sampling.* A periodic history must be written at least ``MIN_SAMPLES_PER_PERIOD`` times per
period of its dominant oscillation, or the peak may be an alias or noise.

*The developed part* of a periodic record is its second half; the first half is the transient. The
frequency is the dominant one of that half (``foam_results.dominant_frequency``). The mean and the
amplitude (half the peak-to-peak range) of the last ``2 h`` whole periods are compared between the
two halves of ``h`` periods each. The mean is compared to the larger of the means and amplitudes,
so a signal with a zero mean is judged by its amplitude.

This is a container-side module: it never imports host code (``agents``, ``orchestrator``,
``bench``).
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np

from cfdagent.gates import GateResult
from cfdagent.tools.foam import SolverLogSummary, parse_solver_log_file
from cfdagent.tools.foam_results import (
    FoamResultsError,
    FunctionObjectTable,
    dominant_frequency,
    read_function_object,
)

POST_DIR = "postProcessing"
FLOOR_FRACTION = 1e-3  # of the largest magnitude of a table: the least a column is judged against
MIN_WINDOW_ROWS = 2
MIN_SAMPLES = 16  # fewer samples cannot show a period
# A history written less often than this many times per period of its dominant oscillation cannot
# show a periodic regime: the peak is aliased or is noise (sampling error of the peak-to-peak
# range alone is 1 - cos(pi / N), 1.2 % at N = 20, a large part of the 2 % limit).
MIN_SAMPLES_PER_PERIOD = 20
# The residual of the component normal to the wedge planes of an axisymmetric case is round-off
# noise normalised by itself: it stays near 1e-2 however long the run is, so it is not judged.
WEDGE_SWIRL_RESIDUALS = ("Uz",)
# What to change when a steady run reached endTime without meeting residualControl. A run that
# residualControl ends skips the residual limit and the monitored-change check, so loosening a
# tolerance can pass an unconverged solution: the rule guards against it. ``{limit}`` is the
# final-residual threshold in force.
NOT_ENDED_RULE = (
    "if the residuals were still falling at endTime, raise endTime; if they levelled off, a "
    "tolerance below that level is not reliably met and tightening it alone cannot make the run "
    "converge, while loosening it only ends the run without converging it further: loosen only "
    "up to {limit:g} and only when the monitored quantities have stopped changing, otherwise "
    "look for the cause (an unsteady flow, the convection scheme, the relaxation, the mesh)"
)
# Function-object output that is a residual history, not a quantity of the flow.
NOT_MONITORED_PREFIXES = ("solverinfo", "residual")
# The columns of a ``forceCoeffs`` file that are coefficients of the whole body.
FORCE_COEFFICIENTS = ("Cd", "Cl", "Cs", "Cm", "CmRoll", "CmPitch", "CmYaw")
_GENERATED_COLUMN = re.compile(r"col\d+")


@dataclass(frozen=True)
class ConvergenceThresholds:
    """The limits of the checks. ``final_residual_max``: largest final initial residual of a
    steady run that stopped without ``residualControl``. ``steady_window``: the last fraction of
    the run that must be steady. ``steady_rel_change``: largest relative change of a monitored
    quantity in that window. ``min_periods``: periods of the developed regime a periodic record
    must hold. ``periodic_rel_change``: largest relative change of the mean and of the amplitude
    between the two halves of the developed regime. Confirmed by ``cfd-reviewer`` on the
    calibrations of Task 12, which are all laminar (ADR-0006, Amendments)."""

    final_residual_max: float = 1e-4
    steady_window: float = 0.1
    steady_rel_change: float = 1e-3
    min_periods: int = 10
    periodic_rel_change: float = 0.02


THRESHOLDS = ConvergenceThresholds()


@dataclass(frozen=True)
class SteadinessReport:
    """``criterion``: what showed the run to be steady (``residual_control`` or
    ``monitored_change``), ``none`` when it did not."""

    ok: bool
    criterion: Literal["residual_control", "monitored_change", "none"]
    details: str


@dataclass(frozen=True)
class PeriodicityReport:
    """``n_periods``: periods of the dominant oscillation in the developed (second) half of the
    record; ``frequency``: that oscillation's frequency (``None`` when none was found);
    ``mean_change`` and ``amplitude_change``: the relative change between the two halves of the
    last whole periods (``nan`` when they could not be measured)."""

    ok: bool
    n_periods: float
    frequency: float | None
    mean_change: float
    amplitude_change: float
    details: str


# ------------------------------------------------------------------ monitored quantities


def _finite(value: float | None) -> bool:
    return value is not None and math.isfinite(value)


def _function_object_files(directory: Path) -> list[str]:
    """Names of the files in the start-time directories of one function object."""
    names: set[str] = set()
    for start in directory.iterdir():
        if start.is_symlink() or not start.is_dir():
            continue
        try:
            float(start.name)
        except ValueError:
            continue
        names.update(
            entry.name
            for entry in start.iterdir()
            if entry.is_file() and not entry.is_symlink() and not entry.name.startswith(".")
        )
    return sorted(names)


def read_monitors(case_dir: Path | str) -> dict[str, FunctionObjectTable]:
    """The time-history tables of the function objects of a case, by ``<function object>/<file>``.

    Residual histories, files without a ``# Time`` header (sampled sets and surfaces), files that
    cannot be read and tables with fewer than two rows are not monitors and are left out.
    """
    case_dir = Path(case_dir)
    root = case_dir / POST_DIR
    if root.is_symlink() or not root.is_dir():
        return {}
    monitors: dict[str, FunctionObjectTable] = {}
    for directory in sorted(root.iterdir(), key=lambda entry: entry.name):
        if directory.is_symlink() or not directory.is_dir():
            continue
        try:
            files = _function_object_files(directory)
        except OSError:
            continue
        for file in files:
            stem = Path(file).stem
            if stem.lower().startswith(NOT_MONITORED_PREFIXES):
                continue
            try:
                table = read_function_object(case_dir, directory.name, file)
            except (FoamResultsError, OSError):
                continue
            generated = all(_GENERATED_COLUMN.fullmatch(c) for c in table.columns[1:])
            if len(table.columns) < 2 or len(table.data) < 2 or generated:
                continue
            monitors[f"{directory.name}/{stem}"] = table
    return monitors


# ------------------------------------------------------------------ steadiness


def _changes(
    monitors: dict[str, FunctionObjectTable],
    thresholds: ConvergenceThresholds,
    run_span: tuple[float, float] | None = None,
) -> tuple[list[tuple[str, float]], list[str]]:
    """``((label, relative change), ...)`` of every monitored column over the window, and what made
    a table impossible to judge. The window is the last ``steady_window`` fraction of the run,
    ``run_span`` = ``(first time, last time)`` of the solver log (the span of the table when the
    log gives none), so a monitor that stopped writing early has no samples in it."""
    changes: list[tuple[str, float]] = []
    problems: list[str] = []
    for name, table in monitors.items():
        time = table.data[:, 0]
        first, last = (float(time[0]), float(time[-1])) if run_span is None else run_span
        span = last - first
        inside = time >= last - thresholds.steady_window * span
        if span <= 0 or int(inside.sum()) < MIN_WINDOW_ROWS:
            problems.append(
                f"{name} has {int(inside.sum())} sample(s) in the last "
                f"{thresholds.steady_window:g} of the run (it ends at {time[-1]:g}, the run at "
                f"{last:g}): too few samples to judge it; write it at least every few steps "
                "(writeInterval) and until the end"
            )
            continue
        values = table.data[:, 1:]
        finite = np.isfinite(values)
        table_scale = float(np.abs(values[finite]).max()) if finite.any() else 0.0
        for index, column in enumerate(table.columns[1:]):
            window = values[inside, index]
            if not np.isfinite(window).all():
                changes.append((f"{name}: {column}", math.inf))
                continue
            scale = max(float(np.abs(window).max()), FLOOR_FRACTION * table_scale)
            change = float(np.ptp(window)) / scale if scale > 0 else 0.0
            changes.append((f"{name}: {column}", change))
    return changes, problems


def steadiness(
    summary: SolverLogSummary,
    monitors: dict[str, FunctionObjectTable],
    thresholds: ConvergenceThresholds = THRESHOLDS,
    ignore_residuals: tuple[str, ...] = (),
) -> SteadinessReport:
    """Whether a run reached a steady state; see the module documentation.

    ``summary`` is the parsed solver log, ``monitors`` the time histories of the case (see
    ``read_monitors``). A run that did not complete is never steady. The final initial residuals
    named in ``ignore_residuals`` are not judged (the swirl component of a wedge case).
    """
    if summary.completed and _finite(summary.converged_at):
        return SteadinessReport(
            True,
            "residual_control",
            f"the solver stopped itself at iteration {summary.converged_at:g}: the "
            "residualControl criteria of the case were met",
        )
    problems: list[str] = []
    residuals = {k: v for k, v in summary.initial_residuals.items() if k not in ignore_residuals}
    high = sorted((k, v) for k, v in residuals.items() if not v <= thresholds.final_residual_max)
    if not residuals:
        problems.append("the log has no initial residuals to judge")
    elif high:
        shown = ", ".join(f"{k} {v:.2g}" for k, v in high)
        problems.append(
            f"the final initial residuals are not below {thresholds.final_residual_max:g}: {shown}"
        )
    run_span = None
    if _finite(summary.end_time) and _finite(summary.first_time):
        run_span = (float(summary.first_time), float(summary.end_time))
    elif _finite(summary.end_time) and monitors:
        run_span = (min(float(t.data[0, 0]) for t in monitors.values()), float(summary.end_time))
    changes, unjudged = _changes(monitors, thresholds, run_span)
    problems += unjudged
    if not monitors:
        problems.append(
            "no monitored quantity: the case writes no force, probe or other time history to "
            "judge steadiness by (add a function object that monitors a requested quantity; a "
            "steady SIMPLE run may instead be stopped by residualControl, which must then be "
            "met before endTime: keys are field names, and a `U` entry is never met in an "
            "axisymmetric wedge case)"
        )
    moving = [(label, c) for label, c in changes if not c < thresholds.steady_rel_change]
    if moving:
        shown = ", ".join(f"{label} {c:.2g}" for label, c in moving[:6])
        more = f" and {len(moving) - 6} more" if len(moving) > 6 else ""
        problems.append(
            f"still changing by more than {thresholds.steady_rel_change:g} (relative, peak to "
            f"peak) over the last {thresholds.steady_window:g} of the run: {shown}{more}"
        )
    if problems:
        if summary.converged_at is None:
            opening = [
                "the run was not ended by residualControl",
                NOT_ENDED_RULE.format(limit=thresholds.final_residual_max),
            ]
            if residuals:
                final = ", ".join(f"{k} {v:.2g}" for k, v in sorted(residuals.items()))
                limit = ""
                if high:
                    # this clause already names the residuals that are too high: say it once
                    # (the "not below" clause is always the first problem: it is added first)
                    problems.pop(0)
                    limit = f" (not below {thresholds.final_residual_max:g}: "
                    limit += ", ".join(k for k, _ in high) + ")"
                opening.append(f"final initial residuals {final}{limit}")
            problems[0:0] = opening
        return SteadinessReport(False, "none", "; ".join(problems))
    worst = max((c for _, c in changes), default=0.0)
    skipped = (
        f" (not judged: {', '.join(ignore_residuals)}, the swirl component of the wedge)"
        if ignore_residuals
        else ""
    )
    return SteadinessReport(
        True,
        "monitored_change",
        f"{len(changes)} monitored quantities changed by at most {worst:.2g} over the last "
        f"{thresholds.steady_window:g} of the run and the final initial residuals are below "
        f"{thresholds.final_residual_max:g}{skipped}",
    )


# ------------------------------------------------------------------ periodicity


def _failed(message: str, n_periods: float = 0.0, frequency: float | None = None, **more: float):
    return PeriodicityReport(
        False,
        n_periods,
        frequency,
        more.get("mean_change", math.nan),
        more.get("amplitude_change", math.nan),
        message,
    )


def _window_stats(times: np.ndarray, values: np.ndarray, start: float, end: float):
    """``(mean, amplitude)`` of the signal over ``[start, end]``: the trapezoid mean on the actual
    steps (the ends interpolated) and half the peak-to-peak range."""
    inside = (times > start) & (times < end)
    xs = np.concatenate([[start], times[inside], [end]])
    ys = np.interp(xs, times, values)
    mean = float(np.sum(0.5 * (ys[1:] + ys[:-1]) * np.diff(xs)) / (end - start))
    return mean, float(ys.max() - ys.min()) / 2.0


def periodicity(
    t: Any, y: Any, thresholds: ConvergenceThresholds = THRESHOLDS
) -> PeriodicityReport:
    """Whether ``y(t)`` is a developed periodic signal; see the module documentation.

    Never raises for a bad input: a signal that cannot be judged is a failed report that says why.
    """
    try:
        times, values = np.asarray(t, dtype=float), np.asarray(y, dtype=float)
    except (TypeError, ValueError):
        return _failed("t and y must be arrays of numbers")
    if times.ndim != 1 or values.ndim != 1 or len(times) != len(values):
        return _failed("t and y must be one-dimensional and of the same length")
    if not (np.isfinite(times).all() and np.isfinite(values).all()):
        return _failed("the history holds values that are not finite (NaN or infinity)")
    if len(times) < MIN_SAMPLES or not (np.diff(times) > 0).all():
        return _failed(
            f"the history has {len(times)} samples or times that do not increase: it is too "
            "short to show a period"
        )
    developed_from = float(times[0] + 0.5 * (times[-1] - times[0]))
    span = float(times[-1] - developed_from)
    try:
        frequency = dominant_frequency(times, values, skip_fraction=0.5)
    except FoamResultsError as exc:
        try:  # an estimate from the whole record only tells the agent how far short it is
            whole = dominant_frequency(times, values, skip_fraction=0.0)
        except FoamResultsError:
            return _failed(f"no oscillation was found in the developed half of the record: {exc}")
        return _failed(
            f"only {whole * span:.1f} periods remain after the start-up half of the record "
            f"(at least {thresholds.min_periods} are needed): {exc}",
            n_periods=whole * span,
            frequency=whole,
        )
    n_periods = frequency * span
    developed = times >= developed_from
    step = float(np.median(np.diff(times[developed]))) if developed.sum() > 1 else 0.0
    per_period = 1.0 / (frequency * step) if step > 0 else 0.0
    if round(per_period, 1) < MIN_SAMPLES_PER_PERIOD:  # the estimated frequency is not exact
        return _failed(
            f"the history is written too rarely to show a period: {per_period:.1f} samples per "
            f"period of the dominant oscillation (frequency {frequency:.4g}), at least "
            f"{MIN_SAMPLES_PER_PERIOD} are needed; write the force coefficients at every time "
            "step. If it is written at every step, the dominant 'oscillation' is noise: the "
            "flow shows no periodic regime",
            n_periods=n_periods,
            frequency=frequency,
        )
    whole_periods = int(math.floor(n_periods + 1e-9))
    half = whole_periods // 2
    mean_change = amplitude_change = math.nan
    trend = ""
    if half >= 1:
        period, end = 1.0 / frequency, float(times[-1])
        mid = end - half * period
        mean_a, amp_a = _window_stats(times, values, mid - half * period, mid)
        mean_b, amp_b = _window_stats(times, values, mid, end)
        scale = max(abs(mean_a), abs(mean_b), amp_a, amp_b)
        mean_change = abs(mean_b - mean_a) / scale if scale > 0 else 0.0
        top = max(amp_a, amp_b)
        amplitude_change = abs(amp_b - amp_a) / top if top > 0 else 0.0
        trend = (
            " (growing: the start-up is not finished)"
            if amp_b > amp_a
            else " (decaying: the oscillation is damped out, or the flow settles to a steady state)"
        )
    problems = []
    if n_periods < thresholds.min_periods:
        problems.append(
            f"only {n_periods:.2f} periods of the oscillation (frequency {frequency:.4g}) remain "
            f"in the second half of the record, which is taken as developed; at least "
            f"{thresholds.min_periods} are needed there: run longer"
        )
    if not mean_change < thresholds.periodic_rel_change:
        problems.append(
            f"the mean still changes between the last two halves of the record: "
            f"{mean_change:.2g} (limit {thresholds.periodic_rel_change:g})"
        )
    if not amplitude_change < thresholds.periodic_rel_change:
        problems.append(
            f"the amplitude still changes between the last two halves of the record: "
            f"{amplitude_change:.2g} (limit {thresholds.periodic_rel_change:g}){trend}"
        )
    if problems:
        return PeriodicityReport(
            False, n_periods, frequency, mean_change, amplitude_change, "; ".join(problems)
        )
    return PeriodicityReport(
        True,
        n_periods,
        frequency,
        mean_change,
        amplitude_change,
        f"{n_periods:.1f} periods at frequency {frequency:.4g}; the mean changes by "
        f"{mean_change:.2g} and the amplitude by {amplitude_change:.2g} between the last two "
        "halves of the record",
    )


# ------------------------------------------------------------------ the case on disk


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _force_history(monitors: dict[str, FunctionObjectTable]):
    """``(label, time, values)`` of the force-coefficient column with the strongest oscillation in
    the second half of its record, or ``None`` when the case writes no force coefficients."""
    best = None
    for name, table in monitors.items():
        if not {"Cd", "Cl"} & set(table.columns):
            continue
        time = table.data[:, 0]
        late = time >= time[0] + 0.5 * (time[-1] - time[0])
        for column in table.columns[1:]:
            if column not in FORCE_COEFFICIENTS:
                continue
            values = table.column(column)
            window = values[late]
            swing = float(np.ptp(window)) if np.isfinite(window).all() and len(window) else 0.0
            if best is None or swing > best[0]:
                best = (swing, f"{name}: {column}", column, time, values)
    return None if best is None else best[1:]


def check_convergence(
    case_dir: Path | str,
    solver: str,
    time_dependence: Literal["steady", "transient"],
    periodic_outputs: bool,
    thresholds: ConvergenceThresholds = THRESHOLDS,
    dimensionality: Literal["2D", "axisymmetric", "3D"] = "2D",
) -> GateResult:
    """The convergence gate of a solved case (see the module documentation).

    ``solver`` names the log file (``log.<solver>``); for an ``axisymmetric`` case the swirl
    residual (``WEDGE_SWIRL_RESIDUALS``) is not judged. A steady case gets ``steadiness``, a case
    with ``periodic_outputs`` gets ``periodicity`` on its force-coefficient history; any other case
    passes without a look at the files. ``data`` holds both reports (``None`` for a check that was
    not made) and the thresholds used; ``periodicity`` also names the ``column`` that was judged.
    """
    case_dir = Path(case_dir)
    steady = time_dependence == "steady"
    data: dict[str, Any] = {
        "time_dependence": time_dependence,
        "periodic_outputs": bool(periodic_outputs),
        "dimensionality": dimensionality,
        "thresholds": asdict(thresholds),
        "steadiness": None,
        "periodicity": None,
    }
    if not steady and not periodic_outputs:
        return GateResult(True, [], data)
    log = case_dir / f"log.{solver}"
    try:
        summary = parse_solver_log_file(log)
    except OSError:
        return GateResult(
            False,
            [f"the convergence check cannot read {log.name} in the case: no solver log"],
            data,
        )
    monitors = read_monitors(case_dir)
    errors: list[str] = []
    if steady:
        ignored = WEDGE_SWIRL_RESIDUALS if dimensionality == "axisymmetric" else ()
        report = steadiness(summary, monitors, thresholds, ignored)
        data["steadiness"] = _plain(asdict(report))
        if not report.ok:
            errors.append(f"steady state not reached: {report.details}")
    if periodic_outputs:
        history = _force_history(monitors)
        if history is None:
            errors.append(
                "periodic regime not established: the case writes no force-coefficient history "
                "(Cd, Cl, ...) to judge it by; add a forceCoeffs function object"
            )
        else:
            label, column, time, values = history
            periodic = periodicity(time, values, thresholds)
            data["periodicity"] = {**_plain(asdict(periodic)), "column": column, "source": label}
            if not periodic.ok:
                errors.append(f"periodic regime not established ({label}): {periodic.details}")
    return GateResult(not errors, errors, data)
