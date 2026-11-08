"""scripts/replay_scripted_run.py: setup, summary and exit status, with a fake ``run_task``."""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from cfdagent.config import RunConfig
from cfdagent.record import AttemptRecord, StageRecord

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "replay_scripted_run.py"
ROUTE = REPO / "tests" / "fixtures" / "scripted" / "cavity_pipeline.json"
RUN_ID = "20261106-101500-lid-driven-flow"
ENV = {
    "CFDAGENT_IMAGE": "img:from-env",
    "CFDAGENT_STORAGE_ACCOUNT_URL": "https://acct123.blob.core.windows.net",
    "CFDAGENT_RUNS_DIR": "/home/admin/runs",
    "CFDAGENT_STATE_DIR": "/var/lib/cfdagent",
}


def load():
    spec = importlib.util.spec_from_file_location("replay_scripted_run", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["replay_scripted_run"] = module
    spec.loader.exec_module(module)
    return module


rs = load()

CRITERIA = [
    {"quantity": "u_profile", "passed": True, "error": 0.012, "reason": "within 5 %"},
    {"quantity": "v_profile", "passed": True, "error": 0.021, "reason": "within 5 %"},
]


def stage(name, *seconds):
    attempts = [AttemptRecord(n=i, ok=True, seconds=s) for i, s in enumerate(seconds, start=1)]
    return StageRecord(name=name, attempts=attempts, ok=True)


class Fake:
    """A ``run_task`` stand-in that writes a run directory and remembers how it was called."""

    def __init__(self, tmp_path, *, status="completed", passed=True, log=None):
        self.tmp_path = tmp_path
        self.status, self.passed = status, passed
        self.log = (
            [
                "10:15:00 stage planner: start",
                "10:15:09 upload: start",
                "10:16:00 upload: done, 42 file(s)",
            ]
            if log is None
            else log
        )
        self.calls: list[tuple[tuple, dict]] = []
        self.backend_path = None

    def load_backend(self, path):
        self.backend_path = Path(path)
        return "the-backend"

    def run(self, task, config, **kwargs):
        self.calls.append(((task, config), kwargs))
        run_dir = self.tmp_path / RUN_ID
        run_dir.mkdir()
        (run_dir / "run.log").write_text("\n".join(self.log) + "\n", encoding="utf-8")
        (run_dir / "data.bin").write_bytes(b"x" * 2048)
        record = SimpleNamespace(
            stages=[stage("planner", 12.0), stage("geometry", 3.5, 4.5), stage("mesh", 2.0)],
            wall_seconds=125.5,
            cost_usd=0.0,
            failure_reason="bad thing" if self.status != "completed" else None,
            failed_stage="mesh" if self.status != "completed" else None,
        )
        comparison = {"passed": self.passed, "criteria": CRITERIA}
        return SimpleNamespace(
            run_id=RUN_ID, status=self.status, run_dir=run_dir, record=record, comparison=comparison
        )


def replay(tmp_path, capsys, *, environ=None, route=ROUTE, argv_image=None, **fake_options):
    fake = Fake(tmp_path, **fake_options)
    argv = [str(route)] + (["--image", argv_image] if argv_image else [])
    code = rs.main(
        argv,
        environ=ENV if environ is None else environ,
        run=fake.run,
        load_backend=fake.load_backend,
        size=lambda path: 2048,
    )
    return code, fake, capsys.readouterr()


def test_runs_the_pipeline_with_the_docker_sandbox_and_a_real_upload(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)  # the benchmarks folder must not depend on the working directory
    code, fake, _ = replay(tmp_path, capsys)
    assert code == 0
    ((task, config), kwargs) = fake.calls[0]
    assert fake.backend_path == ROUTE
    assert isinstance(config, RunConfig)
    assert config.sandbox.mode == "docker" and not config.sandbox.allow_local
    assert config.sandbox.image == "img:from-env"
    assert config.benchmark_id == "cavity-re100" and config.task_set == "development"
    assert kwargs["backend"] == "the-backend"
    assert kwargs["upload"] is True
    assert kwargs.get("uploader") is None and kwargs.get("sandbox_factory") is None
    assert kwargs["run_root"] == Path("/home/admin/runs")
    assert kwargs["state_dir"] == Path("/var/lib/cfdagent")
    assert kwargs["benchmarks_root"] == REPO / "benchmarks"
    assert isinstance(task, str)


def test_image_option_wins_over_the_environment(tmp_path, capsys):
    _, fake, _ = replay(tmp_path, capsys, argv_image="opt:1")
    assert fake.calls[0][0][1].sandbox.image == "opt:1"


def test_directories_are_left_to_the_defaults_when_unset(tmp_path, capsys):
    environ = {k: v for k, v in ENV.items() if k.endswith(("IMAGE", "URL"))}
    _, fake, _ = replay(tmp_path, capsys, environ=environ)
    kwargs = fake.calls[0][1]
    assert kwargs["run_root"] is None and kwargs["state_dir"] is None


def test_summary_has_everything_the_runbook_needs(tmp_path, capsys):
    code, _, captured = replay(tmp_path, capsys)
    out = captured.out
    assert code == 0
    assert RUN_ID in out and "completed" in out
    assert "planner" in out and "12.0 s" in out
    assert "geometry" in out and "8.0 s" in out  # the attempts of a stage add up
    assert "125.5" in out  # total wall time
    assert "u_profile" in out and "0.012" in out and "v_profile" in out
    assert "passed: True" in out or "comparison: passed" in out
    assert "upload: done, 42 file(s)" in out
    assert "2.0 KiB" in out  # run directory size
    assert (
        f"az storage blob list --auth-mode login --account-name acct123 "
        f"--container-name runs --prefix {RUN_ID}/"
    ) in out


@pytest.mark.parametrize(
    "options",
    [
        {"status": "failed"},
        {"passed": False},
        {"log": ["10:15:00 upload: start", "10:15:30 upload: incomplete; failed: a.txt"]},
        {"log": ["10:15:00 upload failed: ClientAuthenticationError"]},
        {"log": ["10:15:00 upload skipped: CFDAGENT_STORAGE_ACCOUNT_URL is not set"]},
        {"log": ["10:15:00 stage planner: ok"]},  # no upload line at all
    ],
)
def test_exit_status_is_one_unless_completed_passed_and_uploaded(tmp_path, capsys, options):
    code, _, captured = replay(tmp_path, capsys, **options)
    assert code == 1
    assert RUN_ID in captured.out


def test_only_the_last_upload_line_counts(tmp_path, capsys):
    log = ["10:00:00 upload: done, 3 file(s)", "10:00:01 upload failed: OSError"]
    code, _, captured = replay(tmp_path, capsys, log=log)
    assert code == 1 and "upload failed: OSError" in captured.out


@pytest.mark.parametrize(
    "missing", ["CFDAGENT_IMAGE", "CFDAGENT_STORAGE_ACCOUNT_URL"], ids=["image", "storage"]
)
def test_missing_configuration_is_a_usage_error_before_any_run(tmp_path, capsys, missing):
    environ = {k: v for k, v in ENV.items() if k != missing}
    code, fake, captured = replay(tmp_path, capsys, environ=environ)
    assert code == 2 and fake.calls == []
    assert missing in captured.err


def test_missing_route_file_is_a_usage_error(tmp_path, capsys):
    code, fake, captured = replay(tmp_path, capsys, route=tmp_path / "nope.json")
    assert code == 2 and fake.calls == []
    assert "nope.json" in captured.err


def test_an_exception_from_the_run_is_reported_and_fails(tmp_path, capsys):
    def boom(*args, **kwargs):
        raise ValueError("unknown benchmark 'x'")

    code = rs.main([str(ROUTE)], environ=ENV, run=boom, load_backend=lambda p: "b")
    captured = capsys.readouterr()
    assert code == 1
    assert "ValueError" in captured.err and "unknown benchmark" in captured.err


def test_repo_root_is_derived_from_the_script_location():
    assert rs.REPO_ROOT == REPO
    assert (rs.REPO_ROOT / "benchmarks").is_dir()


def test_the_real_route_file_loads_with_the_real_backend():
    backend = rs.ScriptedBackend.from_json(ROUTE)
    assert backend is not None
