"""The Solver Runner stage, stage 5 of the pipeline (design 6.2, 6.3).

The Case Setup stage has left the run directory a complete ``case/`` (``0/``, ``constant/`` with
the mesh, ``system/``). Every attempt gets a fresh copy of it as ``attempts/solver_runner-N/case/``
(``prepare_attempt``). The agent chooses the number of MPI ranks and runs the solver with the
``run_solver`` tool; it reads the solver log with ``read_solver_log``. It has no write tool: this
stage runs the case exactly as it is, and a failure is diagnosed, not repaired (routing back to
Case Setup or Mesh is M2).

Every ``run_solver`` call works on a fresh copy of the run's ``case/``: a second call in the same
attempt never resumes from the time directories of the first. The call replaces the attempt's
case, so the results of an earlier call are gone; the gate and the log reader see the last call.

The gate is the last ``run_solver`` call of the attempt: ``SolverRun.ok`` (the solver completed,
nothing is non-finite, the last time reaches ``endTime`` within one step, or a steady solver
announced its own convergence). No call, or none that reached the solver, fails the gate. The gate
does NOT judge convergence of a steady run: a run that reached ``endTime`` without the
``residualControl`` line passes, because this agent cannot change the case. The tool summary, the
gate's data and ``solver_run.json`` (promoted to ``logs/``) state whether a steady run converged, so
that the Analyst sees it.

After a failed gate the loop asks the agent for a diagnosis (``diagnosis_request``): the cause, with
evidence from the log, and the specific fix. The text is stored in ``AttemptRecord.diagnosis``,
also for the last attempt.

Errors the agent can fix (a wrong solver name, a rank count out of range, more ranks than hardware
threads) are tool errors and do not count as a run. A failed executor or tool
(``OpResponse.internal``) and a broken precondition raise ``ToolFailure``.

This module is host-side: it imports no tool, CAD, mesh or knowledge code.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, StrictInt

from cfdagent.agents.case_setup import KNOWN_SOLVERS
from cfdagent.agents.casespec import CaseSpec
from cfdagent.agents.stage import Stage, StageContext
from cfdagent.agents.tools import FileScope, Tool
from cfdagent.errors import AgentInputError, ToolFailure
from cfdagent.executor.protocol import OpResponse, RunSolverArgs
from cfdagent.gates import GateResult
from cfdagent.rundir import FileTooLarge, RunDirError
from cfdagent.sandbox.client import outer_timeout

STAGE = "solver_runner"
AGENT = "solver_runner"
CASE_NAME = "case"
PROMOTED_CASE_DIR = "case"  # the solved case of the Analyst
PROMOTED_LOGS_DIR = "logs"
SUMMARY_FILE = "solver_run.json"  # in the attempt, and in logs/ once promoted
CHECKMESH_LOG = "mesh/case/checkMesh.log"
CONTROL_DICT = f"{CASE_NAME}/system/controlDict"
POLYMESH = f"{CASE_NAME}/constant/polyMesh"
MAX_PROCS = 4  # pipeline invariant; limits.max_ranks may only lower it
MAX_OP_TIMEOUT_S = 86_400.0  # the longest time the executor op accepts
STEADY_SOLVERS = ("simpleFoam",)  # the others are transient
MAX_LOG_LINES = 200  # per read_solver_log call
LINE_BYTES = 300  # a longer log line is cut
LOG_PAGE_BYTES = 7000  # a tool result is cut at 8000 bytes; this keeps the footer visible
SCAN_BYTES = 512 * 1024 * 1024  # a log is not counted beyond this
TAIL_BYTES = 2500  # the loop shows the model at most 4096 bytes of all errors together
SUMMARY_TAIL_BYTES = 3000
HEADLINE_CHARS = 500
_CELLS_RE = re.compile(r"^\s*cells:\s+(\d+)\s*$", re.MULTILINE)
_CHECKMESH_MAX_BYTES = 1_000_000
_READ_CHUNK = 4096
LOG_FILE_PREFIX = "log."
NOT_RUN_PREFIX = "the solver was not run"  # the gate error of an attempt without a run
RESERVE_S = 600.0  # kept back from the solver for the diagnosis, the Analyst and the upload ...
RESERVE_FRACTION = 0.2  # ... or this share of the remaining time, whichever is smaller
COURANT_WARNING = 10.0  # a transient run above this is suspect even when it finishes
MAX_ITER_DEFAULT = 1000  # OpenFOAM's default maxIter of the linear solvers
BOUNDING_WARNING = 10  # more 'bounding' messages than this are a warning
CELLS_PER_RANK = "10 000"  # the Planner's rule: at least about this many cells for every rank


class RunSolverToolArgs(BaseModel):
    solver: str = Field(max_length=64, description="The solver named in the task")
    n_procs: StrictInt = Field(description="Number of MPI ranks: 1 for a serial run")


class ReadSolverLogToolArgs(BaseModel):
    offset_lines: StrictInt = Field(
        default=0, description="Lines to skip from the start of the log (0 for the beginning)"
    )
    n_lines: StrictInt = Field(
        default=100, description=f"Number of lines to return, 1 to {MAX_LOG_LINES}"
    )


@dataclass
class _Run:
    """The last ``run_solver`` call of an attempt that reached the solver."""

    solver: str
    n_procs: int
    ok: bool
    data: dict[str, Any]
    summary: dict[str, Any]


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _clip(text: str, limit: int = HEADLINE_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + "..."


def _tail(text: str, limit: int) -> str:
    """The end of a solver output (at most ``limit`` bytes), on a line boundary. Lines of the stack
    printer's failed ``addr2line`` calls are noise that can fill half the tail: they are dropped."""
    kept = "\n".join(line for line in text.splitlines() if "addr2line" not in line)
    data = kept.encode("utf-8", "replace")
    if len(data) <= limit:
        return kept
    cut = data[-limit:].decode("utf-8", "ignore")
    _, newline, rest = cut.partition("\n")
    return "[earlier output omitted]\n" + (rest if newline else cut)


def _internal(response: OpResponse, label: str) -> ToolFailure:
    return ToolFailure(f"{label} failed outside the agent's work: {response.error}")


def _lines(handle, limit: int):
    """``(bytes up to limit, was cut)`` for every line of a binary file, in bounded memory."""
    while True:
        chunk = handle.readline(_READ_CHUNK)
        if not chunk:
            return
        kept = chunk
        cut = False
        while not chunk.endswith(b"\n"):
            chunk = handle.readline(_READ_CHUNK)
            if not chunk:
                break
            cut = True
        kept = kept.rstrip(b"\r\n")
        if len(kept) > limit:
            kept, cut = kept[:limit], True
        yield kept, cut


class SolverRunnerStage(Stage):
    """Stage 5: run the planned solver on the case and report how it went."""

    name = STAGE
    agent = AGENT
    scope = FileScope(
        readable=("casespec.json", CHECKMESH_LOG, "case_setup/*", "attempts/solver_runner-*/*"),
        writable=(),
    )

    def __init__(self) -> None:
        self._last: dict[Path, _Run | None] = {}  # attempt directory -> its last run
        self._copies: dict[Path, int] = {}  # attempt directory -> runs started in it

    # ------------------------------------------------------------------ the agent's task

    def opening_message(self, ctx: StageContext) -> str:
        spec = self._spec(ctx)
        solver = self._solver(spec)
        cells = self._cell_count(ctx)
        cap = self._rank_cap(ctx)
        if cells is None:
            mesh = f"Read the cell count from the `cells:` line of {CHECKMESH_LOG}."
        else:
            mesh = f"The mesh has {cells} cells (from {CHECKMESH_LOG})."
        plan = {
            "title": spec.title,
            "summary": spec.summary,
            "dimensionality": spec.dimensionality,
            "flow_regime": spec.flow_regime,
            "time_dependence": spec.time_dependence,
            "solver": spec.solver.model_dump(),
            "requested_outputs": [o.model_dump() for o in spec.requested_outputs],
        }
        kind = (
            "a steady solver: it ends at endTime, or earlier if its residualControl is met"
            if solver in STEADY_SOLVERS
            else "a transient solver: it ends at endTime"
        )
        estimate = spec.resource_estimate.runtime_minutes
        return (
            f"Run the OpenFOAM solver {solver} (the plan's solver, {kind}) on the case that the "
            "Case Setup stage prepared, and report how it went.\n\n"
            f"{json.dumps(plan, indent=2)}\n\n"
            f"{mesh} The Planner estimated {estimate:g} minutes of solver run time on one core. "
            f"Choose n_procs from the cell count: serial unless every rank would keep at least "
            f"about {CELLS_PER_RANK} cells per rank; never more than {cap} ranks (the limit of "
            "this run) and never more than the hardware threads of the machine (a larger n_procs "
            "is refused with a message). "
            "The case is in case/ of the attempt directory; you cannot edit it. Call "
            f"run_solver(solver={solver!r}, n_procs=...) once, and call check_stage when it has "
            "finished. If the run failed, read the log with read_solver_log (the context of the "
            "FOAM FATAL ERROR, or the first step where a residual or the Courant number jumps) "
            "before you call check_stage: the diagnosis turn that follows a failed check has no "
            "tools, so every number you want to cite must already be in front of you."
        )

    # ------------------------------------------------------------------ attempts

    def prepare_attempt(self, ctx: StageContext, attempt_dir: Path) -> None:
        """Copy the run's ``case/`` into the attempt and forget the earlier runs of the path."""
        self._solver(self._spec(ctx))
        self._last.pop(attempt_dir, None)
        self._copies.pop(attempt_dir, None)
        try:
            if not ctx.run.path(CONTROL_DICT).is_file():
                raise ToolFailure(
                    f"{CONTROL_DICT} does not exist: the Case Setup stage has not produced the "
                    "case, so the pipeline order is broken"
                )
            if not ctx.run.path(POLYMESH).is_dir():
                raise ToolFailure(
                    f"{POLYMESH} does not exist: the case has no mesh, so the pipeline order "
                    "is broken"
                )
        except RunDirError as exc:
            raise ToolFailure(f"the run's case cannot be used: {exc}") from exc
        self._copy_case(ctx, attempt_dir)

    def extra_tools(self, ctx: StageContext, attempt_dir: Path) -> list[Tool]:
        self._last[attempt_dir] = None
        planned = self._solver(self._spec(ctx))
        return [
            Tool(
                "run_solver",
                f"Run the solver of the plan ({planned}) on a fresh copy of the case, with "
                f"n_procs MPI ranks (1 for a serial run, at most {self._rank_cap(ctx)}). The run "
                "ends at the case's endTime or when the time budget of the run is used up. "
                "Returns a summary (did it reach the end time, the last time, the maximum Courant "
                "number, the initial residuals of the last and the first iteration, whether a "
                "residualControl criterion ended a steady run, warnings, the fatal error if any) "
                "and the end of the log. Each call starts from the original case again.",
                RunSolverToolArgs,
                lambda args: self._run_solver(ctx, attempt_dir, planned, args),
            ),
            Tool(
                "read_solver_log",
                "Read part of the log of the last run_solver call of this attempt: "
                f"n_lines (1 to {MAX_LOG_LINES}) lines after skipping offset_lines. Long lines "
                "are cut.",
                ReadSolverLogToolArgs,
                lambda args: self._read_log(ctx, attempt_dir, args),
            ),
        ]

    # ------------------------------------------------------------------ the gate

    def execute_and_gate(self, ctx: StageContext, attempt_dir: Path) -> GateResult:
        solver = self._solver(self._spec(ctx))
        run = self._last.get(attempt_dir)
        if run is None:
            return GateResult(
                False,
                [
                    f"{NOT_RUN_PREFIX} in this attempt: call run_solver(solver={solver!r}, "
                    "n_procs=...) and wait for it to finish before check_stage"
                ],
            )
        if run.ok:
            return GateResult(True, [], run.summary)
        return GateResult(False, [self._failure_text(run)], run.summary)

    def diagnosis_request(self, ctx: StageContext, errors: list[str]) -> str | None:
        return (
            "This attempt failed; the message of the check is above. You cannot edit the case, so "
            "do not try to repair it. This turn has no tools: use only what you have already "
            "read. Reply in plain text in at most about 150 words: (1) the most likely cause, "
            "with the evidence from the log (the FOAM FATAL ERROR message, or the signs of "
            "divergence: which residual or the Courant number started to grow, and at which "
            "time); if the solver was not run, say so; (2) the specific fix that a person "
            "editing the case would make: which file, which keyword, which new value, and why "
            "that value."
        )

    def retry_after_failure(self, errors: list[str]) -> bool:
        """Only a missing run is worth another attempt. A solver run that failed is a property
        of the case, which this stage cannot change (routing back is M2), and the solver is
        deterministic: the same case fails the same way."""
        return len(errors) == 1 and errors[0].startswith(NOT_RUN_PREFIX)

    def retry_text(self, previous: str) -> str:
        return (
            "Call run_solver on the fresh copy of the case, wait for the run to finish and call "
            f"check_stage (the files of the previous attempt stay readable in {previous})."
        )

    def promote(self, ctx: StageContext, attempt_dir: Path) -> None:
        """``case/``: the solved case of the attempt, replacing the one of the Case Setup stage;
        ``logs/``: the solver logs (``log.*`` of the case) and the run summary."""
        rel = ctx.run.relative(attempt_dir)
        try:
            logs = ctx.run.path(PROMOTED_LOGS_DIR)
            if os.path.lexists(logs):
                if logs.is_dir() and not logs.is_symlink():
                    shutil.rmtree(logs)
                else:
                    logs.unlink()
            logs.mkdir(parents=True)
            case = ctx.run.path(f"{rel}/{CASE_NAME}")
            for entry in sorted(os.scandir(case), key=lambda e: e.name):
                if entry.name.startswith(LOG_FILE_PREFIX) and entry.is_file(follow_symlinks=False):
                    shutil.copyfile(entry.path, logs / entry.name)
            summary = attempt_dir / SUMMARY_FILE
            if summary.is_file() and not summary.is_symlink():
                shutil.copyfile(summary, logs / SUMMARY_FILE)
            ctx.run.promote(case, PROMOTED_CASE_DIR)
        except (RunDirError, OSError) as exc:
            raise ToolFailure(
                f"promoting the solved case failed: {type(exc).__name__}: {exc}"
            ) from exc

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _spec(ctx: StageContext) -> CaseSpec:
        if ctx.casespec is None:
            raise ToolFailure("the solver runner needs the CaseSpec; run the planner first")
        return ctx.casespec

    @staticmethod
    def _solver(spec: CaseSpec) -> str:
        solver = spec.solver.suggested_solver
        if solver not in KNOWN_SOLVERS:
            raise ToolFailure(
                f"the plan's suggested solver {solver!r} is not one this pipeline runs "
                f"({', '.join(KNOWN_SOLVERS)}); the planner should have rejected it"
            )
        return solver

    @staticmethod
    def _rank_cap(ctx: StageContext) -> int:
        return min(MAX_PROCS, ctx.config.limits.max_ranks)

    @staticmethod
    def _cell_count(ctx: StageContext) -> int | None:
        try:
            text = ctx.run.read_text(CHECKMESH_LOG, max_bytes=_CHECKMESH_MAX_BYTES)
        except (RunDirError, FileNotFoundError, FileTooLarge, UnicodeDecodeError):
            return None
        found = _CELLS_RE.search(text)
        return int(found.group(1)) if found else None

    @staticmethod
    def _copy_case(ctx: StageContext, attempt_dir: Path) -> None:
        rel = ctx.run.relative(attempt_dir)
        try:
            ctx.run.promote(ctx.run.path(CASE_NAME), f"{rel}/{CASE_NAME}")
        except (RunDirError, OSError) as exc:
            raise ToolFailure(f"could not copy the case into {rel}/: {exc}") from exc

    # ------------------------------------------------------------------ run_solver

    def _check_arguments(self, ctx: StageContext, planned: str, args: RunSolverToolArgs) -> None:
        if args.solver != planned:
            raise AgentInputError(
                f"{args.solver!r} is not the planned solver; this run uses {planned!r}. Call "
                f"run_solver(solver={planned!r}, n_procs=...)"
            )
        cap = self._rank_cap(ctx)
        if args.n_procs < 1:
            raise AgentInputError(f"n_procs must be at least 1 (got {args.n_procs}); 1 is serial")
        if args.n_procs > cap:
            raise AgentInputError(
                f"n_procs must be at most {cap} (got {args.n_procs}): the run is limited to "
                f"{cap} MPI ranks"
            )

    def _run_solver(
        self, ctx: StageContext, attempt_dir: Path, planned: str, args: RunSolverToolArgs
    ) -> str:
        self._check_arguments(ctx, planned, args)
        rel = ctx.run.relative(attempt_dir)
        case_rel = f"{rel}/{CASE_NAME}"
        if self._copies.get(attempt_dir, 0) > 0:
            self._copy_case(ctx, attempt_dir)  # no resume from the time directories of a call
        self._last[attempt_dir] = None  # the case is the original again: nothing has run on it
        self._copies[attempt_dir] = self._copies.get(attempt_dir, 0) + 1
        left = ctx.remaining_s()
        reserve = min(RESERVE_S, RESERVE_FRACTION * left)
        inner = ctx.inner_timeout(min(left - reserve, MAX_OP_TIMEOUT_S))
        response = ctx.sandbox.call(
            "run_solver",
            RunSolverArgs(
                case_dir=case_rel, solver=args.solver, n_procs=args.n_procs, timeout_s=inner
            ),
            outer_timeout(inner),
        )
        if response.internal:
            raise _internal(response, "the solver run")
        data = response.data
        if not response.ok and (not isinstance(data, dict) or "completed" not in data):
            # The op refused the call (for example more ranks than hardware threads): nothing ran.
            raise AgentInputError(response.error or "run_solver was refused without a message")
        if not isinstance(data, dict):
            raise ToolFailure("run_solver answered without data")
        # The executor moves the run's verdict and message out of ``data`` into the response.
        ok = bool(response.ok)
        summary = self._summary(args, data, ok, response.error, self._spec(ctx))
        run = _Run(args.solver, args.n_procs, ok, data, summary)
        self._last[attempt_dir] = run
        try:
            ctx.run.write_text(f"{rel}/{SUMMARY_FILE}", json.dumps(run.summary, indent=2) + "\n")
        except (RunDirError, OSError) as exc:
            raise ToolFailure(f"could not write {SUMMARY_FILE}: {type(exc).__name__}") from exc
        return self._summary_text(run)

    @staticmethod
    def _residuals(value: Any) -> dict[str, float]:
        if not isinstance(value, dict):
            return {}
        return {str(k): float(v) for k, v in value.items() if _number(v) is not None}

    @classmethod
    def _summary(
        cls, args: RunSolverToolArgs, data: dict, ok: bool, error: str | None, spec: CaseSpec
    ) -> dict[str, Any]:
        steady = args.solver in STEADY_SOLVERS
        converged_at = _number(data.get("converged_at"))
        fatal = data.get("fatal_error")
        courant = _number(data.get("max_courant"))
        iterations = data.get("max_linear_iterations")
        bounding = data.get("bounding_count")
        warnings = []
        if not steady and courant is not None and courant > COURANT_WARNING:
            warnings.append(
                f"The maximum Courant number was {courant:.3g}, far above the order of 1 that a "
                "fixed time step needs: the result may be finite and still wrong (a value above 1 "
                "is legitimate only with several outer correctors or an adjustable time step "
                "that holds maxCo)."
            )
        if isinstance(iterations, int) and not isinstance(iterations, bool):
            if iterations >= MAX_ITER_DEFAULT:
                warnings.append(
                    f"A linear solve ran {iterations} iterations (the default limit maxIter is "
                    f"{MAX_ITER_DEFAULT}): it stopped without reaching its tolerance."
                )
        if isinstance(bounding, int) and not isinstance(bounding, bool):
            if bounding > BOUNDING_WARNING:
                warnings.append(
                    f"OpenFOAM printed {bounding} 'bounding' messages: a field left its physical "
                    "range and was clipped."
                )
        return {
            "solver": args.solver,
            "n_procs": args.n_procs,
            "ok": ok,
            "completed": bool(data.get("completed")),
            "diverged": bool(data.get("diverged")),
            "timed_out": bool(data.get("timed_out")),
            "steady": steady,
            "time_dependence": spec.time_dependence,
            "steady_expected": steady or spec.time_dependence == "steady",
            "stopped_by_residual_control": (converged_at is not None) if steady and ok else None,
            "converged_at": converged_at,
            "first_time": _number(data.get("first_time")),
            "end_time": _number(data.get("end_time")),
            "case_end_time": _number(data.get("case_end_time")),
            "last_step": _number(data.get("last_step")),
            "delta_t": _number(data.get("delta_t")),
            "max_courant": courant,
            "initial_residuals": cls._residuals(data.get("initial_residuals")),
            "first_initial_residuals": cls._residuals(data.get("first_initial_residuals")),
            "max_linear_iterations": iterations if isinstance(iterations, int) else None,
            "bounding_count": bounding if isinstance(bounding, int) else None,
            "warnings": warnings,
            "fatal_error": _clip(str(fatal)) if fatal else None,
            "error": _clip(str(error)) if error and not ok else None,
        }

    @staticmethod
    def _summary_text(run: _Run) -> str:
        s = run.summary
        ranks = f"{run.n_procs} rank" + ("s" if run.n_procs != 1 else "")
        end, case_end = s["end_time"], s["case_end_time"]
        end_text = "unknown" if end is None else f"{end:g}"
        if run.ok:
            if s["stopped_by_residual_control"]:
                head = f"{run.solver} on {ranks}: finished; it stopped itself at time {end_text}."
            else:
                head = f"{run.solver} on {ranks}: finished and reached the end time {end_text}."
        else:
            reason = (s["error"] or "the run failed without a message").partition("\n")[0]
            head = f"{run.solver} on {ranks}: FAILED. {_clip(reason)}"
        lines = [head]
        times = []
        if s["first_time"] is not None:
            times.append(f"first time {s['first_time']:g}")
        if end is not None:
            times.append(f"last time {end:g}")
        if case_end is not None:
            times.append(f"endTime of the case {case_end:g}")
        if s["last_step"] is not None:
            times.append(f"last step {s['last_step']:g}")
        if times:
            lines.append("Time: " + ", ".join(times) + ".")
        if s["max_courant"] is not None:
            lines.append(f"Maximum Courant number: {s['max_courant']:.3g}.")
        elif s["steady"]:
            lines.append("Maximum Courant number: steady solver: not printed.")
        else:
            lines.append("Maximum Courant number: no Courant number in the log.")
        last, first = s["initial_residuals"], s["first_initial_residuals"]
        if last:
            shown = ", ".join(f"{k} {v:.3g}" for k, v in sorted(last.items()))
            started = ", ".join(f"{k} {v:.3g}" for k, v in sorted(first.items()))
            lines.append(
                f"Initial residuals of the last iteration (last solve of each field): {shown}"
                + (f"; first iteration: {started}." if started else ".")
            )
        else:
            lines.append("Initial residuals: no initial residuals in the log.")
        lines.extend(SolverRunnerStage._convergence_lines(run))
        if s["warnings"]:
            lines.append("Warnings:")
            lines.extend(f"- {w}" for w in s["warnings"])
        if s["fatal_error"] and s["fatal_error"] not in lines[0]:
            lines.append(f"OpenFOAM reported: {s['fatal_error']}")
        if s["diverged"]:
            lines.append("The solution diverged (non-finite values or very large residuals).")
        if s["timed_out"]:
            lines.append("The run hit the time limit and was killed.")
        tail = run.data.get("tail")
        if isinstance(tail, str) and tail.strip():
            lines.append("End of the log:\n" + _tail(tail, SUMMARY_TAIL_BYTES))
        return "\n".join(lines)

    @staticmethod
    def _convergence_lines(run: _Run) -> list[str]:
        s = run.summary
        lines = []
        if s["steady"]:
            if s["stopped_by_residual_control"]:
                lines.append(
                    f"Steady run: the solver stopped itself at iteration {s['converged_at']:g} "
                    "because the residualControl criteria in case/system/fvSolution were met."
                )
            elif run.ok:
                lines.append(
                    "Steady run: No residualControl criterion ended the run (none set, or not met "
                    "before the end time); judge convergence by the initial residuals above and "
                    "by residualControl in case/system/fvSolution."
                )
            if s["time_dependence"] == "transient":
                lines.append(
                    f"Note: the plan says time_dependence 'transient' but {run.solver} is a "
                    "steady-state solver; its result is a steady state, not a time history."
                )
        elif s["steady_expected"] and run.ok:
            lines.append(
                f"The plan expects a steady state, but {run.solver} is a transient solver with no "
                "stopping test and ran to endTime: compare the initial residuals of the last step "
                "with those of the first step above (a steady state shows them falling by orders "
                "of magnitude and staying low)."
            )
        elif not s["steady_expected"]:
            lines.append("Transient run: no convergence criterion applies; it ends at endTime.")
        return lines

    @staticmethod
    def _failure_text(run: _Run) -> str:
        s = run.summary
        reason = s["error"] or "the run failed without a message"
        headline, _, rest = reason.partition("\n")
        message = f"the {run.solver} run on {run.n_procs} rank(s) failed: {_clip(headline)}"
        if s["fatal_error"] and s["fatal_error"] not in headline:
            message += f"\nOpenFOAM reported: {s['fatal_error']}"
        tail = run.data.get("tail")
        if not isinstance(tail, str) or not tail.strip():
            tail = rest
        if tail.strip():
            message += f"\nEnd of the solver output:\n{_tail(tail, TAIL_BYTES)}"
        return message

    # ------------------------------------------------------------------ read_solver_log

    def _log_path(self, ctx: StageContext, attempt_dir: Path, run: _Run) -> Path:
        rel = ctx.run.relative(attempt_dir)
        for name in (f"{LOG_FILE_PREFIX}{run.solver}", f"{LOG_FILE_PREFIX}decomposePar"):
            path = ctx.run.path(f"{rel}/{CASE_NAME}/{name}")
            if path.is_file():
                return path
        raise AgentInputError(
            "the run wrote no log file (it stopped before the solver started); the output of "
            "run_solver shows what there is"
        )

    def _read_log(self, ctx: StageContext, attempt_dir: Path, args: ReadSolverLogToolArgs) -> str:
        run = self._last.get(attempt_dir)
        if run is None:
            raise AgentInputError("there is no solver run in this attempt yet: call run_solver")
        if args.offset_lines < 0:
            raise AgentInputError(f"offset_lines must be 0 or more (got {args.offset_lines})")
        if not 1 <= args.n_lines <= MAX_LOG_LINES:
            raise AgentInputError(
                f"n_lines must be between 1 and {MAX_LOG_LINES} (got {args.n_lines}); call again "
                "with a larger offset_lines to read further"
            )
        path = self._log_path(ctx, attempt_dir, run)
        first = args.offset_lines
        last = first + args.n_lines
        shown: list[str] = []
        used = 0
        stopped_at: int | None = None  # the line at which the byte budget ran out
        total = 0
        scanned = 0
        complete = True
        with open(path, "rb") as handle:
            for number, (raw, cut) in enumerate(_lines(handle, LINE_BYTES), start=1):
                total = number
                scanned += len(raw) + 1
                if first < number <= last and stopped_at is None:
                    text = raw.decode("utf-8", "replace").replace("\x00", "?")
                    entry = f"{number}: {text}" + (" [line cut]" if cut else "")
                    size = len(entry.encode("utf-8")) + 1
                    if used + size > LOG_PAGE_BYTES:
                        stopped_at = number
                    else:
                        shown.append(entry)
                        used += size
                if scanned > SCAN_BYTES:
                    complete = False
                    break
        if not shown and stopped_at is None:
            if total == 0:
                raise AgentInputError("the log is empty")
            raise AgentInputError(
                f"offset_lines {first} is at or beyond the end of the log, which has {total} lines"
            )
        count = f"{total}" if complete else f"at least {total}"
        shown_last = first + len(shown)
        header = f"{path.name}, lines {first + 1}-{shown_last} of {count}:"
        footer = ""
        if stopped_at is not None:
            footer = (
                f"\n[output limit reached; continue with read_solver_log(offset_lines="
                f"{shown_last}, n_lines=...)]"
            )
        elif shown_last < total:
            footer = f"\n[more lines follow; continue with offset_lines={shown_last}]"
        return header + "\n" + "\n".join(shown) + footer
