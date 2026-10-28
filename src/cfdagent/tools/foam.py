"""Run an OpenFOAM solver and decide, from its log, whether the run really succeeded.

``run_solver`` launches the solver (serially or under MPI) through the allowlisted process runner.
``parse_solver_log`` turns a solver log into a ``SolverLogSummary``. A run counts as completed only
when the log ends with the solver's final ``End`` line AND shows no fatal error AND no divergence;
an exit code of 0 alone is never trusted.
"""

from __future__ import annotations

import dataclasses
import math
import re
from dataclasses import dataclass
from pathlib import Path

from cfdagent.tools.process import ALLOWED_EXECUTABLES, ToolNotAllowed, ToolResult, run_tool

MAX_RANKS = 4
_UTILITY_TIMEOUT_S = 600.0

# Allowlisted executables that are not solvers; ``run_solver`` refuses to treat them as one.
_NON_SOLVERS = frozenset(
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
_END_RE = re.compile(r"^End\s*$", re.MULTILINE)
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
    ``diverged``: a NaN/Inf residual, continuity error or Courant number, or a floating point
    exception. ``fatal_error``: the message after ``--> FOAM FATAL (IO) ERROR`` (first one).
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
            return "solution diverged (NaN/Inf in the log or a floating point exception)"
        return "the log has no final 'End' line: the run did not finish"


def _parse_float(token: str) -> float | None:
    try:
        return float(token)
    except ValueError:
        return None


def _extract_fatal_error(lines: list[str]) -> str | None:
    for i, line in enumerate(lines):
        m = _FATAL_RE.match(line)
        if m is None:
            continue
        message: list[str] = []
        first = _VERSION_TAG_RE.sub("", m.group(1)).lstrip(" :").strip()
        if first:
            message.append(first)
        for follow in lines[i + 1 :]:
            text = _RANK_PREFIX_RE.sub("", follow).rstrip()
            if not text.strip() or _FROM_RE.match(text):
                break
            message.append(text.strip())
        return "\n".join(message) if message else "FOAM FATAL ERROR (no message)"
    return None


def parse_solver_log(log_text: str) -> SolverLogSummary:
    """Summarise a solver log. See ``SolverLogSummary`` for the meaning of each field."""
    lines = log_text.splitlines()

    fatal_error = _extract_fatal_error(lines)
    diverged = bool(_SIGFPE_RE.search(log_text))
    end_time: float | None = None
    final_residuals: dict[str, float] = {}
    courants: list[float] = []

    for line in lines:
        m = _TIME_RE.match(line)
        if m:
            t = _parse_float(m.group(1))
            if t is not None:
                end_time = t
            continue
        m = _RESIDUAL_RE.search(line)
        if m:
            initial, final = _parse_float(m.group(2)), _parse_float(m.group(3))
            if initial is None or final is None or not math.isfinite(initial + final):
                diverged = True
            if final is not None:
                final_residuals[m.group(1)] = final  # later solves overwrite earlier ones
            continue
        m = _COURANT_RE.search(line)
        if m:
            cmax = _parse_float(m.group(2))
            if cmax is None or not math.isfinite(cmax):
                diverged = True
            else:
                courants.append(cmax)
            continue
        if "continuity errors" in line and _NON_FINITE_RE.search(line):
            diverged = True

    has_end = _END_RE.search(log_text) is not None
    completed = has_end and fatal_error is None and not diverged
    return SolverLogSummary(
        completed=completed,
        diverged=diverged,
        fatal_error=fatal_error,
        end_time=end_time,
        final_residuals=final_residuals,
        max_courant=max(courants) if courants else None,
    )


def _check_n_procs(n_procs: int) -> None:
    if isinstance(n_procs, bool) or not isinstance(n_procs, int) or not 1 <= n_procs <= MAX_RANKS:
        raise ValueError(f"n_procs must be an integer from 1 to {MAX_RANKS}, got {n_procs!r}")


def _check_solver(solver: str) -> None:
    if solver not in ALLOWED_EXECUTABLES - _NON_SOLVERS:
        raise ToolNotAllowed(
            f"solver {solver!r} is not an allowed solver; "
            f"allowed: {sorted(ALLOWED_EXECUTABLES - _NON_SOLVERS)}"
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


def _solver_run_succeeded(result: ToolResult) -> ToolResult:
    """Downgrade a solver step whose exit code is 0 but whose log does not show a clean finish."""
    if not result.ok:
        return result
    log_text = result.log_path.read_text(encoding="utf-8", errors="replace")
    if parse_solver_log(log_text).completed:
        return result
    return dataclasses.replace(result, ok=False)


def run_solver(
    case_dir: Path, solver: str, n_procs: int = 1, timeout_s: float = 7200.0
) -> ToolResult:
    """Run ``solver`` in ``case_dir``; logs are written to ``case_dir``.

    ``n_procs`` must be 1..4 (ValueError otherwise, before anything runs) and ``solver`` must be an
    allowlisted solver (ToolNotAllowed otherwise).

    Serial (``n_procs == 1``): ``<solver>`` with log ``log.<solver>``.
    Parallel: ``decomposePar -force`` (``log.decomposePar``; ``system/decomposeParDict`` is created
    with ``numberOfSubdomains n_procs`` and method ``scotch`` if absent, a mismatching existing one
    is a ValueError), ``mpirun -np N <solver> -parallel`` (``log.<solver>``), then
    ``reconstructPar -latestTime`` (``log.reconstructPar``).

    The result of the first failing step is returned (a failed step stops the sequence), otherwise
    the result of the last step. A solver step with exit code 0 whose log is not ``completed``
    (fatal error, divergence, missing ``End``) is returned with ``ok=False``.
    """
    _check_n_procs(n_procs)
    _check_solver(solver)
    case_dir = Path(case_dir)
    solver_log = case_dir / f"log.{solver}"
    utility_timeout = min(timeout_s, _UTILITY_TIMEOUT_S)

    if n_procs == 1:
        return _solver_run_succeeded(
            run_tool([solver], cwd=case_dir, log_path=solver_log, timeout_s=timeout_s)
        )

    _prepare_decomposition(case_dir, n_procs)
    result = run_tool(
        ["decomposePar", "-force"],
        cwd=case_dir,
        log_path=case_dir / "log.decomposePar",
        timeout_s=utility_timeout,
    )
    if not result.ok:
        return result
    result = _solver_run_succeeded(
        run_tool(
            ["mpirun", "-np", str(n_procs), solver, "-parallel"],
            cwd=case_dir,
            log_path=solver_log,
            timeout_s=timeout_s,
        )
    )
    if not result.ok:
        return result
    return run_tool(
        ["reconstructPar", "-latestTime"],
        cwd=case_dir,
        log_path=case_dir / "log.reconstructPar",
        timeout_s=utility_timeout,
    )
