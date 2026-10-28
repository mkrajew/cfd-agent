"""Decision logic of infra/vm/idle-shutdown.sh, run with stubbed system commands.

The script is executed by bash with a PATH that starts with a directory of stub executables
(who, ss, pgrep, docker, curl), so no Azure call and no real session state is involved.
"""

import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "infra" / "vm" / "idle-shutdown.sh"
BASH = shutil.which("bash")
PYTHON3 = shutil.which("python3")

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or BASH is None or PYTHON3 is None,
    reason="needs a POSIX bash and python3 (runs in CI and in the Linux image)",
)

NOW = 1_800_000_000
BOOT_AGO = 2 * 3600  # the stubbed VM booted two hours ago
COMPUTE_JSON = '{"subscriptionId":"s","resourceGroupName":"g","name":"v"}'
TOKEN_JSON = '{"access_token":"secret-token"}'


def _stub(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    calls = tmp_path / "curl-calls.log"
    _stub(stubs, "who", 'printf "%s" "$STUB_WHO"\n')
    _stub(stubs, "ss", 'printf "%s" "$STUB_SS"\n')
    _stub(stubs, "pgrep", 'exit "${STUB_PGREP_RC:-1}"\n')
    _stub(stubs, "docker", 'printf "%s" "$STUB_DOCKER_PS"\n')
    _stub(stubs, "wall", "exit 0\n")
    _stub(
        stubs,
        "curl",
        f'echo "$*" >> "{calls}"\n'
        'case "$*" in\n'
        f"  *instance/compute*) echo '{COMPUTE_JSON}' ;;\n"
        f"  *oauth2/token*) echo '{TOKEN_JSON}' ;;\n"
        "esac\n",
    )
    state = tmp_path / "state"
    return {
        "PATH": f"{stubs}:{Path(PYTHON3 or '').parent}:/usr/local/bin:/usr/bin:/bin",
        "CFDAGENT_STATE_DIR": str(state),
        "CFDAGENT_NOW": str(NOW),
        "CFDAGENT_UPTIME_SECONDS": str(BOOT_AGO),
        "STUB_WHO": "",
        "STUB_SS": "",
        "STUB_DOCKER_PS": "",
    }


def run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run(
        [BASH, str(SCRIPT), *args], env=env, capture_output=True, text=True, check=False
    )


def idle_file(env: dict[str, str]) -> Path:
    return Path(env["CFDAGENT_STATE_DIR"]) / "idle-since"


def set_idle_since(env: dict[str, str], minutes_ago: int) -> None:
    idle_file(env).parent.mkdir(parents=True, exist_ok=True)
    idle_file(env).write_text(f"{NOW - minutes_ago * 60}\n")


def test_first_idle_observation_starts_the_counter(env):
    result = run(env, "--dry-run")
    assert result.returncode == 0
    assert "not yet" in result.stdout
    assert idle_file(env).read_text().strip() == str(NOW)


def test_idle_below_limit_does_not_deallocate(env):
    set_idle_since(env, 29)
    result = run(env, "--dry-run")
    assert "would deallocate" not in result.stdout
    assert "not yet" in result.stdout


def test_idle_at_limit_would_deallocate(env):
    set_idle_since(env, 30)
    result = run(env, "--dry-run")
    assert result.returncode == 0
    assert "would deallocate" in result.stdout


@pytest.mark.parametrize(
    ("variable", "value", "reason"),
    [
        ("STUB_WHO", "azureuser pts/0 2026-10-28 10:00", "login session"),
        ("STUB_SS", "ESTAB 0 0 10.20.0.4:22 203.0.113.5:50000", "ssh connection"),
        ("STUB_PGREP_RC", "0", "cfdagent process"),
        ("STUB_DOCKER_PS", "ghcr.io/mkrajew/cfd-agent:latest\n", "cfd-agent container"),
    ],
)
def test_busy_resets_the_counter(env, variable, value, reason):
    set_idle_since(env, 120)
    env[variable] = value
    result = run(env, "--dry-run")
    assert result.returncode == 0
    assert f"busy ({reason})" in result.stdout
    assert "would deallocate" not in result.stdout
    assert not idle_file(env).exists()


def test_other_container_images_are_not_busy(env):
    set_idle_since(env, 45)
    env["STUB_DOCKER_PS"] = "nginx:stable\nghcr.io/mkrajew/cfd-agent-other:latest\n"
    result = run(env, "--dry-run")
    assert "would deallocate" in result.stdout


def test_timestamp_from_before_boot_counts_from_boot(env):
    # Left over from the previous session: idle since 5 hours ago, but the VM booted 2 hours ago.
    # 2 hours exceeds 30 minutes, so move the boot to 10 minutes ago.
    env["CFDAGENT_UPTIME_SECONDS"] = str(10 * 60)
    set_idle_since(env, 5 * 60)
    result = run(env, "--dry-run")
    assert "would deallocate" not in result.stdout
    assert idle_file(env).read_text().strip() == str(NOW - 10 * 60)


def test_corrupt_state_file_restarts_the_counter(env):
    idle_file(env).parent.mkdir(parents=True)
    idle_file(env).write_text("not-a-number\n")
    result = run(env, "--dry-run")
    assert "not yet" in result.stdout
    assert idle_file(env).read_text().strip() == str(NOW)


def test_deallocation_posts_to_arm_with_the_identity_token(env, tmp_path):
    set_idle_since(env, 31)
    result = run(env)
    assert result.returncode == 0, result.stderr
    assert "secret-token" not in result.stdout + result.stderr
    calls = (tmp_path / "curl-calls.log").read_text()
    assert "-X POST" in calls
    assert (
        "/subscriptions/s/resourceGroups/g/providers/Microsoft.Compute/virtualMachines/v/deallocate"
        in calls
    )


def test_force_deallocates_even_when_busy(env, tmp_path):
    env["STUB_WHO"] = "azureuser pts/0 2026-10-28 10:00"
    result = run(env, "--force")
    assert result.returncode == 0, result.stderr
    assert "forced shutdown" in result.stdout
    assert "/deallocate?" in (tmp_path / "curl-calls.log").read_text()


def test_unknown_argument_is_rejected(env):
    assert run(env, "--bogus").returncode == 2
