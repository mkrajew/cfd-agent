"""Allowlisted tool runner: no shell, one merged log file, timeouts."""

import sys
from pathlib import Path

import pytest

from cfdagent.tools.process import ALLOWED_EXECUTABLES, ToolNotAllowed, run_tool


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
