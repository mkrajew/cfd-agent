"""The orchestrator and ``run_task`` with fake pipeline steps (no model, no sandbox, no network)."""

import json
import os
import re
import signal
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_casespec import SPEC

from cfdagent import orchestrator
from cfdagent.agents.casespec import CaseSpec
from cfdagent.agents.geometry import GeometryStage
from cfdagent.agents.loop import StageOutcome
from cfdagent.config import RunConfig, RunLimits
from cfdagent.errors import ToolFailure
from cfdagent.llm.client import ChatResult, ToolCall, Usage
from cfdagent.llm.scripted import ScriptedBackend
from cfdagent.orchestrator import GatedStep, RunExistsError, Step, run_task
from cfdagent.record import AttemptRecord, StageRecord, read_record
from cfdagent.rundir import RunDir
from cfdagent.sandbox.client import SandboxClient
from cfdagent.storage.blob import UploadReport

NOW = datetime(2026, 11, 12, 14, 30, 5, tzinfo=UTC)
TASK = "# A demo task\n\nSimulate the flow and report `profile_q`.\n"
GIB = 1024**3


class Ticker:
    """A clock that moves one second per reading, so heartbeats can be told apart."""

    def __init__(self):
        self.moment = NOW

    def __call__(self):
        self.moment += timedelta(seconds=1)
        return self.moment


TICKER = Ticker()  # shared, so that two runs of one test never get the same run id


class FakeStep(Step):
    """A pipeline step that records a stage and returns what it was told to."""

    def __init__(self, name, *, ok=True, reason=None, action=None, reads=()):
        self.name = name
        self.ok, self.reason, self.action, self.reads = ok, reason, action, list(reads)
        self.calls = 0
        self.task_text = None

    def run(self, ctx, task_text):
        self.calls += 1
        self.task_text = task_text
        if self.action is not None:
            self.action(ctx)
        attempt = AttemptRecord(n=1, ok=self.ok, errors=[], seconds=0.0)
        ctx.record.stages.append(StageRecord(name=self.name, attempts=[attempt], ok=self.ok))
        return StageOutcome(ok=self.ok, attempts=1, errors=[], reason=self.reason)

    def knowledge_reads(self):
        return list(self.reads)


class FakeUploader:
    """Stands in for the Blob upload; remembers which files it was offered."""

    def __init__(self, report=None, error=None):
        self.report, self.error = report or UploadReport(uploaded=1), error
        self.files = None
        self.calls = 0

    def __call__(self, run):
        self.calls += 1
        self.files = sorted(run.relative(p) for p in run.iter_files())
        if self.error is not None:
            raise self.error
        return self.report


def steps(*made):
    """Pipeline factories that hand out the given step objects."""
    return [lambda step=step: step for step in made]


def go(tmp_path, *made, task=TASK, config=None, **kwargs):
    kwargs.setdefault("uploader", FakeUploader())
    kwargs.setdefault("now", TICKER)
    return run_task(
        task,
        config or RunConfig.default(),
        backend=ScriptedBackend([]),
        run_root=tmp_path / "runs",
        pipeline=steps(*made),
        state_dir=tmp_path / "state",
        **kwargs,
    )


def active_run(tmp_path) -> Path:
    return tmp_path / "state" / "active-run"


def log_text(result) -> str:
    return (result.run_dir / "run.log").read_text(encoding="utf-8")


# ---------------------------------------------------------------------- stage order and failure


def test_a_good_run_runs_every_step_in_order_and_completes(tmp_path):
    first, second = FakeStep("one"), FakeStep("two")
    result = go(tmp_path, first, second)
    assert result.status == "completed"
    assert re.fullmatch(r"\d{8}-\d{6}-a-demo-task", result.run_id)
    assert result.run_dir == tmp_path / "runs" / result.run_id
    assert [s.name for s in result.record.stages] == ["one", "two"]
    assert result.record.status == "completed" and result.record.finished_at is not None
    assert result.comparison is None and result.record.comparison is None
    assert (result.run_dir / "task.md").read_text(encoding="utf-8") == TASK
    assert first.task_text == TASK
    on_disk = read_record(RunDir(result.run_dir))
    assert on_disk.status == "completed" and on_disk.run_id == result.run_id
    summary = (result.run_dir / "summary.md").read_text(encoding="utf-8")
    assert "No reference data for this task" in summary


def test_failed_stage_stops_pipeline_and_records_reason(tmp_path):
    first = FakeStep("one")
    second = FakeStep("two", ok=False, reason="stage 'two' failed after 3 attempts; last errors: x")
    third = FakeStep("three")
    result = go(tmp_path, first, second, third)
    assert result.status == "failed"
    assert result.record.failed_stage == "two"
    assert "failed after 3 attempts" in result.record.failure_reason
    assert third.calls == 0
    assert [s.name for s in result.record.stages] == ["one", "two"]
    summary = (result.run_dir / "summary.md").read_text(encoding="utf-8")
    assert "Failed stage: two" in summary and "failed after 3 attempts" in summary


def test_exceeds_resources_is_a_normal_failure(tmp_path):
    reason = "exceeds resources: the plan needs a mesh of 9000000 cells (limit 2000000)."
    planner = FakeStep("planner", ok=False, reason=reason)
    later = FakeStep("geometry")
    uploader = FakeUploader()
    result = go(tmp_path, planner, later, uploader=uploader)
    assert result.status == "failed"
    assert result.record.failed_stage == "planner"
    assert result.record.failure_reason.startswith("exceeds resources:")
    assert "internal_error" not in result.record.failure_reason
    assert later.calls == 0
    assert uploader.calls == 1
    assert "exceeds resources" in (result.run_dir / "summary.md").read_text(encoding="utf-8")


def test_a_stage_that_does_not_name_its_reason_still_gives_one(tmp_path):
    result = go(tmp_path, FakeStep("one", ok=False))
    assert result.status == "failed" and result.record.failure_reason


def test_cost_limit_ends_run_failed_with_partial_upload(tmp_path):
    def overspend(ctx):
        ctx.run.write_text("partial.txt", "work done so far")
        ctx.cost.add(Usage(10_000_000, 10_000_000, None), 0, 0)  # far above the limit

    uploader = FakeUploader()
    config = RunConfig.default().model_copy(update={"limits": RunLimits(max_cost_usd=0.5)})
    later = FakeStep("later")
    result = go(
        tmp_path, FakeStep("spender", action=overspend), later, uploader=uploader, config=config
    )
    assert result.status == "failed"
    assert result.record.failed_stage == "spender"
    assert "cost limit" in result.record.failure_reason.lower()
    assert "internal_error" not in result.record.failure_reason
    assert later.calls == 0
    assert result.record.cost_usd > 0.5
    assert "partial.txt" in uploader.files
    assert {"task.md", "run_record.json", "summary.md", "run.log", "status.json"} <= set(
        uploader.files
    )


def test_a_cost_limit_reported_by_the_stage_is_named_too(tmp_path):
    reason = "cost limit: LLM cost 1.2 USD exceeds the limit of 1.0 USD"
    result = go(tmp_path, FakeStep("one", ok=False, reason=reason))
    assert result.status == "failed" and "cost limit" in result.record.failure_reason


def test_the_wall_clock_is_checked_between_steps(tmp_path):
    config = RunConfig.default()
    later = FakeStep("later")
    clock_value = {"t": 0.0}

    def clock():
        return clock_value["t"]

    def burn_time(ctx):
        clock_value["t"] += config.limits.wall_clock_s + 1

    result = go(tmp_path, FakeStep("slow", action=burn_time), later, clock=clock)
    assert result.status == "failed"
    assert "wall-clock" in result.record.failure_reason
    assert result.record.failed_stage == "later"
    assert later.calls == 0
    assert result.record.wall_seconds > config.limits.wall_clock_s


def test_tokens_and_cost_reach_the_record(tmp_path):
    def spend(ctx):
        ctx.cost.add(Usage(1000, 500, None), 0, 0)

    result = go(tmp_path, FakeStep("one", action=spend))
    assert (result.record.tokens_in, result.record.tokens_out) == (1000, 500)
    assert result.record.cost_usd == pytest.approx(1000 * 0.15e-6 + 500 * 0.60e-6)
    assert result.record.models.keys() == RunConfig.default().models.keys()
    assert set(result.record.prompt_versions) == set(RunConfig.default().models)
    assert "LLM cost" in (result.run_dir / "summary.md").read_text(encoding="utf-8")


def test_an_unpriced_model_is_refused_before_anything_is_created(tmp_path):
    config = RunConfig.default()
    models = {
        name: m.model_copy(update={"deployment": "mystery-model"})
        for name, m in config.models.items()
    }
    with pytest.raises(ValueError, match="mystery-model"):
        go(tmp_path, FakeStep("one"), config=config.model_copy(update={"models": models}))
    assert not (tmp_path / "runs").exists()


# ---------------------------------------------------------------------- interruption and errors


def test_keyboard_interrupt_marks_interrupted_and_still_uploads(tmp_path):
    def interrupt(ctx):
        ctx.run.write_text("partial.txt", "x")
        raise KeyboardInterrupt

    uploader = FakeUploader()
    later = FakeStep("later")
    result = go(tmp_path, FakeStep("busy", action=interrupt), later, uploader=uploader)
    assert result.status == "interrupted"
    assert result.record.status == "interrupted" and result.record.failed_stage == "busy"
    assert "interrupt" in result.record.failure_reason.lower()
    assert later.calls == 0
    assert (
        uploader.calls == 1 and "partial.txt" in uploader.files and "summary.md" in uploader.files
    )


def test_sigterm_marks_interrupted_and_restores_the_handler(tmp_path):
    before = signal.getsignal(signal.SIGTERM)

    def terminate(ctx):
        signal.raise_signal(signal.SIGTERM)

    uploader = FakeUploader()
    result = go(tmp_path, FakeStep("busy", action=terminate), FakeStep("later"), uploader=uploader)
    assert result.status == "interrupted"
    assert "SIGTERM" in result.record.failure_reason
    assert uploader.calls == 1
    assert signal.getsignal(signal.SIGTERM) == before
    assert not active_run(tmp_path).exists()


def test_no_signal_handler_is_installed_outside_the_main_thread(tmp_path):
    before = signal.getsignal(signal.SIGTERM)
    seen = []
    outcome = []

    def look(ctx):
        seen.append(signal.getsignal(signal.SIGTERM))

    thread = threading.Thread(
        target=lambda: outcome.append(go(tmp_path, FakeStep("one", action=look)))
    )
    thread.start()
    thread.join(timeout=60)
    assert outcome and outcome[0].status == "completed"
    assert seen == [before]


@pytest.mark.parametrize(
    "error",
    [RuntimeError("something broke"), ToolFailure("the executor misbehaved"), KeyError("k")],
)
def test_unhandled_exception_is_internal_error(tmp_path, error):
    def explode(ctx):
        raise error

    uploader = FakeUploader()
    later = FakeStep("later")
    result = go(tmp_path, FakeStep("boom", action=explode), later, uploader=uploader)
    assert result.status == "failed"
    assert result.record.failed_stage == "boom"
    assert result.record.failure_reason.startswith("internal_error")
    assert later.calls == 0
    assert uploader.calls == 1
    summary = (result.run_dir / "summary.md").read_text(encoding="utf-8")
    assert "internal_error" in summary.replace("\\", "")  # the summary escapes underscores


def test_the_reason_of_an_internal_error_is_cleaned(tmp_path):
    def explode(ctx):
        raise RuntimeError(
            "could not reach https://secret.example.test/v1/x with Bearer abcdefgh12345"
        )

    result = go(tmp_path, FakeStep("boom", action=explode))
    assert "secret.example.test" not in result.record.failure_reason
    assert "abcdefgh12345" not in result.record.failure_reason


def _response(*calls, content=None):
    return ChatResult(
        content=content,
        tool_calls=list(calls),
        usage=Usage(100, 50, None),
        parsed=None,
        raw={},
        finish_reason="tool_calls",
    )


class _KilledExecutor:
    """The ``docker run`` client of an executor that the kernel killed (out of memory)."""

    pid = 4242
    returncode = 137

    def wait(self, timeout=None):
        return 137

    def poll(self):
        return 137

    def kill(self):
        pass


def test_an_executor_that_dies_is_an_internal_error_never_a_retryable_gate_failure(tmp_path):
    """Exit 137 of the container that runs agent code is the platform's problem, not the
    agent's: no second attempt, the run ends with ``internal_error``."""
    backend = ScriptedBackend(
        [
            _response(
                ToolCall(
                    id="w1",
                    name="write_file",
                    arguments={"path": "attempts/geometry-1/geometry.py", "content": "print(1)\n"},
                )
            ),
            _response(ToolCall(id="k1", name="check_stage", arguments={})),
        ]
    )

    def plan(ctx):
        ctx.casespec = CaseSpec.model_validate(SPEC)

    def sandbox(config, run, run_id):
        return SandboxClient(
            config,
            run,
            run_id,
            popen=lambda argv, **kw: _KilledExecutor(),
            run=lambda argv, **kw: subprocess.CompletedProcess(argv, 0, b"", b""),
        )

    result = run_task(
        TASK,
        RunConfig.default(),
        backend=backend,
        run_root=tmp_path / "runs",
        pipeline=[lambda: FakeStep("planner", action=plan), lambda: GatedStep(GeometryStage())],
        state_dir=tmp_path / "state",
        uploader=FakeUploader(),
        sandbox_factory=sandbox,
    )
    assert result.status == "failed"
    assert result.record.failed_stage == "geometry"
    assert result.record.failure_reason.startswith("internal_error")
    assert "outside the agent's work" in result.record.failure_reason
    assert len(backend.calls) == 2  # no retry after the executor died
    geometry = result.record.stages[-1]
    assert geometry.name == "geometry" and geometry.attempts == []  # not a failed attempt


def test_a_failing_record_write_does_not_mask_the_outcome(tmp_path, monkeypatch):
    def broken(run, record):
        raise OSError("disk full")

    monkeypatch.setattr(orchestrator, "write_record", broken)
    uploader = FakeUploader()
    result = go(tmp_path, FakeStep("one", ok=False, reason="the real reason"), uploader=uploader)
    assert result.status == "failed"
    assert result.record.failure_reason == "the real reason"
    assert (result.run_dir / "summary.md").is_file()
    assert uploader.calls == 1
    assert "disk full" in log_text(result)
    assert not active_run(tmp_path).exists()


def test_a_failing_summary_write_does_not_skip_the_upload(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(orchestrator, "write_summary", broken)
    uploader = FakeUploader()
    result = go(tmp_path, FakeStep("one"), uploader=uploader)
    assert result.status == "completed" and uploader.calls == 1


# ---------------------------------------------------------------------- the active-run marker


@pytest.mark.parametrize("path", ["completed", "failed", "interrupted", "exception"])
def test_active_run_file_removed_on_every_path(tmp_path, path):
    seen = []

    def look(ctx):
        marker = active_run(tmp_path)
        seen.append(marker.is_file() and marker.read_text(encoding="utf-8").strip())
        if path == "interrupted":
            raise KeyboardInterrupt
        if path == "exception":
            raise RuntimeError("boom")

    result = go(tmp_path, FakeStep("one", action=look, ok=path != "failed"))
    assert result.status == {"exception": "failed"}.get(path, path)
    assert seen == [str(os.getpid())]  # held during the run, the content is the process id
    assert not active_run(tmp_path).exists()


def test_the_active_run_file_is_removed_when_the_run_is_refused_late(tmp_path):
    # A removal that finds nothing to remove is not an error.
    def vanish(ctx):
        active_run(tmp_path).unlink()

    result = go(tmp_path, FakeStep("one", action=vanish))
    assert result.status == "completed"


def test_an_unwritable_state_directory_is_a_warning_not_a_failure(tmp_path):
    blocker = tmp_path / "state"
    blocker.write_text("a file where the directory should be", encoding="utf-8")
    result = go(tmp_path, FakeStep("one"))
    assert result.status == "completed"
    assert "active-run" in log_text(result) and "warning" in log_text(result).lower()


def test_the_state_directory_comes_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("CFDAGENT_STATE_DIR", str(tmp_path / "from-env"))
    seen = []
    run_task(
        TASK,
        RunConfig.default(),
        backend=ScriptedBackend([]),
        run_root=tmp_path / "runs",
        pipeline=steps(
            FakeStep(
                "one",
                action=lambda ctx: seen.append((tmp_path / "from-env" / "active-run").is_file()),
            )
        ),
        uploader=FakeUploader(),
    )
    assert seen == [True]
    assert not (tmp_path / "from-env" / "active-run").exists()


# ---------------------------------------------------------------------- status.json and run.log


def test_status_json_heartbeat_updates_each_stage(tmp_path):
    snapshots = []

    def snap(ctx):
        snapshots.append(ctx.run.read_json("status.json"))

    result = go(tmp_path, FakeStep("one", action=snap), FakeStep("two", action=snap))
    first, second = snapshots
    assert (first["state"], first["stage"]) == ("running", "one")
    assert (second["state"], second["stage"]) == ("running", "two")
    assert first["pid"] == second["pid"] == os.getpid()
    beat_one, beat_two = (datetime.fromisoformat(s["heartbeat"]) for s in snapshots)
    assert beat_one.tzinfo is not None and beat_two > beat_one
    final = json.loads((result.run_dir / "status.json").read_text(encoding="utf-8"))
    assert final["state"] == "completed"
    assert datetime.fromisoformat(final["heartbeat"]) > beat_two
    assert set(final) == {"state", "stage", "pid", "heartbeat"}


def test_the_status_file_is_replaced_atomically_and_failure_is_visible(tmp_path):
    result = go(tmp_path, FakeStep("one", ok=False, reason="no"))
    final = json.loads((result.run_dir / "status.json").read_text(encoding="utf-8"))
    assert final["state"] == "failed" and final["stage"] == "one"
    assert not list(result.run_dir.glob(".status.json.*"))  # no temporary file left behind


def test_run_log_has_stage_start_and_end_events(tmp_path):
    result = go(tmp_path, FakeStep("one"), FakeStep("two", ok=False, reason="no"))
    text = log_text(result)
    assert re.search(r"stage one: start", text) and re.search(r"stage one: ok", text)
    assert re.search(r"stage two: start", text) and re.search(r"stage two: failed", text)
    assert text.index("stage one: ok") < text.index("stage two: start")
    assert re.search(r"run: (completed|failed)", text)


def test_the_record_is_saved_after_every_step_even_without_progress_callbacks(tmp_path):
    saved = []

    def look(ctx):
        saved.append([s["name"] for s in ctx.run.read_json("run_record.json")["stages"]])

    result = go(tmp_path, FakeStep("one"), FakeStep("two", action=look))
    assert result.status == "completed"
    assert saved == [["one"]]


# ---------------------------------------------------------------------- upload


def test_upload_failure_never_changes_the_status(tmp_path):
    uploader = FakeUploader(error=RuntimeError("storage unreachable"))
    result = go(tmp_path, FakeStep("one"), uploader=uploader)
    assert result.status == "completed"
    assert "upload failed" in log_text(result)
    assert "Upload" in (result.run_dir / "summary.md").read_text(encoding="utf-8")


def test_a_partial_upload_is_stated(tmp_path):
    uploader = FakeUploader(report=UploadReport(uploaded=3, failed=["case.tar.zst"]))
    result = go(tmp_path, FakeStep("one"), uploader=uploader)
    assert result.status == "completed"
    assert "case.tar.zst" in log_text(result)
    assert "case.tar.zst" in (result.run_dir / "summary.md").read_text(encoding="utf-8")


def test_upload_false_never_touches_the_uploader(tmp_path):
    uploader = FakeUploader()
    result = go(tmp_path, FakeStep("one"), uploader=uploader, upload=False)
    assert result.status == "completed" and uploader.calls == 0


def test_without_a_storage_url_the_upload_is_skipped_with_a_note(tmp_path, monkeypatch):
    monkeypatch.delenv("CFDAGENT_STORAGE_ACCOUNT_URL", raising=False)
    result = run_task(
        TASK,
        RunConfig.default(),
        backend=ScriptedBackend([]),
        run_root=tmp_path / "runs",
        pipeline=steps(FakeStep("one")),
        state_dir=tmp_path / "state",
    )
    assert result.status == "completed"
    assert "CFDAGENT_STORAGE_ACCOUNT_URL" in log_text(result)


def test_the_default_uploader_uses_the_account_url_of_the_environment(tmp_path, monkeypatch):
    url = "https://storage.example.test"
    monkeypatch.setenv("CFDAGENT_STORAGE_ACCOUNT_URL", url)
    calls = []
    monkeypatch.setattr(
        orchestrator,
        "upload_run",
        lambda run, account_url: calls.append((run.root.name, account_url)) or UploadReport(),
    )
    result = run_task(
        TASK,
        RunConfig.default(),
        backend=ScriptedBackend([]),
        run_root=tmp_path / "runs",
        pipeline=steps(FakeStep("one")),
        state_dir=tmp_path / "state",
    )
    assert calls == [(result.run_id, url)]
    assert url not in log_text(result)  # the location never reaches a file


# ---------------------------------------------------------------------- run directory


def test_an_existing_run_directory_is_refused_before_anything_is_written(tmp_path):
    now = lambda: NOW  # noqa: E731 - the same second and slug give the same run id
    first = go(tmp_path, FakeStep("one"), now=now)
    before = sorted(p.relative_to(first.run_dir).as_posix() for p in first.run_dir.rglob("*"))
    record_before = (first.run_dir / "run_record.json").read_bytes()
    second_step = FakeStep("one")
    with pytest.raises(RunExistsError, match=first.run_id):
        go(tmp_path, second_step, now=now)
    assert second_step.calls == 0
    assert (
        sorted(p.relative_to(first.run_dir).as_posix() for p in first.run_dir.rglob("*")) == before
    )
    assert (first.run_dir / "run_record.json").read_bytes() == record_before
    assert not active_run(tmp_path).exists()


def test_an_ad_hoc_task_names_the_run_after_its_first_line(tmp_path):
    result = go(tmp_path, FakeStep("one"), task="Simulate  a *heated* plate!\nmore")
    assert result.run_id.endswith("-simulate-a-heated-plate")


# ---------------------------------------------------------------------- benchmarks and the Scorer


def make_benchmarks(tmp_path) -> Path:
    root = tmp_path / "benchmarks"
    bench = root / "bench-one"
    (bench / "reference").mkdir(parents=True)
    (bench / "reference" / "ref.csv").write_text(
        "x,value\n0.0,0.00000\n0.5,0.31416\n1.0,0.71828\n", encoding="utf-8"
    )
    (bench / "task.md").write_text(
        "# A demo benchmark task\n\nCompute the profile and report `profile_q`.\n", encoding="utf-8"
    )
    (bench / "benchmark.toml").write_text(
        'id = "bench-one"\nfamily = "demo"\ntask_file = "task.md"\n'
        'citation = "Demo source (1999)"\n\n'
        '[[criteria]]\nquantity = "profile_q"\nkind = "profile"\nmetric = "max_abs_error"\n'
        'threshold = 0.05\nreference_file = "reference/ref.csv"\n',
        encoding="utf-8",
    )
    return root


GOOD_QUANTITIES = {
    "profile_q": {"x": [0.0, 0.25, 0.5, 0.75, 1.0], "value": [0.0, 0.2, 0.31416, 0.5, 0.71828]}
}
BAD_QUANTITIES = {"profile_q": {"x": [0.0, 0.5, 1.0], "value": [1.0, 1.0, 1.0]}}


def write_quantities(data):
    def action(ctx):
        ctx.run.write_text("results/quantities.json", json.dumps(data))

    return action


def bench_config(task_set="development"):
    return RunConfig.default().model_copy(
        update={"benchmark_id": "bench-one", "task_set": task_set}
    )


def test_scorer_runs_only_for_benchmark_runs_and_never_retries(tmp_path):
    root = make_benchmarks(tmp_path)
    analyst = FakeStep("analyst", action=write_quantities(GOOD_QUANTITIES))
    # an ad hoc run: no scoring, no comparison file, whatever the quantities say
    plain = go(tmp_path, analyst, benchmarks_root=root)
    assert plain.comparison is None
    assert not (plain.run_dir / "results" / "comparison.json").exists()

    # a benchmark run that passes
    analyst = FakeStep("analyst", action=write_quantities(GOOD_QUANTITIES))
    scored = go(tmp_path, analyst, config=bench_config(), benchmarks_root=root, task="ignored")
    assert scored.status == "completed"
    assert scored.comparison["passed"] is True
    assert scored.record.comparison == scored.comparison
    on_disk = json.loads(
        (scored.run_dir / "results" / "comparison.json").read_text(encoding="utf-8")
    )
    assert on_disk == scored.comparison
    assert on_disk["criteria"][0]["quantity"] == "profile_q"
    summary = (scored.run_dir / "summary.md").read_text(encoding="utf-8")
    assert "Compared with the reference data" in summary and "bench-one" in summary

    # a benchmark run that does not pass: still a completed run, the stage ran once
    analyst = FakeStep("analyst", action=write_quantities(BAD_QUANTITIES))
    missed = go(tmp_path, analyst, config=bench_config(), benchmarks_root=root)
    assert missed.status == "completed" and missed.comparison["passed"] is False
    assert analyst.calls == 1


def test_the_scorer_does_not_run_after_a_failed_stage(tmp_path, monkeypatch):
    root = make_benchmarks(tmp_path)

    def must_not_run(*args, **kwargs):
        raise AssertionError("the Scorer ran after a failed stage")

    monkeypatch.setattr(orchestrator, "score", must_not_run)
    failing = FakeStep(
        "analyst", ok=False, reason="gate failed", action=write_quantities(GOOD_QUANTITIES)
    )
    result = go(tmp_path, failing, config=bench_config(), benchmarks_root=root)
    assert result.status == "failed" and result.comparison is None
    assert "not scored" in (result.run_dir / "summary.md").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "content",
    [
        None,  # quantities.json is missing
        "{not json",
        json.dumps({"profile_q": [1, 2, 3]}),  # a list is neither a number nor a profile
        json.dumps({"profile_q": {"x": [0, 1], "value": [0, True]}}),
    ],
)
def test_a_quantities_problem_is_recorded_in_the_comparison_not_a_crash(tmp_path, content):
    root = make_benchmarks(tmp_path)

    def write(ctx):
        if content is not None:
            ctx.run.write_text("results/quantities.json", content)

    analyst = FakeStep("analyst", action=write)
    result = go(tmp_path, analyst, config=bench_config(), benchmarks_root=root)
    assert result.status == "completed"
    assert result.comparison["passed"] is False
    assert result.comparison["problem"]
    assert analyst.calls == 1  # never a retry
    assert result.record.comparison == result.comparison
    assert (result.run_dir / "results" / "comparison.json").is_file()
    summary = (result.run_dir / "summary.md").read_text(encoding="utf-8")
    assert "could not be made" in summary and "No reference data" not in summary


def test_an_unknown_benchmark_is_refused_before_anything_is_created(tmp_path):
    root = make_benchmarks(tmp_path)
    config = RunConfig.default().model_copy(
        update={"benchmark_id": "no-such", "task_set": "development"}
    )
    with pytest.raises(ValueError, match="no-such"):
        go(tmp_path, FakeStep("one"), config=config, benchmarks_root=root)
    assert not (tmp_path / "runs").exists() and not active_run(tmp_path).exists()


def test_a_benchmark_run_hands_agents_only_the_task_file(tmp_path):
    root = make_benchmarks(tmp_path)
    step = FakeStep("one")
    result = go(tmp_path, step, config=bench_config(), benchmarks_root=root, task="the CLI text")
    expected = (root / "bench-one" / "task.md").read_text(encoding="utf-8")
    assert step.task_text == expected
    assert (result.run_dir / "task.md").read_text(encoding="utf-8") == expected
    assert result.record.task_set == "development" and result.record.benchmark_id == "bench-one"


def leaks(text: str, *, benchmark_id: str, reference_values: list[str]) -> list[str]:
    found = []
    if re.search(r"ghia", text, re.IGNORECASE):
        found.append("ghia")
    if benchmark_id in text:
        found.append(benchmark_id)
    for value in reference_values:
        if re.search(rf"(?<![0-9.]){re.escape(value)}(?![0-9])", text):
            found.append(value)
    return found


def reference_values(root: Path) -> list[str]:
    values = []
    for csv_file in (root / "bench-one" / "reference").glob("*.csv"):
        for line in csv_file.read_text(encoding="utf-8").splitlines()[1:]:
            values.append(line.split(",")[1].strip())
    return [v for v in values if v not in {"0.00000", "0", "1", "1.0"}]


def test_transcript_contains_no_reference_data(tmp_path):
    root = make_benchmarks(tmp_path)
    values = reference_values(root)
    assert "0.31416" in values
    # the check can fail: a planted value and a planted word are found
    assert leaks("... 0.31416 ...", benchmark_id="bench-one", reference_values=values) == [
        "0.31416"
    ]
    assert leaks("Ghia et al.", benchmark_id="bench-one", reference_values=values) == ["ghia"]
    assert leaks("run bench-one", benchmark_id="bench-one", reference_values=values) == [
        "bench-one"
    ]

    mid_run = {}

    def work(ctx):
        # What a stage does: it talks to the model about the task, and the transcript records it.
        ctx.transcript.append(
            "one", "planner", 1, "gpt-oss-120b", [{"role": "user", "content": step.task_text}], None
        )
        ctx.run.write_text("results/quantities.json", json.dumps(GOOD_QUANTITIES))
        mid_run["record"] = ctx.run.read_json("run_record.json")
        mid_run["status"] = (ctx.run.root / "status.json").read_text(encoding="utf-8")

    step = FakeStep("one", action=work)
    result = go(tmp_path, step, config=bench_config(), benchmarks_root=root)
    assert result.comparison["passed"] is True
    for name in ("llm_transcript.jsonl", "task.md", "status.json", "run.log"):
        text = (result.run_dir / name).read_text(encoding="utf-8")
        assert leaks(text, benchmark_id="bench-one", reference_values=values) == [], name
    # while the run is going, the record on disk names neither the task set nor the benchmark
    assert mid_run["record"]["benchmark_id"] is None and mid_run["record"]["task_set"] is None
    assert "bench-one" not in mid_run["status"]
    # the final record and summary do name it
    assert read_record(RunDir(result.run_dir)).benchmark_id == "bench-one"


def test_the_run_id_does_not_name_the_benchmark(tmp_path):
    root = make_benchmarks(tmp_path)
    result = go(tmp_path, FakeStep("one"), config=bench_config(), benchmarks_root=root)
    assert "bench-one" not in result.run_id


# ---------------------------------------------------------------------- residual control line


def solver_summary(**changes):
    base = {
        "solver": "simpleFoam",
        "ok": True,
        "steady": True,
        "steady_expected": True,
        "stopped_by_residual_control": True,
        "end_time": 312.0,
        "initial_residuals": {"p": 9.1e-6, "Ux": 8.0e-6},
        "warnings": [],
    }
    base.update(changes)
    return base


def residual_line(tmp_path, summary):
    def write(ctx):
        if summary is not None:
            ctx.run.write_text("logs/solver_run.json", json.dumps(summary))

    result = go(tmp_path, FakeStep("one", action=write))
    text = (result.run_dir / "summary.md").read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if line.startswith("Residual control")]
    assert len(lines) == 1, text
    return lines[0]


def test_summary_says_residual_control_was_met_for_a_steady_run(tmp_path):
    line = residual_line(tmp_path, solver_summary())
    assert "met" in line and "not met" not in line and "312" in line


def test_summary_says_residual_control_was_not_met_for_a_steady_run(tmp_path):
    line = residual_line(
        tmp_path,
        solver_summary(
            stopped_by_residual_control=False, warnings=["A linear solve ran 1000 iterations"]
        ),
    )
    assert "no residualControl criterion ended the run" in line
    assert "not shown to be converged" in line
    assert "9.1e-06" in line or "9.1e-6" in line  # the last initial residuals


def test_summary_residual_control_is_not_applicable_to_transient_or_unknown_runs(tmp_path):
    transient = solver_summary(
        solver="icoFoam", steady=False, steady_expected=False, stopped_by_residual_control=None
    )
    assert "not applicable" in residual_line(tmp_path, transient)
    marched = solver_summary(
        solver="icoFoam", steady=False, steady_expected=True, stopped_by_residual_control=None
    )
    not_tested = residual_line(tmp_path / "b", marched)
    assert "not available" in not_tested and "convergence was not tested" in not_tested
    assert "not applicable" in residual_line(tmp_path / "c", None)  # no solver summary
    failed_run = solver_summary(ok=False, stopped_by_residual_control=None)
    assert "unknown" in residual_line(tmp_path / "d", failed_run).lower()


def test_a_garbled_solver_summary_is_not_applicable_not_a_crash(tmp_path):
    def write(ctx):
        ctx.run.write_text("logs/solver_run.json", "{not json")

    result = go(tmp_path, FakeStep("one", action=write))
    assert result.status == "completed"
    assert "Residual control: not applicable" in (result.run_dir / "summary.md").read_text(
        encoding="utf-8"
    )


def test_knowledge_reads_of_the_steps_reach_the_record(tmp_path):
    result = go(
        tmp_path, FakeStep("one", reads=["a/b", "c/d"]), FakeStep("two", reads=["a/b", "e/f"])
    )
    assert result.record.knowledge_reads == ["a/b", "c/d", "e/f"]
    assert read_record(RunDir(result.run_dir)).knowledge_reads == ["a/b", "c/d", "e/f"]


# ---------------------------------------------------------------------- backend and sandbox


def test_without_a_backend_a_client_is_built_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("CFDAGENT_LLM_ENDPOINT", "https://models.example.test/openai/v1/")
    built = []

    class FakeClient:
        def __init__(self, settings):
            built.append(settings)

    monkeypatch.setattr(orchestrator, "LLMClient", FakeClient)
    seen = []
    result = run_task(
        TASK,
        RunConfig.default(),
        run_root=tmp_path / "runs",
        pipeline=steps(FakeStep("one", action=lambda ctx: seen.append(ctx.backend))),
        state_dir=tmp_path / "state",
        uploader=FakeUploader(),
    )
    assert result.status == "completed"
    assert len(built) == 1 and built[0].max_retries == 1
    assert seen == [seen[0]] and isinstance(seen[0], FakeClient)
    assert "models.example.test" not in json.dumps(result.record.model_dump(mode="json"))


def test_the_context_is_built_once_with_the_run_start(tmp_path):
    contexts = []
    go(tmp_path, FakeStep("one", action=contexts.append), FakeStep("two", action=contexts.append))
    first, second = contexts
    assert first is second or (first.started_at == second.started_at and first.run is second.run)
    assert first.on_progress is not None
    assert first.sandbox.run_id == first.run.root.name


# ---------------------------------------------------------------------- the disk watchdog


def test_disk_growth_watchdog_kills_executors_and_fails_the_run(tmp_path):
    killed = threading.Event()

    class Sandbox:
        def kill_active(self):
            killed.set()

    def hang(ctx):
        if not killed.wait(30):
            raise AssertionError("the watchdog never fired")
        raise ToolFailure("the executor failed (exit code 137)")  # what a killed call turns into

    later = FakeStep("later")
    result = go(
        tmp_path,
        FakeStep("grower", action=hang),
        later,
        sandbox_factory=lambda config, run, run_id: Sandbox(),
        dir_size=lambda root: 50 * GIB,
        watchdog_interval_s=0.01,
    )
    assert result.status == "failed"
    assert result.record.failed_stage == "grower"
    assert "run directory exceeded 20 GiB" in result.record.failure_reason
    assert "internal_error" not in result.record.failure_reason
    assert later.calls == 0


def test_a_run_that_stays_under_the_cap_is_not_touched(tmp_path):
    killed = []

    class Sandbox:
        def kill_active(self):
            killed.append(1)

    result = go(
        tmp_path,
        FakeStep("one", action=lambda ctx: threading.Event().wait(0.2)),
        sandbox_factory=lambda config, run, run_id: Sandbox(),
        dir_size=lambda root: GIB,
        watchdog_interval_s=0.01,
    )
    assert result.status == "completed" and killed == []


def test_the_watchdog_never_raises_into_the_run_and_is_stopped_afterwards(tmp_path):
    before = threading.active_count()
    calls = []

    def broken_size(root):
        calls.append(1)
        raise OSError("cannot list the directory")

    result = go(
        tmp_path,
        FakeStep("one", action=lambda ctx: threading.Event().wait(0.2)),
        dir_size=broken_size,
        watchdog_interval_s=0.01,
    )
    assert result.status == "completed" and calls
    deadline = threading.Event()
    deadline.wait(0.2)
    assert threading.active_count() == before


def test_the_default_directory_size_counts_files_without_following_links(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "f1").write_bytes(b"x" * 1000)
    (tmp_path / "f2").write_bytes(b"y" * 500)
    assert orchestrator.directory_size(tmp_path) == 1500
    assert orchestrator.directory_size(tmp_path / "missing") == 0


# ---------------------------------------------------------------------- fix round 1


def test_a_baseexception_that_is_not_an_interrupt_never_leaves_a_completed_record(tmp_path):
    """SystemExit in a step: the record on disk says failed, the exception still propagates."""

    def leave(ctx):
        raise SystemExit(3)

    uploader = FakeUploader()
    with pytest.raises(SystemExit):
        go(tmp_path, FakeStep("one", action=leave), uploader=uploader)
    (run_dir,) = (tmp_path / "runs").iterdir()
    record = json.loads((run_dir / "run_record.json").read_text(encoding="utf-8"))
    assert record["status"] == "failed"
    assert record["failure_reason"].startswith("internal_error")
    status = json.loads((run_dir / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "failed"
    summary = (run_dir / "summary.md").read_text(encoding="utf-8")
    assert "Status: **failed**" in summary
    assert uploader.calls == 1
    assert not active_run(tmp_path).exists()


def test_the_watchdog_keeps_killing_and_later_executor_calls_are_refused(tmp_path):
    """The cap is crossed while the agent waits on the model: nothing runs to kill at that
    moment, so the next executor call must be refused instead of running unwatched."""
    from cfdagent.sandbox.config import SandboxConfig

    kills = []
    tripped = threading.Event()

    class Sandbox:
        closed = False

        def kill_active(self):
            kills.append(1)
            self.closed = True
            tripped.set()

        def call(self, *args):
            if self.closed:
                raise ToolFailure("the run directory exceeded its cap; no more executors")
            return None

    holder = Sandbox()

    def model_wait_then_call(ctx):
        assert tripped.wait(30), "the watchdog never fired"
        ctx.sandbox.call("run_script")  # refused: the executor is closed

    result = go(
        tmp_path,
        FakeStep("grower", action=model_wait_then_call),
        sandbox_factory=lambda config, run, run_id: holder,
        dir_size=lambda root: 50 * GIB,
        watchdog_interval_s=0.01,
    )
    assert result.status == "failed"
    assert "run directory exceeded 20 GiB" in result.record.failure_reason
    assert len(kills) >= 1
    assert SandboxConfig  # (the real client is covered in test_sandbox_client)


def test_the_watchdog_loop_goes_on_killing_and_heartbeating_after_the_cap_is_crossed(tmp_path):
    kills, beats = [], []
    monitor = orchestrator._Monitor(
        tmp_path, 10, 0.01, lambda root: 100, lambda: kills.append(1), lambda: beats.append(1)
    )
    monitor.start()
    deadline = time.monotonic() + 5
    while len(kills) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    monitor.stop()
    assert monitor.exceeded and len(kills) >= 3 and len(beats) >= 3


def test_a_failing_pre_write_block_does_not_skip_summary_and_upload(tmp_path, monkeypatch):
    uploader = FakeUploader()
    original = orchestrator._Session.sync_record
    calls = []

    def sync_that_fails_at_the_end(self):
        calls.append(1)
        if self._finishing:
            raise RuntimeError("cannot sync")
        return original(self)

    monkeypatch.setattr(orchestrator._Session, "sync_record", sync_that_fails_at_the_end)
    result = go(tmp_path, FakeStep("one", ok=False, reason="the real reason"), uploader=uploader)
    assert result.status == "failed"
    assert (result.run_dir / "summary.md").is_file() and uploader.calls == 1
    assert "cannot sync" in log_text(result)
    assert not active_run(tmp_path).exists()


def test_a_failing_knowledge_reads_does_not_break_the_record(tmp_path):
    class BrokenReads(FakeStep):
        def knowledge_reads(self):
            raise RuntimeError("no reads for you")

    result = go(tmp_path, BrokenReads("one"))
    assert result.status == "completed"
    assert "no reads for you" in log_text(result)


def test_a_comparison_that_cannot_be_recorded_becomes_a_problem_comparison(tmp_path, monkeypatch):
    root = make_benchmarks(tmp_path)

    def odd(self):
        return {"passed": True, "criteria": [], "weird": float("nan")}

    monkeypatch.setattr(orchestrator._Session, "compare", odd)
    analyst = FakeStep("analyst", action=write_quantities(GOOD_QUANTITIES))
    result = go(tmp_path, analyst, config=bench_config(), benchmarks_root=root)
    assert result.status == "completed"
    assert result.comparison["passed"] is False and result.comparison["problem"]


# ------------------------------------------------------------------ a run id chosen by the caller


def test_new_run_id_is_built_from_the_first_line_of_the_task():
    moment = datetime(2026, 11, 12, 14, 30, 5, tzinfo=UTC)
    assert (
        orchestrator.new_run_id("\n# Simulate  a *heated* plate!\nmore", moment)
        == "20261112-143005-simulate-a-heated-plate"
    )
    assert orchestrator.new_run_id("   \n", moment) == "20261112-143005-run"


def test_new_run_id_defaults_to_the_current_utc_time():
    assert re.fullmatch(r"\d{8}-\d{6}-a-task", orchestrator.new_run_id("A task"))


def test_a_run_id_given_by_the_caller_names_the_run_directory(tmp_path):
    run_id = orchestrator.new_run_id(TASK, NOW)
    result = go(tmp_path, FakeStep("one"), run_id=run_id)
    assert result.run_id == run_id
    assert result.run_dir == tmp_path / "runs" / run_id
    assert read_record(RunDir(result.run_dir)).run_id == run_id


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "../escape",
        "20261112-143005-ok/../x",
        "20261112-143005-Upper",
        "20261112-143005-",
        "20261112-143005-ok\n",
        "run-1",
        "20261112-143005-a b",
    ],
)
def test_a_malformed_run_id_is_refused_before_anything_is_created(tmp_path, bad):
    with pytest.raises(ValueError, match="run id"):
        go(tmp_path, FakeStep("one"), run_id=bad)
    assert not (tmp_path / "runs").exists()
    assert not active_run(tmp_path).exists()


def test_a_given_run_id_that_exists_is_refused(tmp_path):
    run_id = "20261112-143005-taken"
    (tmp_path / "runs" / run_id).mkdir(parents=True)
    with pytest.raises(RunExistsError):
        go(tmp_path, FakeStep("one"), run_id=run_id)
    assert list((tmp_path / "runs" / run_id).iterdir()) == []
