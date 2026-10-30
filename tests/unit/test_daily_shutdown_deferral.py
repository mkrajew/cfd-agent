"""`idle-shutdown.sh --daily`: the daily shutdown waits for a run that is marked active.

The marker is `${CFDAGENT_STATE_DIR}/active-run`, created by `cfdagent run` and removed when it
ends. Only its modification time counts: younger than 4 hours defers the shutdown, anything else
(no file, older, not a regular file, a time in the future) is ignored. The script runs with the same
stub commands as test_idle_shutdown.py, so no Azure call is made.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_idle_shutdown import (
    BASH,
    BOOT_AGO,
    CURL_STUB,
    NOW,
    PYTHON3,
    SCRIPT,
)
from test_idle_shutdown import _stub as stub_executable

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or BASH is None or PYTHON3 is None,
    reason="needs a POSIX bash and python3 (runs in CI and in the Linux image)",
)

FOUR_HOURS = 4 * 3600


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name in ("who", "ss", "docker"):
        stub_executable(stubs, name, "exit 0\n")
    stub_executable(stubs, "pgrep", "exit 1\n")
    stub_executable(stubs, "wall", "exit 0\n")
    stub_executable(stubs, "curl", CURL_STUB)
    return {
        "PATH": f"{stubs}:{Path(PYTHON3 or '').parent}:/usr/local/bin:/usr/bin:/bin",
        "CFDAGENT_STATE_DIR": str(tmp_path / "state"),
        "CFDAGENT_NOW": str(NOW),
        "CFDAGENT_UPTIME_SECONDS": str(BOOT_AGO),
        "CFDAGENT_RETRY_SLEEP": "0",
        "STUB_CURL_LOG": str(tmp_path / "curl-calls.log"),
        "STUB_COUNTER": str(tmp_path / "arm-counter"),
        "STUB_COMPUTE_FILE": "",
    }


def run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run(
        [BASH, str(SCRIPT), *args], env=env, capture_output=True, text=True, check=False
    )


def mark_active(env: dict[str, str], age_seconds: int, content: str = "") -> Path:
    marker = Path(env["CFDAGENT_STATE_DIR"]) / "active-run"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(content)
    os.utime(marker, (NOW - age_seconds, NOW - age_seconds))
    return marker


def curl_calls(env: dict[str, str]) -> str:
    log = Path(env["STUB_CURL_LOG"])
    return log.read_text() if log.exists() else ""


def test_daily_defers_while_run_active(env):
    marker = mark_active(env, age_seconds=3600)
    result = run(env, "--daily")
    assert result.returncode == 0, result.stderr
    assert "deferred: active run" in result.stdout
    assert curl_calls(env) == ""
    assert marker.exists(), "the script must never remove the marker"


def test_daily_defers_just_below_four_hours(env):
    mark_active(env, age_seconds=FOUR_HOURS - 1)
    result = run(env, "--daily")
    assert result.returncode == 0, result.stderr
    assert "deferred: active run" in result.stdout
    assert curl_calls(env) == ""


def test_daily_forces_after_four_hours(env):
    mark_active(env, age_seconds=FOUR_HOURS)
    result = run(env, "--daily")
    assert result.returncode == 0, result.stderr
    assert "deferred" not in result.stdout
    assert "stale" in result.stdout
    assert "/deallocate?" in curl_calls(env)


def test_daily_without_active_run_deallocates(env):
    result = run(env, "--daily")
    assert result.returncode == 0, result.stderr
    assert "deferred" not in result.stdout
    assert "/deallocate?" in curl_calls(env)


def test_daily_without_active_run_dry_run_says_it_would_deallocate(env):
    result = run(env, "--daily", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "dry run: would deallocate" in result.stdout
    assert curl_calls(env) == ""


def test_daily_dry_run_defers_too(env):
    mark_active(env, age_seconds=60)
    result = run(env, "--daily", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "deferred: active run" in result.stdout
    assert "would deallocate" not in result.stdout


def test_daily_ignores_logged_in_users_like_force(env):
    # The daily shutdown is unconditional (apart from the deferral): a login does not defer it.
    stub_executable(Path(env["PATH"].split(":")[0]), "who", 'echo "azureuser pts/0"\n')
    result = run(env, "--daily")
    assert result.returncode == 0, result.stderr
    assert "/deallocate?" in curl_calls(env)


def test_daily_ignores_a_stale_marker_with_a_log_line(env):
    mark_active(env, age_seconds=10 * 3600)
    result = run(env, "--daily", "--dry-run")
    assert "ignoring stale active-run marker" in result.stdout
    assert "dry run: would deallocate" in result.stdout


def test_daily_content_of_the_marker_is_irrelevant(env):
    mark_active(env, age_seconds=60, content="\x00garbage \xff not a timestamp")
    result = run(env, "--daily", "--dry-run")
    assert "deferred: active run" in result.stdout


def test_daily_marker_that_is_not_a_regular_file_counts_as_no_active_run(env):
    (Path(env["CFDAGENT_STATE_DIR"]) / "active-run").mkdir(parents=True)
    result = run(env, "--daily", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "deferred" not in result.stdout
    assert "dry run: would deallocate" in result.stdout


def test_daily_dangling_symlink_counts_as_no_active_run(env):
    state = Path(env["CFDAGENT_STATE_DIR"])
    state.mkdir(parents=True)
    (state / "active-run").symlink_to(state / "does-not-exist")
    result = run(env, "--daily", "--dry-run")
    assert "deferred" not in result.stdout
    assert "dry run: would deallocate" in result.stdout


def test_daily_marker_dated_in_the_future_is_ignored(env):
    # A clock jump must not make the deferral unbounded.
    mark_active(env, age_seconds=-3600)
    result = run(env, "--daily", "--dry-run")
    assert "deferred" not in result.stdout
    assert "dry run: would deallocate" in result.stdout


def test_daily_retries_a_failed_deallocation_and_reports_failure(env):
    env["STUB_ARM_FAILS"] = "99"
    result = run(env, "--daily")
    assert result.returncode != 0
    assert curl_calls(env).count("/deallocate?") == 3


def test_idle_mode_is_not_affected_by_the_marker(env):
    # Only --daily defers. The idle timer already treats a running cfdagent as busy.
    mark_active(env, age_seconds=60)
    result = run(env, "--dry-run")
    assert "deferred" not in result.stdout


def test_force_ignores_the_marker(env):
    mark_active(env, age_seconds=60)
    result = run(env, "--force")
    assert result.returncode == 0, result.stderr
    assert "/deallocate?" in curl_calls(env)
