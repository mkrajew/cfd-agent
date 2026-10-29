"""Allowlisted tool runner: no shell, one merged log file, timeouts."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cfdagent.tools.process import (
    ALLOWED_EXECUTABLES,
    SAFE_ENV_KEYS,
    SAFE_ENV_PREFIXES,
    TAIL_MAX_BYTES,
    ToolNotAllowed,
    run_tool,
)


def test_rejects_non_allowlisted_executable(tmp_path):
    with pytest.raises(ToolNotAllowed):
        run_tool(["bash", "-c", "ls"], cwd=tmp_path, log_path=tmp_path / "log.txt")


@pytest.mark.parametrize("name", ["/bin/bash", "./python", "..\\python", "/usr/bin/python"])
def test_rejects_executable_given_as_path(tmp_path, name):
    with pytest.raises(ToolNotAllowed):
        run_tool([name, "-c", "pass"], cwd=tmp_path, log_path=tmp_path / "log.txt")


def test_rejects_empty_argv(tmp_path):
    with pytest.raises(ToolNotAllowed):
        run_tool([], cwd=tmp_path, log_path=tmp_path / "log.txt")


def test_allowlist_contents():
    assert ALLOWED_EXECUTABLES == frozenset(
        {
            "python",
            "gmshToFoam",
            "checkMesh",
            "foamDictionary",
            "changeDictionary",
            "icoFoam",
            "simpleFoam",
            "pimpleFoam",
            "decomposePar",
            "reconstructPar",
            "postProcess",
            "foamListTimes",
            "mpirun",
        }
    )


def test_successful_command_writes_log(tmp_path):
    log = tmp_path / "logs" / "run.log"
    result = run_tool(["python", "-c", "print('hello')"], cwd=tmp_path, log_path=log)
    assert result.ok
    assert result.returncode == 0
    assert not result.timed_out
    assert result.log_path == log
    assert "hello" in log.read_text()
    assert "hello" in result.tail


def test_stderr_is_merged_into_log(tmp_path):
    code = "import sys; print('out'); print('err', file=sys.stderr)"
    result = run_tool(["python", "-c", code], cwd=tmp_path, log_path=tmp_path / "log.txt")
    text = result.log_path.read_text()
    assert "out" in text and "err" in text


def test_tail_is_last_50_lines(tmp_path):
    code = "for i in range(120): print(f'line{i}')"
    result = run_tool(["python", "-c", code], cwd=tmp_path, log_path=tmp_path / "log.txt")
    lines = result.tail.splitlines()
    assert len(lines) == 50
    assert lines[-1] == "line119"
    assert lines[0] == "line70"


def test_runs_in_cwd(tmp_path):
    code = "import os; print(os.getcwd())"
    result = run_tool(["python", "-c", code], cwd=tmp_path, log_path=tmp_path / "log.txt")
    assert tmp_path.name in result.tail


def test_pwd_environment_follows_cwd(tmp_path, monkeypatch):
    # OpenFOAM utilities warn "PWD is not the cwd()" when the inherited PWD is stale.
    monkeypatch.setenv("PWD", "/somewhere/else")
    code = "import os; print('PWD=' + os.environ['PWD'])"
    result = run_tool(["python", "-c", code], cwd=tmp_path, log_path=tmp_path / "log.txt")
    assert f"PWD={tmp_path}" in result.tail


def test_relative_cwd_is_resolved(tmp_path, monkeypatch):
    (tmp_path / "sub").mkdir()
    monkeypatch.chdir(tmp_path)
    code = "import os; print('PWD=' + os.environ['PWD']); print('CWD=' + os.getcwd())"
    result = run_tool(["python", "-c", code], cwd=Path("sub"), log_path=tmp_path / "log.txt")
    expected = (tmp_path / "sub").resolve()
    assert f"PWD={expected}" in result.tail
    assert f"CWD={expected}" in result.tail


def test_nonzero_exit_is_not_ok(tmp_path):
    result = run_tool(
        ["python", "-c", "import sys; sys.exit(3)"], cwd=tmp_path, log_path=tmp_path / "log.txt"
    )
    assert not result.ok
    assert result.returncode == 3
    assert not result.timed_out


def test_timeout(tmp_path):
    result = run_tool(
        ["python", "-c", "import time; time.sleep(5)"],
        cwd=tmp_path,
        log_path=tmp_path / "log.txt",
        timeout_s=0.5,
    )
    assert not result.ok
    assert result.timed_out
    assert result.returncode is None


def test_allowlisted_but_not_installed_is_not_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    result = run_tool(["checkMesh"], cwd=tmp_path, log_path=tmp_path / "log.txt")
    assert not result.ok
    assert result.returncode is None
    assert not result.timed_out
    assert "checkMesh" in result.log_path.read_text()
    assert "not found" in result.tail


def test_python_resolves_to_current_interpreter(tmp_path):
    code = "import sys; print(sys.executable)"
    result = run_tool(["python", "-c", code], cwd=tmp_path, log_path=tmp_path / "log.txt")
    assert sys.executable in result.tail


def test_missing_cwd_returns_not_ok(tmp_path):
    result = run_tool(
        ["python", "-c", "print('never runs')"],
        cwd=tmp_path / "does_not_exist",
        log_path=tmp_path / "log.txt",
    )
    assert not result.ok
    assert result.returncode is None
    assert not result.timed_out
    assert "cwd" in result.tail
    assert "cwd" in result.log_path.read_text()


def test_cwd_that_is_a_file_returns_not_ok(tmp_path):
    a_file = tmp_path / "file.txt"
    a_file.write_text("x")
    result = run_tool(["python", "-c", "pass"], cwd=a_file, log_path=tmp_path / "log.txt")
    assert not result.ok
    assert "cwd" in result.tail


def test_tail_byte_limit_value():
    assert TAIL_MAX_BYTES == 8000


def test_tail_is_capped_by_bytes(tmp_path):
    code = "print('a' * 3_000_000 + 'THE-END')"
    result = run_tool(["python", "-c", code], cwd=tmp_path, log_path=tmp_path / "log.txt")
    assert result.ok
    assert len(result.tail.encode()) <= TAIL_MAX_BYTES
    assert result.tail.rstrip("\r\n").endswith("THE-END")
    # A single huge line is kept (its end), not dropped as a "broken first line".
    assert len(result.tail) > 1000


def test_tail_byte_cap_drops_broken_first_line(tmp_path):
    code = "for i in range(3000): print(f'row-{i:05d} ' + 'x' * 20)"
    result = run_tool(["python", "-c", code], cwd=tmp_path, log_path=tmp_path / "log.txt")
    lines = result.tail.splitlines()
    assert len(result.tail.encode()) <= TAIL_MAX_BYTES
    assert lines[-1].startswith("row-02999")
    assert all(line.startswith("row-") and len(line) == 30 for line in lines)


def test_tail_byte_cap_with_multibyte_text(tmp_path):
    code = "import sys; sys.stdout.buffer.write(('\\u00e9' * 100000 + 'END\\n').encode('utf-8'))"
    result = run_tool(["python", "-c", code], cwd=tmp_path, log_path=tmp_path / "log.txt")
    assert len(result.tail.encode()) <= TAIL_MAX_BYTES
    assert "�" not in result.tail
    assert result.tail.rstrip().endswith("END")


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        process_query_limited_information = 0x1000
        still_active = 259
        handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, 0, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == still_active
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# A child that spawns a long-lived grandchild and records the grandchild's pid.
_SPAWN_GRANDCHILD = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
open(sys.argv[1], 'w').write(str(child.pid))
time.sleep(60)
"""


def _wait_for_pid_file(pid_file: Path) -> int:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if pid_file.exists() and pid_file.read_text().strip():
            return int(pid_file.read_text())
        time.sleep(0.05)
    raise AssertionError("the child did not write its pid")


def _assert_dies_within(pid: int, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.05)
    raise AssertionError(f"process {pid} is still alive")


def test_timeout_kills_child_process(tmp_path):
    pid_file = tmp_path / "grandchild.pid"
    result = run_tool(
        ["python", "-c", _SPAWN_GRANDCHILD, str(pid_file)],
        cwd=tmp_path,
        log_path=tmp_path / "log.txt",
        timeout_s=3.0,
    )
    assert result.timed_out
    _assert_dies_within(_wait_for_pid_file(pid_file), 2.0)


def test_keyboard_interrupt_kills_child_and_reraises(tmp_path, monkeypatch):
    pid_file = tmp_path / "grandchild.pid"
    original_wait = subprocess.Popen.wait
    started: list[subprocess.Popen] = []

    def fake_wait(self, timeout=None):
        if not started:
            started.append(self)
            # Let the child spawn its own child before the simulated Ctrl-C arrives.
            _wait_for_pid_file(pid_file)
            raise KeyboardInterrupt
        return original_wait(self, timeout)

    monkeypatch.setattr(subprocess.Popen, "wait", fake_wait)
    with pytest.raises(KeyboardInterrupt):
        run_tool(
            ["python", "-c", _SPAWN_GRANDCHILD, str(pid_file)],
            cwd=tmp_path,
            log_path=tmp_path / "log.txt",
            timeout_s=60.0,
        )
    assert started[0].poll() is not None
    _assert_dies_within(int(pid_file.read_text()), 2.0)


def test_environment_is_scrubbed(tmp_path, monkeypatch):
    secrets = {
        "AZURE_CLIENT_SECRET": "secret-value-1",
        "CFDAGENT_LLM_API_KEY": "secret-value-2",
        "IDENTITY_ENDPOINT": "secret-value-3",
        "MSI_ENDPOINT": "secret-value-4",
    }
    for key, value in secrets.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("FOAM_TEST_MARKER", "kept")
    code = "import os, json; print('ENV=' + json.dumps(dict(os.environ)))"
    result = run_tool(["python", "-c", code], cwd=tmp_path, log_path=tmp_path / "log.txt")
    assert result.ok
    text = result.log_path.read_text()
    for key, value in secrets.items():
        assert key not in text
        assert value not in text
    child_env = json.loads(text.split("ENV=", 1)[1])
    assert any(key.upper() == "PATH" for key in child_env)
    assert child_env["PWD"] == str(tmp_path.resolve())
    assert child_env["FOAM_TEST_MARKER"] == "kept"
    for key in child_env:
        assert key in SAFE_ENV_KEYS or key == "PWD" or key.startswith(SAFE_ENV_PREFIXES)


def test_safe_env_constants():
    assert {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "USER", "LD_LIBRARY_PATH"} <= set(
        SAFE_ENV_KEYS
    )
    # Windows needs these to start a Python child at all.
    assert {"SYSTEMROOT", "PATHEXT", "TEMP", "TMP", "COMSPEC"} <= set(SAFE_ENV_KEYS)
    assert set(SAFE_ENV_PREFIXES) == {
        "FOAM_",
        "WM_",
        "MPI_",
        "OMPI_",
        "PYTHON",
        "VTK_",
        "PYVISTA_",
        "MPL",
    }
