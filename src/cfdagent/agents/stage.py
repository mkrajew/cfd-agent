"""The ``Stage`` contract and the ``StageContext`` a stage loop runs in.

A stage is one step of the fixed pipeline (design 6.2): an agent with a system prompt and tools,
a deterministic gate that judges the files the agent produced, and a promotion of the accepted
attempt to its canonical place in the run directory.
"""

from __future__ import annotations

import os
import shutil
import stat
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from cfdagent.agents.tools import FileScope, Tool
from cfdagent.agents.transcript import TranscriptWriter
from cfdagent.config import RunConfig
from cfdagent.gates import GateResult
from cfdagent.llm.client import ChatBackend
from cfdagent.llm.cost import CostTracker
from cfdagent.prompts import load_prompt
from cfdagent.record import RunRecord
from cfdagent.rundir import RunDir, RunDirError, UnsafePath
from cfdagent.sandbox.client import SandboxClient

if TYPE_CHECKING:
    from cfdagent.agents.casespec import CaseSpec

MIN_INNER_TIMEOUT_S = 1.0  # sandbox calls refuse a timeout of zero or less
# The plan as a script finds it: scripts run with an attempt directory (``attempts/<stage>-<n>``)
# as their working directory, and ``casespec.json`` sits in the run directory two levels above.
CASESPEC_FROM_ATTEMPT = "../../casespec.json"
MAX_CARRIED_LISTED = 30  # carried files named in a retry message; the rest is counted


@dataclass
class StageContext:
    """Everything a stage loop needs.

    ``started_at`` is a reading of ``clock`` taken when the whole RUN began; it is required on
    purpose. The wall-clock budget (``limits.wall_clock_s``) is measured from it, so build one
    context per run (the orchestrator makes a per-stage copy with ``dataclasses.replace`` when it
    needs to swap a field). A context created afresh for every stage would restart the two-hour
    budget at each stage.

    ``clock`` and ``sleep`` are injectable for tests. ``on_progress`` is called with the stage
    name after each attempt so the orchestrator can save the run record; an error it raises
    propagates (as ``ToolFailure`` if it is an ``AgentInputError``).
    """

    run: RunDir
    config: RunConfig
    backend: ChatBackend
    sandbox: SandboxClient
    cost: CostTracker
    record: RunRecord
    transcript: TranscriptWriter
    started_at: float
    casespec: CaseSpec | None = None
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    on_progress: Callable[[str], None] | None = None

    def remaining_s(self) -> float:
        """Seconds left of the run's wall-clock budget (never negative)."""
        used = self.clock() - self.started_at
        return max(0.0, self.config.limits.wall_clock_s - used)

    def inner_timeout(self, requested_s: float) -> float:
        """An op's own time limit, clipped to the time left in the run (at least one second).

        Pass the result through ``sandbox.client.outer_timeout`` for the call's timeout.
        """
        return max(MIN_INNER_TIMEOUT_S, min(float(requested_s), self.remaining_s()))


def carried_files_text(carried: list[str]) -> str:
    """The sentence that tells the agent which files of the failed attempt are already in the new
    attempt directory, and that only the reported problems need fixing (long lists are cut)."""
    shown = ", ".join(carried[:MAX_CARRIED_LISTED])
    if len(carried) > MAX_CARRIED_LISTED:
        shown += f" and {len(carried) - MAX_CARRIED_LISTED} more"
    return (
        "The files you wrote in the previous attempt are already in the new directory "
        f"(copied there): {shown}. Only the problems reported above need fixing."
    )


class Stage(ABC):
    """One pipeline stage. Subclasses set ``name`` (letters, digits, underscores), ``agent`` (a
    key of ``RunConfig.models`` and the prompt file name) and ``scope`` (file access)."""

    name: str
    agent: str
    scope: FileScope

    def system_prompt(self) -> str:
        """The agent's system prompt; by default the text of ``prompts/<agent>.md``."""
        return load_prompt(self.agent).text

    @abstractmethod
    def opening_message(self, ctx: StageContext) -> str:
        """The first user message: the task for this stage."""

    def prepare_attempt(self, ctx: StageContext, attempt_dir: Path) -> None:
        """Put the stage's inputs into the fresh, empty attempt directory (``attempt_dir`` is
        absolute). Called by the loop for every attempt, right after the directory is created.
        A missing input means the pipeline order is broken: raise ``ToolFailure``, it is not the
        agent's fault. The default does nothing."""
        return None

    def carry_over(self, ctx: StageContext, previous_dir: Path, attempt_dir: Path) -> list[str]:
        """Seed the new attempt directory with the agent's files of the failed attempt (repair
        mode ``in_place``, ADR-0005). Called by the loop right after ``prepare_attempt``, for the
        second and later attempts of a pass only.

        Copies every regular file of ``previous_dir`` that this stage's scope lets the agent write
        (writable and not excluded, judged by its path in the new directory); the stage's inputs
        come from ``prepare_attempt``, never from here. A symbolic link, a path through one, a
        hidden file and anything else that is not a regular file is left out. Returns the copied
        files as run-relative paths of the new directory, sorted.
        """
        run = ctx.run
        source_rel = run.relative(previous_dir)
        target_rel = run.relative(attempt_dir)
        carried: list[str] = []
        try:
            top = run.path(source_rel)
            # A linked directory is not entered (``followlinks=False``) and a linked file fails
            # ``run.path`` below, so nothing outside the run directory can be copied.
            for directory, _names, files in os.walk(top, followlinks=False):
                for name in sorted(files):
                    inside = Path(directory, name).relative_to(top).as_posix()
                    target = f"{target_rel}/{inside}"
                    try:
                        if not self.scope.can_write(target):
                            continue
                        source = run.path(f"{source_rel}/{inside}")  # refuses links on the way
                        if not stat.S_ISREG(os.lstat(source).st_mode):
                            continue
                        destination = run.path(target)
                    except UnsafePath:
                        continue
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, destination, follow_symlinks=False)
                    carried.append(target)
        except OSError as exc:
            raise RunDirError(f"cannot copy {source_rel!r} ({type(exc).__name__})") from None
        return sorted(carried)

    def extra_tools(self, ctx: StageContext, attempt_dir: Path) -> list[Tool]:
        """Stage-specific tools, built afresh for every attempt (``attempt_dir`` is absolute)."""
        return []

    @abstractmethod
    def execute_and_gate(self, ctx: StageContext, attempt_dir: Path) -> GateResult:
        """Run the deterministic checks on the files in ``attempt_dir``. Errors the agent can
        fix come back as a failed ``GateResult``; anything else propagates."""

    def diagnosis_request(self, ctx: StageContext, errors: list[str]) -> str | None:
        """Ask the agent for the cause of a failed attempt. Called by the loop after every failed
        gate, the last attempt's included; ``errors`` are the gate's errors. Return the question
        for the agent (one extra model turn without tools, whose text is stored as the attempt's
        ``diagnosis``) or ``None`` for no question. The default asks nothing."""
        return None

    def retry_after_failure(self, errors: list[str]) -> bool:
        """Whether another attempt can fix a failure (``errors`` are the gate's). A stage whose
        failures are deterministic and outside the agent's reach returns ``False``: the loop then
        ends the stage after the diagnosis. The default is ``True``."""
        return True

    def passed_text(self, result: GateResult) -> str:
        """What the gate found in order, shown after the errors of a failed gate (the loop adds
        it to the check result of every failed attempt). Empty by default."""
        return ""

    def retry_text(self, previous: str, carried: list[str] | None = None) -> str:
        """What the agent is told about the new attempt directory after a failed gate
        (``previous`` is the failed attempt's directory, relative to the run directory).
        ``carried`` lists the files ``carry_over`` copied into the new directory; with it the
        text says they are already there."""
        if carried:
            return (
                f"{carried_files_text(carried)} Leave everything else as it is and add any file "
                f"that is missing. The previous attempt stays readable in {previous}. Call "
                "check_stage again when done."
            )
        return (
            "Write the corrected files there; the files of the previous attempt stay readable in "
            f"{previous}. Call check_stage again when done."
        )

    @abstractmethod
    def promote(self, ctx: StageContext, attempt_dir: Path) -> None:
        """Copy the accepted attempt to its canonical place in the run directory."""
