"""The run record (``run_record.json``) and the human-readable ``summary.md``.

The record is the raw data for the hypothesis test: what was asked, which models and prompts
were used, every stage attempt with its errors, tokens, cost and the verdict. It is rewritten
atomically after every stage, so a run that dies still leaves a complete, valid file.

What the record never holds
---------------------------
* Secrets and locations. There is no field for an endpoint or an account URL and unknown fields
  are refused. Free text that may echo a service error (failure reason, attempt errors) is
  bounded in length and cleaned of: schemed URLs (``scheme://...``), bare Azure host names
  (``*.azure.com``, ``*.azure.net``, ``*.windows.net``, ``*.microsoftonline.com``,
  ``*.azure-api.net``), e-mail addresses, IPv4 addresses, GUIDs, ``Bearer`` tokens and JWT-like
  tokens. This is pattern based and best effort: nothing else (for example a bare IPv6 address)
  is recognised.
* Blind-evaluation information while the run is going. The record file lives in the run
  directory, which agent-written code can read. While ``status == "running"`` the file on
  disk therefore holds ``null`` for ``task_set`` and ``benchmark_id``; the final write (status
  ``completed``, ``failed`` or ``interrupted``, after the Scorer) holds the real values. The
  in-memory record is never changed by this. The same rule binds the callers: the CLI and the
  orchestrator must not put the benchmark id into any file an agent can read (``status.json``,
  ``run.log`` and the like) before the run has finished; ``write_summary`` is for the end of the
  run only.

Both writers first rebuild the record through validation (``model_dump`` then
``model_validate``), so a record mutated in place after its construction (a list appended to,
a field assigned a bad value) is cleaned or rejected before anything reaches the disk.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cfdagent.llm.config import ModelConfig
from cfdagent.rundir import RunDir

RECORD_FILE = "run_record.json"
SUMMARY_FILE = "summary.md"
NO_REFERENCE_SENTENCE = (
    "No reference data for this task; result assessed by physical checks only "
    "(convergence, mass balance, plausibility)."
)
NO_CRITIC_LINE = "No Critic reviewed this run (the Critic agent is not part of M1)."

MAX_TEXT_CHARS = 4000  # failure reason
MAX_ERROR_CHARS = 500  # one attempt error
MAX_ERRORS_PER_ATTEMPT = 50
SOFT_RECORD_BYTES = 1_500_000  # above this the attempt errors are cut down (limit is 2 MB)
SHRUNK_ERRORS = 5
SHRUNK_ERROR_CHARS = 200

_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]{0,15}://\S+")
_AZURE_HOST = re.compile(
    r"[A-Za-z0-9.-]{1,253}\.(?:azure\.com|azure\.net|windows\.net|microsoftonline\.com|"
    r"azure-api\.net)\b",
    re.IGNORECASE,
)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){1,8}")
_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_GUID = re.compile(r"\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b")
_BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,512}", re.IGNORECASE)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{10,2000}(?:\.[A-Za-z0-9_-]{1,2000}){0,2}")
_HEX12 = re.compile(r"[0-9a-f]{12}")
_MARKER = re.compile(r"\[truncated: (\d{1,9}) more\]")
_MD_CELL = re.compile(r"([\\`*\[\]<>|])")
_MD_TEXT = re.compile(r"([\\`*\[\]<>|_#~])")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def redact(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    """Bound ``text`` to ``limit`` characters and clean it as the module docstring says.

    Characters that cannot be encoded (lone surrogates) become ``?``.
    """
    text = text.encode("utf-8", "replace").decode("utf-8")[:limit]
    text = _URL.sub("<url>", text)
    text = _AZURE_HOST.sub("<host>", text)
    text = _EMAIL.sub("<email>", text)
    text = _BEARER.sub("Bearer <token>", text)
    text = _JWT.sub("<token>", text)
    text = _GUID.sub("<guid>", text)
    return _IPV4.sub("<ip>", text)


def _cap_errors(items: list[str], max_items: int, max_chars: int) -> list[str]:
    """Clean and bound a list of errors; excess entries become one ``[truncated: N more]``.

    Idempotent: a marker already at the end of the list is counted, not duplicated.
    """
    omitted = 0
    if items:
        marker = _MARKER.fullmatch(items[-1])
        if marker:
            omitted = int(marker.group(1))
            items = items[:-1]
    cleaned = [redact(item, max_chars) for item in items]
    omitted += max(0, len(cleaned) - max_items)
    cleaned = cleaned[:max_items]
    if omitted:
        cleaned.append(f"[truncated: {omitted} more]")
    return cleaned


class AttemptRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    n: int = Field(ge=1)
    ok: bool
    errors: list[str] = Field(default_factory=list)
    seconds: float = Field(ge=0)

    @field_validator("errors")
    @classmethod
    def _clean_errors(cls, value: list[str]) -> list[str]:
        return _cap_errors(value, MAX_ERRORS_PER_ATTEMPT, MAX_ERROR_CHARS)


class StageRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    name: str
    attempts: list[AttemptRecord] = Field(default_factory=list)
    ok: bool


class RunRecord(BaseModel):
    """Everything worth knowing about one run; see the module docstring.

    ``task_set`` is ``None`` only in the on-disk form of a running run (blind evaluation).
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    run_id: str
    task_set: Literal["development", "generality", "ad hoc"] | None
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

    @model_validator(mode="after")
    def _task_set_agrees_with_benchmark(self) -> RunRecord:
        if self.task_set is None:
            if self.status != "running":
                raise ValueError("task_set may be missing only while the run is still running")
            if self.benchmark_id is not None:
                raise ValueError("a record without task_set cannot name a benchmark")
        elif self.task_set == "ad hoc":
            if self.benchmark_id is not None:
                raise ValueError("an ad hoc run has no benchmark_id")
        elif self.benchmark_id is None:
            raise ValueError(f"task set {self.task_set!r} needs a benchmark_id")
        return self


def _normalised(record: RunRecord) -> RunRecord:
    """Rebuild through validation: cleans in-place mutations, rejects invalid ones."""
    return RunRecord.model_validate(record.model_dump())


def _shrunk(record: RunRecord) -> RunRecord:
    data = record.model_dump()
    for stage in data["stages"]:
        for attempt in stage["attempts"]:
            attempt["errors"] = _cap_errors(attempt["errors"], SHRUNK_ERRORS, SHRUNK_ERROR_CHARS)
    return RunRecord.model_validate(data)


def _document(record: RunRecord) -> str:
    data = record.model_dump(mode="json")
    if record.status == "running":  # blind evaluation: agents can read this file
        data["task_set"] = None
        data["benchmark_id"] = None
    return json.dumps(data, indent=2) + "\n"


def write_record(run: RunDir, record: RunRecord) -> None:
    """Write ``run_record.json`` atomically (temporary file, then replace).

    A running record is written without ``task_set`` and ``benchmark_id`` (see the module
    docstring). Raises ``ValidationError`` for a record that was mutated into an invalid state.
    """
    record = _normalised(record)
    text = _document(record)
    if len(text.encode("utf-8")) > SOFT_RECORD_BYTES:
        text = _document(_shrunk(record))
    run.write_text(RECORD_FILE, text)


def read_record(run: RunDir) -> RunRecord:
    return RunRecord.model_validate(run.read_json(RECORD_FILE))


def _flat(value: object) -> str:
    return _CONTROL.sub(" ", str(value))


def _cell(value: object) -> str:
    """Text for a table cell: control characters and Markdown link/HTML/table syntax escaped."""
    return _MD_CELL.sub(r"\\\1", _flat(value))


def _md(value: object) -> str:
    """Text for a heading or bullet: like ``_cell`` plus emphasis and heading characters."""
    return _MD_TEXT.sub(r"\\\1", _flat(value))


def _comparison_lines(comparison: dict[str, Any]) -> list[str]:
    criteria = comparison.get("criteria")
    if not isinstance(criteria, list):
        dumped = json.dumps(comparison, indent=2, default=str)[:MAX_TEXT_CHARS]
        return ["```json", dumped.replace("```", "'''"), "```"]
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
    """Write ``summary.md``: outcome, stages, cost, and how the result was assessed.

    For the end of the run only: it names the benchmark (see the module docstring).
    """
    record = _normalised(record)
    lines = [
        f"# Run {_md(record.run_id)}",
        "",
        f"- Status: **{record.status}**",
        f"- Task set: {record.task_set}",
    ]
    if record.benchmark_id:
        lines.append(f"- Benchmark: {_md(record.benchmark_id)}")
    if record.failed_stage:
        lines.append(f"- Failed stage: {_md(record.failed_stage)}")
    if record.failure_reason:
        lines.append(f"- Reason: {_md(record.failure_reason)}")
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
            lines.append(f"| {_md(stage.name)} | {len(stage.attempts)} | {result} |")
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
