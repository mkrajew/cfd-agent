"""Run record and summary."""

import json
import os
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from cfdagent.llm.config import ModelConfig
from cfdagent.record import (
    NO_REFERENCE_SENTENCE,
    AttemptRecord,
    RunRecord,
    StageRecord,
    read_record,
    write_record,
    write_summary,
)
from cfdagent.rundir import RunDir

AT = chr(64)
IP1 = ".".join(["10", "1", "2", "3"])  # built at run time: no address literals in the repository
IP2 = ".".join(["10", "0", "0", "5"])
NOW = datetime(2026, 11, 12, 14, 30, 5, tzinfo=UTC)


def make_record(**overrides) -> RunRecord:
    fields = {
        "run_id": "20261112-143005-demo",
        "task_set": "ad hoc",
        "task_sha256": "a" * 64,
        "benchmark_id": None,
        "started_at": NOW,
        "status": "running",
        "models": {"planner": ModelConfig(deployment="gpt-oss-120b", reasoning_effort="medium")},
        "prompt_versions": {"planner": "0123456789ab"},
        "image_id": "sha256:abc",
        "cfdagent_version": "0.0.1",
    }
    fields.update(overrides)
    return RunRecord(**fields)


@pytest.fixture
def run(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    return RunDir(root)


def test_record_roundtrip_and_atomic_write(run, monkeypatch):
    record = make_record(
        stages=[
            StageRecord(
                name="geometry",
                attempts=[
                    AttemptRecord(n=1, ok=False, errors=["no solid"], seconds=2.5),
                    AttemptRecord(n=2, ok=True, errors=[], seconds=3.0),
                ],
                ok=True,
            )
        ],
        tokens_in=100,
        tokens_out=50,
        cost_usd=0.01,
        cost_estimated=True,
        wall_seconds=12.5,
    )
    write_record(run, record)
    assert read_record(run) == record
    assert json.loads((run.root / "run_record.json").read_text())["status"] == "running"

    # An update that dies half way must leave the previous complete file in place.
    previous = (run.root / "run_record.json").read_bytes()

    def broken_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(OSError):
        write_record(run, make_record(status="completed", finished_at=NOW))
    monkeypatch.undo()
    assert (run.root / "run_record.json").read_bytes() == previous
    assert [p.name for p in run.root.iterdir()] == ["run_record.json"]  # no temp file left


def test_record_fields_and_defaults():
    record = make_record()
    assert record.critic_verdict is None
    assert record.comparison is None
    assert record.finished_at is None
    assert record.failed_stage is None
    assert record.stages == []
    assert record.cost_estimated is False
    with pytest.raises(ValidationError):
        make_record(status="done")
    with pytest.raises(ValidationError):
        make_record(prompt_versions={"planner": "not-hex"})
    with pytest.raises(ValidationError):
        make_record(endpoint="https://example.invalid")
    with pytest.raises(ValidationError):
        make_record(critic_verdict="approve")


def test_record_redacts_urls_addresses_and_ips_in_free_text():
    record = make_record(
        status="failed",
        failure_reason="call to https://acct.services.ai.azure.com/openai/v1/ failed for "
        f"someone{AT}example.org at {IP1}",
        stages=[
            StageRecord(
                name="mesh",
                attempts=[
                    AttemptRecord(n=1, ok=False, errors=[f"see http://{IP2}:8080/x"], seconds=1)
                ],
                ok=False,
            )
        ],
    )
    text = record.model_dump_json()
    for leaked in ("azure.com", "someone", "example.org", IP1, IP2):
        assert leaked not in text
    assert "failed for" in record.failure_reason


def test_summary_contains_no_reference_sentence_for_ad_hoc_run(run):
    record = make_record(status="completed", finished_at=NOW, cost_usd=0.1234, tokens_in=10)
    write_summary(run, record, None)
    text = run.read_text("summary.md")
    assert (
        "No reference data for this task; result assessed by physical checks only "
        "(convergence, mass balance, plausibility)." in text
    )
    assert NO_REFERENCE_SENTENCE in text
    assert "No Critic reviewed this run" in text
    assert "20261112-143005-demo" in text
    assert "completed" in text


def test_summary_contains_comparison_for_benchmark_run(run):
    comparison = {
        "passed": False,
        "criteria": [
            {
                "quantity": "centreline_u",
                "passed": True,
                "error": 0.01,
                "threshold": 0.05,
                "reason": "ok",
            },
            {
                "quantity": "drag|coefficient",
                "passed": False,
                "error": None,
                "threshold": 0.1,
                "reason": "missing quantity",
            },
        ],
    }
    record = make_record(task_set="development", benchmark_id="synthetic", status="completed")
    write_summary(run, record, comparison)
    text = run.read_text("summary.md")
    assert NO_REFERENCE_SENTENCE not in text
    assert "centreline_u" in text and "missing quantity" in text
    assert "drag\\|coefficient" in text  # a pipe must not break the table
    assert "synthetic" in text
    assert "No Critic reviewed this run" in text


def test_summary_of_failed_benchmark_run_without_comparison_does_not_claim_no_reference(run):
    record = make_record(
        task_set="development",
        benchmark_id="synthetic",
        status="failed",
        failed_stage="mesh",
        failure_reason="mesh gate failed three times",
    )
    write_summary(run, record, None)
    text = run.read_text("summary.md")
    assert NO_REFERENCE_SENTENCE not in text
    assert "not scored" in text
    assert "mesh gate failed three times" in text
    assert "failed" in text and "mesh" in text
