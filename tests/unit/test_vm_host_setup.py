"""scripts/vm-host-setup.sh: options, rendered files, source checkout and the guarded installs.

The script can be sourced (it runs `main` only when executed), so its functions are exercised
directly with stub commands (`sshd`, `systemctl`, `docker`, `systemd-analyze`), a local git
repository as the "remote" and temporary directories instead of /etc and /opt. Nothing here needs
root, the network or a VM.
"""

import getpass
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
from shell_stubs import stub_executable

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "vm-host-setup.sh"
VM_FILES = REPO / "infra" / "vm"
BASH = shutil.which("bash")
SH = shutil.which("sh")
GIT = shutil.which("git")

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


def run_function(
    env: dict[str, str], snippet: str, **extra: str
) -> subprocess.CompletedProcess[str]:
    """Source the script and run `snippet` in the same shell."""
    assert BASH is not None
    command = f'source "{SCRIPT}"; {snippet}'
    return subprocess.run(
        [BASH, "-c", command], env={**env, **extra}, capture_output=True, text=True, check=False
    )


def test_missing_arguments_are_rejected_with_usage():
    for args in ([], ["main"], ["main", "--llm-endpoint", "https://a.invalid"]):
        result = run_script(*args)
        assert result.returncode == 2, args
        assert "usage:" in result.stderr


def test_unknown_option_and_bad_values_are_rejected():
    assert run_script("main", *REQUIRED_ARGS, "--bogus", "x").returncode == 2
    assert render("env", "--user", "bad user; rm").returncode == 2
    assert render("timer", "--shutdown-time", "2460").returncode == 2
    assert render("timer", "--timezone", "Europe/War saw").returncode == 2
    bad_url = run_script("main", "--llm-endpoint", "not a url", "--storage-account-url", "x")
    assert bad_url.returncode == 2
    assert render("env", "--image", "bad image").returncode == 2


def test_force_while_running_is_a_flag_without_a_value():
    result = render("env", "--force-while-running")
    assert result.returncode == 0, result.stderr


def test_help_prints_the_header_documentation():
    result = run_script("--help")
    assert result.returncode == 0
    assert "--force-while-running" in result.stderr
    assert "exec bash" not in result.stderr


def test_the_script_re_executes_itself_with_bash_when_started_by_sh():
    assert SH is not None
    result = subprocess.run(
        [SH, str(SCRIPT), "main", *REQUIRED_ARGS, "--render", "env"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "CFDAGENT_LLM_ENDPOINT=" in result.stdout


def test_env_file_has_the_documented_variables_and_defaults():
    result = render("env")
    assert result.returncode == 0, result.stderr
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    assert values["CFDAGENT_LLM_ENDPOINT"] == "https://llm.example.invalid/v1/"
    assert values["CFDAGENT_STORAGE_ACCOUNT_URL"] == "https://storage.example.invalid"
    assert values["CFDAGENT_RUNS_DIR"].endswith("/runs")
    assert values["CFDAGENT_STATE_DIR"] == "/var/lib/cfdagent"
    assert values["CFDAGENT_IMAGE"] == "ghcr.io/mkrajew/cfd-agent:sha-0123456"


def test_image_tag_and_commit_come_from_the_resolved_checkout_commit():
    out = run_function(
        {"PATH": os.environ["PATH"]},
        f"REPO_REF=main; LLM_ENDPOINT=https://a.invalid; STORAGE_URL=https://b.invalid; "
        f"COMMIT={SHA}; render_env",
    )
    assert out.returncode == 0, out.stderr
    assert "CFDAGENT_IMAGE=ghcr.io/mkrajew/cfd-agent:sha-0123456\n" in out.stdout
    assert f"CFDAGENT_COMMIT={SHA}\n" in out.stdout


def test_render_of_a_branch_ref_has_no_checkout_to_resolve():
    for ref in ("main", "v0.1.0", "infra/m1-vm-host"):
        result = render("env", ref=ref)
        assert "CFDAGENT_IMAGE=ghcr.io/mkrajew/cfd-agent:latest" in result.stdout, ref
        assert "CFDAGENT_COMMIT" not in result.stdout


def test_image_option_accepts_a_tag_or_a_full_reference():
    tag = render("env", "--image", "v0.2.0")
    assert "CFDAGENT_IMAGE=ghcr.io/mkrajew/cfd-agent:v0.2.0" in tag.stdout
    full = render("env", "--image", "registry.example.invalid/team/img:abc")
    assert "CFDAGENT_IMAGE=registry.example.invalid/team/img:abc" in full.stdout


def test_env_file_contains_nothing_that_looks_like_a_secret():
    lines = render("env").stdout.splitlines()
    out = "\n".join(line for line in lines if not line.startswith("#"))
    assert not re.search(r"KEY|TOKEN|SECRET|PASSWORD", out, re.IGNORECASE)


def test_daily_timer_has_one_calendar_entry_and_the_ten_minute_retry():
    result = render("timer")
    assert result.returncode == 0, result.stderr
    entries = [line for line in result.stdout.splitlines() if line.startswith("OnCalendar=")]
    assert entries == ["OnCalendar=*-*-* 23:00:00 Europe/Warsaw"]
    assert "\nOnUnitActiveSec=10min\n" in result.stdout
    assert "Persistent=false" in result.stdout
    assert "__" not in result.stdout


def test_daily_timer_follows_the_shutdown_time_and_zone():
    result = render("timer", "--shutdown-time", "2335", "--timezone", "UTC")
    entries = [line for line in result.stdout.splitlines() if line.startswith("OnCalendar=")]
    assert entries == ["OnCalendar=*-*-* 23:35:00 UTC"]


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


def test_the_installs_that_matter_for_security_come_before_the_network_steps():
    text = SCRIPT.read_text()
    main = text[text.index("\nmain() {") :]
    order = [
        "refuse_while_run_active",
        "sync_source",
        "check_vm_files",
        "install_vm_units",
        "install_ssh_dropin",
        "build_venv",
        "write_env",
        "install_wrapper",
        "pull_image",
    ]
    positions = [main.index(name) for name in order]
    assert positions == sorted(positions)


# --- shared fixtures ---------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name in ("etc", "units", "sbin", "bin", "state", "home", "sshd_config.d"):
        (tmp_path / name).mkdir()
    stub_executable(stubs, "sshd", 'echo "sshd $*" >> "$STUB_CALLS"\n[ -z "$STUB_SSHD_FAIL" ]\n')
    stub_executable(stubs, "systemctl", 'echo "systemctl $*" >> "$STUB_CALLS"\n')
    stub_executable(
        stubs,
        "systemd-analyze",
        'echo "systemd-analyze $*" >> "$STUB_CALLS"\n[ -z "$STUB_ANALYZE_FAIL" ]\n',
    )
    stub_executable(
        stubs, "docker", 'echo "docker $*" >> "$STUB_CALLS"\n[ -z "$STUB_DOCKER_FAIL" ]\n'
    )
    return {
        "PATH": f"{stubs}:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "CFDAGENT_SETUP_SSHD_DIR": str(tmp_path / "sshd_config.d"),
        "CFDAGENT_SETUP_RUN_SSHD_DIR": str(tmp_path / "run-sshd"),
        "CFDAGENT_SETUP_VM_FILES_DIR": str(VM_FILES),
        "CFDAGENT_SETUP_UNIT_DIR": str(tmp_path / "units"),
        "CFDAGENT_SETUP_SBIN_DIR": str(tmp_path / "sbin"),
        "CFDAGENT_SETUP_BIN_DIR": str(tmp_path / "bin"),
        "CFDAGENT_SETUP_ENV_DIR": str(tmp_path / "etc"),
        "CFDAGENT_SETUP_HOST_DIR": str(tmp_path / "host"),
        "CFDAGENT_SETUP_STATE_DIR": str(tmp_path / "state"),
        "CFDAGENT_SETUP_HOME_DIR": str(tmp_path / "home"),
        "CFDAGENT_SETUP_ENV_OWNER": getpass.getuser(),
        "STUB_CALLS": str(tmp_path / "calls"),
    }


def calls(env: dict[str, str]) -> list[str]:
    path = Path(env["STUB_CALLS"])
    return path.read_text().splitlines() if path.exists() else []


SETTINGS = (
    f"ADMIN_USER={getpass.getuser()}; LLM_ENDPOINT=https://a.invalid; "
    "STORAGE_URL=https://b.invalid; REPO_REF=main; SHUTDOWN_TIME=2300; TIMEZONE=Europe/Warsaw"
)


# --- guarded sshd drop-in ------------------------------------------------------------------------


def install_ssh(env: dict[str, str], user: str = "azureuser") -> subprocess.CompletedProcess[str]:
    return run_function(env, f"ADMIN_USER={user}; install_ssh_dropin")


def dropin(env: dict[str, str]) -> Path:
    return Path(env["CFDAGENT_SETUP_SSHD_DIR"]) / "20-cfdagent-ssh-extra.conf"


def test_dropin_is_installed_checked_and_then_sshd_is_reloaded_if_active(env):
    result = install_ssh(env)
    assert result.returncode == 0, result.stderr
    assert "AllowUsers azureuser" in dropin(env).read_text()
    assert calls(env) == ["sshd -t", "systemctl try-reload-or-restart ssh"]
    leftovers = sorted(p.name for p in Path(env["CFDAGENT_SETUP_SSHD_DIR"]).iterdir())
    assert leftovers == ["20-cfdagent-ssh-extra.conf"]


def test_failed_sshd_test_refuses_to_reload_and_keeps_the_previous_dropin(env):
    dropin(env).write_text("MaxAuthTries 6\nAllowUsers previous\n")
    env["STUB_SSHD_FAIL"] = "1"
    result = install_ssh(env)
    assert result.returncode != 0
    assert "sshd -t failed" in result.stderr
    assert dropin(env).read_text() == "MaxAuthTries 6\nAllowUsers previous\n"
    assert not any(call.startswith("systemctl") for call in calls(env))
    leftovers = sorted(p.name for p in Path(env["CFDAGENT_SETUP_SSHD_DIR"]).iterdir())
    assert leftovers == ["20-cfdagent-ssh-extra.conf"]


def test_failed_sshd_test_without_a_previous_dropin_leaves_none(env):
    env["STUB_SSHD_FAIL"] = "1"
    assert install_ssh(env).returncode != 0
    assert list(Path(env["CFDAGENT_SETUP_SSHD_DIR"]).iterdir()) == []
    assert not any(call.startswith("systemctl") for call in calls(env))


def test_installing_twice_gives_the_same_result(env):
    assert install_ssh(env).returncode == 0
    first = dropin(env).read_text()
    assert install_ssh(env).returncode == 0
    assert dropin(env).read_text() == first
    names = [p.name for p in Path(env["CFDAGENT_SETUP_SSHD_DIR"]).iterdir()]
    assert names == [dropin(env).name]


def test_installing_for_another_user_replaces_the_allowed_user(env):
    assert install_ssh(env, "alice").returncode == 0
    assert install_ssh(env, "bob").returncode == 0
    text = dropin(env).read_text()
    assert "AllowUsers bob" in text
    assert "alice" not in text


# --- scripts and units ---------------------------------------------------------------------------


def test_unit_installation_copies_the_files_and_enables_the_firewall(env, tmp_path):
    result = run_function(env, f"{SETTINGS}; install_vm_units")
    assert result.returncode == 0, result.stderr
    sbin, unit_dir = tmp_path / "sbin", tmp_path / "units"
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
    timer = (unit_dir / "cfdagent-daily-shutdown.timer").read_text()
    assert "OnCalendar=*-*-* 23:00:00 Europe/Warsaw\n" in timer
    assert "OnUnitActiveSec=10min" in timer
    assert "__ON_CALENDAR__" not in timer
    log = calls(env)
    assert "systemd-analyze calendar *-*-* 23:00:00 Europe/Warsaw" in log
    assert "systemctl daemon-reload" in log
    assert "systemctl enable cfdagent-iptables.service" in log
    assert "systemctl restart cfdagent-iptables.service" in log
    assert "systemctl restart cfdagent-daily-shutdown.timer" in log


def test_a_calendar_that_systemd_rejects_installs_nothing(env, tmp_path):
    env["STUB_ANALYZE_FAIL"] = "1"
    result = run_function(env, f"{SETTINGS}; install_vm_units")
    assert result.returncode != 0
    assert "systemd rejects the calendar expression" in result.stderr
    assert list((tmp_path / "units").iterdir()) == []
    assert list((tmp_path / "sbin").iterdir()) == []
    assert not any(call.startswith("systemctl") for call in calls(env))


def test_the_timer_is_installed_through_a_temporary_file(env, tmp_path):
    assert run_function(env, f"{SETTINGS}; install_vm_units").returncode == 0
    hidden = [p.name for p in (tmp_path / "units").iterdir() if p.name.startswith(".")]
    assert hidden == []


# --- the pre-flight on the checkout --------------------------------------------------------------


def test_every_required_vm_file_is_checked_before_anything_is_changed(env, tmp_path):
    files = tmp_path / "vm-files"
    shutil.copytree(VM_FILES, files)
    env["CFDAGENT_SETUP_VM_FILES_DIR"] = str(files)
    assert run_function(env, f"{SETTINGS}; check_vm_files").returncode == 0
    (files / "cfdagent-iptables.service").unlink()
    result = run_function(env, f"{SETTINGS}; REPO_REF=oldref; check_vm_files")
    assert result.returncode == 1
    assert (
        "ref oldref does not contain infra/vm/cfdagent-iptables.service; "
        "use a ref that includes the VM host files"
    ) in result.stderr


def test_the_required_file_list_matches_what_the_installer_reads():
    text = SCRIPT.read_text()
    listed = set(re.findall(r"^    ([\w.-]+)$", text[: text.index("SCRIPT_DIR=")], re.MULTILINE))
    listed.discard("")
    installed = set(re.findall(r'"\$\{files\}/([\w.-]+)"', text))
    assert installed <= listed | {"20-cfdagent-ssh-extra.conf"}


# --- the active-run guard ------------------------------------------------------------------------


def set_marker_age(env: dict[str, str], seconds: int) -> None:
    marker = Path(env["CFDAGENT_SETUP_STATE_DIR"]) / "active-run"
    marker.write_text("")
    stamp = time.time() - seconds
    os.utime(marker, (stamp, stamp))


def test_setup_refuses_while_a_fresh_run_marker_exists(env):
    set_marker_age(env, 60)
    result = run_function(env, f"{SETTINGS}; refuse_while_run_active")
    assert result.returncode == 1
    assert "a run is active" in result.stderr
    assert "--force-while-running" in result.stderr


def test_setup_proceeds_with_the_flag_a_stale_marker_or_no_marker(env):
    assert run_function(env, f"{SETTINGS}; refuse_while_run_active").returncode == 0
    set_marker_age(env, 60)
    forced = run_function(env, f"{SETTINGS}; FORCE_WHILE_RUNNING=1; refuse_while_run_active")
    assert forced.returncode == 0
    set_marker_age(env, 5 * 3600)
    assert run_function(env, f"{SETTINGS}; refuse_while_run_active").returncode == 0


# --- env file, wrapper, exit status --------------------------------------------------------------


def test_env_file_is_written_with_mode_640_and_the_admin_group(env, tmp_path):
    result = run_function(env, f"{SETTINGS}; COMMIT={SHA}; write_env")
    assert result.returncode == 0, result.stderr
    path = tmp_path / "etc" / "env"
    info = path.stat()
    assert stat.S_IMODE(info.st_mode) == 0o640
    group = subprocess.run(["id", "-gn"], capture_output=True, text=True, check=True).stdout
    owner_group = subprocess.run(
        ["stat", "-c", "%G", str(path)], capture_output=True, text=True, check=True
    ).stdout
    assert owner_group == group
    text = path.read_text()
    assert "CFDAGENT_LLM_ENDPOINT=https://a.invalid\n" in text
    assert f"CFDAGENT_COMMIT={SHA}\n" in text
    assert stat.S_IMODE((tmp_path / "etc").stat().st_mode) == 0o750
    assert list((tmp_path / "etc").iterdir()) == [path], "no temporary file is left behind"


def test_wrapper_loads_the_env_file_and_forwards_the_arguments(env, tmp_path):
    host_bin = tmp_path / "host" / "bin"
    host_bin.mkdir(parents=True)
    stub_executable(
        host_bin,
        "cfdagent",
        'echo "endpoint=$CFDAGENT_LLM_ENDPOINT"\nfor a in "$@"; do echo "arg=[$a]"; done\n',
    )
    assert run_function(env, f"{SETTINGS}; write_env; install_wrapper").returncode == 0
    wrapper = tmp_path / "bin" / "cfdagent"
    assert wrapper.stat().st_mode & stat.S_IXUSR
    out = subprocess.run(
        [str(wrapper), "run", "two words", "--flag=x y"],
        env={"PATH": env["PATH"]},
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines() == [
        "endpoint=https://a.invalid",
        "arg=[run]",
        "arg=[two words]",
        "arg=[--flag=x y]",
    ]


def test_wrapper_warns_when_the_console_script_is_missing(env):
    result = run_function(env, f"{SETTINGS}; write_env; install_wrapper")
    assert result.returncode == 0
    assert "does not exist at this commit" in result.stderr


def test_failed_image_pull_is_loud_and_finishes_with_exit_status_3(env, tmp_path):
    env["STUB_DOCKER_FAIL"] = "1"
    snippet = f"{SETTINGS}; COMMIT={SHA}; write_env; pull_image; finish"
    result = run_function(env, snippet)
    assert result.returncode == 3
    assert "could not pull ghcr.io/mkrajew/cfd-agent:sha-0123456" in result.stderr
    assert "NOT PULLED" in result.stdout
    assert f"commit {SHA}" in result.stdout
    assert "docker pull ghcr.io/mkrajew/cfd-agent:sha-0123456" in calls(env)


def test_finish_succeeds_when_the_image_was_pulled(env):
    result = run_function(env, f"{SETTINGS}; COMMIT={SHA}; write_env; pull_image; finish")
    assert result.returncode == 0, result.stderr
    assert "image        pulled" in result.stdout


def test_the_venv_is_judged_by_pip_not_python():
    text = SCRIPT.read_text()
    assert '[ -x "${HOST_DIR}/bin/pip" ] || python3 -m venv' in text
    assert '"${HOST_DIR}/bin/python" ]' not in text


# --- source checkout (a local repository stands in for GitHub) -----------------------------------


def git(repo: Path, *args: str) -> str:
    assert GIT is not None
    base = ["-c", "user.name=ci", "-c", "user.email=none", "-c", "commit.gpgsign=false"]
    result = subprocess.run(
        [GIT, "-C", str(repo), *base, *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    assert GIT is not None
    repo = tmp_path / "origin"
    subprocess.run([GIT, "init", "--quiet", "-b", "main", str(repo)], check=True)
    (repo / "file.txt").write_text("one\n")
    git(repo, "add", "file.txt")
    git(repo, "commit", "--quiet", "-m", "one")
    return repo


def sync(env: dict[str, str], tmp_path: Path, origin: Path, ref: str):
    return run_function(
        env,
        f"{SETTINGS}; REPO_REF={ref}; sync_source; echo COMMIT=$COMMIT",
        CFDAGENT_SETUP_REPO_URL=str(origin),
        CFDAGENT_SETUP_SRC_DIR=str(tmp_path / "src"),
    )


@pytest.mark.skipif(GIT is None, reason="needs git")
def test_sync_resolves_a_commit_id_a_tag_and_a_branch(env, tmp_path, origin):
    first = git(origin, "rev-parse", "HEAD")
    git(origin, "tag", "v1")
    git(origin, "switch", "--quiet", "-c", "topic")
    (origin / "file.txt").write_text("two\n")
    git(origin, "commit", "--quiet", "-am", "two")
    second = git(origin, "rev-parse", "HEAD")
    for ref, expected in ((first, first), (first[:10], first), ("v1", first), ("topic", second)):
        result = sync(env, tmp_path, origin, ref)
        assert result.returncode == 0, (ref, result.stderr)
        assert f"COMMIT={expected}" in result.stdout, ref
        assert (tmp_path / "src" / "file.txt").read_text() == (
            "one\n" if expected == first else "two\n"
        )


@pytest.mark.skipif(GIT is None, reason="needs git")
def test_sync_follows_the_remote_branch_not_a_stale_local_one(env, tmp_path, origin):
    assert sync(env, tmp_path, origin, "main").returncode == 0
    (origin / "file.txt").write_text("newer\n")
    git(origin, "commit", "--quiet", "-am", "newer")
    newest = git(origin, "rev-parse", "HEAD")
    result = sync(env, tmp_path, origin, "main")
    assert f"COMMIT={newest}" in result.stdout
    assert (tmp_path / "src" / "file.txt").read_text() == "newer\n"


@pytest.mark.skipif(GIT is None, reason="needs git")
def test_sync_fails_clearly_for_an_unknown_ref(env, tmp_path, origin):
    result = sync(env, tmp_path, origin, "no-such-ref")
    assert result.returncode == 1
    assert "ref no-such-ref not found" in result.stderr


@pytest.mark.skipif(GIT is None, reason="needs git")
def test_sync_never_prompts_for_credentials(env, tmp_path, origin):
    result = run_function(
        env,
        f"{SETTINGS}; sync_source >/dev/null 2>&1; echo prompt=$GIT_TERMINAL_PROMPT",
        CFDAGENT_SETUP_REPO_URL=str(origin),
        CFDAGENT_SETUP_SRC_DIR=str(tmp_path / "src"),
    )
    assert "prompt=0" in result.stdout
