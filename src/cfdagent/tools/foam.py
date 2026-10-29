"""Run an OpenFOAM solver and decide, from its log, whether the run really succeeded.

``run_solver`` launches the solver (serially or under MPI) through the allowlisted process runner
and returns a ``SolverRun``. ``parse_solver_log`` and ``parse_solver_log_file`` turn a solver log
into a ``SolverLogSummary``. A run counts as successful only when the step exited cleanly, the log
ends with the solver's final ``End`` line, shows no fatal error and no divergence, AND the last
time it reports is the ``endTime`` of the case's ``controlDict``; an exit code of 0 alone is never
trusted. ``trial_run`` does a few time steps in a scratch copy of a case, to find setup errors
cheaply.
"""

from __future__ import annotations

import io
import math
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

# A residual or Courant number above this value is a blow-up, not a solution: the run counts as
# diverged even when the log still ends with ``End`` and shows no NaN. Healthy runs stay many
# orders of magnitude below it (residuals are normalised, Courant numbers are of order 1).
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
    Number ... max`` values, or None if the log has none.
    """

    completed: bool
    diverged: bool
    fatal_error: str | None
    end_time: float | None
    final_residuals: dict[str, float]
    max_courant: float | None

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
    could not be read.
    """

    tool: ToolResult
    solver_log: Path
    summary: SolverLogSummary
    case_end_time: float | None

    @property
    def ok(self) -> bool:
        """True only if the step succeeded, the log is clean AND the run reached ``endTime``.

        The end time is compared with the last ``Time = ...`` of the log, relative tolerance 1e-9.
        """
        return (
            self.tool.ok
            and self.summary.completed
            and _reached(self.summary.end_time, self.case_end_time)
        )


def _reached(end_time: float | None, case_end_time: float | None) -> bool:
    if end_time is None or case_end_time is None:
        return False
    return math.isclose(end_time, case_end_time, rel_tol=1e-9, abs_tol=0.0)


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
                self.end_time = t
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


def _read_control_number(case_dir: Path, entry: str) -> tuple[float | None, str | None]:
    """Read a number from ``system/controlDict`` with ``foamDictionary``: ``(value, reason)``.

    ``reason`` says why there is no value (missing or unparsable file, missing entry, not a plain
    number); that is a problem with what the agent wrote, not an error of ours. A foamDictionary
    that cannot be started or times out raises ``ToolFailure``.
    """
    result = run_tool(
        ["foamDictionary", _CONTROL_DICT, "-entry", entry, "-value"],
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
        detail = lines[-1] if lines else f"foamDictionary exited with code {result.returncode}"
        return None, f"cannot read '{entry}' from {_CONTROL_DICT} ({detail})"
    value = _parse_float(lines[-1]) if lines else None
    if value is None or not math.isfinite(value):
        shown = lines[-1] if lines else "nothing"
        return None, (
            f"'{entry}' in {_CONTROL_DICT} is not a plain number (foamDictionary printed: {shown})"
        )
    return value, None


def _solver_problem(
    step: ToolResult,
    summary: SolverLogSummary,
    case_end_time: float | None,
    end_time_problem: str | None,
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
    if not _reached(summary.end_time, case_end_time):
        return (
            f"the solver stopped at Time = {summary.end_time:g} but {_CONTROL_DICT} has "
            f"endTime {case_end_time:g}: the run did not reach the requested end time"
        )
    return None


def run_solver(
    case_dir: Path, solver: str, n_procs: int = 1, timeout_s: float = 7200.0
) -> SolverRun:
    """Run ``solver`` in ``case_dir``; logs are written to ``case_dir``.

    ``n_procs`` must be 1..4 (ValueError otherwise, before anything runs) and ``solver`` must be in
    ``SOLVERS`` (ToolNotAllowed otherwise).

    Serial (``n_procs == 1``): ``<solver>`` with log ``log.<solver>``.
    Parallel: ``decomposePar -force`` (``log.decomposePar``; ``system/decomposeParDict`` is created
    with ``numberOfSubdomains n_procs`` and method ``scotch`` if absent, a mismatching existing one
    is a ValueError), ``mpirun --use-hwthread-cpus -np N <solver> -parallel`` (``log.<solver>``),
    then ``reconstructPar -latestTime`` (``log.reconstructPar``).

    ``SolverRun.tool`` is the first failing step, otherwise the last step. A solver step with exit
    code 0 whose log is not ``completed`` (fatal error, divergence, missing ``End``), or whose
    last ``Time`` differs from the ``endTime`` of ``system/controlDict`` (read with
    ``foamDictionary``; unreadable counts as a failure), is turned into a failed step: the reason
    is appended to the solver log and so is visible in ``tool.tail``. A run that fails this way is
    not reconstructed. A ``foamDictionary`` that cannot run or times out raises ``ToolFailure``.
    """
    _check_n_procs(n_procs)
    _check_solver(solver)
    case_dir = Path(case_dir)
    solver_log = case_dir / f"log.{solver}"

    if n_procs == 1:
        step = run_tool([solver], cwd=case_dir, log_path=solver_log, timeout_s=timeout_s)
    else:
        _prepare_decomposition(case_dir, n_procs)
        decomposed = run_tool(
            ["decomposePar", "-force"],
            cwd=case_dir,
            log_path=case_dir / "log.decomposePar",
            timeout_s=min(timeout_s, _UTILITY_TIMEOUT_S),
        )
        if not decomposed.ok:
            case_end_time, _ = _read_control_number(case_dir, "endTime")
            return SolverRun(decomposed, solver_log, parse_solver_log(""), case_end_time)
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
    case_end_time, end_time_problem = _read_control_number(case_dir, "endTime")
    problem = _solver_problem(step, summary, case_end_time, end_time_problem)
    if problem is not None:
        step = fail_after_run(step, problem)
    if not step.ok or n_procs == 1:
        return SolverRun(step, solver_log, summary, case_end_time)

    reconstructed = run_tool(
        ["reconstructPar", "-latestTime"],
        cwd=case_dir,
        log_path=case_dir / "log.reconstructPar",
        timeout_s=min(timeout_s, _UTILITY_TIMEOUT_S),
    )
    return SolverRun(reconstructed, solver_log, summary, case_end_time)


def _is_time_directory(path: Path) -> bool:
    if not path.is_dir():
        return False
    try:
        float(path.name)
    except ValueError:
        return False
    return True


def _trial_ignore(directory: str, names: list[str]) -> list[str]:
    """Top level of a case: skip results, logs, decomposed data and every time directory."""
    top = Path(directory)
    return [
        name
        for name in names
        if name == "postProcessing"
        or name.startswith(("processor", "log."))
        or _is_time_directory(top / name)
    ]


def _trial_control_number(scratch: Path, entry: str) -> float:
    value, reason = _read_control_number(scratch, entry)
    if value is None:
        raise AgentInputError(
            f"trial run impossible: {reason}. Write '{entry} <number>;' in {_CONTROL_DICT}."
        )
    return value


def _set_control_entry(scratch: Path, entry: str, value: str) -> None:
    result = run_tool(
        ["foamDictionary", _CONTROL_DICT, "-entry", entry, "-set", value],
        cwd=scratch,
        log_path=scratch / _DICTIONARY_LOG_NAME,
        timeout_s=_DICTIONARY_TIMEOUT_S,
    )
    if not result.ok:
        raise ToolFailure(f"foamDictionary could not set {entry} to {value}:\n{result.tail}")


def trial_run(case_dir: Path, solver: str, steps: int = 3, timeout_s: float = 300.0) -> SolverRun:
    """Run ``steps`` time steps of the case, serially, in a scratch copy; find setup errors early.

    The copy leaves out ``postProcessing``, logs, ``processor*`` and every time directory except
    the one of ``startTime``. In the copy ``startFrom`` is ``startTime`` and ``endTime`` is
    ``startTime + steps * deltaT``, so a successful result means the setup runs and reaches that
    time. ``case_dir`` is never written to, and the scratch directory is deleted afterwards, also
    on an exception; this means the paths in the returned ``SolverRun`` no longer exist, and only
    its ``tool.tail``, ``summary`` and ``case_end_time`` are usable.

    A case without ``system/controlDict``, or without a readable ``startTime`` or positive
    ``deltaT``, raises ``AgentInputError`` (the agent wrote the case). A ``case_dir`` that is not a
    directory raises ``ToolFailure``. ``steps`` must be an integer of at least 1 (ValueError).
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

    with tempfile.TemporaryDirectory(prefix="cfdagent-trial-") as tmp:
        scratch = Path(tmp) / "case"
        shutil.copytree(case_dir, scratch, symlinks=True, ignore=_trial_ignore)
        start_time = _trial_control_number(scratch, "startTime")
        delta_t = _trial_control_number(scratch, "deltaT")
        if delta_t <= 0:
            raise AgentInputError(f"deltaT in {_CONTROL_DICT} must be positive, got {delta_t:g}")
        for candidate in case_dir.iterdir():
            if _is_time_directory(candidate) and _reached(float(candidate.name), start_time):
                shutil.copytree(candidate, scratch / candidate.name, symlinks=True)
        _set_control_entry(scratch, "startFrom", "startTime")
        _set_control_entry(scratch, "endTime", f"{start_time + steps * delta_t:.12g}")
        return run_solver(scratch, solver, n_procs=1, timeout_s=timeout_s)
