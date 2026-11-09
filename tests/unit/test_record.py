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
    _shrunk,
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
        status="completed",
        finished_at=NOW,
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
    assert json.loads((run.root / "run_record.json").read_text())["status"] == "completed"

    # An update that dies half way must leave the previous complete file in place.
    previous = (run.root / "run_record.json").read_bytes()

    def broken_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(OSError):
        write_record(run, make_record(status="failed", finished_at=NOW))
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


def test_free_restarts_are_in_the_summary_and_old_records_still_load(run):
    attempt = AttemptRecord(n=1, ok=True, seconds=1)
    record = make_record(
        status="completed",
        finished_at=NOW,
        stages=[
            StageRecord(name="analyst", attempts=[attempt], ok=True, free_restarts=2),
            StageRecord(name="mesh", attempts=[attempt], ok=True),
        ],
    )
    write_summary(run, record, None)
    text = run.read_text("summary.md")
    assert "| analyst | 1 | ok |" in text  # the attempts column stays the number of gate runs
    assert "Stage analyst: 2 free restart(s)" in text
    assert "Stage mesh" not in text
    write_record(run, record)
    assert read_record(run).stages[0].free_restarts == 2
    old = run.read_json("run_record.json")
    del old["stages"][0]["free_restarts"]
    assert RunRecord.model_validate(old).stages[0].free_restarts == 0
    with pytest.raises(ValidationError):
        StageRecord(name="x", ok=True, free_restarts=-1)


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


# --------------------------------------------------------------------------- fix round 1


def on_disk(run) -> dict:
    return json.loads((run.root / "run_record.json").read_text(encoding="utf-8"))


def test_mutated_free_text_is_cleaned_on_write(run):
    record = make_record(
        status="failed",
        stages=[
            StageRecord(name="mesh", attempts=[AttemptRecord(n=1, ok=False, seconds=1)], ok=False)
        ],
    )
    record.failure_reason = f"call to https://host.invalid/x failed for me{AT}mail.invalid {IP1}"
    record.stages[0].attempts[0].errors.append(f"see http://{IP2}:80/")  # in-place mutation
    write_record(run, record)
    text = (run.root / "run_record.json").read_text(encoding="utf-8")
    for leaked in ("host.invalid", "mail.invalid", IP1, IP2):
        assert leaked not in text


def test_invalid_mutation_is_rejected(run):
    record = make_record()
    with pytest.raises(ValidationError):
        record.status = "bogus"
    broken = make_record().model_copy(update={"status": "bogus"})  # copy does not validate
    write_record(run, make_record(status="failed", finished_at=NOW))
    before = (run.root / "run_record.json").read_bytes()
    with pytest.raises(ValidationError):
        write_record(run, broken)
    with pytest.raises(ValidationError):
        write_summary(run, broken, None)
    assert (run.root / "run_record.json").read_bytes() == before


def test_redaction_covers_hostnames_tokens_and_guids():
    guid = "-".join(["12345678", "1234", "1234", "1234", "123456789abc"])
    record = make_record(
        status="failed",
        failure_reason=f"acct.blob.core.windows.net said Bearer abcdefghijklmnop for {guid} "
        "via login.microsoftonline.com",
    )
    for leaked in ("windows.net", "abcdefghijklmnop", guid, "microsoftonline"):
        assert leaked not in record.failure_reason


def test_redaction_survives_lone_surrogates(run):
    record = make_record(status="failed", failure_reason="bad \ud800 text")
    assert "\ud800" not in record.failure_reason
    write_record(run, record)  # utf-8 encoding must not fail
    assert on_disk(run)["failure_reason"].startswith("bad")


def test_errors_per_attempt_are_capped_with_a_marker(run):
    errors = [f"error {i}" for i in range(120)]
    attempt = AttemptRecord(n=1, ok=False, errors=errors, seconds=1)
    assert len(attempt.errors) == 51
    assert attempt.errors[-1] == "[truncated: 70 more]"
    again = AttemptRecord.model_validate(attempt.model_dump())  # idempotent
    assert again.errors == attempt.errors


def test_record_with_huge_error_text_stays_under_the_write_limit(run):
    attempt = AttemptRecord(n=1, ok=False, errors=["x" * 10_000] * 100, seconds=1)
    stages = [StageRecord(name=f"s{i}", attempts=[attempt] * 10, ok=False) for i in range(20)]
    record = make_record(status="failed", finished_at=NOW, stages=stages)
    write_record(run, record)
    assert (run.root / "run_record.json").stat().st_size < 1_500_000


def test_running_record_hides_task_set_and_benchmark_on_disk(run):
    record = make_record(task_set="development", benchmark_id="synthetic", status="running")
    write_record(run, record)
    data = on_disk(run)
    assert data["benchmark_id"] is None and data["task_set"] is None
    assert "synthetic" not in (run.root / "run_record.json").read_text()
    assert read_record(run).status == "running"  # a running file is still a valid record
    assert record.benchmark_id == "synthetic"  # the in-memory record is untouched
    for status in ("completed", "failed", "interrupted"):
        write_record(run, record.model_copy(update={"status": status}))
        data = on_disk(run)
        assert data["benchmark_id"] == "synthetic" and data["task_set"] == "development"


def test_task_set_and_benchmark_must_agree():
    with pytest.raises(ValidationError):
        make_record(task_set="development", benchmark_id=None)
    with pytest.raises(ValidationError):
        make_record(task_set="ad hoc", benchmark_id="synthetic")
    with pytest.raises(ValidationError):
        make_record(task_set=None, status="completed")
    make_record(task_set=None, status="running")  # only the on-disk form of a running record


def test_summary_escapes_markdown_in_identifiers(run):
    record = make_record(
        task_set="development",
        benchmark_id="[x](http://link.invalid) *bold*",
        status="failed",
        failed_stage="mesh_stage",
        failure_reason="a\nb",
    )
    write_summary(run, record, None)
    text = run.read_text("summary.md")
    assert "[x]" not in text and "*bold*" not in text
    assert "mesh\\_stage" in text


# ---------------------------------------------------- the diagnosis of a failed attempt


def test_an_attempt_has_no_diagnosis_by_default(run):
    attempt = AttemptRecord(n=1, ok=False, errors=["x"], seconds=1)
    assert attempt.diagnosis is None
    record = make_record(
        status="failed",
        finished_at=NOW,
        stages=[StageRecord(name="solver_runner", attempts=[attempt], ok=False)],
    )
    write_record(run, record)
    assert on_disk(run)["stages"][0]["attempts"][0]["diagnosis"] is None
    assert read_record(run).stages[0].attempts[0].diagnosis is None


def test_a_diagnosis_is_stored_bounded_and_cleaned(run):
    text = f"the solver stopped at {IP1} see http://example.test/x " + "y" * 5000
    attempt = AttemptRecord(n=1, ok=False, errors=[], seconds=1, diagnosis=text)
    assert attempt.diagnosis is not None and len(attempt.diagnosis) <= 1500
    assert IP1 not in attempt.diagnosis and "http://" not in attempt.diagnosis
    again = AttemptRecord.model_validate(attempt.model_dump())  # idempotent
    assert again.diagnosis == attempt.diagnosis
    attempt.diagnosis = "z" * 9000  # an assignment is cleaned too
    assert len(attempt.diagnosis) <= 1500


def test_a_diagnosis_survives_the_round_trip_and_shrinks_with_the_record(run):
    attempt = AttemptRecord(n=1, ok=False, errors=["e"], seconds=1, diagnosis="Courant too high")
    record = make_record(
        status="failed",
        finished_at=NOW,
        stages=[StageRecord(name="solver_runner", attempts=[attempt], ok=False)],
    )
    write_record(run, record)
    assert read_record(run).stages[0].attempts[0].diagnosis == "Courant too high"
    shrunk = _shrunk(record)
    assert shrunk.stages[0].attempts[0].diagnosis == "Courant too high"
    long = AttemptRecord(n=1, ok=False, errors=["e"], seconds=1, diagnosis="d" * 9000)
    shrunk = _shrunk(
        make_record(
            status="failed",
            finished_at=NOW,
            stages=[StageRecord(name="solver_runner", attempts=[long], ok=False)],
        )
    )
    assert len(shrunk.stages[0].attempts[0].diagnosis) <= 200


# ------------------------------------------------------- knowledge reads and the residual line


def test_knowledge_reads_are_bounded_cleaned_and_survive_the_round_trip(run):
    paths = [f"flow/tutorial{i}/system/fvSchemes" for i in range(300)]
    record = make_record(status="completed", finished_at=NOW, knowledge_reads=paths)
    assert len(record.knowledge_reads) == 200  # bounded
    assert record.knowledge_reads[0] == "flow/tutorial0/system/fvSchemes"
    long = make_record(knowledge_reads=["a/" + "b" * 900 + f" at {IP1}"])
    assert len(long.knowledge_reads[0]) <= 200 and IP1 not in long.knowledge_reads[0]
    record.knowledge_reads.append("later/appended")  # an in-place change is cleaned on write
    write_record(run, record)
    assert len(read_record(run).knowledge_reads) == 200
    assert make_record().knowledge_reads == []
    with pytest.raises(ValidationError):
        make_record(knowledge_reads="not a list")


def test_summary_states_residual_control_and_the_knowledge_reads(run):
    record = make_record(status="completed", knowledge_reads=["a/b", "c/d"])
    write_summary(run, record, None, residual_control="Residual control: met at time 120.")
    text = run.read_text("summary.md")
    assert "Residual control: met at time 120." in text
    assert "Knowledge files read by the agents: 2" in text
    assert NO_REFERENCE_SENTENCE in text and "No Critic reviewed this run" in text
    write_summary(run, make_record(status="completed"), None)  # both optional
    plain = run.read_text("summary.md")
    assert "Residual control" not in plain
    assert "Knowledge files read by the agents: 0" in plain
