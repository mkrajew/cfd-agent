"""Run configuration: limits, per-agent models, sandbox and run identity.

Nothing secret lives here. The model endpoint is read from the environment when a run starts
(``RunConfig.llm_settings_from_env``) and is never part of the configuration, so a configuration
can be stored in the run record without leaking where the models are served.
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cfdagent.llm.config import LLMSettings, ModelConfig
from cfdagent.sandbox.config import SandboxConfig

AGENT_NAMES = ("planner", "geometry", "mesh", "case_setup", "solver_runner", "analyst")
DEFAULT_DEPLOYMENT = "gpt-oss-120b"
# Case Setup writes a whole set of dictionaries and checks them before its attempt ends; two runs
# of M1 used all 40 turns without reaching a passing check.
DEFAULT_TURNS_BY_STAGE = {"case_setup": 60}
# At reasoning effort ``high`` the reasoning can use the whole structured-call limit and cut the
# JSON reply off, so the Planner's structured call gets more room.
HIGH_EFFORT_STRUCTURED_TOKENS = 8192
DEFAULT_IMAGE = "cfdagent:runtime"  # the shipped image (target `runtime`), not the test image
MAX_SLUG_CHARS = 40
MAX_RANKS_LIMIT = 4  # pipeline invariant: never more than four MPI ranks

_NOT_SLUG = re.compile(r"[^a-z0-9]+")
# ``YYYYMMDD-HHMMSS-<slug>``; match with ``fullmatch`` (``$`` would accept a trailing newline).
RUN_ID_PATTERN = re.compile(r"\d{8}-\d{6}-[a-z0-9-]+")


class RunLimits(BaseModel):
    """Per-run limits (design 6.5). Every value is finite and positive."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_attempts_per_stage: int = Field(default=3, ge=1, le=100)
    wall_clock_s: float = 7200
    max_cost_usd: float = 1.0
    max_ranks: int = Field(default=MAX_RANKS_LIMIT, ge=1, le=MAX_RANKS_LIMIT)
    max_turns_per_attempt: int = Field(default=40, ge=1, le=1000)
    # Per stage name: replaces ``max_turns_per_attempt`` for that stage. The cost cap is not
    # affected by it.
    max_turns_per_attempt_by_stage: dict[str, Annotated[int, Field(ge=1, le=1000)]] = Field(
        default_factory=dict
    )
    max_cells: int = Field(default=2_000_000, ge=1)
    max_runtime_minutes: float = 90
    max_memory_gb: float = 12

    @field_validator("wall_clock_s", "max_cost_usd", "max_runtime_minutes", "max_memory_gb")
    @classmethod
    def _finite_and_positive(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            raise ValueError("must be a finite number greater than zero")
        return value


class RunConfig(BaseModel):
    """Everything that decides how a run behaves, except secrets and the model endpoint."""

    model_config = ConfigDict(extra="forbid")

    limits: RunLimits = Field(default_factory=RunLimits)
    models: dict[str, ModelConfig]
    sandbox: SandboxConfig
    benchmark_id: str | None = None
    task_set: Literal["development", "generality", "ad hoc"] = "ad hoc"
    # Token limit of the Planner's structured (JSON) call; the other calls use ModelConfig.
    planner_structured_max_tokens: int = Field(default=4096, ge=1)
    # How a retry of a failed stage starts (ADR-0005): ``fresh`` is an empty attempt directory,
    # ``in_place`` one seeded with the files the agent wrote in the failed attempt.
    repair_mode: Literal["fresh", "in_place"] = "fresh"

    @model_validator(mode="after")
    def _models_cover_exactly_the_agents(self) -> Self:
        if set(self.models) != set(AGENT_NAMES):
            raise ValueError(f"models must be configured for exactly these agents: {AGENT_NAMES}")
        return self

    @classmethod
    def default(cls, image: str = DEFAULT_IMAGE) -> Self:
        """All six agents on the primary model with medium reasoning effort."""
        models = {
            name: ModelConfig(
                deployment=DEFAULT_DEPLOYMENT, reasoning_effort="medium", max_tokens=8192
            )
            for name in AGENT_NAMES
        }
        limits = RunLimits(max_turns_per_attempt_by_stage=dict(DEFAULT_TURNS_BY_STAGE))
        return cls(limits=limits, models=models, sandbox=SandboxConfig(image=image))

    @staticmethod
    def llm_settings_from_env() -> LLMSettings:
        """Endpoint and auth mode from the environment, with a single SDK retry.

        A retried call that stalled can be billed again without the cost tracker seeing it, so a
        cost-capped run allows one retry only.
        """
        settings = LLMSettings.from_env()
        return settings.model_copy(update={"max_retries": 1})


def slugify(text: str) -> str:
    """Lowercase ``[a-z0-9-]`` slug of at most 40 characters; ``run`` if nothing is left."""
    slug = _NOT_SLUG.sub("-", text.lower()).strip("-")[:MAX_SLUG_CHARS].strip("-")
    return slug or "run"


def is_run_id(text: object) -> bool:
    """True when ``text`` has the shape of a run id; it is then safe as one path component."""
    return isinstance(text, str) and RUN_ID_PATTERN.fullmatch(text) is not None


def make_run_id(slug: str, now: datetime) -> str:
    """``YYYYMMDD-HHMMSS-<slug>`` for ``now`` (the caller chooses the time zone)."""
    return f"{now:%Y%m%d-%H%M%S}-{slugify(slug)}"
