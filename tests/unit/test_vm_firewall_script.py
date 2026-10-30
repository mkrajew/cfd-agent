"""infra/vm/cfdagent-iptables.sh with a stub `iptables` that keeps its rules in a text file.

The stub understands `-w`, `-L <chain>`, `-C`, `-I <chain> <position>` and `-D`; a rule is the text
after the chain name, one line per rule. No real firewall is touched.
"""

import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

VM_DIR = Path(__file__).resolve().parents[2] / "infra" / "vm"
SCRIPT = VM_DIR / "cfdagent-iptables.sh"
UNIT = VM_DIR / "cfdagent-iptables.service"
BASH = shutil.which("bash")

IPTABLES_STUB = r"""#!/bin/sh
[ "$1" = "-w" ] && shift
op=$1; chain=$2; shift 2
echo "iptables $op $chain $*" >> "$STUB_CALLS"
touch "$STUB_RULES"
case "$op" in
  -L) [ -z "$STUB_NO_CHAIN" ]; exit $? ;;
  -C) grep -qxF -- "$*" "$STUB_RULES"; exit $? ;;
  -I)
    pos=$1; shift
    count=$(wc -l < "$STUB_RULES")
    if [ "$pos" -gt "$count" ]; then echo "$*" >> "$STUB_RULES"; exit 0; fi
    sed -i "${pos}i\\
$*" "$STUB_RULES"; exit 0 ;;
  -D)
    line=$(grep -nxF -- "$*" "$STUB_RULES" | head -n1 | cut -d: -f1)
    [ -n "$line" ] || exit 1
    sed -i "${line}d" "$STUB_RULES"; exit 0 ;;
esac
exit 2
"""

EXPECTED = [
    "-d 168.63.129.16/32 -p udp -m udp --dport 53 -j ACCEPT",
    "-d 168.63.129.16/32 -p tcp -m tcp --dport 53 -j ACCEPT",
    "-d 168.63.129.16/32 -j DROP",
    "-d 169.254.169.254/32 -j DROP",
]

pytestmark = pytest.mark.skipif(
    sys.platform == "win32" or BASH is None, reason="needs a POSIX bash (runs in CI and the image)"
)


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    stub = stubs / "iptables"
    stub.write_text(IPTABLES_STUB)
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return {
        "PATH": f"{stubs}:/usr/local/bin:/usr/bin:/bin",
        "STUB_RULES": str(tmp_path / "rules"),
        "STUB_CALLS": str(tmp_path / "calls"),
    }


def run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run(
        [BASH, str(SCRIPT), *args], env=env, capture_output=True, text=True, check=False
    )


def rules(env: dict[str, str]) -> list[str]:
    path = Path(env["STUB_RULES"])
    return path.read_text().splitlines() if path.exists() else []


def test_start_inserts_dns_accepts_before_the_drops(env):
    result = run(env, "start")
    assert result.returncode == 0, result.stderr
    assert rules(env) == EXPECTED


def test_start_is_idempotent(env):
    assert run(env, "start").returncode == 0
    assert run(env, "start").returncode == 0
    assert rules(env) == EXPECTED


def test_start_checks_with_dash_c_before_every_insert(env):
    assert run(env, "start").returncode == 0
    calls = Path(env["STUB_CALLS"]).read_text().splitlines()
    for index, call in enumerate(calls):
        if " -I " in f" {call} ":
            rule = call.split(maxsplit=4)[4]
            assert any(
                earlier.startswith("iptables -C DOCKER-USER") and earlier.endswith(rule)
                for earlier in calls[:index]
            ), f"no -C check before: {call}"


def test_start_keeps_other_rules_after_ours(env):
    Path(env["STUB_RULES"]).write_text("-s 10.1.0.0/16 -j RETURN\n")
    assert run(env, "start").returncode == 0
    assert rules(env) == [*EXPECTED, "-s 10.1.0.0/16 -j RETURN"]


def test_start_repairs_a_wrong_order(env):
    Path(env["STUB_RULES"]).write_text(
        "-d 169.254.169.254/32 -j DROP\n-d 168.63.129.16/32 -j DROP\n"
    )
    assert run(env, "start").returncode == 0
    assert rules(env) == EXPECTED


def test_stop_removes_only_our_rules(env):
    Path(env["STUB_RULES"]).write_text("-s 10.1.0.0/16 -j RETURN\n")
    assert run(env, "start").returncode == 0
    assert run(env, "stop").returncode == 0
    assert rules(env) == ["-s 10.1.0.0/16 -j RETURN"]


def test_stop_without_rules_succeeds(env):
    assert run(env, "stop").returncode == 0
    assert rules(env) == []


def test_missing_docker_chain_fails_start_with_a_clear_message(env):
    env["STUB_NO_CHAIN"] = "1"
    result = run(env, "start")
    assert result.returncode != 0
    assert "DOCKER-USER" in result.stderr


def test_unknown_action_is_rejected(env):
    assert run(env, "restart").returncode == 2


def test_status_reports_missing_and_present_rules(env):
    assert run(env, "status").returncode != 0
    assert run(env, "start").returncode == 0
    assert run(env, "status").returncode == 0


def test_unit_runs_the_script_after_docker_and_removes_the_rules_on_stop():
    unit = UNIT.read_text()
    assert "After=docker.service" in unit
    assert "Requires=docker.service" in unit
    assert "Type=oneshot" in unit
    assert "RemainAfterExit=yes" in unit
    assert "ExecStart=/usr/local/sbin/cfdagent-iptables start" in unit
    assert "ExecStop=/usr/local/sbin/cfdagent-iptables stop" in unit
    assert "WantedBy=multi-user.target" in unit
