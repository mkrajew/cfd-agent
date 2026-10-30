"""The ``Stage`` contract and the ``StageContext`` a stage loop runs in.

A stage is one step of the fixed pipeline (design 6.2): an agent with a system prompt and tools,
a deterministic gate that judges the files the agent produced, and a promotion of the accepted
attempt to its canonical place in the run directory.
"""

from __future__ import annotations

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
from cfdagent.rundir import RunDir
from cfdagent.sandbox.client import SandboxClient

if TYPE_CHECKING:
    from cfdagent.agents.casespec import CaseSpec

MIN_INNER_TIMEOUT_S = 1.0  # sandbox calls refuse a timeout of zero or less


@dataclass
class StageContext:
    """Everything a stage loop needs. ``clock`` and ``sleep`` are injectable for tests;
    ``started_at`` (a reading of ``clock`` at the start of the run) defaults to now.
    ``on_progress`` is called with the stage name after each attempt so the orchestrator can
    save the run record.
    """

    run: RunDir
    config: RunConfig
    backend: ChatBackend
    sandbox: SandboxClient
    cost: CostTracker
    record: RunRecord
    transcript: TranscriptWriter
    casespec: CaseSpec | None = None
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    started_at: float | None = None
    on_progress: Callable[[str], None] | None = None

    def __post_init__(self) -> None:
        if self.started_at is None:
            self.started_at = self.clock()

    def remaining_s(self) -> float:
        """Seconds left of the run's wall-clock budget (never negative)."""
        assert self.started_at is not None
        used = self.clock() - self.started_at
        return max(0.0, self.config.limits.wall_clock_s - used)

    def inner_timeout(self, requested_s: float) -> float:
        """An op's own time limit, clipped to the time left in the run (at least one second).

        Pass the result through ``sandbox.client.outer_timeout`` for the call's timeout.
        """
        return max(MIN_INNER_TIMEOUT_S, min(float(requested_s), self.remaining_s()))


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

    def extra_tools(self, ctx: StageContext, attempt_dir: Path) -> list[Tool]:
        """Stage-specific tools, built afresh for every attempt (``attempt_dir`` is absolute)."""
        return []

    @abstractmethod
    def execute_and_gate(self, ctx: StageContext, attempt_dir: Path) -> GateResult:
        """Run the deterministic checks on the files in ``attempt_dir``. Errors the agent can
        fix come back as a failed ``GateResult``; anything else propagates."""

    @abstractmethod
    def promote(self, ctx: StageContext, attempt_dir: Path) -> None:
        """Copy the accepted attempt to its canonical place in the run directory."""
