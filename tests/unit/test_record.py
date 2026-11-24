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
    CriticCheck,
    CriticVerdict,
    FixLoopRecord,
    RunRecord,
    StageRecord,
    _shrunk,
    critic_agreement,
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
    assert record.critic_rounds == 0


def make_verdict(**changes) -> CriticVerdict:
    fields = {
        "verdict": "partial",
        "checks": [
            CriticCheck(name="convergence", passed=False, evidence="the residuals did not fall"),
            CriticCheck(name="plausibility", passed=True, evidence="the values are in range"),
        ],
        "reasons": ["The run did not converge."],
        "send_back_to": "case_setup",
        "instruction": "Run longer and set a residual criterion.",
    }
    fields.update(changes)
    return CriticVerdict(**fields)


def test_old_run_record_without_critic_fields_loads(run):
    write_record(run, make_record(status="completed", finished_at=NOW))
    document = json.loads((run.root / "run_record.json").read_text())
    assert document["critic_verdict"] is None and document["critic_rounds"] == 0
    del document["critic_verdict"]  # an M1 record has the placeholder, an early M2 one neither
    del document["critic_rounds"]
    (run.root / "run_record.json").write_text(json.dumps(document), encoding="utf-8")
    loaded = read_record(run)
    assert loaded.critic_verdict is None and loaded.critic_rounds == 0
    document["critic_verdict"] = None  # the M1 form: the field exists and holds null
    (run.root / "run_record.json").write_text(json.dumps(document), encoding="utf-8")
    assert read_record(run).critic_verdict is None


def test_a_critic_verdict_survives_the_round_trip(run):
    verdict = make_verdict()
    record = make_record(
        status="completed", finished_at=NOW, critic_verdict=verdict, critic_rounds=2
    )
    write_record(run, record)
    loaded = read_record(run)
    assert loaded.critic_verdict == verdict and loaded.critic_rounds == 2
    document = json.loads((run.root / "run_record.json").read_text())
    assert document["critic_verdict"]["checks"][0] == {
        "name": "convergence",
        "passed": False,
        "evidence": "the residuals did not fall",
    }
    assert document["critic_verdict"]["send_back_to"] == "case_setup"


def test_the_critic_verdict_in_a_record_is_validated_and_cleaned():
    with pytest.raises(ValidationError):
        make_record(critic_verdict={"verdict": "approve"})
    with pytest.raises(ValidationError):
        make_record(critic_rounds=-1)
    with pytest.raises(ValidationError):
        make_verdict(send_back_to="solver_runner")  # the Solver Runner edits nothing
    with pytest.raises(ValidationError):
        make_verdict(extra="field")
    cleaned = make_verdict(reasons=[f"write to a{AT}b.example from {IP1}"])
    assert AT not in cleaned.reasons[0] and IP1 not in cleaned.reasons[0]
    record = make_record(critic_verdict=make_verdict())
    record.critic_verdict = None  # a record can be reset: a failed Critic leaves no verdict
    assert record.critic_verdict is None


def test_the_repair_mode_is_recorded_and_defaults_to_fresh(run):
    assert make_record().repair_mode == "fresh"
    record = make_record(repair_mode="in_place")
    write_record(run, record)
    assert json.loads((run.root / "run_record.json").read_text())["repair_mode"] == "in_place"
    assert read_record(run).repair_mode == "in_place"
    with pytest.raises(ValidationError):
        make_record(repair_mode="sometimes")


def test_old_run_record_without_repair_mode_loads(run):
    write_record(run, make_record(status="completed", finished_at=NOW))
    document = json.loads((run.root / "run_record.json").read_text())
    del document["repair_mode"]
    (run.root / "run_record.json").write_text(json.dumps(document), encoding="utf-8")
    assert read_record(run).repair_mode == "fresh"


def test_old_run_record_without_fix_loops_loads(run):
    stage = StageRecord(name="mesh", attempts=[AttemptRecord(n=1, ok=True, seconds=1)], ok=True)
    write_record(run, make_record(status="completed", finished_at=NOW, stages=[stage]))
    document = json.loads((run.root / "run_record.json").read_text())
    del document["fix_loops"]
    del document["stages"][0]["pass_no"]
    (run.root / "run_record.json").write_text(json.dumps(document), encoding="utf-8")
    loaded = read_record(run)
    assert loaded.fix_loops == []
    assert loaded.stages[0].pass_no == 0
    assert make_record().fix_loops == []


def test_a_fix_loop_is_recorded_and_survives_the_round_trip(run):
    loop = FixLoopRecord(
        n=1,
        source="solver_runner",
        target="case_setup",
        origin="gate",
        reason="the residuals did not fall",
        moved=["case/0", "logs", "results"],
    )
    stage = StageRecord(name="case_setup", ok=True, pass_no=1)
    write_record(run, make_record(status="failed", fix_loops=[loop], stages=[stage]))
    loaded = read_record(run)
    assert loaded.fix_loops == [loop]
    assert loaded.stages[0].pass_no == 1
    assert read_record(run).fix_loops[0].moved == ["case/0", "logs", "results"]


def test_a_fix_loop_is_bounded_and_cleaned_like_the_other_free_text():
    loop = FixLoopRecord(
        n=2,
        source="critic",
        target="mesh",
        origin="critic",
        reason="see https://example.test/x " + "r" * 5000,
        moved=[f"case/{i}" + "m" * 500 for i in range(500)],
    )
    assert len(loop.reason) <= 1000 and "example.test" not in loop.reason
    assert len(loop.moved) <= 200 and all(len(item) <= 200 for item in loop.moved)
    with pytest.raises(ValidationError):
        FixLoopRecord(n=0, source="a", target="b", origin="gate", reason="x", moved=[])
    with pytest.raises(ValidationError):
        FixLoopRecord(n=1, source="a", target="b", origin="gate", reason="x", moved=[], extra=1)
    with pytest.raises(ValidationError):
        StageRecord(name="x", ok=True, pass_no=-1)


def test_the_summary_names_the_pass_of_a_stage_that_ran_again(run):
    attempt = AttemptRecord(n=1, ok=True, seconds=1)
    record = make_record(
        status="completed",
        finished_at=NOW,
        stages=[
            StageRecord(name="case_setup", attempts=[attempt], ok=True),
            StageRecord(name="case_setup", attempts=[attempt], ok=True, pass_no=1),
            StageRecord(name="analyst", attempts=[attempt], ok=True, pass_no=1, free_restarts=1),
        ],
    )
    write_summary(run, record, None)
    text = run.read_text("summary.md")
    assert r"| case\_setup | 1 | ok |" in text  # pass 0 reads as in M1 (names are escaped)
    assert r"| case\_setup (pass 1) | 1 | ok |" in text
    assert "Stage analyst (pass 1): 1 free restart(s)" in text


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


COMPARISON_PASSED = {
    "passed": True,
    "criteria": [
        {"quantity": "q", "passed": True, "error": 0.01, "threshold": 0.05, "reason": "ok"}
    ],
}
COMPARISON_MISSED = {**COMPARISON_PASSED, "passed": False}


def make_loop(n=1, source="critic", target="mesh", origin="critic", reason="Refine the mesh."):
    return FixLoopRecord(
        n=n, source=source, target=target, origin=origin, reason=reason, moved=["case/0"]
    )


@pytest.mark.parametrize(
    ("verdict", "comparison", "expected"),
    [
        ("success", COMPARISON_PASSED, True),
        ("success", COMPARISON_MISSED, False),
        ("partial", COMPARISON_PASSED, False),
        ("partial", COMPARISON_MISSED, True),
        ("failed", COMPARISON_PASSED, False),
        ("failed", COMPARISON_MISSED, True),
        (None, COMPARISON_PASSED, None),  # no verdict: nothing to agree with
        ("success", None, None),  # no comparison (an ad hoc run, or a run that ended early)
        (None, None, None),
        # a comparison that could not be made states passed=False: it counts as a Scorer that did
        # not pass (a Critic that accepted a run with unusable results was wrong)
        ("success", {"passed": False, "problem": "quantities.json cannot be read"}, False),
        ("failed", {"passed": False, "problem": "quantities.json cannot be read"}, True),
        ("success", {"criteria": []}, None),  # a comparison without an overall result
        ("success", {"passed": "yes"}, None),
    ],
)
def test_critic_agreement_values(verdict, comparison, expected):
    final = None if verdict is None else make_verdict(verdict=verdict)
    assert critic_agreement(final, comparison) is expected


def test_critic_agreement_is_recorded_and_old_records_still_load(run):
    assert make_record().critic_agreement is None
    write_record(run, make_record(status="completed", finished_at=NOW, critic_agreement=False))
    assert read_record(run).critic_agreement is False
    document = run.read_json("run_record.json")
    assert document["critic_agreement"] is False
    del document["critic_agreement"]  # a record written before Task 15
    (run.root / "run_record.json").write_text(json.dumps(document), encoding="utf-8")
    assert read_record(run).critic_agreement is None
    with pytest.raises(ValidationError):
        make_record(critic_agreement="maybe")


def test_summary_lists_critic_verdict_and_fix_loops(run):
    verdict = make_verdict(
        reasons=["The run did not converge.", "The lift is implausible."],
        checks=[
            CriticCheck(name="convergence", passed=False, evidence="the residuals did not fall"),
            CriticCheck(name="plausibility", passed=True, evidence="values are in range"),
        ],
    )
    record = make_record(
        task_set="development",
        benchmark_id="synthetic",
        status="completed",
        finished_at=NOW,
        critic_verdict=verdict,
        critic_rounds=1,
        critic_agreement=True,
        fix_loops=[
            make_loop(
                1, source="solver_runner", target="case_setup", origin="gate", reason="Gate."
            ),
            make_loop(2, reason="Refine the mesh near the wall."),
        ],
    )
    write_summary(run, record, COMPARISON_MISSED)
    text = run.read_text("summary.md")
    assert "No Critic reviewed this run" not in text
    assert "- Status: **completed**" in text  # the status stays the pipeline status
    assert "## Critic" in text and "Verdict: **partial**" in text
    assert "The run did not converge." in text and "The lift is implausible." in text
    assert "convergence" in text and "the residuals did not fall" in text  # the failed check
    assert "values are in range" not in text  # a passed check is only counted
    assert "1 of 2 checks failed" in text
    assert "Send-back not followed: case\\_setup" in text  # the final verdict still asks for one
    assert "Run longer and set a residual criterion." in text
    assert "## Fix loops" in text and "2 fix loop(s)" in text
    assert "| 1 | solver_runner | case_setup | gate | Gate. |" in text
    assert "| 2 | critic | mesh | critic | Refine the mesh near the wall. |" in text
    assert "Critic send-backs followed: 1" in text
    assert "Critic agreed with the Scorer: **yes**" in text
    assert "Compared with the reference data" in text


def test_summary_states_the_disagreement_and_missing_agreement(run):
    record = make_record(
        task_set="development",
        benchmark_id="synthetic",
        status="completed",
        finished_at=NOW,
        critic_verdict=make_verdict(verdict="success", checks=[], reasons=[], send_back_to=None),
        critic_agreement=False,
    )
    write_summary(run, record, COMPARISON_MISSED)
    assert "Critic agreed with the Scorer: **no**" in run.read_text("summary.md")
    record.critic_agreement = None
    write_summary(run, record, COMPARISON_MISSED)
    text = run.read_text("summary.md")
    assert "Critic agreed with the Scorer: not available" in text
    ad_hoc = make_record(
        status="completed", finished_at=NOW, critic_verdict=make_verdict(verdict="success")
    )
    write_summary(run, ad_hoc, None)
    assert "agreed with the Scorer" not in run.read_text("summary.md")  # no Scorer, no line


def test_summary_no_reference_sentence_unchanged(run):
    ad_hoc = make_record(
        status="completed", finished_at=NOW, critic_verdict=make_verdict(verdict="failed")
    )
    write_summary(run, ad_hoc, None)
    text = run.read_text("summary.md")
    assert NO_REFERENCE_SENTENCE in text  # design 6.8, word for word
    assert "Verdict: **failed**" in text and "No Critic reviewed this run" not in text
    without_critic = make_record(status="completed", finished_at=NOW)
    write_summary(run, without_critic, None)
    text = run.read_text("summary.md")
    assert NO_REFERENCE_SENTENCE in text
    assert "No Critic reviewed this run" in text and "## Critic" not in text


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


def test_resource_retry_is_in_the_summary_and_old_records_still_load(run):
    attempt = AttemptRecord(n=1, ok=True, seconds=1)
    record = make_record(
        status="completed",
        finished_at=NOW,
        stages=[
            StageRecord(name="planner", attempts=[attempt], ok=True, resource_retries=1),
            StageRecord(name="mesh", attempts=[attempt], ok=True),
        ],
    )
    write_summary(run, record, None)
    text = run.read_text("summary.md")
    assert "Stage planner: the plan was reworked once after a resource rejection" in text
    assert "Stage mesh" not in text
    write_record(run, record)
    assert read_record(run).stages[0].resource_retries == 1
    old = run.read_json("run_record.json")
    del old["stages"][0]["resource_retries"]
    assert RunRecord.model_validate(old).stages[0].resource_retries == 0
    with pytest.raises(ValidationError):
        StageRecord(name="x", ok=True, resource_retries=-1)


def test_resource_retry_line_names_the_pass_of_the_stage(run):
    attempt = AttemptRecord(n=1, ok=True, seconds=1)
    record = make_record(
        status="completed",
        finished_at=NOW,
        stages=[
            StageRecord(name="planner", attempts=[attempt], ok=True, pass_no=1, resource_retries=1)
        ],
    )
    write_summary(run, record, None)
    text = run.read_text("summary.md")
    assert "Stage planner (pass 1): the plan was reworked once after a resource rejection" in text


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
