"""scripts/vm-host-setup.sh: option handling, rendered files and the guarded sshd drop-in.

The script can be sourced (it runs `main` only when executed), so the functions are exercised
directly with stub `sshd` and `systemctl` commands and a temporary directory instead of /etc.
Nothing here needs root, the network or a VM.
"""

import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "vm-host-setup.sh"
VM_FILES = REPO / "infra" / "vm"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or BASH is None, reason="needs a POSIX bash (runs in CI and the image)"
)

REQUIRED = ["--llm-endpoint", "https://llm.example.invalid/v1/", "--storage-account-url"]
REQUIRED_ARGS = [*REQUIRED, "https://storage.example.invalid"]
SHA = "0123456789abcdef0123456789abcdef01234567"


def run_script(*args: str) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run([BASH, str(SCRIPT), *args], capture_output=True, text=True, check=False)


def render(what: str, *args: str, ref: str = SHA) -> subprocess.CompletedProcess[str]:
    return run_script(ref, *REQUIRED_ARGS, *args, "--render", what)


def test_missing_arguments_are_rejected_with_usage():
    for args in ([], ["main"], ["main", "--llm-endpoint", "https://a.invalid"]):
        result = run_script(*args)
        assert result.returncode == 2, args
        assert "usage:" in result.stderr


def test_unknown_option_and_bad_values_are_rejected():
    assert run_script("main", *REQUIRED_ARGS, "--bogus").returncode == 2
    assert render("env", "--user", "bad user; rm").returncode == 2
    assert render("timer", "--shutdown-time", "2460").returncode == 2
    assert render("timer", "--timezone", "Europe/War saw").returncode == 2
    bad_url = run_script("main", "--llm-endpoint", "not a url", "--storage-account-url", "x")
    assert bad_url.returncode == 2
    assert render("env", "--image", "bad image").returncode == 2


def test_env_file_has_the_documented_variables_and_defaults():
    result = render("env")
    assert result.returncode == 0, result.stderr
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    assert values["CFDAGENT_LLM_ENDPOINT"] == "https://llm.example.invalid/v1/"
    assert values["CFDAGENT_STORAGE_ACCOUNT_URL"] == "https://storage.example.invalid"
    assert values["CFDAGENT_RUNS_DIR"].endswith("/runs")
    assert values["CFDAGENT_STATE_DIR"] == "/var/lib/cfdagent"
    assert values["CFDAGENT_IMAGE"] == "ghcr.io/mkrajew/cfd-agent:sha-0123456"


def test_image_defaults_to_latest_for_a_branch_or_tag_ref():
    for ref in ("main", "v0.1.0", "infra/m1-vm-host"):
        result = render("env", ref=ref)
        assert "CFDAGENT_IMAGE=ghcr.io/mkrajew/cfd-agent:latest" in result.stdout, ref


def test_image_option_accepts_a_tag_or_a_full_reference():
    tag = render("env", "--image", "v0.2.0")
    assert "CFDAGENT_IMAGE=ghcr.io/mkrajew/cfd-agent:v0.2.0" in tag.stdout
    full = render("env", "--image", "registry.example.invalid/team/img:abc")
    assert "CFDAGENT_IMAGE=registry.example.invalid/team/img:abc" in full.stdout


def test_env_file_contains_nothing_that_looks_like_a_secret():
    lines = render("env").stdout.splitlines()
    out = "\n".join(line for line in lines if not line.startswith("#"))
    assert not re.search(r"KEY|TOKEN|SECRET|PASSWORD", out, re.IGNORECASE)


def test_daily_timer_has_25_attempts_from_the_shutdown_time_every_10_minutes():
    result = render("timer")
    assert result.returncode == 0, result.stderr
    entries = [line for line in result.stdout.splitlines() if line.startswith("OnCalendar=")]
    assert len(entries) == 25
    assert entries[0] == "OnCalendar=*-*-* 23:00:00 Europe/Warsaw"
    assert entries[1] == "OnCalendar=*-*-* 23:10:00 Europe/Warsaw"
    assert entries[-1] == "OnCalendar=*-*-* 03:00:00 Europe/Warsaw"
    assert "__ON_CALENDAR__" not in result.stdout
    assert "Persistent=false" in result.stdout
    assert result.stdout.count("__") == 0


def test_daily_timer_follows_the_shutdown_time_and_zone():
    result = render("timer", "--shutdown-time", "2335", "--timezone", "UTC")
    entries = [line for line in result.stdout.splitlines() if line.startswith("OnCalendar=")]
    assert entries[0] == "OnCalendar=*-*-* 23:35:00 UTC"
    assert entries[-1] == "OnCalendar=*-*-* 03:35:00 UTC"


def test_ssh_dropin_is_rendered_for_the_user():
    result = render("ssh", "--user", "operator")
    assert result.returncode == 0, result.stderr
    assert "MaxAuthTries 3" in result.stdout
    assert "AllowUsers operator" in result.stdout
    assert "__ADMIN_USER__" not in result.stdout
    assert render("ssh").stdout.count("AllowUsers azureuser") == 1


def test_the_ssh_dropin_sorts_after_the_hardening_dropin():
    name = "20-cfdagent-ssh-extra.conf"
    assert (VM_FILES / name).is_file()
    assert name > "10-cfdagent-hardening.conf"


# --- guarded sshd drop-in ------------------------------------------------------------------------


def stub(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def ssh_env(tmp_path: Path) -> dict[str, str]:
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (tmp_path / "sshd_config.d").mkdir()
    stub(stubs, "sshd", 'echo "sshd $*" >> "$STUB_CALLS"\n[ -z "$STUB_SSHD_FAIL" ]\n')
    stub(stubs, "systemctl", 'echo "systemctl $*" >> "$STUB_CALLS"\n')
    return {
        "PATH": f"{stubs}:/usr/local/bin:/usr/bin:/bin",
        "CFDAGENT_SETUP_SSHD_DIR": str(tmp_path / "sshd_config.d"),
        "CFDAGENT_SETUP_RUN_SSHD_DIR": str(tmp_path / "run-sshd"),
        "CFDAGENT_SETUP_VM_FILES_DIR": str(VM_FILES),
        "STUB_CALLS": str(tmp_path / "calls"),
    }


def install_ssh(env: dict[str, str], user: str = "azureuser") -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    command = f'source "{SCRIPT}"; ADMIN_USER={user}; install_ssh_dropin'
    return subprocess.run([BASH, "-c", command], env=env, capture_output=True, text=True)


def dropin(env: dict[str, str]) -> Path:
    return Path(env["CFDAGENT_SETUP_SSHD_DIR"]) / "20-cfdagent-ssh-extra.conf"


def calls(env: dict[str, str]) -> list[str]:
    path = Path(env["STUB_CALLS"])
    return path.read_text().splitlines() if path.exists() else []


def test_dropin_is_installed_checked_and_then_sshd_is_reloaded(ssh_env):
    result = install_ssh(ssh_env)
    assert result.returncode == 0, result.stderr
    assert "AllowUsers azureuser" in dropin(ssh_env).read_text()
    assert calls(ssh_env) == ["sshd -t", "systemctl reload ssh"]
    leftovers = sorted(p.name for p in Path(ssh_env["CFDAGENT_SETUP_SSHD_DIR"]).iterdir())
    assert leftovers == ["20-cfdagent-ssh-extra.conf"]


def test_failed_sshd_test_refuses_to_reload_and_keeps_the_previous_dropin(ssh_env):
    dropin(ssh_env).write_text("MaxAuthTries 6\nAllowUsers previous\n")
    ssh_env["STUB_SSHD_FAIL"] = "1"
    result = install_ssh(ssh_env)
    assert result.returncode != 0
    assert "sshd -t failed" in result.stderr
    assert dropin(ssh_env).read_text() == "MaxAuthTries 6\nAllowUsers previous\n"
    assert not any(call.startswith("systemctl") for call in calls(ssh_env))
    leftovers = sorted(p.name for p in Path(ssh_env["CFDAGENT_SETUP_SSHD_DIR"]).iterdir())
    assert leftovers == ["20-cfdagent-ssh-extra.conf"]


def test_failed_sshd_test_without_a_previous_dropin_leaves_none(ssh_env):
    ssh_env["STUB_SSHD_FAIL"] = "1"
    assert install_ssh(ssh_env).returncode != 0
    assert list(Path(ssh_env["CFDAGENT_SETUP_SSHD_DIR"]).iterdir()) == []
    assert not any(call.startswith("systemctl") for call in calls(ssh_env))


def test_installing_twice_gives_the_same_result(ssh_env):
    assert install_ssh(ssh_env).returncode == 0
    first = dropin(ssh_env).read_text()
    assert install_ssh(ssh_env).returncode == 0
    assert dropin(ssh_env).read_text() == first
    names = [p.name for p in Path(ssh_env["CFDAGENT_SETUP_SSHD_DIR"]).iterdir()]
    assert names == [dropin(ssh_env).name]


def test_installing_for_another_user_replaces_the_allowed_user(ssh_env):
    assert install_ssh(ssh_env, "alice").returncode == 0
    assert install_ssh(ssh_env, "bob").returncode == 0
    text = dropin(ssh_env).read_text()
    assert "AllowUsers bob" in text
    assert "alice" not in text


# --- units ---------------------------------------------------------------------------------------


def test_unit_installation_copies_the_files_and_enables_the_firewall(tmp_path: Path):
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    stub(stubs, "systemctl", 'echo "systemctl $*" >> "$STUB_CALLS"\n')
    unit_dir = tmp_path / "units"
    sbin = tmp_path / "sbin"
    unit_dir.mkdir()
    sbin.mkdir()
    env = {
        "PATH": f"{stubs}:/usr/local/bin:/usr/bin:/bin",
        "CFDAGENT_SETUP_UNIT_DIR": str(unit_dir),
        "CFDAGENT_SETUP_SBIN_DIR": str(sbin),
        "CFDAGENT_SETUP_VM_FILES_DIR": str(VM_FILES),
        "STUB_CALLS": str(tmp_path / "calls"),
    }
    command = f'source "{SCRIPT}"; SHUTDOWN_TIME=2300; TIMEZONE=Europe/Warsaw; install_vm_units'
    assert BASH is not None
    result = subprocess.run([BASH, "-c", command], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (sbin / "cfdagent-idle-shutdown").read_text() == (
        VM_FILES / "idle-shutdown.sh"
    ).read_text()
    assert (sbin / "cfdagent-iptables").stat().st_mode & stat.S_IXUSR
    names = sorted(p.name for p in unit_dir.iterdir())
    assert names == [
        "cfdagent-daily-shutdown.service",
        "cfdagent-daily-shutdown.timer",
        "cfdagent-idle-shutdown.service",
        "cfdagent-idle-shutdown.timer",
        "cfdagent-iptables.service",
    ]
    assert "--daily" in (unit_dir / "cfdagent-daily-shutdown.service").read_text()
    assert "__ON_CALENDAR__" not in (unit_dir / "cfdagent-daily-shutdown.timer").read_text()
    log = (tmp_path / "calls").read_text().splitlines()
    assert "systemctl daemon-reload" in log
    assert "systemctl enable cfdagent-iptables.service" in log
    assert "systemctl restart cfdagent-iptables.service" in log
    assert "systemctl restart cfdagent-daily-shutdown.timer" in log
