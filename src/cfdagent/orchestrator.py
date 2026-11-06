"""The orchestrator: ``run_task`` drives one task through the fixed pipeline (design 6.2).

``run_task`` is the core API; the command line only calls it. A run is:

1. Validate the request (benchmark, prices, model backend), then create the run directory
   ``<run_root>/<run-id>/`` (an existing directory is refused before anything is written), the run
   record, ``task.md``, ``status.json`` and ``run.log``.
2. Hold the deferral marker ``$CFDAGENT_STATE_DIR/active-run`` (default ``/var/lib/cfdagent``)
   for the whole run, so that the VM's daily shutdown waits (``infra/vm/idle-shutdown.sh`` looks
   only at the file's modification time, which the heartbeat refreshes; the content is the process
   id). A state directory that cannot be written is a warning in ``run.log``, not an error.
3. Run the pipeline steps in order: Planner, Geometry, Mesh, Case Setup, Solver Runner, Analyst.
   A pipeline is a list of step factories, so a later milestone can insert the Critic and the fix
   loops. The first step that fails ends the run ``failed`` with ``failed_stage`` and
   ``failure_reason``; a Planner reason that starts with ``exceeds resources:`` is such a normal
   result. Limits end the run ``failed`` and name the limit (cost, wall clock, disk).
   ``KeyboardInterrupt`` and SIGTERM end it ``interrupted``; ToolFailure and every other
   uncaught exception end it ``failed`` with ``internal_error`` (the executor dying is the
   platform's problem and never a retryable gate failure).
4. Only when every step passed and the configuration names a benchmark: the Scorer. It validates
   ``results/quantities.json``, scores it against the benchmark's reference data and writes
   ``results/comparison.json`` and ``record.comparison``. A scoring problem is stated in the
   comparison; it never causes a retry and never changes the run status.
5. Always, in a ``finally``: stop the watchdog, write the final record, ``status.json`` and
   ``summary.md``, upload everything produced so far (when asked) and release the marker. A
   failing writer or upload is logged and never masks the outcome of the run.

Blind evaluation. For a benchmark run the task text is the benchmark's ``task.md`` and only that
text goes to the agents. The benchmark id and task set appear in no file an agent can read while
the run is going: not in the record on disk (see ``record``), ``status.json``, ``run.log`` or
``task.md``, and the run id is made from the task's first line, not from the benchmark id.
Reference files are opened only inside the Scorer step.

Operational notes. The active-run marker is one file per VM: one run at a time is assumed. The
default run root is ``~/runs``; the command line (Task 21) wires ``CFDAGENT_RUNS_DIR`` to
``run_root``. ``SandboxClient.kill_active`` in local mode kills only the client process of an
executor (local mode is test-only); in docker mode it kills the named containers.

Environment variables: ``CFDAGENT_STATE_DIR`` (marker directory), ``CFDAGENT_STORAGE_ACCOUNT_URL``
(the Blob account URL the upload uses; without it the upload is skipped with a note) and the
model variables read by ``RunConfig.llm_settings_from_env``.

Disk watchdog. A small thread samples the size of the run directory every few seconds. Above
``MAX_RUN_DIR_BYTES`` it kills the run's executor containers; the run then ends ``failed`` with
"run directory exceeded <cap>". The thread never raises into the run and is stopped in the
``finally``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import signal
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import cfdagent
from cfdagent.agents.analyst import (
    QUANTITIES_MAX_BYTES,
    SOLVER_SUMMARY,
    AnalystStage,
)
from cfdagent.agents.case_setup import CaseSetupStage
from cfdagent.agents.casespec import CaseSpec
from cfdagent.agents.geometry import GeometryStage
from cfdagent.agents.loop import StageOutcome, internal, run_stage
from cfdagent.agents.mesh import MeshStage
from cfdagent.agents.planner import CASESPEC_FILE, run_planner
from cfdagent.agents.solver_runner import SolverRunnerStage
from cfdagent.agents.stage import Stage, StageContext
from cfdagent.agents.transcript import TranscriptWriter
from cfdagent.bench.registry import Benchmark, load_registry
from cfdagent.bench.scoring import score
from cfdagent.config import RunConfig, make_run_id, slugify
from cfdagent.llm.client import ChatBackend, LLMClient
from cfdagent.llm.cost import PRICING, CostLimitExceeded, CostTracker, Pricing
from cfdagent.prompts import load_prompt
from cfdagent.quantities import validate_quantities
from cfdagent.record import RunRecord, redact, write_record, write_summary
from cfdagent.rundir import RunDir, RunDirError
from cfdagent.sandbox.client import SandboxClient
from cfdagent.storage.blob import UploadReport, upload_run

STATE_DIR_ENV = "CFDAGENT_STATE_DIR"
DEFAULT_STATE_DIR = "/var/lib/cfdagent"
STORAGE_URL_ENV = "CFDAGENT_STORAGE_ACCOUNT_URL"
ACTIVE_RUN_FILE = "active-run"
STATUS_FILE = "status.json"
LOG_FILE = "run.log"
TASK_FILE = "task.md"
QUANTITIES_FILE = "results/quantities.json"
COMPARISON_FILE = "results/comparison.json"
MAX_RUN_DIR_BYTES = 20 * 1024**3
WATCHDOG_INTERVAL_S = 10.0
MAX_LOG_REASON_CHARS = 300
MAX_PROBLEM_CHARS = 1000
MAX_NOTE_ITEMS = 5


class RunExistsError(FileExistsError):
    """The run directory of the new run exists already (two runs with one id)."""


class _Terminated(KeyboardInterrupt):
    """Raised by the SIGTERM handler, so that cleanup code written for Ctrl-C also runs."""


# ---------------------------------------------------------------------------------- pipeline


class Step(ABC):
    """One entry of the pipeline: a name and a way to run on the shared ``StageContext``."""

    name: str

    @abstractmethod
    def run(self, ctx: StageContext, task_text: str) -> StageOutcome:
        """Run the step. A failed ``StageOutcome`` ends the run; an exception does too."""

    def knowledge_reads(self) -> list[str]:
        """Knowledge-base paths the step's agent asked to read (for the run record)."""
        return []


class PlannerStep(Step):
    """The Planner is a function, not a ``Stage``; this wraps it so the list is uniform."""

    name = "planner"

    def run(self, ctx: StageContext, task_text: str) -> StageOutcome:
        outcome = run_planner(ctx, task_text)
        if outcome.ok:  # casespec.json on disk is the source of truth for the later stages
            with internal("loading casespec.json"):
                data = ctx.run.read_json(CASESPEC_FILE)
            ctx.casespec = CaseSpec.model_validate(data)
        return outcome


class GatedStep(Step):
    """A ``Stage`` run by the gated stage loop."""

    def __init__(self, stage: Stage) -> None:
        self.stage = stage
        self.name = stage.name

    def run(self, ctx: StageContext, task_text: str) -> StageOutcome:
        return run_stage(self.stage, ctx)

    def knowledge_reads(self) -> list[str]:
        return list(getattr(self.stage, "knowledge_reads", []))


StepFactory = Callable[[], Step]
Pipeline = list[StepFactory]


def default_pipeline() -> Pipeline:
    """Planner, Geometry, Mesh, Case Setup, Solver Runner, Analyst (a fresh stage per run)."""
    return [
        PlannerStep,
        lambda: GatedStep(GeometryStage()),
        lambda: GatedStep(MeshStage()),
        lambda: GatedStep(CaseSetupStage()),
        lambda: GatedStep(SolverRunnerStage()),
        lambda: GatedStep(AnalystStage()),
    ]


# ---------------------------------------------------------------------------------- results


@dataclass(frozen=True)
class RunResult:
    run_id: str
    status: str
    run_dir: Path
    record: RunRecord
    comparison: dict | None


# ---------------------------------------------------------------------------------- helpers


def directory_size(root: Path) -> int:
    """Total size in bytes of the regular files under ``root``; links are not followed, entries
    that cannot be read count as zero, a missing directory is empty."""
    total = 0
    stack = [Path(root)]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _format_bytes(count: int) -> str:
    for unit, size in (("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)):
        if count >= size:
            return f"{count / size:g} {unit}"
    return f"{count} B"


def _pricing(config: RunConfig) -> Pricing:
    """One price for the run's tracker: the highest of the configured models, per direction."""
    prices = []
    for agent, model in config.models.items():
        price = PRICING.get(model.deployment)
        if price is None:
            raise ValueError(
                f"no price is known for the deployment {model.deployment!r} (agent {agent!r}); "
                "add it to llm.cost.PRICING"
            )
        prices.append(price)
    # Conservative on purpose: a mixed-model run is priced at the dearest model in each direction,
    # so the cost is overestimated and the cap trips early rather than late.
    return Pricing(
        input_per_m=max(p.input_per_m for p in prices),
        output_per_m=max(p.output_per_m for p in prices),
    )


def _slug_source(task_text: str) -> str:
    for line in task_text.splitlines():
        if line.strip():
            return line
    return "run"


def _exception_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def residual_control_line(run: RunDir) -> str:
    """One sentence for ``summary.md``: was the solver's residual control met?

    Read from ``logs/solver_run.json``. The Analyst's gate only looks for the word "convergence",
    so this line is the plain statement for steady runs. A transient run, a missing or unreadable
    summary and a run whose solver did not finish are stated as such.
    """
    prefix = "Residual control: "
    try:
        data = run.read_json(SOLVER_SUMMARY)
    except (RunDirError, OSError):
        data = None
    if not isinstance(data, dict):
        return prefix + "not applicable (no solver summary is available)."
    solver = str(data.get("solver") or "the solver")[:60]
    if not data.get("steady_expected"):
        return (
            prefix + "not applicable (transient run: residual control belongs to steady solvers; "
            "see analysis.md for how the final state was judged)."
        )
    if not data.get("steady"):
        return (
            prefix + f"not available ({solver} is a transient solver and has no residual control: "
            "convergence was not tested by the solver; whether the flow reached a steady state "
            "is judged from the change of the results between the last write times, see "
            "analysis.md)."
        )
    if data.get("ok") is not True or data.get("stopped_by_residual_control") is None:
        return prefix + "unknown (the solver run did not finish)."
    residuals = data.get("initial_residuals")
    shown = ""
    if isinstance(residuals, dict) and residuals:
        parts = []
        for name, value in list(residuals.items())[:8]:
            number = _number(value)
            if number is not None:
                parts.append(f"{str(name)[:20]} {number:.3g}")
        if parts:
            shown = " Final initial residuals: " + ", ".join(parts) + "."
    end = _number(data.get("end_time"))
    at = f" at time {end:g}" if end is not None else ""
    if data["stopped_by_residual_control"]:
        return (
            prefix
            + f"met: the solver stopped itself{at} because its residualControl was "
            + f"satisfied.{shown}"
        )
    warnings = data.get("warnings")
    warned = (
        f" The solver reported {len(warnings)} warning(s)."
        if isinstance(warnings, list) and warnings
        else ""
    )
    return (
        prefix + f"not met: the run ended{at} and no residualControl criterion ended the run "
        "(none is set, or it was not satisfied), so the result is not shown to be converged by "
        f"the solver's own criterion.{shown}{warned}"
    )


# ---------------------------------------------------------------------------------- watchdog


class _Monitor:
    """A thread that refreshes the heartbeat and watches the size of the run directory."""

    def __init__(
        self,
        root: Path,
        cap_bytes: int,
        interval_s: float,
        size_fn: Callable[[Path], int],
        kill: Callable[[], None],
        heartbeat: Callable[[], None],
    ) -> None:
        self.root, self.cap, self.interval = root, cap_bytes, interval_s
        self.size_fn, self.kill, self.heartbeat = size_fn, kill, heartbeat
        self.exceeded = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="cfdagent-monitor", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.heartbeat()
            except Exception:  # a watchdog never raises into the run
                pass
            try:
                # Once over the cap, keep killing every interval: an executor started after the
                # first kill (the agent was waiting on the model) must not run unwatched.
                if self.exceeded or self.size_fn(self.root) > self.cap:
                    self.exceeded = True  # first, so the run can tell why its executor died
                    self.kill()
            except Exception:
                pass


# ---------------------------------------------------------------------------------- the run


class _Session:
    """The state of one ``run_task`` call."""

    def __init__(
        self,
        *,
        config: RunConfig,
        record: RunRecord,
        run: RunDir,
        run_id: str,
        task_text: str,
        benchmark: Benchmark | None,
        backend: ChatBackend,
        pricing: Pricing,
        pipeline: Pipeline,
        upload: bool,
        uploader: Callable[[RunDir], UploadReport] | None,
        sandbox_factory: Callable[[Any, RunDir, str], Any] | None,
        state_dir: Path | None,
        now: Callable[[], datetime],
        clock: Callable[[], float],
        watchdog_interval_s: float,
        max_run_dir_bytes: int,
        dir_size: Callable[[Path], int],
    ) -> None:
        self.config, self.record, self.run, self.run_id = config, record, run, run_id
        self.task_text, self.benchmark, self.backend = task_text, benchmark, backend
        self.pipeline, self.upload, self.uploader = pipeline, upload, uploader
        self.now, self.clock = now, clock
        self.cap, self.interval, self.dir_size = max_run_dir_bytes, watchdog_interval_s, dir_size
        self.state_dir = state_dir
        self.cost = CostTracker(config.limits.max_cost_usd, pricing)
        factory = sandbox_factory or (lambda cfg, rd, rid: SandboxClient(cfg, rd, rid))
        self.sandbox = factory(config.sandbox, run, run_id)
        self.started_clock = clock()
        self.ctx = StageContext(
            run=run,
            config=config,
            backend=backend,
            sandbox=self.sandbox,
            cost=self.cost,
            record=record,
            transcript=TranscriptWriter(run, self.cost),
            started_at=self.started_clock,
            clock=clock,
            on_progress=self._on_progress,
        )
        self.steps: list[Step] = []
        self.current: str | None = None
        # Pessimistic until the pipeline and the Scorer have returned normally: an exit that is
        # neither an interrupt nor an Exception (SystemExit and the like) never reads "completed".
        self.status = "failed"
        self._failure_set = False
        self.failed_stage: str | None = None
        self.reason: str | None = "internal_error: the run ended abnormally"
        self.comparison: dict | None = None
        self.monitor: _Monitor | None = None
        self._marker: Path | None = None
        self._log_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._finishing = False
        self._terminated = False

    # ------------------------------------------------------------------ files

    def log(self, message: str) -> None:
        """Append one line to ``run.log``; never raises."""
        line = f"{self.now().isoformat(timespec='seconds')} {message}\n"
        with self._log_lock:
            try:
                with open(self.run.path(LOG_FILE), "a", encoding="utf-8") as handle:
                    handle.write(line)
            except Exception:
                pass

    def write_status(self, state: str, stage: str | None) -> None:
        document = {
            "state": state,
            "stage": stage,
            "pid": os.getpid(),
            "heartbeat": self.now().isoformat(),
        }
        with self._status_lock:
            try:
                self.run.write_text(STATUS_FILE, json.dumps(document, indent=2) + "\n")
            except Exception as exc:
                self.log(f"status.json could not be written: {_exception_text(exc)[:200]}")
            if self._marker is not None:
                try:
                    os.utime(self._marker)
                except OSError:
                    pass

    def _heartbeat(self) -> None:
        self.write_status("running", self.current)

    def sync_record(self) -> None:
        record = self.record
        record.tokens_in = self.cost.tokens_in
        record.tokens_out = self.cost.tokens_out
        record.cost_usd = self.cost.total_usd
        record.cost_estimated = self.cost.estimated
        record.wall_seconds = max(0.0, self.clock() - self.started_clock)
        reads = list(record.knowledge_reads)
        for step in self.steps:
            try:
                found = step.knowledge_reads()
            except Exception as exc:  # an audit list must not break the record
                detail = _exception_text(exc)[:200]
                self.log(f"knowledge reads of {step.name} unavailable: {detail}")
                continue
            for path in found:
                if path not in reads:
                    reads.append(path)
        record.knowledge_reads = reads

    def save_record(self) -> None:
        """Sync and write the record; a failure is logged and never raised."""
        try:
            self.sync_record()
            write_record(self.run, self.record)
        except Exception as exc:
            self.log(f"run_record.json could not be written: {_exception_text(exc)[:200]}")

    def _on_progress(self, stage: str) -> None:
        self.save_record()
        self.write_status("running", stage)

    # ------------------------------------------------------------------ marker and signals

    def acquire_marker(self) -> None:
        directory = Path(self.state_dir or os.environ.get(STATE_DIR_ENV) or DEFAULT_STATE_DIR)
        marker = directory / ACTIVE_RUN_FILE
        try:
            directory.mkdir(parents=True, exist_ok=True)
            marker.write_text(f"{os.getpid()}\n", encoding="utf-8")
        except OSError as exc:
            self.log(
                f"warning: the active-run marker could not be written ({type(exc).__name__}); "
                "the VM's daily shutdown will not wait for this run"
            )
            return
        self._marker = marker

    def release_marker(self) -> None:
        if self._marker is None:
            return
        try:
            self._marker.unlink(missing_ok=True)
        except OSError:
            pass
        self._marker = None

    def install_sigterm(self) -> Any:
        if threading.current_thread() is not threading.main_thread():
            return None

        def handler(signum, frame):
            if self._finishing or self._terminated:
                return  # finalising already: let the upload finish
            self._terminated = True
            raise _Terminated

        try:
            return signal.signal(signal.SIGTERM, handler)
        except (ValueError, OSError):
            return None

    @staticmethod
    def restore_sigterm(previous: Any) -> None:
        if previous is None:
            return
        try:
            signal.signal(signal.SIGTERM, previous)
        except (ValueError, OSError):
            pass

    # ------------------------------------------------------------------ the pipeline

    def fail(self, stage: str | None, reason: str, status: str = "failed") -> None:
        self.status, self.failed_stage, self.reason = status, stage, reason
        self._failure_set = True

    def run_pipeline(self) -> None:
        for make in self.pipeline:
            step = make()
            self.steps.append(step)
            name = self.current = step.name
            if self.monitor is not None and self.monitor.exceeded:
                return self.fail(name, f"run directory exceeded {_format_bytes(self.cap)}")
            if self.ctx.remaining_s() <= 0:
                limit = self.config.limits.wall_clock_s
                return self.fail(
                    name, f"wall-clock limit of {limit:.0f} s reached before stage {name!r}"
                )
            self.log(f"stage {name}: start")
            self.write_status("running", name)
            outcome = step.run(self.ctx, self.task_text)
            self.save_record()
            if self.monitor is not None and self.monitor.exceeded:
                self.log(f"stage {name}: stopped, the run directory grew beyond its cap")
                return self.fail(name, f"run directory exceeded {_format_bytes(self.cap)}")
            if not outcome.ok:
                reason = outcome.reason or f"stage {name!r} failed without giving a reason"
                self.log(f"stage {name}: failed ({redact(reason, MAX_LOG_REASON_CHARS)})")
                return self.fail(name, reason)
            self.log(f"stage {name}: ok after {outcome.attempts} attempt(s)")
            self.write_status("running", name)
        self.current = None

    # ------------------------------------------------------------------ the Scorer

    @staticmethod
    def _problem(text: str) -> dict:
        return {"passed": False, "problem": redact(text, MAX_PROBLEM_CHARS)}

    def compare(self) -> dict:
        """Score ``results/quantities.json``; every problem is stated in the returned dict."""
        assert self.benchmark is not None
        try:
            data = self.run.read_json(QUANTITIES_FILE, QUANTITIES_MAX_BYTES)
        except (RunDirError, OSError) as exc:
            return self._problem(f"{QUANTITIES_FILE} cannot be read: {exc}")
        try:
            quantities = validate_quantities(data)
        except ValueError as exc:
            return self._problem(f"{QUANTITIES_FILE} is not usable: {exc}")
        try:
            result = score(self.benchmark, quantities)
        except Exception as exc:
            return self._problem(f"scoring failed: {_exception_text(exc)}")
        return {
            "passed": result.passed,
            "criteria": [dataclasses.asdict(c) for c in result.criteria],
        }

    def run_scorer(self) -> None:
        """Score the quantities (called only after every step passed)."""
        if self.benchmark is None:
            return
        self.current = "scorer"
        self.log("scorer: start")
        try:
            comparison = self.compare()
            text = json.dumps(comparison, indent=2, allow_nan=False) + "\n"  # NaN is not JSON
            self.record.comparison = comparison
        except Exception as exc:  # an odd value must not turn a passed gate into a failed run
            comparison = self._problem(
                f"the comparison could not be recorded: {_exception_text(exc)}"
            )
            text = json.dumps(comparison, indent=2) + "\n"
            self.record.comparison = comparison
        self.comparison = comparison
        self.log("scorer: done" if "criteria" in comparison else "scorer: could not score")
        try:
            self.run.write_text(COMPARISON_FILE, text)
        except Exception as exc:
            self.log(f"comparison.json could not be written: {_exception_text(exc)[:200]}")

    # ------------------------------------------------------------------ the whole run

    def execute(self) -> RunResult:
        previous = self.install_sigterm()
        try:
            try:
                self.log("run: start")
                self.acquire_marker()
                self.monitor = _Monitor(
                    self.run.root,
                    self.cap,
                    self.interval,
                    self.dir_size,
                    getattr(self.sandbox, "kill_active", lambda: None),
                    self._heartbeat,
                )
                self.monitor.start()
                self.run.write_text(TASK_FILE, self.task_text)
                self.save_record()
                self.write_status("running", None)
                self.run_pipeline()
                if not self._failure_set:
                    self.run_scorer()
                    self.status, self.failed_stage, self.reason = "completed", None, None
            except KeyboardInterrupt as exc:
                what = "SIGTERM" if isinstance(exc, _Terminated) else "the user (KeyboardInterrupt)"
                self.fail(self.current, f"interrupted by {what}", "interrupted")
            except CostLimitExceeded as exc:
                self.fail(self.current, f"cost limit: {exc}")
            except Exception as exc:
                if self.monitor is not None and self.monitor.exceeded:
                    self.fail(self.current, f"run directory exceeded {_format_bytes(self.cap)}")
                else:
                    self.fail(self.current, f"internal_error: {_exception_text(exc)}")
            except BaseException as exc:  # SystemExit and the like: record it, then let it go on
                name = type(exc).__name__
                self.fail(self.current, f"internal_error: the run ended abnormally ({name})")
                raise
        finally:
            self._finishing = True
            try:
                self.finish()
            finally:
                self.release_marker()
                self.restore_sigterm(previous)
        return RunResult(self.run_id, self.status, self.run.root, self.record, self.comparison)

    # ------------------------------------------------------------------ the end of the run

    def finish(self) -> None:
        if self.monitor is not None:
            self.monitor.stop()
        record = self.record
        try:
            record.status = self.status  # type: ignore[assignment]
            record.failed_stage = self.failed_stage
            record.failure_reason = self.reason
            record.finished_at = self.now()
            self.sync_record()
        except Exception as exc:  # the summary and the upload must still happen
            self.log(f"the final record could not be prepared: {_exception_text(exc)[:200]}")
        suffix = f" ({redact(self.reason, MAX_LOG_REASON_CHARS)})" if self.reason else ""
        self.log(f"run: {self.status}{suffix}")
        self.write_status(self.status, self.failed_stage)
        self.save_record()
        try:
            write_summary(
                self.run,
                record,
                self.comparison,
                residual_control=residual_control_line(self.run),
            )
        except Exception as exc:
            self.log(f"summary.md could not be written: {_exception_text(exc)[:200]}")
        self.upload_everything()

    def upload_everything(self) -> None:
        if not self.upload:
            return
        note: str | None = None
        try:
            if self.uploader is not None:
                report = self.uploader(self.run)
            else:
                url = os.environ.get(STORAGE_URL_ENV)
                if not url:
                    note = f"Upload skipped: {STORAGE_URL_ENV} is not set."
                    self.log(f"upload skipped: {STORAGE_URL_ENV} is not set")
                    report = None
                else:
                    self.log("upload: start")
                    report = upload_run(self.run, url)
            if report is not None and (report.failed or report.stopped_early):
                failed = ", ".join(report.failed[:MAX_NOTE_ITEMS]) or "(see reasons)"
                more = (
                    ""
                    if len(report.failed) <= MAX_NOTE_ITEMS
                    else f" and {len(report.failed) - MAX_NOTE_ITEMS} more"
                )
                note = (
                    f"Upload incomplete: {report.uploaded} file(s) uploaded; not uploaded: "
                    f"{failed}{more}. The local run directory holds everything."
                )
                self.log(f"upload: incomplete; failed: {failed}{more}")
            elif report is not None:
                self.log(f"upload: done, {report.uploaded} file(s)")
        except Exception as exc:
            note = (
                f"Upload failed ({type(exc).__name__}); the local run directory holds everything."
            )
            self.log(f"upload failed: {type(exc).__name__}")
        if note is not None:
            self.append_summary_note(note)

    def append_summary_note(self, note: str) -> None:
        try:
            text = self.run.read_text("summary.md")
            self.run.write_text("summary.md", f"{text.rstrip()}\n\n## Upload\n\n{note}\n")
        except Exception as exc:
            self.log(f"summary.md could not be extended: {_exception_text(exc)[:200]}")


# ---------------------------------------------------------------------------------- the API


def run_task(
    task: str,
    config: RunConfig,
    *,
    backend: ChatBackend | None = None,
    run_root: Path | None = None,
    upload: bool = True,
    pipeline: Pipeline | None = None,
    uploader: Callable[[RunDir], UploadReport] | None = None,
    sandbox_factory: Callable[[Any, RunDir, str], Any] | None = None,
    benchmarks_root: Path | None = None,
    state_dir: Path | None = None,
    now: Callable[[], datetime] | None = None,
    clock: Callable[[], float] | None = None,
    watchdog_interval_s: float = WATCHDOG_INTERVAL_S,
    max_run_dir_bytes: int = MAX_RUN_DIR_BYTES,
    dir_size: Callable[[Path], int] = directory_size,
) -> RunResult:
    """Run ``task`` through the pipeline; see the module docstring.

    ``task`` is the task text. When ``config.benchmark_id`` is set, the text of the benchmark's
    ``task.md`` is used instead and ``task`` is ignored (blind evaluation). ``backend=None`` builds
    an ``LLMClient`` from the environment. The run directory is ``<run_root>/<run-id>`` (default
    root ``~/runs``). Raises before creating anything for an unknown benchmark, an unpriced model,
    an invalid configuration or an existing run directory (``RunExistsError``).

    ``pipeline``, ``uploader``, ``sandbox_factory``, ``benchmarks_root``, ``state_dir``, ``now``,
    ``clock``, ``watchdog_interval_s``, ``max_run_dir_bytes`` and ``dir_size`` are seams for tests.
    """
    now = now or (lambda: datetime.now(UTC))
    clock = clock or time.monotonic

    benchmark: Benchmark | None = None
    task_text = task
    if config.benchmark_id is not None:
        registry = (
            load_registry(benchmarks_root) if benchmarks_root is not None else load_registry()
        )
        benchmark = registry.get(config.benchmark_id)
        if benchmark is None:
            raise ValueError(f"unknown benchmark {config.benchmark_id!r}")
        task_text = (benchmark.root / benchmark.task_file).read_text(encoding="utf-8")

    pricing = _pricing(config)
    if backend is None:
        backend = LLMClient(RunConfig.llm_settings_from_env())

    started = now()
    run_id = make_run_id(slugify(_slug_source(task_text)), started)
    record = RunRecord(
        run_id=run_id,
        task_set=config.task_set,
        task_sha256=hashlib.sha256(task_text.encode("utf-8")).hexdigest(),
        benchmark_id=config.benchmark_id,
        started_at=started,
        status="running",
        models=dict(config.models),
        prompt_versions={agent: load_prompt(agent).version for agent in config.models},
        image_id=config.sandbox.image,
        cfdagent_version=cfdagent.__version__,
    )

    root = (run_root if run_root is not None else Path.home() / "runs") / run_id
    if root.exists():
        raise RunExistsError(f"the run directory for run {run_id} already exists")
    try:
        root.mkdir(parents=True)
    except FileExistsError:
        raise RunExistsError(f"the run directory for run {run_id} already exists") from None

    session = _Session(
        config=config,
        record=record,
        run=RunDir(root),
        run_id=run_id,
        task_text=task_text,
        benchmark=benchmark,
        backend=backend,
        pricing=pricing,
        pipeline=pipeline if pipeline is not None else default_pipeline(),
        upload=upload,
        uploader=uploader,
        sandbox_factory=sandbox_factory,
        state_dir=state_dir,
        now=now,
        clock=clock,
        watchdog_interval_s=watchdog_interval_s,
        max_run_dir_bytes=max_run_dir_bytes,
        dir_size=dir_size,
    )
    return session.execute()
