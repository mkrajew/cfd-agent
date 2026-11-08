"""scripts/verify_sandbox.py: the pass/fail table logic, tested with a fake harness (no Docker)."""

import ast
import importlib.util
import io
import json
import re
import sys
import textwrap
from pathlib import Path

import pytest

from cfdagent.executor.protocol import OpResponse, failure
from cfdagent.rundir import RunDir

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "verify_sandbox.py"
CHECK_NAMES = [
    "no network",
    "no credentials in the environment",
    "read-only root filesystem",
    "#codeStream refused",
    "mpirun -np 4 --use-hwthread-cpus",
    "timeout leaves no container",
    "run directory is the only mount",
]


def load():
    spec = importlib.util.spec_from_file_location("verify_sandbox", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["verify_sandbox"] = module
    spec.loader.exec_module(module)
    return module


vs = load()

GOOD_PROBES = {
    "network.py": {
        "1.1.1.1:443": "blocked: [Errno 101] Network is unreachable",
        "169.254.169.254:80": "blocked: [Errno 101] Network is unreachable",
        "168.63.129.16:80": "blocked: [Errno 101] Network is unreachable",
        "dns": "blocked: [Errno -3] Temporary failure in name resolution",
        "interfaces": ["lo"],
    },
    "credentials.py": {"environ": {"PATH": "/usr/bin", "HOME": "/tmp/home-1"}, "pid1": ""},
    "readonly.py": {
        "write /etc": "refused: [Errno 30] Read-only file system",
        "write /usr/local": "refused: [Errno 30] Read-only file system",
        "write /opt": "refused: [Errno 30] Read-only file system",
        "write /tmp": "ALLOWED",
        "write /job": "ALLOWED",
        "mount /": ["ro", "relatime"],
        "mount /tmp": ["rw", "nosuid", "nodev", "noexec"],
        "no_new_privs": "1",
        "cap_eff": "0000000000000000",
        "cap_bnd": "0000000000000000",
    },
    "mpi.py": {"returncode": 0, "lines": ["rank"] * 4, "stderr": ""},
    "mounts.py": {
        "mounts": [
            ["/", "overlay", "/"],
            ["/proc", "proc", "/"],
            ["/proc/kcore", "tmpfs", "/"],
            ["/dev", "tmpfs", "/"],
            ["/dev/shm", "tmpfs", "/"],
            ["/sys", "sysfs", "/"],
            ["/sys/fs/cgroup", "cgroup2", "/"],
            ["/etc/hosts", "ext4", "/var/lib/docker/containers/abc/hosts"],
            ["/etc/hostname", "ext4", "/var/lib/docker/containers/abc/hostname"],
            ["/etc/resolv.conf", "ext4", "/var/lib/docker/containers/abc/resolv.conf"],
            ["/tmp", "tmpfs", "/"],
            ["/usr/sbin/docker-init", "ext4", "/usr/bin/docker-init", ["ro", "relatime"]],
            ["/job", "ext4", "/home/admin/runs/RUNNAME"],
        ],
        "job_options": ["rw", "relatime"],
    },
}


class FakeHarness:
    """The collaborator of the checks: canned probe results and canned op responses."""

    def __init__(self, root: Path, probes=None, responses=None):
        self.root = root
        self.run_dir = RunDir(root)
        self.run_id = "20260101-000000-verify"
        self.probes = json.loads(json.dumps(probes if probes is not None else GOOD_PROBES))
        self.responses = dict(responses or {})
        self.container_ids = ""
        self.calls: list[tuple[str, dict, float]] = []
        self.probed: list[str] = []

    def write(self, rel: str, text: str) -> None:
        self.run_dir.write_text(rel, text)

    def probe(self, source: str, name: str, **extra) -> dict:
        ast.parse(source)  # every probe is valid Python
        self.probed.append(name)
        result = self.probes[name]
        if isinstance(result, Exception):
            raise result
        if name == "mounts.py":
            result = json.loads(json.dumps(result).replace("RUNNAME", self.root.name))
        return result

    def call(self, op: str, args: dict, timeout_s: float) -> OpResponse:
        self.calls.append((op, args, timeout_s))
        response = self.responses.get((op, args.get("case_dir") or args.get("script")))
        if callable(response):
            return response(self)
        assert response is not None, f"no canned response for {op} {args}"
        return response

    def containers(self) -> str:
        return self.container_ids


REFUSAL = failure(
    "OpenFOAM refused case-supplied code: dictionaries carrying their own C++ ...\n"
    "plain numbers ... #eval ..."
)


def codestream_responses():
    return {
        ("check_dictionaries", "case"): REFUSAL,
        ("check_dictionaries", "control"): OpResponse(ok=True, data={"checked": 3}),
    }


def timeout_responses():
    def slow(harness):
        (harness.root / "started").write_text("x")
        return failure("run_script timed out after 20 s and was killed")

    return {("run_script", "slow.py"): slow}


def make(tmp_path, probes=None, responses=None) -> FakeHarness:
    merged = {**codestream_responses(), **timeout_responses(), **(responses or {})}
    return FakeHarness(tmp_path, probes, merged)


def run_one(name: str, harness: FakeHarness):
    checks = [check for check in vs.CHECKS if check[0] == name]
    assert len(checks) == 1, name
    (row,) = vs.run_checks(harness, checks)
    return row


def test_the_checks_are_the_seven_required_rows_in_order():
    assert [name for name, _ in vs.CHECKS] == CHECK_NAMES


def test_probe_sources_are_short_valid_python_that_print_a_result_line():
    for name, source in vs.PROBES.items():
        source = textwrap.dedent(source)
        ast.parse(source)
        assert "RESULT=" in source, name
        assert len(source.strip().splitlines()) <= 40, f"{name} is not short"


def test_every_check_passes_with_the_expected_facts(tmp_path):
    harness = make(tmp_path)
    rows = vs.run_checks(harness)
    assert [row.name for row in rows] == CHECK_NAMES
    assert all(row.passed for row in rows), [(r.name, r.evidence) for r in rows if not r.passed]
    assert all(row.evidence for row in rows)


@pytest.mark.parametrize(
    ("name", "probe", "key", "value"),
    [
        ("no network", "network.py", "1.1.1.1:443", "CONNECTED"),
        ("no network", "network.py", "169.254.169.254:80", "CONNECTED"),
        ("no network", "network.py", "dns", "RESOLVED"),
        ("no network", "network.py", "interfaces", ["eth0", "lo"]),
        ("no credentials in the environment", "credentials.py", "pid1", "AZURE_CLIENT_SECRET=x"),
        ("read-only root filesystem", "readonly.py", "write /etc", "ALLOWED"),
        ("read-only root filesystem", "readonly.py", "write /job", "refused: no"),
        ("read-only root filesystem", "readonly.py", "mount /", ["rw"]),
        ("read-only root filesystem", "readonly.py", "mount /tmp", ["rw", "nosuid"]),
        ("read-only root filesystem", "readonly.py", "no_new_privs", "0"),
        ("read-only root filesystem", "readonly.py", "cap_eff", "00000000000000c0"),
        ("mpirun -np 4 --use-hwthread-cpus", "mpi.py", "returncode", 1),
        ("mpirun -np 4 --use-hwthread-cpus", "mpi.py", "lines", ["rank"] * 3),
        ("run directory is the only mount", "mounts.py", "job_options", ["ro"]),
    ],
)
def test_a_changed_fact_fails_only_its_row(tmp_path, name, probe, key, value):
    probes = json.loads(json.dumps(GOOD_PROBES))
    probes[probe][key] = value
    harness = make(tmp_path, probes)
    rows = vs.run_checks(harness)
    failed = [row.name for row in rows if not row.passed]
    assert failed == [name], [(r.name, r.evidence) for r in rows]


def test_a_secret_from_the_host_environment_in_the_container_fails_the_credentials_row(tmp_path):
    probes = json.loads(json.dumps(GOOD_PROBES))
    probes["credentials.py"]["environ"]["X"] = vs.SECRET_MARKERS["AZURE_CLIENT_SECRET"]
    row = run_one("no credentials in the environment", make(tmp_path, probes))
    assert not row.passed and "AZURE_CLIENT_SECRET" in row.evidence


def test_the_credentials_check_plants_secrets_in_the_host_and_removes_them(tmp_path, monkeypatch):
    for key in vs.SECRET_MARKERS:
        monkeypatch.delenv(key, raising=False)
    seen = {}
    harness = make(tmp_path)
    original = harness.probe

    def spying_probe(source, name, **extra):
        if name == "credentials.py":
            import os

            seen.update({key: os.environ.get(key) for key in vs.SECRET_MARKERS})
        return original(source, name, **extra)

    harness.probe = spying_probe
    assert run_one("no credentials in the environment", harness).passed
    assert seen == vs.SECRET_MARKERS
    import os

    assert all(key not in os.environ for key in vs.SECRET_MARKERS)


def test_an_unexpected_mount_fails_the_mount_row(tmp_path):
    probes = json.loads(json.dumps(GOOD_PROBES))
    probes["mounts.py"]["mounts"].append(["/work", "ext4", "/home/admin/repo"])
    row = run_one("run directory is the only mount", make(tmp_path, probes))
    assert not row.passed and "/work" in row.evidence


def test_a_writable_docker_init_or_another_file_mount_fails_the_mount_row(tmp_path):
    probes = json.loads(json.dumps(GOOD_PROBES))
    probes["mounts.py"]["mounts"][-2][3] = ["rw"]
    assert not run_one("run directory is the only mount", make(tmp_path, probes)).passed
    probes = json.loads(json.dumps(GOOD_PROBES))
    probes["mounts.py"]["mounts"][-2][0] = "/usr/sbin/something-else"
    assert not run_one("run directory is the only mount", make(tmp_path, probes)).passed


@pytest.mark.parametrize(
    ("label", "entry"),
    [
        ("ext4 under /dev", ["/dev/host-disk", "ext4", "/home/admin/data", ["rw"]]),
        ("ext4 under /proc", ["/proc/stolen", "ext4", "/home/admin/data", ["rw"]]),
        ("ext4 under /sys", ["/sys/stolen", "overlay", "/", ["rw"]]),
        ("docker-init below /job", ["/job/docker-init", "ext4", "/usr/bin/docker-init", ["ro"]]),
        ("docker-init at another path", ["/usr/bin/docker-init", "ext4", "/x/docker-init", ["ro"]]),
        ("docker-init from another source", ["/sbin/docker-init", "ext4", "/x/evil", ["ro"]]),
        ("docker-init writable", ["/sbin/docker-init", "ext4", "/x/docker-init", ["rw"]]),
    ],
)
def test_tightened_mount_rules_reject_lookalikes(tmp_path, label, entry):
    probes = json.loads(json.dumps(GOOD_PROBES))
    probes["mounts.py"]["mounts"].insert(-1, entry)
    row = run_one("run directory is the only mount", make(tmp_path, probes))
    assert not row.passed and entry[0] in row.evidence, label


@pytest.mark.parametrize(
    "entry",
    [
        ["/sbin/docker-init", "overlay", "/usr/libexec/docker/docker-init", ["ro", "relatime"]],
        ["/usr/sbin/docker-init", "ext4", "/usr/bin/docker-init", ["ro"]],
        ["/dev/pts", "devpts", "/", ["rw"]],
        ["/dev/mqueue", "mqueue", "/", ["rw"]],
        ["/proc/bus", "proc", "/bus", ["ro"]],
        ["/sys/firmware", "tmpfs", "/", ["ro"]],
    ],
)
def test_what_a_hardened_container_really_has_is_accepted(tmp_path, entry):
    probes = json.loads(json.dumps(GOOD_PROBES))
    probes["mounts.py"]["mounts"].insert(-1, entry)
    assert run_one("run directory is the only mount", make(tmp_path, probes)).passed


@pytest.mark.parametrize(
    "entry",
    [
        ["/tmp", "ext4", "/home/admin/tmp", ["rw"]],  # --tmpfs makes /tmp a tmpfs, nothing else
        ["/etc/hosts", "ext4", "/home/admin/secrets", ["ro"]],  # Docker's own file is named hosts
    ],
)
def test_tmp_and_docker_managed_files_must_look_like_docker_made_them(tmp_path, entry):
    probes = json.loads(json.dumps(GOOD_PROBES))
    mounts = probes["mounts.py"]["mounts"]
    mounts[:] = [m for m in mounts if m[0] != entry[0]]
    mounts.insert(-1, entry)
    row = run_one("run directory is the only mount", make(tmp_path, probes))
    assert not row.passed and entry[0] in row.evidence


def test_leftover_run_directory_is_warned_about(tmp_path):
    left = tmp_path / "verify-left"
    left.mkdir()
    err = io.StringIO()
    assert vs.remove_run_dir(left, remove=lambda path, **kw: None, err=err) is False
    assert "verify-left" in err.getvalue() and "warning" in err.getvalue()
    err = io.StringIO()
    assert vs.remove_run_dir(left, err=err) is True and not left.exists()
    assert err.getvalue() == ""


def test_a_missing_job_mount_or_another_source_fails_the_mount_row(tmp_path):
    probes = json.loads(json.dumps(GOOD_PROBES))
    probes["mounts.py"]["mounts"] = [m for m in probes["mounts.py"]["mounts"] if m[0] != "/job"]
    assert not run_one("run directory is the only mount", make(tmp_path, probes)).passed
    probes = json.loads(json.dumps(GOOD_PROBES))
    probes["mounts.py"]["mounts"][-1][2] = "/home/admin/runs/something-else"
    assert not run_one("run directory is the only mount", make(tmp_path, probes)).passed


def test_codestream_row_needs_the_refusal_the_hint_and_no_trace_of_the_code(tmp_path):
    harness = make(tmp_path)
    assert run_one("#codeStream refused", harness).passed
    (op, args, _), (control_op, control_args, _) = harness.calls[:2]
    assert (op, control_op) == ("check_dictionaries", "check_dictionaries")
    assert {args["case_dir"], control_args["case_dir"]} == {"case", "control"}
    text = (tmp_path / "case" / "system" / "controlDict").read_text()
    assert "#codeStream" in text
    assert "#codeStream" not in (tmp_path / "control" / "system" / "controlDict").read_text()


def test_codestream_row_fails_when_the_code_ran(tmp_path):
    def ran(harness):
        (harness.root / "EXECUTED_MARKER").write_text("ran")
        return REFUSAL

    row = run_one(
        "#codeStream refused", make(tmp_path, responses={("check_dictionaries", "case"): ran})
    )
    assert not row.passed and "ran" in row.evidence


@pytest.mark.parametrize(
    "response",
    [
        OpResponse(ok=True, data={"checked": 3}),  # accepted: the code was not refused
        failure("some other error"),  # refused for another reason, no hint
        failure("x", internal=True),  # the tool broke
    ],
)
def test_codestream_row_fails_without_a_proper_refusal(tmp_path, response):
    row = run_one(
        "#codeStream refused",
        make(tmp_path, responses={("check_dictionaries", "case"): response}),
    )
    assert not row.passed


def test_codestream_row_fails_when_the_control_case_is_refused_too(tmp_path):
    row = run_one(
        "#codeStream refused",
        make(tmp_path, responses={("check_dictionaries", "control"): REFUSAL}),
    )
    assert not row.passed and "control" in row.evidence


def test_timeout_row_passes_and_fails(tmp_path):
    assert run_one("timeout leaves no container", make(tmp_path)).passed
    left = make(tmp_path)
    left.container_ids = "abc123"
    row = run_one("timeout leaves no container", left)
    assert not row.passed and "abc123" in row.evidence

    def quick(harness):
        (harness.root / "started").write_text("x")
        return OpResponse(ok=True, data={})

    never = make(tmp_path, responses={("run_script", "slow.py"): quick})
    assert not run_one("timeout leaves no container", never).passed

    def not_started(harness):
        return failure("run_script timed out after 20 s and was killed")

    (tmp_path / "started").unlink()
    nothing = make(tmp_path, responses={("run_script", "slow.py"): not_started})
    assert not run_one("timeout leaves no container", nothing).passed


def test_an_exception_in_a_check_is_a_failed_row_not_a_crash(tmp_path):
    harness = make(tmp_path)
    harness.probes["network.py"] = RuntimeError("docker is gone")
    rows = vs.run_checks(harness)
    assert [row.name for row in rows if not row.passed] == ["no network"]
    assert "docker is gone" in rows[0].evidence and "RuntimeError" in rows[0].evidence
    assert len(rows) == len(CHECK_NAMES)  # the others still ran


def test_table_has_one_line_per_check_and_a_total():
    rows = [vs.Row("alpha", True, "fine"), vs.Row("beta check", False, "went wrong\nbadly")]
    text = vs.format_table(rows)
    lines = text.splitlines()
    assert any(re.match(r"alpha\s+PASS\s+fine$", line) for line in lines)
    assert any(re.match(r"beta check\s+FAIL\s+went wrong badly$", line) for line in lines)
    assert lines[-1] == "1/2 checks passed"


def test_evidence_is_kept_to_one_short_line():
    row = vs.Row("x", True, "a" * 500 + "\nsecond")
    assert "\n" not in vs.format_table([row]).splitlines()[2]
    assert len(vs.format_table([row]).splitlines()[2]) < 200


class Recorder:
    def __init__(self, harness):
        self.harness = harness
        self.closed = False

    def __enter__(self):
        return self.harness

    def __exit__(self, *exc):
        self.closed = True


def test_main_exit_status_follows_the_rows(tmp_path, capsys):
    harness = make(tmp_path)
    recorder = Recorder(harness)
    seen = {}

    def factory(image, runs_dir):
        seen.update(image=image, runs_dir=runs_dir)
        return recorder

    status = vs.main(
        [], make_harness=factory, environ={"CFDAGENT_IMAGE": "img:1", "CFDAGENT_RUNS_DIR": "/r"}
    )
    assert status == 0 and recorder.closed
    assert seen == {"image": "img:1", "runs_dir": Path("/r")}
    out = capsys.readouterr().out
    assert "7/7 checks passed" in out

    probes = json.loads(json.dumps(GOOD_PROBES))
    probes["mpi.py"]["returncode"] = 1
    status = vs.main(
        ["--image", "other:2"],
        make_harness=lambda image, runs_dir: Recorder(make(tmp_path / "b", probes)),
        environ={"CFDAGENT_IMAGE": "img:1"},
    )
    assert status == 1
    assert "6/7 checks passed" in capsys.readouterr().out


def test_main_image_option_wins_and_runs_dir_defaults_to_none(tmp_path):
    seen = {}

    def factory(image, runs_dir):
        seen.update(image=image, runs_dir=runs_dir)
        return Recorder(make(tmp_path))

    vs.main(["--image", "from-option:3"], make_harness=factory, environ={"CFDAGENT_IMAGE": "e:1"})
    assert seen == {"image": "from-option:3", "runs_dir": None}


def test_main_without_an_image_is_a_usage_error(capsys):
    status = vs.main([], make_harness=lambda *a: pytest.fail("must not start"), environ={})
    assert status == 2
    assert "CFDAGENT_IMAGE" in capsys.readouterr().err
