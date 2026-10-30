"""Run configuration: limits, per-agent models, sandbox and run identity.

Nothing secret lives here. The model endpoint is read from the environment when a run starts
(``RunConfig.llm_settings_from_env``) and is never part of the configuration, so a configuration
can be stored in the run record without leaking where the models are served.
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cfdagent.llm.config import LLMSettings, ModelConfig
from cfdagent.sandbox.config import SandboxConfig

AGENT_NAMES = ("planner", "geometry", "mesh", "case_setup", "solver_runner", "analyst")
DEFAULT_DEPLOYMENT = "gpt-oss-120b"
DEFAULT_IMAGE = "cfdagent:dev"
MAX_SLUG_CHARS = 40
MAX_RANKS_LIMIT = 4  # pipeline invariant: never more than four MPI ranks

_NOT_SLUG = re.compile(r"[^a-z0-9]+")


class RunLimits(BaseModel):
    """Per-run limits (design 6.5). Every value is finite and positive."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_attempts_per_stage: int = Field(default=3, ge=1, le=100)
    wall_clock_s: float = 7200
    max_cost_usd: float = 1.0
    max_ranks: int = Field(default=MAX_RANKS_LIMIT, ge=1, le=MAX_RANKS_LIMIT)
    max_turns_per_attempt: int = Field(default=30, ge=1, le=1000)
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

    @classmethod
    def default(cls, image: str = DEFAULT_IMAGE) -> Self:
        """All six agents on the primary model with medium reasoning effort."""
        models = {
            name: ModelConfig(
                deployment=DEFAULT_DEPLOYMENT, reasoning_effort="medium", max_tokens=8192
            )
            for name in AGENT_NAMES
        }
        return cls(models=models, sandbox=SandboxConfig(image=image))

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


def make_run_id(slug: str, now: datetime) -> str:
    """``YYYYMMDD-HHMMSS-<slug>`` for ``now`` (the caller chooses the time zone)."""
    return f"{now:%Y%m%d-%H%M%S}-{slugify(slug)}"
