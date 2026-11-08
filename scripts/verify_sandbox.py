"""Verify the executor sandbox on the machine it runs on (the VM) and print a pass/fail table.

Usage, on the VM with the host environment (host dependencies only, no pytest, no toolchain):

    /opt/cfdagent-host/bin/python scripts/verify_sandbox.py [--image IMAGE]

The image is ``--image`` or ``$CFDAGENT_IMAGE`` (``/etc/cfdagent/env`` provides it). Everything runs
through ``SandboxClient`` in ``docker`` mode, exactly as a run does, in a temporary run directory
under ``$CFDAGENT_RUNS_DIR`` (or the system temporary directory) that is removed at the end.

One row per check, mirroring the real-Docker tests in ``tests/sandbox/test_docker_mode.py``: no
network, no credentials in the environment, read-only root, case-supplied code refused, four MPI
ranks, a timeout leaves no container, the run directory is the only mount. The rows are smoke
checks on the target machine, not a replacement for those tests: the ``#codeStream`` row tries one
variant through ``check_dictionaries`` (the full set of hostile cases lives in ``tests/sandbox``)
and the MPI row starts four ranks of a trivial program, not a solver. The exit status is 0
only when every row passes. The probes are short scripts that the executor runs in the container
(``run_script``); the host side only judges what they print. The cost is a few minutes of VM time.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, TextIO

from cfdagent.rundir import RunDir
from cfdagent.sandbox.client import SandboxClient, outer_timeout
from cfdagent.sandbox.config import SandboxConfig

IMAGE_ENV = "CFDAGENT_IMAGE"
RUNS_DIR_ENV = "CFDAGENT_RUNS_DIR"
PROBE_TIMEOUT_S = 120.0
CODE_CHECK_TIMEOUT_S = 300.0
CONTAINER_TIMEOUT_S = 20.0
EVIDENCE_CHARS = 110
MARKER = "EXECUTED_MARKER"  # the file the planted code would create if it ever ran
# Planted in this process before the network probe: neither name nor value may reach a container.
SECRET_MARKERS = {
    "AZURE_CLIENT_SECRET": "s3cret-azure-value",
    "CFDAGENT_LLM_API_KEY": "s3cret-llm-value",
}
# What a hardened container has besides the run directory (checked against ``docker run`` on
# Docker Desktop and on the VM): the root, the run directory (judged separately), the ``--tmpfs``
# /tmp, Docker's own hosts/hostname/resolv.conf files, the host's ``docker-init`` binary that
# ``--init`` bind-mounts read-only, and virtual file systems under /proc, /sys and /dev.
VIRTUAL_FS = {"proc", "sysfs", "tmpfs", "devpts", "mqueue", "cgroup", "cgroup2"}
VIRTUAL_PREFIXES = ("/proc", "/sys", "/dev")
DOCKER_FILES = {"/etc/hosts", "/etc/hostname", "/etc/resolv.conf"}  # source has the same name
DOCKER_INIT_POINTS = {"/sbin/docker-init", "/usr/sbin/docker-init"}

PROBES = {
    "network.py": """
        import json, os, socket
        out = {}
        for host, port in (("1.1.1.1", 443), ("169.254.169.254", 80), ("168.63.129.16", 80)):
            s = socket.socket()
            s.settimeout(3)
            try:
                s.connect((host, port))
                out[f"{host}:{port}"] = "CONNECTED"
            except OSError as exc:
                out[f"{host}:{port}"] = f"blocked: {exc}"
            finally:
                s.close()
        try:
            socket.getaddrinfo("example.org", 80)
            out["dns"] = "RESOLVED"
        except OSError as exc:
            out["dns"] = f"blocked: {exc}"
        out["interfaces"] = sorted(os.listdir("/sys/class/net"))
        print("RESULT=" + json.dumps(out))
    """,
    "credentials.py": """
        import json, os
        pid1 = open("/proc/1/environ", "rb").read().decode("utf-8", "replace")
        print("RESULT=" + json.dumps({"environ": dict(os.environ), "pid1": pid1}))
    """,
    "readonly.py": """
        import json
        out = {}
        for label, path in (("write /etc", "/etc/cfd-test"), ("write /usr/local", "/usr/local/x"),
                            ("write /opt", "/opt/cfd-test"), ("write /tmp", "/tmp/cfd-test"),
                            ("write /job", "/job/cfd-test")):
            try:
                open(path, "w").close()
                out[label] = "ALLOWED"
            except OSError as exc:
                out[label] = f"refused: {exc}"
        status = dict(x.split(":", 1) for x in open("/proc/self/status").read().splitlines()
                      if ":" in x)
        out["no_new_privs"] = status["NoNewPrivs"].strip()
        out["cap_eff"] = status["CapEff"].strip()
        out["cap_bnd"] = status["CapBnd"].strip()
        for line in open("/proc/mounts"):
            fields = line.split()
            if fields[1] in ("/", "/tmp", "/job"):
                out["mount " + fields[1]] = fields[3].split(",")
        print("RESULT=" + json.dumps(out))
    """,
    "mpi.py": """
        import json, subprocess
        done = subprocess.run(
            ["mpirun", "-np", "4", "--use-hwthread-cpus", "python", "-P", "-c", "print('rank')"],
            capture_output=True, text=True, timeout=100,
        )
        print("RESULT=" + json.dumps({"returncode": done.returncode,
                                      "lines": done.stdout.split(), "stderr": done.stderr[-300:]}))
    """,
    "mounts.py": """
        import json
        mounts, options = [], {}
        for line in open("/proc/self/mountinfo"):
            left, right = line.split(" - ")
            fields = left.split()
            mounts.append([fields[4], right.split()[0], fields[3], fields[5].split(",")])
            options[fields[4]] = fields[5].split(",")
        print("RESULT=" + json.dumps({"mounts": mounts, "job_options": options.get("/job", [])}))
    """,
}

SLOW_SCRIPT = "import pathlib, time\npathlib.Path('started').write_text('x')\ntime.sleep(300)\n"

FOAM_HEADER = (
    "FoamFile\n{\n    version 2.0;\n    format ascii;\n    class dictionary;\n    object %s;\n}\n"
)
# Code that would write MARKER into the run directory if OpenFOAM ever compiled and ran it.
CODE_STREAM = (
    "trap #codeStream\n{\n    codeInclude #{ #include <fstream> #};\n"
    f'    code #{{ std::ofstream("/job/{MARKER}") << "ran"; os << 1; #}};\n}};\n'
)


@dataclass(frozen=True)
class Row:
    name: str
    passed: bool
    evidence: str


class Harness(Protocol):
    """What the checks need: a run directory with an executor behind it."""

    root: Path
    run_id: str

    def write(self, rel: str, text: str) -> None: ...

    def probe(self, source: str, name: str, **extra: Any) -> dict[str, Any]: ...

    def call(self, op: str, args: dict[str, Any], timeout_s: float) -> Any: ...

    def containers(self) -> str: ...


Verdict = tuple[bool, str]


def _probe(h: Harness, name: str) -> dict[str, Any]:
    return h.probe(textwrap.dedent(PROBES[name]), name)


# ---------------------------------------------------------------------------------- the checks


def check_no_network(h: Harness) -> Verdict:
    result = _probe(h, "network.py")
    targets = ("1.1.1.1:443", "169.254.169.254:80", "168.63.129.16:80", "dns")
    open_ = [t for t in targets if not str(result.get(t, "")).startswith("blocked")]
    if open_:
        return False, f"reachable from inside: {', '.join(open_)}"
    if result.get("interfaces") != ["lo"]:
        return False, f"interfaces are {result.get('interfaces')}, expected ['lo']"
    return True, "public IP:443, metadata:80, wireserver:80 and DNS blocked; only lo present"


def check_no_credentials(h: Harness) -> Verdict:
    saved = {key: os.environ.get(key) for key in SECRET_MARKERS}
    os.environ.update(SECRET_MARKERS)
    try:
        result = _probe(h, "credentials.py")
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    blob = json.dumps(result)
    found = [key for key, value in SECRET_MARKERS.items() if key in blob or value in blob]
    if found:
        return False, f"planted host secrets visible in the container: {', '.join(found)}"
    count = len(result.get("environ", {}))
    return True, f"planted secrets absent from the {count} environment variables and PID 1"


def check_read_only_root(h: Harness) -> Verdict:
    r = _probe(h, "readonly.py")
    problems = []
    for label in ("write /etc", "write /usr/local", "write /opt"):
        if not str(r.get(label, "")).startswith("refused"):
            problems.append(f"{label} not refused")
    for label in ("write /tmp", "write /job"):
        if r.get(label) != "ALLOWED":
            problems.append(f"{label} not writable")
    if "ro" not in r.get("mount /", []):
        problems.append("root not mounted ro")
    for flag in ("rw", "noexec", "nosuid", "nodev"):
        if flag not in r.get("mount /tmp", []):
            problems.append(f"/tmp lacks {flag}")
    if r.get("no_new_privs") != "1":
        problems.append("NoNewPrivs is not 1")
    for key in ("cap_eff", "cap_bnd"):
        if int(r.get(key, "1") or "1", 16) != 0:
            problems.append(f"{key} is {r.get(key)}")
    if problems:
        return False, "; ".join(problems)
    return True, "root ro; /tmp rw,noexec,nosuid,nodev; /job rw; no capabilities; no_new_privs=1"


def _case_files(h: Harness, case: str, with_code: bool) -> None:
    h.write(f"{case}/0/U", FOAM_HEADER % "U" + "internalField uniform (0 0 0);\n")
    h.write(f"{case}/constant/transportProperties", FOAM_HEADER % "transportProperties")
    control = FOAM_HEADER % "controlDict" + "application icoFoam;\n"
    h.write(f"{case}/system/controlDict", control + (CODE_STREAM if with_code else ""))


def check_code_stream_refused(h: Harness) -> Verdict:
    """Smoke check: one ``#codeStream`` variant is refused by ``check_dictionaries``.

    The full set of hostile cases (``#calc``, coded patches, info switches, solver ops) is in
    ``tests/sandbox``. A control case without code must be accepted, so a syntax error is not
    mistaken for a refusal.
    """
    _case_files(h, "case", with_code=True)
    _case_files(h, "control", with_code=False)
    refused = h.call("check_dictionaries", {"case_dir": "case"}, timeout_s=CODE_CHECK_TIMEOUT_S)
    control = h.call("check_dictionaries", {"case_dir": "control"}, timeout_s=CODE_CHECK_TIMEOUT_S)
    root = h.root
    traces = [
        label
        for label, pattern in (("marker", MARKER), ("dynamicCode", "dynamicCode"), ("lib", "*.so"))
        if list(root.rglob(pattern))
    ]
    if traces:
        return False, f"the code in the case ran or was prepared: {', '.join(traces)}"
    if not control.ok:
        return False, f"the control case without code was refused too: {control.error}"
    error = refused.error or ""
    if refused.ok or refused.internal:
        return False, f"not refused as a case problem: ok={refused.ok} internal={refused.internal}"
    if "case-supplied code" not in error or "plain numbers" not in error or "#eval" not in error:
        return False, f"refused, but without the general hint: {error}"
    return True, "refused with the general hint; control case accepted; no marker, no compiled code"


def check_mpi(h: Harness) -> Verdict:
    """Four MPI ranks of a trivial program start and finish inside the container (no solver)."""
    r = _probe(h, "mpi.py")
    if r.get("returncode") != 0 or r.get("lines") != ["rank"] * 4:
        return (
            False,
            f"exit {r.get('returncode')}, {len(r.get('lines', []))} ranks: {r.get('stderr')}",
        )
    return True, "4 ranks started and finished under --cap-drop ALL"


def check_timeout_leaves_no_container(h: Harness) -> Verdict:
    h.write("slow.py", SLOW_SCRIPT)
    response = h.call("run_script", {"script": "slow.py"}, timeout_s=CONTAINER_TIMEOUT_S)
    started = (h.root / "started").is_file()
    left = h.containers()
    if response.ok or "timed out" not in (response.error or ""):
        return False, f"the call did not time out: ok={response.ok} error={response.error}"
    if not started:
        return False, "the script never started in the container"
    if left:
        return False, f"container left behind: {left}"
    return True, f"killed after {CONTAINER_TIMEOUT_S:g} s; script had started; no container left"


def _is_docker_init(point: str, root: str, options: Sequence[str]) -> bool:
    return point in DOCKER_INIT_POINTS and Path(root).name == "docker-init" and "ro" in options


def _is_expected_mount(mount: Sequence[Any]) -> bool:
    point, fstype, root = mount[0], mount[1], mount[2]
    options = mount[3] if len(mount) > 3 else []
    if point in ("/", "/job"):  # /job is judged on its own
        return True
    if point == "/tmp":
        return fstype == "tmpfs"
    if point in DOCKER_FILES:
        return Path(root).name == Path(point).name
    if any(point == p or point.startswith(p + "/") for p in VIRTUAL_PREFIXES):
        return fstype in VIRTUAL_FS
    return _is_docker_init(point, root, options)


def check_only_mount(h: Harness) -> Verdict:
    r = _probe(h, "mounts.py")
    mounts = r.get("mounts", [])
    unexpected = [m[0] for m in mounts if not _is_expected_mount(m)]
    if unexpected:
        return False, f"unexpected mounts: {', '.join(unexpected)}"
    jobs = [m for m in mounts if m[0] == "/job"]
    if len(jobs) != 1:
        return False, f"/job is mounted {len(jobs)} times, expected once"
    if not jobs[0][2].rstrip("/").endswith(h.root.name):
        return False, f"/job comes from {jobs[0][2]}, not from the run directory {h.root.name}"
    if "rw" not in r.get("job_options", []):
        return False, f"/job options are {r.get('job_options')}"
    return True, f"only /job comes from the host (the run directory); {len(mounts)} mounts in all"


CHECKS: list[tuple[str, Callable[[Harness], Verdict]]] = [
    ("no network", check_no_network),
    ("no credentials in the environment", check_no_credentials),
    ("read-only root filesystem", check_read_only_root),
    ("#codeStream refused", check_code_stream_refused),
    ("mpirun -np 4 --use-hwthread-cpus", check_mpi),
    ("timeout leaves no container", check_timeout_leaves_no_container),
    ("run directory is the only mount", check_only_mount),
]


def run_checks(
    harness: Harness, checks: Sequence[tuple[str, Callable[[Harness], Verdict]]] = CHECKS
) -> list[Row]:
    """Run every check; a check that raises is a failed row, the others still run."""
    rows = []
    for name, check in checks:
        try:
            passed, evidence = check(harness)
        except Exception as exc:
            passed, evidence = False, f"{type(exc).__name__}: {exc}"
        rows.append(Row(name, bool(passed), evidence))
    return rows


def _one_line(text: str) -> str:
    line = " ".join(text.split())
    return line if len(line) <= EVIDENCE_CHARS else line[: EVIDENCE_CHARS - 3] + "..."


def format_table(rows: Sequence[Row]) -> str:
    width = max([len("CHECK"), *(len(row.name) for row in rows)])
    lines = [f"{'CHECK':<{width}}  RESULT  EVIDENCE", f"{'-' * width}  ------  --------"]
    for row in rows:
        result = "PASS" if row.passed else "FAIL"
        lines.append(f"{row.name:<{width}}  {result:<6}  {_one_line(row.evidence)}")
    lines.append(f"{sum(row.passed for row in rows)}/{len(rows)} checks passed")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------- real Docker


class SetupError(Exception):
    """The environment cannot run the checks at all (no Docker, image missing)."""


class DockerHarness:
    """A temporary run directory and a docker-mode ``SandboxClient`` for it."""

    def __init__(self, root: Path, image: str) -> None:
        self.run_dir = RunDir(root)
        self.root = self.run_dir.root
        self.run_id = f"{datetime.now(UTC):%Y%m%d-%H%M%S}-verify"
        config = SandboxConfig(mode="docker", image=image)
        self.client = SandboxClient(config, self.run_dir, self.run_id)

    def write(self, rel: str, text: str) -> None:
        self.run_dir.write_text(rel, text)

    def call(self, op: str, args: dict[str, Any], timeout_s: float) -> Any:
        return self.client.call(op, args, timeout_s=timeout_s)

    def probe(self, source: str, name: str, **extra: Any) -> dict[str, Any]:
        """Run ``source`` as a script; it must print one line ``RESULT=<json>``."""
        self.write(name, source)
        response = self.call(
            "run_script", {"script": name, **extra}, timeout_s=outer_timeout(PROBE_TIMEOUT_S)
        )
        if response.data is None:
            raise RuntimeError(f"run_script {name} failed: {response.error}")
        matches = re.findall(r"^RESULT=(.*)$", response.data["tail"], flags=re.MULTILINE)
        if not matches:
            raise RuntimeError(f"{name} printed no RESULT line: {response.data['tail'][-200:]}")
        return json.loads(matches[-1])

    def containers(self) -> str:
        """Ids of any container (running or not) whose name belongs to this run."""
        listed = subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"name=cfd-{self.run_id}-"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return listed.stdout.strip()


def remove_run_dir(
    root: Path, *, remove: Callable[..., Any] = shutil.rmtree, err: TextIO | None = None
) -> bool:
    """Remove the temporary run directory; warn on ``err`` and return False if it is still there."""
    remove(root, ignore_errors=True)
    if not root.exists():
        return True
    print(f"warning: could not remove the temporary run directory {root}", file=err or sys.stderr)
    return False


@contextlib.contextmanager
def docker_harness(image: str, runs_dir: Path | None) -> Iterator[DockerHarness]:
    if shutil.which("docker") is None:
        raise SetupError("docker is not on PATH")
    found = subprocess.run(
        ["docker", "image", "inspect", image], capture_output=True, text=True, timeout=60
    )
    if found.returncode != 0:
        raise SetupError(
            f"the image {image} is not available locally: {found.stderr.strip()[:200]}"
        )
    if runs_dir is not None:
        runs_dir.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="verify-", dir=runs_dir))
    try:
        yield DockerHarness(root, image)
    finally:
        remove_run_dir(root)


# ---------------------------------------------------------------------------------- entry point


def main(
    argv: Sequence[str] | None = None,
    *,
    make_harness: Callable[[str, Path | None], AbstractContextManager[Harness]] = docker_harness,
    environ: Mapping[str, str] | None = None,
    out: TextIO | None = None,
) -> int:
    environ = os.environ if environ is None else environ
    out = out or sys.stdout
    parser = argparse.ArgumentParser(description="Verify the executor sandbox (pass/fail table).")
    parser.add_argument("--image", help=f"executor image (default: ${IMAGE_ENV})")
    args = parser.parse_args(argv)
    image = args.image or environ.get(IMAGE_ENV)
    if not image:
        print(f"error: give --image or set {IMAGE_ENV}", file=sys.stderr)
        return 2
    runs = environ.get(RUNS_DIR_ENV)
    try:
        with make_harness(image, Path(runs) if runs else None) as harness:
            rows = run_checks(harness)
    except SetupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"image: {image}", file=out)
    print(format_table(rows), file=out)
    return 0 if all(row.passed for row in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
