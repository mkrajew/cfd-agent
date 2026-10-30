"""The run record (``run_record.json``) and the human-readable ``summary.md``.

The record is the raw data for the hypothesis test: what was asked, which models and prompts
were used, every stage attempt with its errors, tokens, cost and the verdict. It is rewritten
atomically after every stage, so a run that dies still leaves a complete, valid file.

The record never holds a secret or a location: it has no field for an endpoint or an account
URL, unknown fields are refused, and free text that may echo a service error (failure reason,
attempt errors) has URLs, e-mail addresses and IPv4 addresses replaced before it is stored.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cfdagent.llm.config import ModelConfig
from cfdagent.rundir import RunDir

RECORD_FILE = "run_record.json"
SUMMARY_FILE = "summary.md"
NO_REFERENCE_SENTENCE = (
    "No reference data for this task; result assessed by physical checks only "
    "(convergence, mass balance, plausibility)."
)
NO_CRITIC_LINE = "No Critic reviewed this run (the Critic agent is not part of M1)."

MAX_TEXT_CHARS = 4000
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]{0,15}://\S+")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){1,8}")
_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_HEX12 = re.compile(r"[0-9a-f]{12}")


def redact(text: str) -> str:
    """Cut ``text`` to a bounded length and replace URLs, e-mail addresses and IPv4 addresses."""
    text = text[:MAX_TEXT_CHARS]
    text = _URL.sub("<url>", text)
    text = _EMAIL.sub("<email>", text)
    return _IPV4.sub("<ip>", text)


class AttemptRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    n: int = Field(ge=1)
    ok: bool
    errors: list[str] = Field(default_factory=list)
    seconds: float = Field(ge=0)

    @field_validator("errors")
    @classmethod
    def _redact_errors(cls, value: list[str]) -> list[str]:
        return [redact(item) for item in value]


class StageRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    attempts: list[AttemptRecord] = Field(default_factory=list)
    ok: bool


class RunRecord(BaseModel):
    """Everything worth knowing about one run; see the module docstring."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    task_set: Literal["development", "generality", "ad hoc"]
    task_sha256: str
    benchmark_id: str | None = None
    started_at: datetime
    finished_at: datetime | None = None
    status: Literal["running", "completed", "failed", "interrupted"]
    failed_stage: str | None = None
    failure_reason: str | None = None
    models: dict[str, ModelConfig] = Field(default_factory=dict)
    prompt_versions: dict[str, str] = Field(default_factory=dict)  # agent -> 12-hex hash
    image_id: str | None = None
    cfdagent_version: str
    stages: list[StageRecord] = Field(default_factory=list)
    tokens_in: int = Field(default=0, ge=0)
    tokens_out: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    cost_estimated: bool = False
    wall_seconds: float = Field(default=0.0, ge=0)
    critic_verdict: None = None  # filled from M2 on
    comparison: dict[str, Any] | None = None

    @field_validator("failure_reason")
    @classmethod
    def _redact_reason(cls, value: str | None) -> str | None:
        return None if value is None else redact(value)

    @field_validator("prompt_versions")
    @classmethod
    def _hashes_are_12_hex(cls, value: dict[str, str]) -> dict[str, str]:
        for agent, digest in value.items():
            if not _HEX12.fullmatch(digest):
                raise ValueError(f"prompt version of {agent!r} must be 12 lowercase hex digits")
        return value


def write_record(run: RunDir, record: RunRecord) -> None:
    """Write ``run_record.json`` atomically (temporary file, then replace)."""
    run.write_text(RECORD_FILE, record.model_dump_json(indent=2) + "\n")


def read_record(run: RunDir) -> RunRecord:
    return RunRecord.model_validate(run.read_json(RECORD_FILE))


def _cell(value: object) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ")
    return text.replace("|", "\\|")


def _comparison_lines(comparison: dict[str, Any]) -> list[str]:
    criteria = comparison.get("criteria")
    if not isinstance(criteria, list):
        return ["```json", _cell(comparison), "```"]
    verdict = "passed" if comparison.get("passed") else "not passed"
    lines = [
        f"Overall: **{verdict}** against the benchmark's acceptance criteria.",
        "",
        "| Quantity | Result | Error | Threshold | Note |",
        "|---|---|---|---|---|",
    ]
    for item in criteria:
        row = item if isinstance(item, dict) else {}
        result = "pass" if row.get("passed") else "FAIL"
        cells = [
            row.get(key, "") if row.get(key) is not None else "n/a"
            for key in ("quantity", "error", "threshold", "reason")
        ]
        lines.append(
            f"| {_cell(cells[0])} | {result} | {_cell(cells[1])} | {_cell(cells[2])} "
            f"| {_cell(cells[3])} |"
        )
    return lines


def write_summary(run: RunDir, record: RunRecord, comparison: dict[str, Any] | None) -> None:
    """Write ``summary.md``: outcome, stages, cost, and how the result was assessed."""
    lines = [
        f"# Run {record.run_id}",
        "",
        f"- Status: **{record.status}**",
        f"- Task set: {record.task_set}",
    ]
    if record.benchmark_id:
        lines.append(f"- Benchmark: {record.benchmark_id}")
    if record.failed_stage:
        lines.append(f"- Failed stage: {record.failed_stage}")
    if record.failure_reason:
        lines.append(f"- Reason: {_cell(record.failure_reason)}")
    estimated = " (estimated from text length for some calls)" if record.cost_estimated else ""
    lines += [
        f"- Wall time: {record.wall_seconds:.0f} s",
        f"- Tokens: {record.tokens_in} in, {record.tokens_out} out",
        f"- LLM cost: {record.cost_usd:.4f} USD{estimated}",
        "",
        "## Stages",
        "",
    ]
    if record.stages:
        lines += ["| Stage | Attempts | Result |", "|---|---|---|"]
        for stage in record.stages:
            result = "ok" if stage.ok else "failed"
            lines.append(f"| {_cell(stage.name)} | {len(stage.attempts)} | {result} |")
    else:
        lines.append("No stage was completed.")
    lines += ["", "## Assessment", ""]
    if comparison is not None:
        lines += ["Compared with the reference data of the benchmark.", ""]
        lines += _comparison_lines(comparison)
    elif record.benchmark_id:
        lines.append(
            "Benchmark run, but not scored: the run ended before a comparison with the "
            "reference data was produced."
        )
    else:
        lines.append(NO_REFERENCE_SENTENCE)
    lines += ["", NO_CRITIC_LINE, ""]
    run.write_text(SUMMARY_FILE, "\n".join(lines))
