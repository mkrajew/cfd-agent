"""SandboxClient: container naming, timeouts, cleanup and hostile responses (no Docker).

Local-mode tests start the real executor as a plain subprocess; they need nothing but Python.
"""

import json
import math
import os
import subprocess
import sys
import time

import pytest

from cfdagent.executor.protocol import OpResponse, RunSolverArgs
from cfdagent.rundir import RunDir
from cfdagent.sandbox import client as client_module
from cfdagent.sandbox.client import OUTER_TIMEOUT_MARGIN_S, SandboxClient, outer_timeout
from cfdagent.sandbox.config import SandboxConfig

RUN_ID = "20260101-000000-demo"
SOLVE_ARGS = {"case_dir": "case", "solver": "icoFoam"}


def _config(**overrides):
    values = {"image": "cfdagent:test", "uid": 1234, "gid": 4321}
    values.update(overrides)
    if values.get("mode") == "local":
        values["allow_local"] = True
    return SandboxConfig(**values)


def _value_after(argv, flag):
    assert argv.count(flag) == 1
    return argv[argv.index(flag) + 1]


class FakeProcess:
    """Stands in for the ``docker run`` client process."""

    def __init__(self, fake, argv, kwargs):
        self.fake, self.argv, self.kwargs = fake, argv, kwargs
        self.pid = 4242
        self.returncode = None
        self.killed = False
        self.waits = 0

    def wait(self, timeout=None):
        self.waits += 1
        behaviour = self.fake.behaviour
        if behaviour == "interrupt":
            raise KeyboardInterrupt
        if behaviour == "hang" and not self.fake.container_killed:
            raise subprocess.TimeoutExpired(self.argv, timeout)
        if behaviour == "stuck" and not self.killed:
            raise subprocess.TimeoutExpired(self.argv, timeout)
        self.fake.on_exit(self)
        self.returncode = self.fake.exit_code
        return self.returncode

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True

    terminate = kill


class FakeRunner:
    """Records every process the client starts; plays the executor when the run is normal."""

    def __init__(self, behaviour="normal", exit_code=0, response=None, stderr=b""):
        self.behaviour, self.exit_code = behaviour, exit_code
        self.response, self.stderr = response, stderr
        self.popen_calls: list[list[str]] = []
        self.run_calls: list[list[str]] = []
        self.container_killed = False
        self.processes: list[FakeProcess] = []
        self.run_dir: RunDir | None = None

    def popen(self, argv, **kwargs):
        self.popen_calls.append(list(argv))
        proc = FakeProcess(self, argv, kwargs)
        self.processes.append(proc)
        if self.stderr:
            kwargs["stdout"].write(self.stderr)
            kwargs["stdout"].flush()
        return proc

    def run(self, argv, **kwargs):
        self.run_calls.append(list(argv))
        if argv[:2] == ["docker", "kill"]:
            self.container_killed = True
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    def on_exit(self, proc):
        if self.response is None:
            return
        request_rel = proc.argv[-1]
        target = self.run_dir.root / request_rel.replace(".request.json", ".response.json")
        body = self.response if isinstance(self.response, str) else self.response.model_dump_json()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")


def _client(tmp_path, runner, **config_overrides):
    root = tmp_path / "run"
    root.mkdir(exist_ok=True)
    run_dir = RunDir(root)
    runner.run_dir = run_dir
    client = SandboxClient(
        _config(**config_overrides), run_dir, RUN_ID, popen=runner.popen, run=runner.run
    )
    return client, run_dir


def test_client_runs_docker_with_a_unique_named_container_per_call(tmp_path):
    runner = FakeRunner(response=OpResponse(ok=True, data={"v": 1}))
    client, run_dir = _client(tmp_path, runner)
    first = client.call("run_solver", SOLVE_ARGS, timeout_s=30)
    second = client.call("run_solver", SOLVE_ARGS, timeout_s=30)
    assert first.ok and first.data == {"v": 1} and second.ok
    names = [_value_after(argv, "--name") for argv in runner.popen_calls]
    assert names == [f"cfd-{RUN_ID}-1", f"cfd-{RUN_ID}-2"]
    argv = runner.popen_calls[0]
    assert argv[:3] == ["docker", "run", "--rm"]
    command = argv[argv.index("cfdagent:test") + 1 :]
    assert command[:5] == ["python", "-P", "-m", "cfdagent.executor", "run_solver"]
    request_rel = argv[-1]
    assert request_rel.startswith(".sandbox/") and request_rel.endswith(".request.json")
    request = run_dir.read_json(request_rel)
    assert request["op"] == "run_solver"
    assert request["args"] == {**SOLVE_ARGS, "n_procs": 1, "timeout_s": 7200.0}
    assert runner.popen_calls[0][-1] != runner.popen_calls[1][-1]  # separate request files


def test_client_accepts_pydantic_args(tmp_path):
    runner = FakeRunner(response=OpResponse(ok=True))
    client, run_dir = _client(tmp_path, runner)
    args = RunSolverArgs(case_dir="case", solver="icoFoam", n_procs=2)
    client.call("run_solver", args, 30)
    assert run_dir.read_json(runner.popen_calls[0][-1])["args"]["n_procs"] == 2


def test_client_kills_named_container_on_timeout(tmp_path):
    runner = FakeRunner(behaviour="hang")
    client, _ = _client(tmp_path, runner)
    response = client.call("run_solver", SOLVE_ARGS, timeout_s=0.5)
    name = f"cfd-{RUN_ID}-1"
    assert ["docker", "kill", name] in runner.run_calls
    assert runner.run_calls.index(["docker", "kill", name]) == 0  # the container first
    assert ["docker", "rm", "-f", name] in runner.run_calls  # belt and braces
    assert response.ok is False
    assert "timed out" in response.error and name in response.error
    assert runner.processes[0].waits >= 2  # waited for the client to exit after the kill


def test_client_kills_the_client_process_if_docker_kill_does_not_end_it(tmp_path):
    runner = FakeRunner(behaviour="stuck")
    client, _ = _client(tmp_path, runner)
    response = client.call("run_solver", SOLVE_ARGS, timeout_s=0.1)
    assert runner.processes[0].killed
    assert response.ok is False


def test_client_cleans_up_on_keyboard_interrupt(tmp_path):
    runner = FakeRunner(behaviour="interrupt")
    client, _ = _client(tmp_path, runner)
    with pytest.raises(KeyboardInterrupt):
        client.call("run_solver", SOLVE_ARGS, timeout_s=30)
    assert ["docker", "kill", f"cfd-{RUN_ID}-1"] in runner.run_calls
    assert runner.processes[0].killed


def test_timeout_ignores_a_response_written_before_the_kill(tmp_path):
    runner = FakeRunner(behaviour="hang", response=OpResponse(ok=True, data={"late": 1}))
    client, _ = _client(tmp_path, runner)
    response = client.call("run_solver", SOLVE_ARGS, timeout_s=0.1)
    assert response.ok is False


def test_missing_response_is_a_failed_response_with_the_stderr_tail(tmp_path):
    runner = FakeRunner(exit_code=137, stderr=b"line one\nOOM killed marker\n")
    client, _ = _client(tmp_path, runner)
    response = client.call("run_solver", SOLVE_ARGS, timeout_s=30)
    assert response.ok is False and response.internal is True
    assert "137" in response.error and "OOM killed marker" in response.error


def test_stderr_tail_is_capped_by_bytes(tmp_path):
    runner = FakeRunner(exit_code=1, stderr=b"y" * 5_000_000 + b"END-MARKER")
    client, _ = _client(tmp_path, runner)
    response = client.call("run_solver", SOLVE_ARGS, timeout_s=30)
    assert "END-MARKER" in response.error
    assert len(response.error) < 20_000


@pytest.mark.parametrize(
    "body",
    [
        "{not json",
        "[]",
        '{"ok": "yes", "pwned": 1}',
        '{"ok": true, "extra": 1}',
        "x" * 2_000_000,
        "[" * 200_000,
    ],
)
def test_garbled_or_hostile_response_is_a_failed_response(tmp_path, body):
    runner = FakeRunner(response=body, stderr=b"container said hi\n")
    client, _ = _client(tmp_path, runner)
    response = client.call("run_solver", SOLVE_ARGS, timeout_s=30)
    assert response.ok is False and response.internal is True
    assert "container said hi" in response.error


def test_symlinked_response_is_not_followed(tmp_path):
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps({"ok": True, "data": {"stolen": 1}}), encoding="utf-8")
    runner = FakeRunner()
    client, run_dir = _client(tmp_path, runner)

    def plant_symlink(proc):
        link = run_dir.root / proc.argv[-1].replace(".request.json", ".response.json")
        try:
            os.symlink(secret, link)
        except (OSError, NotImplementedError):
            pytest.skip("this platform or user cannot create symlinks")

    runner.on_exit = plant_symlink
    response = client.call("run_solver", SOLVE_ARGS, timeout_s=30)
    assert response.ok is False
    assert "stolen" not in json.dumps(response.model_dump())


def test_symlinked_sandbox_directory_is_refused(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    runner = FakeRunner(response=OpResponse(ok=True))
    client, run_dir = _client(tmp_path, runner)
    try:
        os.symlink(outside, run_dir.root / ".sandbox", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this platform or user cannot create symlinks")
    response = client.call("run_solver", SOLVE_ARGS, timeout_s=30)
    assert response.ok is False
    assert runner.popen_calls == []  # no container was started
    assert list(outside.iterdir()) == []


def test_unknown_op_and_invalid_args_do_not_start_a_container(tmp_path):
    runner = FakeRunner(response=OpResponse(ok=True))
    client, _ = _client(tmp_path, runner)
    unknown = client.call("rm_rf", {}, timeout_s=30)
    invalid = client.call("run_solver", {"case_dir": "../../etc", "solver": "icoFoam"}, 30)
    assert unknown.ok is False and "rm_rf" in unknown.error
    assert invalid.ok is False
    assert runner.popen_calls == []


def test_docker_not_installed_is_a_failed_response(tmp_path):
    def no_docker(argv, **kwargs):
        raise FileNotFoundError("docker")

    runner = FakeRunner()
    root = tmp_path / "run"
    root.mkdir()
    client = SandboxClient(_config(), RunDir(root), RUN_ID, popen=no_docker, run=runner.run)
    response = client.call("run_solver", SOLVE_ARGS, timeout_s=30)
    assert response.ok is False and response.internal is True
    assert "docker" in response.error


@pytest.mark.parametrize("run_id", ["", "has space", "../x", "-x", "a" * 200, "UPPER:colon"])
def test_client_rejects_run_ids_that_cannot_be_container_names(tmp_path, run_id):
    root = tmp_path / "run"
    root.mkdir()
    with pytest.raises(ValueError):
        SandboxClient(_config(), RunDir(root), run_id)


def test_local_mode_runs_the_executor_as_a_plain_subprocess(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    (root / "hello.py").write_text("print('hi from the script')\n", encoding="utf-8")
    client = SandboxClient(_config(mode="local"), RunDir(root), RUN_ID)
    response = client.call("run_script", {"script": "hello.py"}, timeout_s=120)
    assert response.ok, response.error
    assert "hi from the script" in response.data["tail"]


def test_local_mode_starts_without_home_outside_the_openfoam_environment(tmp_path, monkeypatch):
    """HOME is unset in a plain Windows shell; the HOME guard is for where OpenFOAM can run."""
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("WM_PROJECT_DIR", raising=False)
    root = tmp_path / "run"
    root.mkdir()
    (root / "hello.py").write_text("print('hi without home')\n", encoding="utf-8")
    client = SandboxClient(_config(mode="local"), RunDir(root), RUN_ID)
    response = client.call("run_script", {"script": "hello.py"}, timeout_s=120)
    assert response.ok, response.error
    assert "hi without home" in response.data["tail"]


def test_local_mode_timeout_kills_the_executor(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    (root / "slow.py").write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    client = SandboxClient(_config(mode="local"), RunDir(root), RUN_ID)
    started = time.monotonic()
    response = client.call("run_script", {"script": "slow.py", "timeout_s": 30}, timeout_s=3)
    assert response.ok is False
    assert "timed out" in response.error
    # The executor is killed at once; there is no container whose exit could be awaited.
    assert time.monotonic() - started < 20


def test_local_mode_environment_is_scrubbed(tmp_path, monkeypatch):
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "s3cret-value")
    root = tmp_path / "run"
    root.mkdir()
    (root / "env.py").write_text("import os\nprint(dict(os.environ))\n", encoding="utf-8")
    client = SandboxClient(_config(mode="local"), RunDir(root), RUN_ID)
    response = client.call("run_script", {"script": "env.py"}, timeout_s=120)
    assert response.ok, response.error
    assert "s3cret-value" not in response.data["tail"]


def test_local_env_allowlist_matches_the_tool_layer():
    from cfdagent.sandbox import client as client_module
    from cfdagent.tools import process

    assert tuple(client_module.SAFE_ENV_KEYS) == process.SAFE_ENV_KEYS
    assert tuple(client_module.SAFE_ENV_PREFIXES) == process.SAFE_ENV_PREFIXES


# --------------------------------------------------------------------------------------------
# Fix round 1
# --------------------------------------------------------------------------------------------


def test_local_mode_command_does_not_put_the_run_directory_on_sys_path(tmp_path):
    runner = FakeRunner(response=OpResponse(ok=True))
    client, _ = _client(tmp_path, runner, mode="local")
    client.call("run_solver", SOLVE_ARGS, timeout_s=30)
    argv = runner.popen_calls[0]
    assert argv[:4] == [sys.executable, "-P", "-m", "cfdagent.executor"]


@pytest.mark.parametrize("module", ["json", "pydantic", "os", "cfdagent"])
def test_planted_module_in_the_run_directory_is_never_imported(tmp_path, module):
    root = tmp_path / "run"
    root.mkdir()
    marker = tmp_path / "IMPORTED"
    package = root / module
    if module == "cfdagent":
        package.mkdir()
        (package / "__init__.py").write_text(f"open({str(marker)!r}, 'w')\n", encoding="utf-8")
    else:
        (root / f"{module}.py").write_text(
            f"open({str(marker)!r}, 'w')\nraise SystemExit(7)\n", encoding="utf-8"
        )
    (root / "hello.py").write_text("print('fine')\n", encoding="utf-8")
    client = SandboxClient(_config(mode="local"), RunDir(root), RUN_ID)
    response = client.call("run_script", {"script": "hello.py"}, timeout_s=120)
    assert response.ok, response.error
    assert "fine" in response.data["tail"]
    assert not marker.exists()


def test_nonzero_exit_with_a_planted_response_is_a_failure(tmp_path):
    runner = FakeRunner(exit_code=1, response=OpResponse(ok=True, data={"forged": 1}))
    client, _ = _client(tmp_path, runner)
    response = client.call("run_solver", SOLVE_ARGS, timeout_s=30)
    assert response.ok is False and response.internal is True
    assert "exit code 1" in response.error


def test_a_response_present_before_the_start_is_deleted(tmp_path, monkeypatch):
    class FixedUuid:
        hex = "deadbeef" * 4

    class FixedModule:
        @staticmethod
        def uuid4():
            return FixedUuid()

    monkeypatch.setattr(client_module, "uuid", FixedModule)
    runner = FakeRunner()  # the "container" writes no response
    client, run_dir = _client(tmp_path, runner)
    planted = ".sandbox/00001-deadbeef.response.json"
    run_dir.write_text(planted, OpResponse(ok=True, data={"stale": 1}).model_dump_json())
    response = client.call("run_solver", SOLVE_ARGS, timeout_s=30)
    assert response.ok is False  # the stale file was not taken for an answer
    assert not (run_dir.root / planted).exists()


@pytest.mark.parametrize("timeout_s", [0, -1, math.nan, math.inf, -math.inf, "5", None, True])
def test_call_rejects_unusable_timeouts(tmp_path, timeout_s):
    runner = FakeRunner(response=OpResponse(ok=True))
    client, _ = _client(tmp_path, runner)
    with pytest.raises(ValueError, match="timeout"):
        client.call("run_solver", SOLVE_ARGS, timeout_s=timeout_s)
    assert runner.popen_calls == []


def test_outer_timeout_adds_the_documented_margin():
    assert OUTER_TIMEOUT_MARGIN_S == 1500.0
    assert outer_timeout(300) == 1800.0
    assert outer_timeout(0.5) == 1500.5
    for bad in (0, -1, math.nan, math.inf):
        with pytest.raises(ValueError):
            outer_timeout(bad)


def test_responses_do_not_leak_host_paths(tmp_path):
    host = str(tmp_path)
    stderr = f"docker: invalid mount {host}/run:/job\nopen {host.replace(chr(92), '/')}/x: denied"
    runner = FakeRunner(exit_code=125, stderr=stderr.encode())
    client, run_dir = _client(tmp_path, runner)
    missing = client.call("run_solver", SOLVE_ARGS, timeout_s=30)
    assert missing.ok is False
    assert host not in missing.error and host.replace(chr(92), "/") not in missing.error
    assert str(run_dir.root) not in missing.error

    def no_docker(argv, **kwargs):
        raise FileNotFoundError(2, "No such file", f"{host}/docker")

    client = SandboxClient(_config(), run_dir, RUN_ID, popen=no_docker, run=runner.run)
    refused = client.call("run_solver", SOLVE_ARGS, timeout_s=30)
    assert refused.ok is False and refused.internal is True
    assert host not in refused.error and "FileNotFoundError" in refused.error


def test_unwritable_request_reports_the_class_not_the_path(tmp_path, monkeypatch):
    runner = FakeRunner(response=OpResponse(ok=True))
    client, run_dir = _client(tmp_path, runner)

    def deny(*_args, **_kwargs):
        raise PermissionError(13, "denied", str(run_dir.root / ".sandbox"))

    monkeypatch.setattr(run_dir, "write_text", deny)
    response = client.call("run_solver", SOLVE_ARGS, timeout_s=30)
    assert response.ok is False and response.internal is True
    assert str(tmp_path) not in response.error and "PermissionError" in response.error
