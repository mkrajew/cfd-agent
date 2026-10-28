"""Decision logic of infra/vm/idle-shutdown.sh, run with stubbed system commands.

The script is executed by bash with a PATH that starts with a directory of stub executables
(who, ss, pgrep, docker, curl, wall), so no Azure call and no real session state is involved.
The curl stub answers per URL with a status code and a body and fails the ARM deallocate call
a configurable number of times.
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

# Stub for curl: logs its arguments, writes a body to -o, prints the status code for -w and
# exits 22 on HTTP errors like `curl --fail-with-body`. The URL is the last argument.
CURL_STUB = r"""
echo "$*" >> "$STUB_CURL_LOG"
out=
prev=
url=
for arg in "$@"; do
  if [ "$prev" = "-o" ]; then out=$arg; fi
  prev=$arg
  url=$arg
done
code=200
body='{}'
case "$url" in
  *instance/compute*) code=${STUB_META_CODE:-200}; body='__COMPUTE__' ;;
  *oauth2/token*) code=${STUB_TOKEN_CODE:-200}; body='__TOKEN__' ;;
  */deallocate*)
    n=$(cat "$STUB_COUNTER" 2>/dev/null || echo 0)
    echo $((n + 1)) > "$STUB_COUNTER"
    if [ "$n" -lt "${STUB_ARM_FAILS:-0}" ]; then
      code=500; body='{"error":"boom"}'
    else
      code=202; body=''
    fi ;;
  *virtualMachines/v\?*) code=${STUB_GET_CODE:-200}; body='{"name":"v"}' ;;
esac
if [ -n "$out" ]; then printf '%s' "$body" > "$out"; fi
printf '%s' "$code"
if [ "$code" -ge 400 ]; then exit 22; fi
""".replace("__COMPUTE__", COMPUTE_JSON).replace("__TOKEN__", TOKEN_JSON)

PGREP_STUB = r"""
case "$*" in
  *-x*) exit "${STUB_PGREP_X_RC:-1}" ;;
  *) exit "${STUB_PGREP_F_RC:-1}" ;;
esac
"""


def _stub(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    _stub(stubs, "who", 'printf "%s" "$STUB_WHO"\n')
    _stub(stubs, "ss", 'printf "%s" "$STUB_SS"\n')
    _stub(stubs, "pgrep", PGREP_STUB)
    _stub(stubs, "docker", 'printf "%s" "$STUB_DOCKER_PS"\n')
    _stub(stubs, "wall", "exit 0\n")
    _stub(stubs, "curl", CURL_STUB)
    return {
        "PATH": f"{stubs}:{Path(PYTHON3 or '').parent}:/usr/local/bin:/usr/bin:/bin",
        "CFDAGENT_STATE_DIR": str(tmp_path / "state"),
        "CFDAGENT_NOW": str(NOW),
        "CFDAGENT_UPTIME_SECONDS": str(BOOT_AGO),
        "CFDAGENT_RETRY_SLEEP": "0",
        "STUB_WHO": "",
        "STUB_SS": "",
        "STUB_DOCKER_PS": "",
        "STUB_CURL_LOG": str(tmp_path / "curl-calls.log"),
        "STUB_COUNTER": str(tmp_path / "arm-counter"),
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


def curl_calls(env: dict[str, str]) -> str:
    log = Path(env["STUB_CURL_LOG"])
    return log.read_text() if log.exists() else ""


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
    assert curl_calls(env) == ""


@pytest.mark.parametrize(
    ("variable", "value", "reason"),
    [
        ("STUB_WHO", "azureuser pts/0 2026-10-28 10:00", "login session"),
        ("STUB_SS", "ESTAB 0 0 10.20.0.4:22 203.0.113.5:50000", "ssh connection"),
        ("STUB_PGREP_X_RC", "0", "cfdagent process"),  # pgrep -x cfdagent
        ("STUB_PGREP_F_RC", "0", "cfdagent process"),  # pgrep -f '-m cfdagent'
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


def test_busy_in_real_mode_makes_no_azure_call(env):
    env["STUB_WHO"] = "azureuser pts/0 2026-10-28 10:00"
    set_idle_since(env, 120)
    result = run(env)
    assert result.returncode == 0
    assert curl_calls(env) == ""


def test_other_container_images_are_not_busy(env):
    set_idle_since(env, 45)
    env["STUB_DOCKER_PS"] = "nginx:stable\nghcr.io/mkrajew/cfd-agent-other:latest\n"
    result = run(env, "--dry-run")
    assert "would deallocate" in result.stdout


def test_timestamp_from_before_boot_counts_from_boot(env):
    # Left over from the previous session: idle since 5 hours ago, but the VM booted 10 minutes
    # ago, so the idle time counts from the boot and the limit is not reached.
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


def test_deallocation_posts_to_arm_with_the_identity_token(env):
    set_idle_since(env, 31)
    result = run(env)
    assert result.returncode == 0, result.stderr
    assert "secret-token" not in result.stdout + result.stderr
    calls = curl_calls(env)
    assert "resource=https%3A%2F%2Fmanagement.azure.com" in calls
    assert "Authorization: Bearer secret-token" in calls
    assert "-X POST" in calls
    assert (
        "/subscriptions/s/resourceGroups/g/providers/Microsoft.Compute/virtualMachines/v/deallocate"
        in calls
    )


def test_force_deallocates_even_when_busy(env):
    env["STUB_WHO"] = "azureuser pts/0 2026-10-28 10:00"
    result = run(env, "--force")
    assert result.returncode == 0, result.stderr
    assert "forced shutdown" in result.stdout
    assert "/deallocate?" in curl_calls(env)


def test_arm_failure_is_retried_and_then_succeeds(env):
    set_idle_since(env, 31)
    env["STUB_ARM_FAILS"] = "2"
    result = run(env)
    assert result.returncode == 0, result.stderr
    assert curl_calls(env).count("/deallocate?") == 3
    assert "attempt 2 of 3 failed" in result.stderr


def test_force_is_retried_too(env):
    env["STUB_ARM_FAILS"] = "2"
    result = run(env, "--force")
    assert result.returncode == 0, result.stderr
    assert curl_calls(env).count("/deallocate?") == 3


def test_arm_failure_exits_nonzero_keeps_state_and_next_run_retries(env):
    set_idle_since(env, 31)
    stored = idle_file(env).read_text()
    env["STUB_ARM_FAILS"] = "99"
    failed = run(env)
    assert failed.returncode != 0
    assert "ARM returned 500" in failed.stderr
    assert "boom" in failed.stderr
    assert "secret-token" not in failed.stdout + failed.stderr
    assert curl_calls(env).count("/deallocate?") == 3
    assert idle_file(env).read_text() == stored
    env["STUB_ARM_FAILS"] = "0"
    retried = run(env)
    assert retried.returncode == 0, retried.stderr
    assert "deallocation requested" in retried.stdout


def test_imds_token_failure_is_reported_without_calling_arm(env):
    set_idle_since(env, 31)
    env["STUB_TOKEN_CODE"] = "400"
    result = run(env)
    assert result.returncode != 0
    assert "IMDS token failed (HTTP 400)" in result.stderr
    assert "/deallocate?" not in curl_calls(env)
    assert idle_file(env).exists()


def test_check_reads_the_vm_without_deallocating(env):
    env["STUB_WHO"] = "azureuser pts/0 2026-10-28 10:00"
    result = run(env, "--check")
    assert result.returncode == 0, result.stderr
    assert "check ok" in result.stdout
    calls = curl_calls(env)
    assert "virtualMachines/v?api-version" in calls
    assert "Authorization: Bearer secret-token" in calls
    assert "/deallocate" not in calls
    assert "-X POST" not in calls


def test_check_fails_when_arm_denies_the_read(env):
    env["STUB_GET_CODE"] = "403"
    result = run(env, "--check")
    assert result.returncode != 0
    assert "ARM returned 403" in result.stderr


def test_unknown_argument_is_rejected(env):
    assert run(env, "--bogus").returncode == 2
