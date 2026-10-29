"""Run an OpenFOAM solver and decide, from its log, whether the run really succeeded.

``run_solver`` launches the solver (serially or under MPI) through the allowlisted process runner
and returns a ``SolverRun``. ``parse_solver_log`` and ``parse_solver_log_file`` turn a solver log
into a ``SolverLogSummary``. A run counts as successful only when the step exited cleanly, the log
ends with the solver's final ``End`` line, shows no fatal error and no divergence, AND the last
time it reports reaches the ``endTime`` of the case's ``controlDict`` (within one time step and the
printed precision, see ``SolverRun.ok``; a steady solver that announces convergence may stop
earlier); an exit code of 0 alone is never trusted. ``trial_run`` does a few time steps in a
scratch copy of a case, to find setup errors cheaply.

Security note: ``foamDictionary`` expands ``#calc`` and ``#codeStream`` in the dictionaries an agent
wrote, so reading ``controlDict`` can execute agent-written code. The confinement is the executor
sandbox (container without network or credentials), not this module.
"""

from __future__ import annotations

import io
import math
import os
import re
import shutil
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from cfdagent.errors import AgentInputError, ToolFailure
from cfdagent.tools.process import (
    ToolNotAllowed,
    ToolResult,
    fail_after_run,
    run_tool,
)

MAX_RANKS = 4
_UTILITY_TIMEOUT_S = 600.0
_DICTIONARY_TIMEOUT_S = 60.0
_DICTIONARY_LOG_NAME = "log.foamDictionary"
_CONTROL_DICT = "system/controlDict"
_DEFAULT_TIME_PRECISION = 6  # OpenFOAM's timePrecision when controlDict does not set it
_DICTIONARY_PRECISION = "15"  # digits foamDictionary prints and writes numbers with

# A residual or Courant number above this value is a blow-up, not a solution: the run counts as
# diverged even when the log still ends with ``End`` and shows no NaN. It is a safety net: healthy
# residuals are normalised (at most about 1) and healthy Courant numbers reach tens to hundreds at
# most, and on real blow-ups the Final-residual check usually fires first.
DIVERGENCE_LIMIT = 1e6

# Executables a solver run may launch. Every other allowlisted name is in NON_SOLVERS; a test
# asserts that together they are exactly ``ALLOWED_EXECUTABLES``.
SOLVERS: frozenset[str] = frozenset({"icoFoam", "simpleFoam", "pimpleFoam"})
NON_SOLVERS: frozenset[str] = frozenset(
    {
        "python",
        "gmshToFoam",
        "checkMesh",
        "foamDictionary",
        "changeDictionary",
        "decomposePar",
        "reconstructPar",
        "postProcess",
        "foamListTimes",
        "mpirun",
    }
)

_FATAL_RE = re.compile(r"^\s*(?:\[\d+\]\s*)?-->\s*FOAM FATAL (?:IO )?ERROR\b(.*)$")
_VERSION_TAG_RE = re.compile(r"\(\s*openfoam-[^)]*\)", re.IGNORECASE)
_RANK_PREFIX_RE = re.compile(r"^\s*\[\d+\]\s?")
_FROM_RE = re.compile(r"^\s*From\b")
_END_RE = re.compile(r"^End\s*$")
_TIME_RE = re.compile(r"^Time = ([-+0-9.eE]+)s?\s*$")
# Anchored on the whole line: only the steady-state solvers' own convergence announcement counts.
_CONVERGED_RE = re.compile(r"^SIMPLE solution converged in (\d+) iterations\s*$")
_RESIDUAL_RE = re.compile(
    r"Solving for (\S+?), Initial residual = (\S+?), Final residual = (\S+?), No Iterations"
)
_COURANT_RE = re.compile(r"Courant Number mean: (\S+) max: (\S+)")
# nan / -nan / inf / -inf as whole tokens (not part of a field name).
_NON_FINITE_RE = re.compile(r"(?<![A-Za-z0-9_])[-+]?(?:nan|inf(?:inity)?)(?![A-Za-z0-9_])", re.I)
# The header of every healthy log says "Floating point exception trapping enabled (FOAM_SIGFPE)",
# so the exception itself is recognised by OpenFOAM's handler name, or by the shell/OS wording.
_SIGFPE_RE = re.compile(r"\bsigFpe::sigHandler\b|Floating point exception(?! trapping)")


@dataclass(frozen=True)
class SolverLogSummary:
    """What a solver log says about the run.

    ``completed``: the final ``End`` line is present and there is no fatal error and no divergence.
    ``diverged``: a NaN/Inf residual, continuity error or Courant number, a residual or Courant
    number above ``DIVERGENCE_LIMIT``, or a floating point exception. ``fatal_error``: the message
    after ``--> FOAM FATAL (IO) ERROR`` (first one).
    ``end_time``: the last ``Time = ...`` value. ``final_residuals``: per field component the
    ``Final residual`` of the last solve of that field in the log, so when a field is solved
    several times per step (PISO/PIMPLE correctors) the last solve of the last step wins; a
    diverged run may contain ``nan`` here. ``max_courant``: the maximum of the finite ``Courant
    Number ... max`` values, or None if the log has none. ``converged_at``: the iteration given by
    the steady solvers' ``SIMPLE solution converged in N iterations`` line, None if absent.
    ``first_time`` / ``last_step``: the first ``Time = ...`` value and the difference between the
    last two (None when the log has fewer than one / two of them).
    """

    completed: bool
    diverged: bool
    fatal_error: str | None
    end_time: float | None
    final_residuals: dict[str, float]
    max_courant: float | None
    converged_at: float | None = None
    first_time: float | None = None
    last_step: float | None = None

    @property
    def failure_reason(self) -> str | None:
        """A one-line reason why the run is not completed, or None when it completed."""
        if self.completed:
            return None
        if self.fatal_error is not None:
            return f"FOAM fatal error: {self.fatal_error}"
        if self.diverged:
            return (
                "solution diverged (NaN/Inf, a residual or Courant number above "
                f"{DIVERGENCE_LIMIT:g}, or a floating point exception)"
            )
        return "the log has no final 'End' line: the run did not finish"


@dataclass(frozen=True)
class SolverRun:
    """The outcome of ``run_solver`` or ``trial_run``.

    ``tool``: the first failing step, otherwise the last step (``reconstructPar`` for a parallel
    run). ``solver_log``: the solver's own log (``log.<solver>``; it does not exist when the
    solver never started). ``summary``: what that log says (that of an empty log when the solver
    never started). ``case_end_time``: the ``endTime`` of the case's ``controlDict``, None when it
    could not be read. ``delta_t`` and ``time_precision``: ``deltaT`` (None when unreadable) and
    ``timePrecision`` (default 6) of the same file; they set the tolerance of the end-time check.
    """

    tool: ToolResult
    solver_log: Path
    summary: SolverLogSummary
    case_end_time: float | None
    delta_t: float | None = None
    time_precision: int = _DEFAULT_TIME_PRECISION

    @property
    def ok(self) -> bool:
        """True if the solver ran cleanly to the end of the requested time.

        The step succeeded, the log is complete and clean, and the last ``Time`` reaches
        ``endTime`` (see ``reach_problem``). It does NOT mean the solution converged: the
        steady-state residual check belongs to the Solver Runner gate.
        """
        return (
            self.tool.ok
            and self.summary.completed
            and reach_problem(self.summary, self.case_end_time, self.delta_t, self.time_precision)
            is None
        )


ZERO_STEP_MESSAGE = (
    "the solver advanced no time step; the start time is at or after endTime. "
    "startFrom latestTime picks up old time directories. Delete them or set startFrom startTime."
)


def _print_tolerance(a: float, b: float, time_precision: int) -> float:
    """Largest error from the log printing times with ``time_precision`` significant digits."""
    return max(1e-9, 10.0 ** -(time_precision - 1)) * max(abs(a), abs(b))


def reach_problem(
    summary: SolverLogSummary,
    case_end_time: float | None,
    delta_t: float | None,
    time_precision: int = _DEFAULT_TIME_PRECISION,
) -> str | None:
    """Why the last time in the log does not reach ``endTime``; None if it does.

    Reached means ``|end_time - endTime| <= step + print_tol``. ``step`` is the larger of
    ``deltaT`` and the last step in the log (a time-stepping run ends within one step of the
    target, also with an adjustable step), ``print_tol`` is the error of printing times with
    ``time_precision`` digits. A steady solver that announced convergence (``converged_at`` equal
    to the last ``Time``) may stop earlier than ``endTime``.
    """
    if case_end_time is None:
        return f"cannot read 'endTime' from {_CONTROL_DICT}"
    end = summary.end_time
    if end is None:
        return ZERO_STEP_MESSAGE
    tol = _print_tolerance(end, case_end_time, time_precision)
    step = max(delta_t if delta_t is not None and math.isfinite(delta_t) else 0.0, 0.0)
    step = max(step, summary.last_step or 0.0)
    if (
        summary.converged_at is not None
        and summary.completed
        and abs(summary.converged_at - end) <= tol
        and end <= case_end_time + step + tol
    ):
        return None
    if abs(end - case_end_time) <= step + tol:
        return None
    return (
        f"the solver stopped at Time = {end:.12g} but {_CONTROL_DICT} has endTime "
        f"{case_end_time:.12g}: the run did not reach the requested end time. "
        "stopAt must be endTime; endTime should be a multiple of deltaT"
    )


def _parse_float(token: str) -> float | None:
    try:
        return float(token)
    except ValueError:
        return None


def _blown_up(value: float | None) -> bool:
    """True for a value that is unreadable, not finite or above ``DIVERGENCE_LIMIT``."""
    return value is None or not math.isfinite(value) or value > DIVERGENCE_LIMIT


class _LogScanner:
    """Reads a solver log one line at a time, so a huge log never has to fit in memory."""

    def __init__(self) -> None:
        self.diverged = False
        self.has_end = False
        self.end_time: float | None = None
        self.first_time: float | None = None
        self.last_step: float | None = None
        self.converged_at: float | None = None
        self.final_residuals: dict[str, float] = {}
        self.max_courant: float | None = None
        self._fatal: list[str] | None = None  # message lines of the first fatal error
        self._collecting = False

    def feed(self, line: str) -> None:
        line = line.rstrip("\r\n")
        self._feed_fatal(line)
        if _SIGFPE_RE.search(line):
            self.diverged = True
        if _END_RE.match(line):
            self.has_end = True
        m = _TIME_RE.match(line)
        if m:
            t = _parse_float(m.group(1))
            if t is not None:
                if self.end_time is None:
                    self.first_time = t
                else:
                    self.last_step = t - self.end_time
                self.end_time = t
            return
        m = _CONVERGED_RE.match(line)
        if m:
            self.converged_at = float(m.group(1))
            return
        m = _RESIDUAL_RE.search(line)
        if m:
            initial, final = _parse_float(m.group(2)), _parse_float(m.group(3))
            if _blown_up(initial) or _blown_up(final):
                self.diverged = True
            if final is not None:
                self.final_residuals[m.group(1)] = final  # later solves overwrite earlier ones
            return
        m = _COURANT_RE.search(line)
        if m:
            mean, cmax = _parse_float(m.group(1)), _parse_float(m.group(2))
            if _blown_up(mean) or _blown_up(cmax):
                self.diverged = True
            if cmax is not None and math.isfinite(cmax):
                self.max_courant = cmax if self.max_courant is None else max(self.max_courant, cmax)
            return
        if "continuity errors" in line and _NON_FINITE_RE.search(line):
            self.diverged = True

    def _feed_fatal(self, line: str) -> None:
        """Collect the message of the first ``FOAM FATAL (IO) ERROR``.

        The message is the rest of the marker line plus the following lines up to the first blank
        line or ``From ...`` location line.
        """
        if self._collecting:
            text = _RANK_PREFIX_RE.sub("", line).rstrip()
            if not text.strip() or _FROM_RE.match(text):
                self._collecting = False
            else:
                assert self._fatal is not None
                self._fatal.append(text.strip())
            return
        if self._fatal is not None:  # the first fatal error is complete; later ones are ignored
            return
        m = _FATAL_RE.match(line)
        if m is None:
            return
        self._collecting = True
        first = _VERSION_TAG_RE.sub("", m.group(1)).lstrip(" :").strip()
        self._fatal = [first] if first else []

    def summary(self) -> SolverLogSummary:
        fatal_error: str | None = None
        if self._fatal is not None:
            fatal_error = "\n".join(self._fatal) if self._fatal else "FOAM FATAL ERROR (no message)"
        completed = self.has_end and fatal_error is None and not self.diverged
        return SolverLogSummary(
            completed=completed,
            diverged=self.diverged,
            fatal_error=fatal_error,
            end_time=self.end_time,
            final_residuals=dict(self.final_residuals),
            max_courant=self.max_courant,
            converged_at=self.converged_at,
            first_time=self.first_time,
            last_step=self.last_step,
        )


def _scan(lines: Iterable[str]) -> SolverLogSummary:
    scanner = _LogScanner()
    for line in lines:
        scanner.feed(line)
    return scanner.summary()


def parse_solver_log(log_text: str) -> SolverLogSummary:
    """Summarise a solver log. See ``SolverLogSummary`` for the meaning of each field."""
    # The same line splitting as reading a file (CRLF and a lone CR end a line), so that
    # ``parse_solver_log_file`` gives the identical result.
    return _scan(io.StringIO(log_text, newline=None))


def parse_solver_log_file(path: Path) -> SolverLogSummary:
    """Summarise a solver log file, reading it line by line (the log may be very large).

    Invalid UTF-8 is replaced, never an error; a missing file raises ``FileNotFoundError``.
    """
    with open(path, encoding="utf-8", errors="replace", newline=None) as log:
        return _scan(log)


def _check_n_procs(n_procs: int) -> None:
    if isinstance(n_procs, bool) or not isinstance(n_procs, int) or not 1 <= n_procs <= MAX_RANKS:
        raise ValueError(f"n_procs must be an integer from 1 to {MAX_RANKS}, got {n_procs!r}")


def _check_solver(solver: str) -> None:
    if solver not in SOLVERS:
        raise ToolNotAllowed(
            f"solver {solver!r} is not an allowed solver; allowed: {sorted(SOLVERS)}"
        )


def _prepare_decomposition(case_dir: Path, n_procs: int) -> None:
    """Make sure ``system/decomposeParDict`` exists and asks for ``n_procs`` subdomains.

    A missing dictionary is created with ``numberOfSubdomains n_procs`` and method ``scotch``
    (needs no further coefficients). An existing dictionary is never rewritten; if it asks for a
    different number of subdomains a ValueError is raised, because the run would use the wrong
    number of ranks.
    """
    path = case_dir / "system" / "decomposeParDict"
    if path.exists():
        text = re.sub(r"//.*", "", path.read_text(encoding="utf-8", errors="replace"))
        m = re.search(r"\bnumberOfSubdomains\s+(\d+)\s*;", text)
        if m and int(m.group(1)) != n_procs:
            raise ValueError(
                f"{path} has numberOfSubdomains {m.group(1)} but n_procs={n_procs}; make them equal"
            )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "FoamFile\n{\n    version 2.0;\n    format ascii;\n    class dictionary;\n"
        "    object decomposeParDict;\n}\n\n"
        f"numberOfSubdomains {n_procs};\n\n"
        "method scotch;\n"
    )
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


def _available_threads() -> int:
    """Hardware threads this process may use (one MPI slot each with --use-hwthread-cpus)."""
    affinity = getattr(os, "sched_getaffinity", None)
    return len(affinity(0)) if affinity else (os.cpu_count() or 1)


def _read_control_number(
    case_dir: Path, entry: str, default: float | None = None
) -> tuple[float | None, str | None]:
    """Read a number from ``system/controlDict`` with ``foamDictionary``: ``(value, reason)``.

    The value is printed with 15 digits. An absent entry gives ``default`` when there is one.
    ``reason`` says why there is no value (missing or unparsable file, missing entry, not a plain
    number); that is a problem with what the agent wrote, not an error of ours. A foamDictionary
    that cannot be started or times out raises ``ToolFailure``.
    """
    result = run_tool(
        [
            "foamDictionary",
            _CONTROL_DICT,
            "-entry",
            entry,
            "-value",
            "-precision",
            _DICTIONARY_PRECISION,
        ],
        cwd=case_dir,
        log_path=case_dir / _DICTIONARY_LOG_NAME,
        timeout_s=_DICTIONARY_TIMEOUT_S,
    )
    if result.timed_out or result.returncode is None:
        raise ToolFailure(
            f"foamDictionary could not read {entry} from {_CONTROL_DICT}:\n{result.tail}"
        )
    lines = [line.strip() for line in result.tail.splitlines() if line.strip()]
    if not result.ok:
        if default is not None and "Cannot find entry" in result.tail:
            return default, None
        detail = lines[-1] if lines else f"foamDictionary exited with code {result.returncode}"
        return None, f"cannot read '{entry}' from {_CONTROL_DICT} ({detail})"
    value = _parse_float(lines[-1]) if lines else None
    if value is None or not math.isfinite(value):
        shown = lines[-1] if lines else "nothing"
        return None, (
            f"'{entry}' in {_CONTROL_DICT} is not a plain number (foamDictionary printed: {shown})"
        )
    return value, None


def _read_run_settings(case_dir: Path) -> tuple[float | None, str | None, float | None, int]:
    """``(endTime, why endTime is unreadable, deltaT or None, timePrecision)`` of the case."""
    case_end_time, problem = _read_control_number(case_dir, "endTime")
    if case_end_time is None:
        return None, problem, None, _DEFAULT_TIME_PRECISION
    delta_t, _ = _read_control_number(case_dir, "deltaT")
    precision, _ = _read_control_number(case_dir, "timePrecision", default=_DEFAULT_TIME_PRECISION)
    if precision is None or not 1 <= precision <= 17:
        precision = _DEFAULT_TIME_PRECISION
    return case_end_time, None, delta_t, int(precision)


def _solver_problem(
    step: ToolResult,
    summary: SolverLogSummary,
    case_end_time: float | None,
    end_time_problem: str | None,
    delta_t: float | None,
    time_precision: int,
) -> str | None:
    """Why a solver step is not a success; None if it is one (or if the log already says why).

    A fatal error is repeated at the end of the log even for a step that failed by itself: the
    solver prints a long list of valid choices after the message, which pushes it out of the tail.
    """
    if not step.ok:
        return summary.failure_reason if summary.fatal_error or summary.diverged else None
    if not summary.completed:
        return summary.failure_reason
    if case_end_time is None:
        return end_time_problem or f"cannot read 'endTime' from {_CONTROL_DICT}"
    return reach_problem(summary, case_end_time, delta_t, time_precision)


def run_solver(
    case_dir: Path, solver: str, n_procs: int = 1, timeout_s: float = 7200.0
) -> SolverRun:
    """Run ``solver`` in ``case_dir``; logs are written to ``case_dir``.

    ``n_procs`` must be 1..4 (ValueError otherwise, before anything runs) and ``solver`` must be in
    ``SOLVERS`` (ToolNotAllowed otherwise). More ranks than hardware threads raise
    ``AgentInputError`` (the choice of ``n_procs`` is the caller's; there is no clamping).

    Serial (``n_procs == 1``): ``<solver>`` with log ``log.<solver>``.
    Parallel: ``decomposePar -force`` (``log.decomposePar``; ``system/decomposeParDict`` is created
    with ``numberOfSubdomains n_procs`` and method ``scotch`` if absent, a mismatching existing one
    is a ValueError), ``mpirun --use-hwthread-cpus -np N <solver> -parallel`` (``log.<solver>``),
    then ``reconstructPar -latestTime`` (``log.reconstructPar``). A failing ``decomposePar`` is
    returned as it is (``summary`` of an empty log, ``case_end_time`` None).

    ``SolverRun.tool`` is the first failing step, otherwise the last step. A solver step with exit
    code 0 whose log is not ``completed`` (fatal error, divergence, missing ``End``), that advanced
    no time step, or whose last ``Time`` does not reach the ``endTime`` of ``system/controlDict``
    (read with ``foamDictionary``, see ``reach_problem``; unreadable counts as a failure), is
    turned into a failed step: the reason is appended to the solver log and so is visible in
    ``tool.tail``. A run that fails this way is not reconstructed. A ``foamDictionary`` that cannot
    run or times out raises ``ToolFailure``.
    """
    _check_n_procs(n_procs)
    _check_solver(solver)
    case_dir = Path(case_dir)
    solver_log = case_dir / f"log.{solver}"

    if n_procs == 1:
        step = run_tool([solver], cwd=case_dir, log_path=solver_log, timeout_s=timeout_s)
    else:
        available = _available_threads()
        if n_procs > available:
            raise AgentInputError(
                f"n_procs={n_procs} but only {available} hardware threads are available; "
                f"use n_procs <= {available}"
            )
        _prepare_decomposition(case_dir, n_procs)
        decomposed = run_tool(
            ["decomposePar", "-force"],
            cwd=case_dir,
            log_path=case_dir / "log.decomposePar",
            timeout_s=min(timeout_s, _UTILITY_TIMEOUT_S),
        )
        if not decomposed.ok:
            return SolverRun(decomposed, solver_log, parse_solver_log(""), None)
        step = run_tool(
            ["mpirun", "--use-hwthread-cpus", "-np", str(n_procs), solver, "-parallel"],
            cwd=case_dir,
            log_path=solver_log,
            timeout_s=timeout_s,
        )

    try:
        summary = parse_solver_log_file(solver_log)
    except FileNotFoundError:  # the process could not be started and wrote no log
        summary = parse_solver_log("")
    case_end_time, end_time_problem, delta_t, time_precision = _read_run_settings(case_dir)
    problem = _solver_problem(
        step, summary, case_end_time, end_time_problem, delta_t, time_precision
    )
    if problem is not None:
        step = fail_after_run(step, problem)
    if not step.ok or n_procs == 1:
        return SolverRun(step, solver_log, summary, case_end_time, delta_t, time_precision)

    reconstructed = run_tool(
        ["reconstructPar", "-latestTime"],
        cwd=case_dir,
        log_path=case_dir / "log.reconstructPar",
        timeout_s=min(timeout_s, _UTILITY_TIMEOUT_S),
    )
    return SolverRun(reconstructed, solver_log, summary, case_end_time, delta_t, time_precision)


def _time_directory_value(path: Path) -> float | None:
    """The time a directory name stands for, None if it is not a time directory."""
    if not path.is_dir():
        return None
    try:
        value = float(path.name)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _raise_symlink(path: Path, case_dir: Path) -> None:
    shown = path.relative_to(case_dir).as_posix()
    raise AgentInputError(
        f"{shown} is a symbolic link; a case must not contain symbolic links; write real files"
    )


def _reject_symlinks(path: Path, case_dir: Path) -> None:
    """Raise ``AgentInputError`` for a symbolic link at or below ``path`` (never followed)."""
    if path.is_symlink():
        _raise_symlink(path, case_dir)
    if not path.is_dir():
        return
    for root, dirnames, filenames in os.walk(path, followlinks=False):
        for name in (*dirnames, *filenames):
            candidate = Path(root) / name
            if candidate.is_symlink():
                _raise_symlink(candidate, case_dir)


def _copy_into(source: Path, target: Path) -> None:
    """Copy a directory by value; a full disk or similar is a tool failure, not the agent's."""
    try:
        shutil.copytree(source, target, symlinks=False)
    except OSError as exc:  # shutil.Error is an OSError
        raise ToolFailure(
            f"could not copy {source.name} into the scratch directory: {exc}; "
            "check the free disk space"
        ) from exc


def _trial_control_number(scratch: Path, entry: str) -> float:
    value, reason = _read_control_number(scratch, entry)
    if value is None:
        raise AgentInputError(
            f"trial run impossible: {reason}. Write '{entry} <number>;' in {_CONTROL_DICT}."
        )
    return value


def _set_control_entry(scratch: Path, entry: str, value: str) -> None:
    result = run_tool(
        [
            "foamDictionary",
            _CONTROL_DICT,
            "-entry",
            entry,
            "-set",
            value,
            "-precision",
            _DICTIONARY_PRECISION,
        ],
        cwd=scratch,
        log_path=scratch / _DICTIONARY_LOG_NAME,
        timeout_s=_DICTIONARY_TIMEOUT_S,
    )
    if not result.ok:
        raise ToolFailure(f"foamDictionary could not set {entry} to {value}:\n{result.tail}")


def trial_run(case_dir: Path, solver: str, steps: int = 3, timeout_s: float = 300.0) -> SolverRun:
    """Run about ``steps`` time steps of the case, serially, in a scratch copy; find setup errors.

    Only ``constant``, ``system`` and the start-time directory (and ``0``, if that is another one)
    are copied, by value; results, logs, decomposed data and other time directories are not. In
    the copy ``startFrom`` is ``startTime``, ``stopAt`` is ``endTime``, ``endTime`` is
    ``startTime + steps * deltaT`` and a write is forced at the end (``writeControl timeStep``,
    ``writeInterval steps``), so that errors that only show when fields or function objects are
    written surface too. The result is judged like a normal run (see ``SolverRun.ok``): clean log,
    at least one time step, last time within one step of the target.

    Limits: with an adjustable time step the solver does not make exactly ``steps`` steps, it
    stops at about the target time. The maximum Courant number after a few steps is not evidence
    of stability.

    ``case_dir`` is never written to, and the scratch directory is deleted afterwards, also on an
    exception; this means the paths in the returned ``SolverRun`` no longer exist, and only its
    ``tool.tail``, ``summary`` and ``case_end_time`` are usable.

    A case without ``system/controlDict``, without a readable ``startTime`` or positive
    ``deltaT``, or containing a symbolic link in the copied parts, raises ``AgentInputError`` (the
    agent wrote the case). A ``case_dir`` that is not a directory, or a failed copy (full disk),
    raises ``ToolFailure``. ``steps`` must be an integer of at least 1 (ValueError).
    """
    if isinstance(steps, bool) or not isinstance(steps, int) or steps < 1:
        raise ValueError(f"steps must be an integer of at least 1, got {steps!r}")
    _check_solver(solver)
    case_dir = Path(case_dir)
    if not case_dir.is_dir():
        raise ToolFailure(f"{case_dir} is not a directory; there is no case to trial-run")
    if not (case_dir / "system" / "controlDict").is_file():
        raise AgentInputError(
            f"{case_dir} has no {_CONTROL_DICT}; write it (application, startFrom, startTime, "
            "endTime, deltaT, write settings) before running the solver"
        )
    for name in ("constant", "system"):
        _reject_symlinks(case_dir / name, case_dir)

    try:
        tmp = tempfile.mkdtemp(prefix="cfdagent-trial-")
    except OSError as exc:
        raise ToolFailure(f"could not create a scratch directory: {exc}") from exc
    try:
        scratch = Path(tmp) / "case"
        scratch.mkdir()
        for name in ("constant", "system"):
            if (case_dir / name).is_dir():
                _copy_into(case_dir / name, scratch / name)
        start_time = _trial_control_number(scratch, "startTime")
        delta_t = _trial_control_number(scratch, "deltaT")
        if delta_t <= 0:
            raise AgentInputError(f"deltaT in {_CONTROL_DICT} must be positive, got {delta_t:g}")
        for candidate in sorted(case_dir.iterdir()):
            value = _time_directory_value(candidate)
            if value is None:
                continue
            if candidate.name == "0" or math.isclose(
                value, start_time, rel_tol=1e-9, abs_tol=1e-12
            ):
                _reject_symlinks(candidate, case_dir)
                _copy_into(candidate, scratch / candidate.name)
        for entry, value_text in (
            ("startFrom", "startTime"),
            ("stopAt", "endTime"),
            ("endTime", f"{start_time + steps * delta_t:.15g}"),
            ("writeControl", "timeStep"),
            ("writeInterval", str(steps)),
        ):
            _set_control_entry(scratch, entry, value_text)
        return run_solver(scratch, solver, n_procs=1, timeout_s=timeout_s)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
