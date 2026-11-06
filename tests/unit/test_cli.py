"""The command line: run, status, logs, list. The orchestrator and the backend are faked."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cfdagent import cli, orchestrator
from cfdagent.bench.registry import load_registry
from cfdagent.config import RunConfig
from cfdagent.llm.config import ModelConfig
from cfdagent.orchestrator import RunExistsError, RunResult
from cfdagent.record import RunRecord, write_record
from cfdagent.rundir import RunDir

NOW = datetime(2026, 11, 12, 14, 30, 5, tzinfo=UTC)
TASK = "# Heated plate\n\nSimulate it and report `profile_q`.\n"
BENCH_TASK = "# A demo benchmark task\n\nCompute the profile.\n"
SRC = Path(__file__).resolve().parents[2] / "src"
RUN_A = "20261101-120000-alpha"
DEAD_PID = 2**31 - 2  # no such process; a finished run's process is gone


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def root(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "runs"
    path.mkdir()
    monkeypatch.setenv("CFDAGENT_RUNS_DIR", str(path))
    return path


@pytest.fixture
def benchmarks(tmp_path, monkeypatch) -> Path:
    base = tmp_path / "benchmarks"
    bench = base / "bench-one"
    (bench / "reference").mkdir(parents=True)
    (bench / "reference" / "ref.csv").write_text("x,value\n0.0,0.0\n1.0,1.0\n", encoding="utf-8")
    (bench / "task.md").write_bytes(BENCH_TASK.encode("utf-8"))
    (bench / "benchmark.toml").write_text(
        'id = "bench-one"\nfamily = "demo"\ntask_file = "task.md"\ncitation = "Demo (1999)"\n\n'
        '[[criteria]]\nquantity = "profile_q"\nkind = "profile"\nmetric = "max_abs_error"\n'
        'threshold = 0.05\nreference_file = "reference/ref.csv"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "_registry", lambda: load_registry(base))
    return base


@pytest.fixture
def task_file(tmp_path) -> Path:
    path = tmp_path / "task.md"
    path.write_bytes(TASK.encode("utf-8"))
    return path


# ----------------------------------------------------------------------------- fake run directories


def make_record(run_id: str, status: str = "completed", cost: float = 0.0123, **extra) -> RunRecord:
    fields = {
        "run_id": run_id,
        "task_set": "ad hoc",
        "task_sha256": "a" * 64,
        "started_at": NOW,
        "status": status,
        "models": {"planner": ModelConfig(deployment="gpt-oss-120b")},
        "cfdagent_version": "0.0.1",
        "cost_usd": cost,
    }
    if status != "running":
        fields["finished_at"] = NOW + timedelta(minutes=5)
    fields.update(extra)
    return RunRecord(**fields)


def fake_run(
    base: Path,
    run_id: str,
    *,
    state: str | None = "completed",
    stage: str | None = None,
    pid: int | None = None,
    heartbeat: datetime | str | None = None,
    record: bool | RunRecord = True,
    log: list[str] | None = None,
) -> Path:
    """Write the files the orchestrator would leave; ``None`` leaves a file out."""
    path = base / run_id
    path.mkdir(parents=True)
    if state is not None:
        beat = heartbeat if heartbeat is not None else datetime.now(UTC)
        beat = beat if isinstance(beat, str) else beat.isoformat()
        if pid is None:
            pid = os.getpid() if state == "running" else DEAD_PID
        document = {"state": state, "stage": stage, "pid": pid, "heartbeat": beat}
        (path / "status.json").write_text(json.dumps(document), encoding="utf-8")
    if record is not False:
        rec = record if isinstance(record, RunRecord) else make_record(run_id, state or "failed")
        write_record(RunDir(path), rec)
    if log is not None:
        (path / "run.log").write_bytes("".join(f"{line}\n" for line in log).encode())
    return path


def result_for(run_id: str, status: str, root: Path, **extra) -> RunResult:
    record = make_record(run_id, status, **extra)
    return RunResult(run_id, status, root / run_id, record, None)


class FakeRunTask:
    """Stands in for ``orchestrator.run_task``; remembers its arguments."""

    def __init__(self, status="completed", error=None, **extra):
        self.status, self.error, self.extra = status, error, extra
        self.calls: list[dict] = []

    def __call__(self, task, config, **kwargs):
        self.calls.append({"task": task, "config": config, **kwargs})
        if self.error is not None:
            raise self.error
        run_id = kwargs.get("run_id") or "20261112-143005-heated-plate"
        return result_for(run_id, self.status, kwargs["run_root"], **self.extra)


@pytest.fixture
def fake_run_task(monkeypatch):
    fake = FakeRunTask()
    monkeypatch.setattr(orchestrator, "run_task", fake)
    monkeypatch.setattr(cli, "apply_retention", lambda *a, **k: None)
    return fake


def invoke(runner, *args, **kwargs):
    return runner.invoke(cli.app, list(args), **kwargs)


# ----------------------------------------------------------------------------- run: arguments


def test_run_requires_task_or_benchmark(runner, root, fake_run_task, task_file):
    neither = invoke(runner, "run")
    assert neither.exit_code == 2
    assert "task file or --benchmark" in neither.output
    both = invoke(runner, "run", str(task_file), "--benchmark", "bench-one")
    assert both.exit_code == 2
    assert "not both" in both.output
    assert fake_run_task.calls == []
    assert "Traceback" not in neither.output + both.output


def test_run_with_a_task_file_is_an_ad_hoc_run(runner, root, fake_run_task, task_file):
    result = invoke(runner, "run", str(task_file))
    assert result.exit_code == 0, result.output
    (call,) = fake_run_task.calls
    assert call["task"] == TASK
    assert call["config"].benchmark_id is None
    assert call["config"].task_set == "ad hoc"
    assert call["run_root"] == root
    assert call["upload"] is True
    assert call.get("run_id") is None


def test_run_benchmark_loads_task_and_sets_benchmark_id(runner, root, fake_run_task, benchmarks):
    result = invoke(runner, "run", "--benchmark", "bench-one")
    assert result.exit_code == 0, result.output
    (call,) = fake_run_task.calls
    assert call["task"] == BENCH_TASK
    assert call["config"].benchmark_id == "bench-one"
    assert call["config"].task_set == "development"


def test_an_unknown_benchmark_lists_the_known_ones(runner, root, fake_run_task, benchmarks):
    result = invoke(runner, "run", "--benchmark", "nope")
    assert result.exit_code == 2
    assert "nope" in result.output and "bench-one" in result.output
    assert fake_run_task.calls == []


def test_a_broken_registry_is_a_usage_error_not_a_traceback(
    runner, root, fake_run_task, monkeypatch
):
    from cfdagent.bench.registry import RegistryError

    def broken():
        raise RegistryError("benchmark root benchmarks does not exist")

    monkeypatch.setattr(cli, "_registry", broken)
    result = invoke(runner, "run", "--benchmark", "bench-one")
    assert result.exit_code == 2
    assert "does not exist" in result.output
    assert not isinstance(result.exception, RegistryError)


@pytest.mark.parametrize("kind", ["missing", "directory", "oversized", "binary", "blank"])
def test_a_bad_task_file_is_a_usage_error(runner, root, fake_run_task, tmp_path, kind):
    path = tmp_path / "bad.md"
    if kind == "directory":
        path.mkdir()
    elif kind == "oversized":
        path.write_bytes(b"x" * (cli.MAX_TASK_BYTES + 1))
    elif kind == "binary":
        path.write_bytes(b"\xff\xfe\x00bad")
    elif kind == "blank":
        path.write_text(" \n\n", encoding="utf-8")
    result = invoke(runner, "run", str(path))
    assert result.exit_code == 2
    assert "Traceback" not in result.output
    assert fake_run_task.calls == []


def test_a_task_file_of_exactly_the_limit_is_accepted(runner, root, fake_run_task, tmp_path):
    path = tmp_path / "big.md"
    path.write_bytes(b"x" * cli.MAX_TASK_BYTES)
    assert invoke(runner, "run", str(path)).exit_code == 0


def test_model_and_effort_apply_to_all_six_agents(runner, root, fake_run_task, task_file):
    result = invoke(
        runner, "run", str(task_file), "--model", "gpt-oss-120b", "--reasoning-effort", "high"
    )
    assert result.exit_code == 0, result.output
    models = fake_run_task.calls[0]["config"].models
    assert len(models) == 6
    assert {m.deployment for m in models.values()} == {"gpt-oss-120b"}
    assert {m.reasoning_effort for m in models.values()} == {"high"}
    assert {m.max_tokens for m in models.values()} == {8192}  # the rest of the default stays


def test_another_model_gets_no_effort_unless_one_is_asked_for(
    runner, root, fake_run_task, task_file
):
    assert invoke(runner, "run", str(task_file), "--model", "Llama-3.3-70B-Instruct").exit_code == 0
    models = fake_run_task.calls[0]["config"].models
    assert {m.deployment for m in models.values()} == {"Llama-3.3-70B-Instruct"}
    assert {m.reasoning_effort for m in models.values()} == {None}


def test_the_default_model_keeps_the_default_effort(runner, root, fake_run_task, task_file):
    assert invoke(runner, "run", str(task_file)).exit_code == 0
    default = RunConfig.default()
    assert fake_run_task.calls[0]["config"].models == default.models


def test_an_unpriced_model_is_a_usage_error(runner, root, fake_run_task, task_file):
    result = invoke(runner, "run", str(task_file), "--model", "mystery-model")
    assert result.exit_code == 2
    assert "mystery-model" in result.output and "gpt-oss-120b" in result.output
    assert fake_run_task.calls == []


def test_an_invalid_effort_is_a_usage_error(runner, root, fake_run_task, task_file):
    result = invoke(runner, "run", str(task_file), "--reasoning-effort", "extreme")
    assert result.exit_code == 2
    assert fake_run_task.calls == []


# ----------------------------------------------------------------------------- run: outcome


@pytest.mark.parametrize(
    ("status", "code"), [("completed", 0), ("failed", 1), ("interrupted", 130)]
)
def test_exit_code_follows_the_run_status(runner, root, monkeypatch, task_file, status, code):
    monkeypatch.setattr(orchestrator, "run_task", FakeRunTask(status, cost_usd=0.25))
    monkeypatch.setattr(cli, "apply_retention", lambda *a, **k: None)
    result = invoke(runner, "run", str(task_file))
    assert result.exit_code == code
    assert f"run 20261112-143005-heated-plate: {status}" in result.output
    assert "$0.2500" in result.output
    assert str(root) in result.output.replace("\n", "")


def test_a_failed_run_names_stage_and_reason(runner, root, monkeypatch, task_file):
    fake = FakeRunTask(
        "failed", failed_stage="mesh", failure_reason="checkMesh: 3 failed checks", cost_usd=0.1
    )
    monkeypatch.setattr(orchestrator, "run_task", fake)
    monkeypatch.setattr(cli, "apply_retention", lambda *a, **k: None)
    result = invoke(runner, "run", str(task_file))
    assert result.exit_code == 1
    assert "mesh" in result.output and "checkMesh: 3 failed checks" in result.output


@pytest.mark.parametrize(
    "error",
    [
        RunExistsError("the run directory for run x already exists"),
        ValueError("CFDAGENT_LLM_ENDPOINT is not set"),
    ],
)
def test_errors_before_the_run_starts_are_usage_errors(runner, root, monkeypatch, task_file, error):
    monkeypatch.setattr(orchestrator, "run_task", FakeRunTask(error=error))
    result = invoke(runner, "run", str(task_file))
    assert result.exit_code == 2
    assert str(error) in result.output
    assert "Traceback" not in result.output


def test_no_upload_is_passed_on_and_skips_retention(runner, root, monkeypatch, task_file):
    fake = FakeRunTask()
    monkeypatch.setattr(orchestrator, "run_task", fake)
    calls = []
    monkeypatch.setattr(cli, "apply_retention", lambda *a, **k: calls.append((a, k)))
    assert invoke(runner, "run", str(task_file), "--no-upload").exit_code == 0
    assert fake.calls[0]["upload"] is False
    assert calls == []


def test_retention_runs_after_an_uploaded_run_and_protects_it(runner, root, monkeypatch, task_file):
    monkeypatch.setattr(orchestrator, "run_task", FakeRunTask())
    calls = []
    monkeypatch.setattr(cli, "apply_retention", lambda *a, **k: calls.append((a, k)))
    assert invoke(runner, "run", str(task_file)).exit_code == 0
    ((args, kwargs),) = calls
    assert args == (root,)
    assert kwargs["protect"] == {"20261112-143005-heated-plate"}


def test_a_failing_retention_never_fails_the_run(runner, root, monkeypatch, task_file):
    monkeypatch.setattr(orchestrator, "run_task", FakeRunTask())

    def boom(*a, **k):
        raise OSError("disk on fire")

    monkeypatch.setattr(cli, "apply_retention", boom)
    result = invoke(runner, "run", str(task_file))
    assert result.exit_code == 0
    assert "retention" in result.output


def test_the_run_id_option_is_passed_to_the_orchestrator(runner, root, fake_run_task, task_file):
    result = invoke(runner, "run", str(task_file), "--run-id", RUN_A)
    assert result.exit_code == 0, result.output
    assert fake_run_task.calls[0]["run_id"] == RUN_A
    assert "--run-id" not in invoke(runner, "run", "--help").output  # hidden


def test_a_malformed_run_id_option_is_a_usage_error(runner, root, fake_run_task, task_file):
    result = invoke(runner, "run", str(task_file), "--run-id", "../x")
    assert result.exit_code == 2
    assert fake_run_task.calls == []


def test_the_runs_directory_defaults_to_home_runs(monkeypatch):
    monkeypatch.delenv("CFDAGENT_RUNS_DIR", raising=False)
    assert cli.runs_root() == Path.home() / "runs"
    monkeypatch.setenv("CFDAGENT_RUNS_DIR", "")
    assert cli.runs_root() == Path.home() / "runs"


# ----------------------------------------------------------------------------- the pid check


def test_pid_alive_for_this_process_and_nonsense_values():
    assert cli.pid_alive(os.getpid()) is True
    for bad in (0, -5, None, "12", 2**40, True):
        assert cli.pid_alive(bad) is False


def test_pid_alive_sees_a_running_and_an_exited_child():
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert cli.pid_alive(child.pid) is True
        assert child.poll() is None  # checking did not harm the child
    finally:
        child.kill()
        child.wait(timeout=30)
    assert cli.pid_alive(child.pid) is False


# ----------------------------------------------------------------------------- status


def test_status_marks_dead_pid_as_interrupted(runner, root, monkeypatch):
    fake_run(root, RUN_A, state="running", stage="mesh", pid=4242, record=False)
    monkeypatch.setattr(cli, "pid_alive", lambda pid: False)
    result = invoke(runner, "status", RUN_A)
    assert result.exit_code == 0, result.output
    assert "interrupted" in result.output
    assert "running" not in result.output.replace("not running", "")


def test_status_marks_an_old_heartbeat_as_interrupted(runner, root, monkeypatch):
    old = datetime.now(UTC) - timedelta(minutes=6)
    fake_run(root, RUN_A, state="running", stage="mesh", heartbeat=old, record=False)
    monkeypatch.setattr(cli, "pid_alive", lambda pid: True)
    assert "interrupted" in invoke(runner, "status", RUN_A).output


def test_status_marks_an_unparseable_heartbeat_as_interrupted(runner, root, monkeypatch):
    fake_run(root, RUN_A, state="running", heartbeat="yesterday-ish", record=False)
    monkeypatch.setattr(cli, "pid_alive", lambda pid: True)
    assert "interrupted" in invoke(runner, "status", RUN_A).output


def test_status_shows_a_live_run_as_running(runner, root, monkeypatch):
    recent = datetime.now(UTC) - timedelta(minutes=4)
    fake_run(root, RUN_A, state="running", stage="solver_runner", heartbeat=recent, record=False)
    monkeypatch.setattr(cli, "pid_alive", lambda pid: True)
    result = invoke(runner, "status", RUN_A)
    assert "running" in result.output and "solver_runner" in result.output
    assert "interrupted" not in result.output
    assert "n/a" in result.output  # no record yet: the cost is not known


def test_status_of_a_finished_run_shows_cost_and_failure(runner, root):
    record = make_record(
        RUN_A, "failed", cost=0.4321, failed_stage="mesh", failure_reason="gate: bad cells"
    )
    fake_run(root, RUN_A, state="failed", stage="mesh", record=record)
    result = invoke(runner, "status", RUN_A)
    assert result.exit_code == 0
    for text in ("failed", "mesh", "$0.4321", "gate: bad cells", RUN_A):
        assert text in result.output


def test_status_falls_back_to_the_record_without_a_status_file(runner, root):
    fake_run(root, RUN_A, state=None, record=make_record(RUN_A, "completed", cost=1.5))
    result = invoke(runner, "status", RUN_A)
    assert "completed" in result.output and "$1.5000" in result.output


def test_status_with_nothing_readable_says_unknown(runner, root):
    (root / RUN_A).mkdir()
    result = invoke(runner, "status", RUN_A)
    assert result.exit_code == 0
    assert "unknown" in result.output


def test_status_survives_a_corrupt_status_file(runner, root):
    path = fake_run(root, RUN_A, state="completed")
    (path / "status.json").write_text("{not json", encoding="utf-8")
    result = invoke(runner, "status", RUN_A)
    assert result.exit_code == 0 and "completed" in result.output  # from the record


def test_status_of_an_unknown_run_is_a_usage_error(runner, root):
    result = invoke(runner, "status", "20261101-120000-nothing")
    assert result.exit_code == 2
    assert "Traceback" not in result.output


@pytest.mark.parametrize(
    "bad", ["..", "../x", "20261101-120000-a/../../b", "x", "20261101-120000-A"]
)
def test_run_ids_are_validated_before_any_path_is_built(runner, root, bad):
    for command in ("status", "logs"):
        result = invoke(runner, command, bad)
        assert result.exit_code == 2, (command, bad)
        assert "run id" in result.output


def test_status_of_a_detached_run_that_has_not_started_points_to_the_launcher_log(runner, root):
    (root / f".launch-{RUN_A}.log").write_bytes(b"starting\n")
    result = invoke(runner, "status", RUN_A)
    assert result.exit_code == 0
    assert "not started" in result.output and "launcher log" in result.output


def test_status_refuses_a_run_directory_that_is_a_link(runner, root, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    make_link(root / RUN_A, target)
    result = invoke(runner, "status", RUN_A)
    assert result.exit_code == 2


def make_link(link: Path, target: Path) -> None:
    """A symbolic link, or a junction on a Windows account that may not create symlinks."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except OSError:
        if sys.platform != "win32":
            pytest.skip("symbolic links cannot be created here")
    done = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        timeout=30,
        check=False,
    )
    if done.returncode != 0:
        pytest.skip("neither symlinks nor junctions can be created here")


def test_status_says_so_while_the_finished_run_is_still_uploading(runner, root, monkeypatch):
    fake_run(root, RUN_A, state="completed", pid=os.getpid(), log=["t run: completed"])
    monkeypatch.setattr(cli, "pid_alive", lambda pid: True)
    result = invoke(runner, "status", RUN_A)
    assert "state: completed" in result.output
    assert "finalising: upload in progress" in result.output


def test_status_has_no_note_once_the_upload_line_or_the_process_is_gone(runner, root, monkeypatch):
    fake_run(root, RUN_A, state="completed", pid=os.getpid(), log=["t upload: done, 2 file(s)"])
    fake_run(root, "20261102-120000-beta", state="completed", log=["t run: completed"])
    monkeypatch.setattr(cli, "pid_alive", lambda pid: pid == os.getpid())
    assert "finalising" not in invoke(runner, "status", RUN_A).output
    assert "finalising" not in invoke(runner, "status", "20261102-120000-beta").output


def test_control_characters_are_stripped_from_what_is_echoed(runner, root):
    esc = chr(27)
    reason = f"bad {esc}[31mred{esc}[0m\tend"
    record = make_record(RUN_A, "failed", failed_stage="mesh", failure_reason=reason)
    path = fake_run(root, RUN_A, state="failed", stage="mesh", record=record)
    (path / "run.log").write_bytes(f"a {esc}]0;title\x07b\tc\r\nd\n".encode())
    status = invoke(runner, "status", RUN_A).output
    logs = invoke(runner, "logs", RUN_A).output
    for text in (status, logs):
        assert esc not in text and "\x07" not in text and "\r" not in text
    assert "bad [31mred[0m\tend" in status
    assert logs == "a ]0;titleb\tc\nd\n"


def test_the_runs_directory_expands_a_home_reference(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("CFDAGENT_RUNS_DIR", "~/my-runs")
    assert cli.runs_root() == tmp_path / "my-runs"


# ----------------------------------------------------------------------------- logs


def test_logs_prints_the_run_log(runner, root):
    fake_run(root, RUN_A, log=["t stage planner: start", "t run: completed"])
    result = invoke(runner, "logs", RUN_A)
    assert result.exit_code == 0
    assert result.output == "t stage planner: start\nt run: completed\n"


def test_logs_of_an_unknown_run_is_a_usage_error(runner, root):
    result = invoke(runner, "logs", "20261101-120000-nothing")
    assert result.exit_code == 2


class Sleeper:
    """Replaces the polling sleep: runs a scripted action per call and bounds the loop."""

    def __init__(self, *actions, limit=20):
        self.actions, self.limit, self.calls = list(actions), limit, 0

    def __call__(self, seconds):
        self.calls += 1
        assert self.calls <= self.limit, "the follow loop did not stop"
        if self.actions:
            action = self.actions.pop(0)
            if action is not None:
                action()


def append(path: Path, text: str):
    def action():
        with open(path, "ab") as handle:
            handle.write(text.encode("utf-8"))

    return action


def set_state(path: Path, state: str):
    def action():
        document = json.loads((path / "status.json").read_text(encoding="utf-8"))
        document["state"] = state
        (path / "status.json").write_text(json.dumps(document), encoding="utf-8")

    return action


def test_logs_follow_stops_when_run_finished(runner, root, monkeypatch):
    path = fake_run(root, RUN_A, state="running", record=False, log=["one"])
    alive = {"yes": True}
    monkeypatch.setattr(cli, "pid_alive", lambda pid: alive["yes"])

    def finish():
        # the last line and the end of the run land together; the process exits a moment later
        append(path / "run.log", "two\nrun: completed\n")()
        set_state(path, "completed")()

    def exit_process():
        append(path / "run.log", "late\n")()
        alive["yes"] = False

    sleeper = Sleeper(append(path / "run.log", "mid\n"), finish, exit_process)
    monkeypatch.setattr(cli, "_sleep", sleeper)
    result = invoke(runner, "logs", RUN_A, "--follow")
    assert result.exit_code == 0
    assert result.output == "one\nmid\ntwo\nrun: completed\nlate\n"
    assert sleeper.calls == 3


def test_logs_follow_goes_on_through_the_upload_and_stops_at_its_last_line(
    runner, root, monkeypatch
):
    path = fake_run(root, RUN_A, state="running", record=False, log=["t stage analyst: ok"])
    monkeypatch.setattr(cli, "pid_alive", lambda pid: True)  # the process uploads, so it is alive

    def finish():
        append(path / "run.log", "t run: completed\n")()
        set_state(path, "completed")()

    sleeper = Sleeper(
        finish,
        append(path / "run.log", "t upload: start\n"),
        append(path / "run.log", "t upload: done, 3 file(s)\n"),
    )
    monkeypatch.setattr(cli, "_sleep", sleeper)
    result = invoke(runner, "logs", RUN_A, "--follow")
    assert result.exit_code == 0
    assert result.output.endswith("t upload: start\nt upload: done, 3 file(s)\n")
    assert sleeper.calls == 3


@pytest.mark.parametrize(
    "line",
    [
        "t upload: incomplete; failed: a.txt",
        "t upload failed: ConnectionError",
        "t upload skipped: CFDAGENT_STORAGE_ACCOUNT_URL is not set",
    ],
)
def test_logs_follow_stops_at_every_terminal_upload_line(runner, root, monkeypatch, line):
    path = fake_run(root, RUN_A, state="completed", pid=os.getpid(), log=["t run: completed"])
    monkeypatch.setattr(cli, "pid_alive", lambda pid: True)
    monkeypatch.setattr(cli, "_sleep", Sleeper(append(path / "run.log", line + "\n"), limit=3))
    result = invoke(runner, "logs", RUN_A, "--follow")
    assert result.output.endswith(line + "\n")


def test_logs_follow_gives_up_on_a_finished_run_whose_process_never_ends(runner, root, monkeypatch):
    fake_run(root, RUN_A, state="completed", pid=os.getpid(), log=["t run: completed"])
    monkeypatch.setattr(cli, "pid_alive", lambda pid: True)
    monkeypatch.setattr(cli, "POLL_INTERVAL_S", 1.0)
    monkeypatch.setattr(cli, "FINALISE_GRACE_S", 3)
    sleeper = Sleeper(limit=10)
    monkeypatch.setattr(cli, "_sleep", sleeper)
    result = invoke(runner, "logs", RUN_A, "--follow")
    assert result.exit_code == 0
    assert result.output == "t run: completed\n"
    assert sleeper.calls == 3


def test_logs_follow_of_a_finished_run_prints_once_and_returns(runner, root, monkeypatch):
    fake_run(root, RUN_A, log=["a", "b"])
    sleeper = Sleeper(limit=0)
    monkeypatch.setattr(cli, "_sleep", sleeper)
    result = invoke(runner, "logs", RUN_A, "--follow")
    assert result.output == "a\nb\n"
    assert sleeper.calls == 0


def test_logs_follow_stops_when_the_run_died(runner, root, monkeypatch):
    fake_run(root, RUN_A, state="running", record=False, log=["a"])
    monkeypatch.setattr(cli, "pid_alive", lambda pid: False)
    monkeypatch.setattr(cli, "_sleep", Sleeper(limit=0))
    result = invoke(runner, "logs", RUN_A, "--follow")
    assert result.exit_code == 0 and result.output == "a\n"


def test_logs_follow_waits_for_a_log_that_does_not_exist_yet(runner, root, monkeypatch):
    path = fake_run(root, RUN_A, state="running", record=False)
    monkeypatch.setattr(cli, "pid_alive", lambda pid: True)

    def create():
        append(path / "run.log", "hello\n")()
        set_state(path, "completed")()

    monkeypatch.setattr(cli, "_sleep", Sleeper(None, create))
    result = invoke(runner, "logs", RUN_A, "--follow")
    assert result.output == "hello\n"


def test_logs_follow_reads_in_pieces_and_tolerates_a_split_character(runner, root, monkeypatch):
    path = fake_run(root, RUN_A, state="running", record=False)
    monkeypatch.setattr(cli, "pid_alive", lambda pid: True)
    data = "café ✓\n".encode()

    def first():
        with open(path / "run.log", "ab") as handle:
            handle.write(data[:4])  # ends inside the two-byte character

    def second():
        with open(path / "run.log", "ab") as handle:
            handle.write(data[4:])
        set_state(path, "completed")()

    monkeypatch.setattr(cli, "_sleep", Sleeper(first, second))
    assert invoke(runner, "logs", RUN_A, "--follow").output == "café ✓\n"


def test_logs_replaces_bytes_that_are_not_utf8(runner, root):
    path = fake_run(root, RUN_A)
    (path / "run.log").write_bytes(b"ok \xff\xfe end\n")
    result = invoke(runner, "logs", RUN_A)
    assert result.exit_code == 0
    assert "ok" in result.output and "end" in result.output


def test_logs_follow_starts_again_when_the_file_shrinks(runner, root, monkeypatch):
    path = fake_run(root, RUN_A, state="running", record=False, log=["a long first line"])
    monkeypatch.setattr(cli, "pid_alive", lambda pid: True)

    def rewrite():
        (path / "run.log").write_bytes(b"new\n")
        set_state(path, "completed")()

    monkeypatch.setattr(cli, "_sleep", Sleeper(rewrite))
    assert invoke(runner, "logs", RUN_A, "--follow").output == "a long first line\nnew\n"


def test_logs_falls_back_to_the_launcher_log_when_the_run_directory_is_missing(runner, root):
    (root / f".launch-{RUN_A}.log").write_bytes(b"Traceback: boom\n")
    result = invoke(runner, "logs", RUN_A)
    assert result.exit_code == 0 and result.output == "Traceback: boom\n"


def test_logs_follow_without_a_run_directory_gives_up_after_a_grace_period(
    runner, root, monkeypatch
):
    (root / f".launch-{RUN_A}.log").write_bytes(b"starting\n")
    sleeper = Sleeper(limit=cli.START_GRACE_S + 5)
    monkeypatch.setattr(cli, "_sleep", sleeper)
    monkeypatch.setattr(cli, "POLL_INTERVAL_S", 1.0)
    result = invoke(runner, "logs", RUN_A, "--follow")
    assert result.exit_code == 1
    assert "starting" in result.output and "did not start" in result.output
    assert sleeper.calls <= cli.START_GRACE_S + 1


def test_logs_follow_switches_to_the_run_log_once_the_run_starts(runner, root, monkeypatch):
    (root / f".launch-{RUN_A}.log").write_bytes(b"launcher\n")

    def start():
        path = fake_run(root, RUN_A, log=["run: start", "run: completed"])
        assert path.exists()

    monkeypatch.setattr(cli, "_sleep", Sleeper(start))
    result = invoke(runner, "logs", RUN_A, "--follow")
    assert result.exit_code == 0
    assert result.output == "launcher\nrun: start\nrun: completed\n"


# ----------------------------------------------------------------------------- list


def test_list_shows_recent_runs(runner, root, monkeypatch):
    monkeypatch.setattr(cli, "pid_alive", lambda pid: False)
    fake_run(root, "20261101-120000-alpha", state="completed", record=make_record("x", cost=0.5))
    fake_run(
        root,
        "20261102-120000-beta",
        state="failed",
        stage="mesh",
        record=make_record("x", "failed", cost=0.25, failed_stage="mesh"),
    )
    fake_run(root, "20261103-120000-gamma", state="running", stage="solver_runner", record=False)
    (root / "not-a-run").mkdir()
    (root / "20261104-120000-empty").mkdir()  # no status and no record: not a run
    result = invoke(runner, "list")
    assert result.exit_code == 0
    lines = [ln for ln in result.output.splitlines() if "-" in ln and ln[:2] == "20"]
    assert [ln.split()[0] for ln in lines] == [
        "20261103-120000-gamma",
        "20261102-120000-beta",
        "20261101-120000-alpha",
    ]
    assert "interrupted" in lines[0] and "solver_runner" in lines[0] and "n/a" in lines[0]
    assert "failed" in lines[1] and "mesh" in lines[1] and "$0.2500" in lines[1]
    assert "completed" in lines[2] and "$0.5000" in lines[2]
    assert "not-a-run" not in result.output and "empty" not in result.output


def test_list_limits_the_rows_and_says_when_there_are_none(runner, root):
    assert "no runs" in invoke(runner, "list").output
    for day in range(1, 6):
        fake_run(root, f"2026110{day}-120000-r{day}")
    default = invoke(runner, "list")
    assert default.output.count("-r") == 5
    limited = invoke(runner, "list", "--limit", "2")
    assert "r5" in limited.output and "r4" in limited.output and "r3" not in limited.output
    assert invoke(runner, "list", "--limit", "0").exit_code == 2


def test_list_shows_the_default_ten_rows(runner, root):
    for n in range(12):
        fake_run(root, f"202611{n + 1:02d}-120000-r{n}")
    out = invoke(runner, "list").output
    assert sum(1 for ln in out.splitlines() if ln.startswith("202611")) == 10


# ----------------------------------------------------------------------------- retention


def done_log():
    return ["t run: completed", "t upload: done, 12 file(s)"]


def build_runs(root: Path, count: int = 14) -> dict[str, Path]:
    """Runs 01..count with complete uploads (oldest first)."""
    return {
        f"202611{n:02d}-120000-r{n}": fake_run(root, f"202611{n:02d}-120000-r{n}", log=done_log())
        for n in range(1, count + 1)
    }


def test_retention_keeps_latest_ten_and_failed_uploads(root):
    runs = build_runs(root, 14)
    names = sorted(runs)
    # the four oldest: complete, upload failed, upload skipped, incomplete
    (runs[names[1]] / "run.log").write_bytes(b"t upload failed: ConnectionError\n")
    (runs[names[2]] / "run.log").write_text(
        "t upload skipped: CFDAGENT_STORAGE_ACCOUNT_URL is not set\n", encoding="utf-8"
    )
    (runs[names[3]] / "run.log").write_bytes(b"t upload: incomplete; failed: a\n")
    (root / f".launch-{names[0]}.log").write_bytes(b"x")
    (root / f".launch-{names[1]}.log").write_bytes(b"x")
    (root / "not-a-run").mkdir()
    (root / "20261105-000000-nostatus").mkdir()

    cli.apply_retention(root, protect={names[-1]})

    remaining = sorted(p.name for p in root.iterdir() if p.is_dir())
    assert names[0] not in remaining
    assert all(name in remaining for name in names[1:])  # failed, skipped, incomplete, newest ten
    assert "not-a-run" in remaining and "20261105-000000-nostatus" in remaining
    assert not (root / f".launch-{names[0]}.log").exists()  # removed with its run
    assert (root / f".launch-{names[1]}.log").exists()  # its run is still there


def test_retention_deletes_old_complete_runs_and_counts_kept_ones(root):
    runs = build_runs(root, 13)
    names = sorted(runs)
    (runs[names[0]] / "run.log").write_bytes(b"t upload failed: X\n")
    cli.apply_retention(root)
    remaining = sorted(p.name for p in root.iterdir())
    # 13 candidates: the oldest three are beyond ten; the first (failed upload) stays
    assert remaining == [names[0], *names[3:]]


def test_retention_never_deletes_a_running_run_with_a_live_pid(root):
    runs = build_runs(root, 12)
    names = sorted(runs)
    oldest = runs[names[0]]
    (oldest / "status.json").write_text(
        json.dumps(
            {
                "state": "running",
                "stage": "mesh",
                "pid": os.getpid(),
                "heartbeat": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    cli.apply_retention(root)
    assert oldest.exists()
    assert not runs[names[1]].exists()


def test_retention_reads_the_state_with_the_same_dead_pid_rule(root, monkeypatch):
    runs = build_runs(root, 11)
    names = sorted(runs)
    (runs[names[0]] / "status.json").write_text(
        json.dumps({"state": "running", "stage": None, "pid": 99, "heartbeat": "x"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "pid_alive", lambda pid: False)
    cli.apply_retention(root)
    assert not runs[names[0]].exists()  # dead pid and a complete upload: deletable


FORGED = "t upload: done, 1 file(s)"


@pytest.mark.parametrize(
    "lines",
    [
        # a failure reason that carries a forged line, then the real upload failure
        ["t run: failed (x\n" + FORGED, "t upload failed: ConnectionError"],
        [FORGED, "t upload: incomplete; failed: a.txt"],
        [FORGED, "t upload skipped: CFDAGENT_STORAGE_ACCOUNT_URL is not set"],
        [FORGED, "t upload: start"],  # the process died during the upload
    ],
)
def test_retention_trusts_only_the_last_upload_line(root, lines):
    runs = build_runs(root, 11)
    names = sorted(runs)
    (runs[names[0]] / "run.log").write_bytes(("\n".join(lines) + "\n").encode())
    cli.apply_retention(root)
    assert runs[names[0]].exists()


def test_retention_deletes_when_the_last_upload_line_is_done(root):
    runs = build_runs(root, 11)
    names = sorted(runs)
    log = "t upload: start\nt upload: done, 4 file(s)\nt summary.md could not be extended: x\n"
    (runs[names[0]] / "run.log").write_bytes(log.encode())
    cli.apply_retention(root)
    assert not runs[names[0]].exists()


def test_retention_without_a_log_or_with_an_unreadable_one_keeps_the_run(root):
    runs = build_runs(root, 12)
    names = sorted(runs)
    (runs[names[0]] / "run.log").unlink()
    (runs[names[1]] / "run.log").unlink()
    (runs[names[1]] / "run.log").mkdir()  # a directory where the log should be
    cli.apply_retention(root)
    assert runs[names[0]].exists() and runs[names[1]].exists()


def test_retention_ignores_symbolic_links_and_leaves_their_targets_alone(root, tmp_path):
    runs = build_runs(root, 11)
    names = sorted(runs)
    target = tmp_path / "precious"
    target.mkdir()
    (target / "status.json").write_text("{}", encoding="utf-8")
    (target / "run.log").write_bytes(b"t upload: done, 1 file(s)\n")
    (target / "data.txt").write_text("keep me", encoding="utf-8")
    make_link(root / "20260101-000000-linked", target)  # the oldest name: it would be deleted

    cli.apply_retention(root)

    assert not runs[names[0]].exists()  # 11 real candidates: the oldest goes
    assert (target / "data.txt").read_text(encoding="utf-8") == "keep me"
    assert (root / "20260101-000000-linked").exists()


def test_retention_does_not_follow_a_link_inside_a_run(root, tmp_path):
    runs = build_runs(root, 11)
    names = sorted(runs)
    target = tmp_path / "outside"
    target.mkdir()
    (target / "data.txt").write_text("keep me", encoding="utf-8")
    make_link(runs[names[0]] / "linked", target)
    cli.apply_retention(root)
    assert not runs[names[0]].exists()
    assert (target / "data.txt").read_text(encoding="utf-8") == "keep me"


def test_retention_protects_the_named_run_and_never_raises(root, monkeypatch, capsys):
    runs = build_runs(root, 12)
    names = sorted(runs)
    cli.apply_retention(root, protect={names[0]})
    assert runs[names[0]].exists() and not runs[names[1]].exists()

    def failing(path):
        raise OSError("locked")

    monkeypatch.setattr(cli, "_remove_tree", failing)
    cli.apply_retention(root, keep=1)  # tries to delete many; every failure is a warning
    err = capsys.readouterr().err
    assert "warning" in err and "retention" in err


def test_retention_removes_stale_orphan_launcher_logs_only(root):
    build_runs(root, 2)
    old = root / ".launch-20260101-000000-gone.log"
    fresh = root / ".launch-20261110-000000-starting.log"
    other = root / ".launch-notarunid.log"
    for path in (old, fresh, other):
        path.write_text("x", encoding="utf-8")
    long_ago = time.time() - 3 * 24 * 3600
    os.utime(old, (long_ago, long_ago))
    os.utime(other, (long_ago, long_ago))
    cli.apply_retention(root)
    assert not old.exists()
    assert fresh.exists()  # a detached run may be about to create its directory
    assert other.exists()  # not ours


def test_retention_on_a_missing_root_is_a_no_op(tmp_path):
    cli.apply_retention(tmp_path / "nothing")


# ----------------------------------------------------------------------------- detach


class FakePopen:
    instances: list[FakePopen] = []

    def __init__(self, args, **kwargs):
        self.args, self.kwargs = args, kwargs
        self.pid = 4321
        FakePopen.instances.append(self)


@pytest.fixture
def popen(monkeypatch):
    FakePopen.instances = []
    monkeypatch.setattr(cli.subprocess, "Popen", FakePopen)
    monkeypatch.setenv(
        "CFDAGENT_LLM_ENDPOINT", "https://<account>.services.ai.azure.com/openai/v1/"
    )
    return FakePopen


def test_detach_returns_run_id_immediately(runner, root, popen, task_file, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_task", FakeRunTask(error=AssertionError("not here")))
    result = invoke(runner, "run", str(task_file), "--detach")
    assert result.exit_code == 0, result.output
    run_id = result.stdout.strip().splitlines()[0]
    assert run_id.endswith("-heated-plate")
    assert orchestrator.new_run_id(TASK, datetime.now(UTC)).split("-")[2:] == run_id.split("-")[2:]
    (child,) = popen.instances
    argv = child.args
    assert argv[:4] == [sys.executable, "-m", "cfdagent", "run"]
    assert "--detach" not in argv
    assert argv[argv.index("--run-id") + 1] == run_id
    assert str(task_file.resolve()) in argv
    assert not (root / run_id).exists()  # the orchestrator creates it, not the CLI


def test_detach_starts_the_child_in_a_new_session_with_a_launcher_log_outside_the_run_dir(
    runner, root, popen, task_file
):
    result = invoke(runner, "run", str(task_file), "--detach")
    run_id = result.stdout.strip().splitlines()[0]
    (child,) = popen.instances
    kwargs = child.kwargs
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["close_fds"] is True
    if sys.platform == "win32":
        flags = kwargs["creationflags"]
        assert flags & subprocess.DETACHED_PROCESS
        assert flags & subprocess.CREATE_NEW_PROCESS_GROUP
        assert flags & subprocess.CREATE_NO_WINDOW
    else:
        assert kwargs["start_new_session"] is True
    launcher = root / f".launch-{run_id}.log"
    assert launcher.is_file()
    assert launcher.read_text(encoding="utf-8") == ""  # only what the child prints goes there
    assert kwargs["stdout"].name == str(launcher) and kwargs["stderr"] is kwargs["stdout"]
    assert kwargs["stdout"].closed  # the parent let go of the file


def test_detach_passes_the_options_on_and_never_the_benchmark_to_a_file(
    runner, root, popen, benchmarks
):
    result = invoke(
        runner,
        "run",
        "--benchmark",
        "bench-one",
        "--model",
        "Llama-3.3-70B-Instruct",
        "--reasoning-effort",
        "low",
        "--no-upload",
        "--detach",
    )
    assert result.exit_code == 0, result.output
    run_id = result.stdout.strip().splitlines()[0]
    argv = popen.instances[0].args
    assert argv[argv.index("--benchmark") + 1] == "bench-one"
    assert argv[argv.index("--model") + 1] == "Llama-3.3-70B-Instruct"
    assert argv[argv.index("--reasoning-effort") + 1] == "low"
    assert "--no-upload" in argv
    assert "bench-one" not in run_id  # the id comes from the task text
    for path in root.rglob("*"):
        assert "bench-one" not in path.read_text(encoding="utf-8") if path.is_file() else True


def test_detach_checks_the_environment_before_starting_anything(
    runner, root, task_file, monkeypatch
):
    monkeypatch.delenv("CFDAGENT_LLM_ENDPOINT", raising=False)
    FakePopen.instances = []
    monkeypatch.setattr(cli.subprocess, "Popen", FakePopen)
    result = invoke(runner, "run", str(task_file), "--detach")
    assert result.exit_code == 2
    assert "CFDAGENT_LLM_ENDPOINT" in result.output
    assert FakePopen.instances == []
    assert list(root.iterdir()) == []


def test_detach_validates_the_arguments_like_a_foreground_run(runner, root, popen):
    assert invoke(runner, "run", "--detach").exit_code == 2
    assert popen.instances == []


def test_detach_refuses_an_id_that_exists_and_cleans_up_when_the_start_fails(
    runner, root, popen, task_file, monkeypatch
):
    def failing(*a, **k):
        raise OSError("no such interpreter")

    monkeypatch.setattr(cli.subprocess, "Popen", failing)
    result = invoke(runner, "run", str(task_file), "--detach")
    assert result.exit_code == 1
    assert "could not start" in result.output
    assert list(root.iterdir()) == []  # no orphan launcher log


# ----------------------------------------------------------------------------- start-up and imports


def _python(code: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=str(SRC))
    return subprocess.run(
        [sys.executable, "-c", code, *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_the_light_commands_do_not_import_the_heavy_layers():
    code = (
        "import sys, cfdagent.cli\n"
        "heavy = ('cfdagent.orchestrator', 'cfdagent.agents', 'cfdagent.sandbox.client',\n"
        "         'cfdagent.storage', 'cfdagent.tools', 'cadquery', 'gmsh', 'pyvista')\n"
        "bad = [m for m in heavy if m in sys.modules]\n"
        "print(','.join(bad))\n"
    )
    done = _python(code)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == ""


def test_python_dash_m_cfdagent_shows_the_commands():
    env = dict(os.environ, PYTHONPATH=str(SRC))
    done = subprocess.run(
        [sys.executable, "-m", "cfdagent", "--help"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    for word in ("run", "status", "logs", "list"):
        assert word in done.stdout


def test_the_console_script_is_declared():
    text = (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    assert '[project.scripts]\ncfdagent = "cfdagent.cli:app"' in text
