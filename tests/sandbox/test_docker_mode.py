"""The sandbox against the real thing: hardened ``docker run`` containers from the image.

Each test sends agent-style code (a Python script) through ``SandboxClient`` and looks at what
that code can and cannot do. Run with ``uv run pytest -m sandbox tests/sandbox`` after rebuilding
the image. The tests also record what was verified live (network cut, no credentials, read-only
root, privilege flags, limits, MPI under ``--cap-drop ALL``).
"""

import json
import re
import shutil
import textwrap
from pathlib import Path

import pytest

from cfdagent.sandbox.client import outer_timeout

pytestmark = pytest.mark.sandbox

ROOT = Path(__file__).parents[2]
CAVITY = ROOT / "tests" / "fixtures" / "handwritten" / "cavity_re100"
CAVITY_BBOX = [0, 0, 0, 1, 1, 0.1]


def probe(sandbox, source: str, name: str = "probe.py", **extra) -> dict:
    """Run ``source`` as a script; it must print one line ``RESULT=<json>``."""
    sandbox.script(name, textwrap.dedent(source))
    response = sandbox.run_script(name, **extra)
    assert response.data is not None, response
    matches = re.findall(r"^RESULT=(.*)$", response.data["tail"], flags=re.MULTILINE)
    assert matches, f"the script printed no RESULT line: {response}"
    return json.loads(matches[-1])


def test_executor_runs_as_the_configured_unprivileged_user(sandbox):
    result = probe(
        sandbox,
        """
        import json, os
        print("RESULT=" + json.dumps({"uid": os.getuid(), "gid": os.getgid(), "cwd": os.getcwd()}))
        """,
    )
    assert result["uid"] == sandbox.config.uid != 0
    assert result["gid"] == sandbox.config.gid != 0
    assert result["cwd"] == "/job"


def test_no_network(sandbox):
    result = probe(
        sandbox,
        """
        import json, os, socket
        outcome = {}
        for host in ("1.1.1.1", "169.254.169.254", "168.63.129.16"):
            s = socket.socket()
            s.settimeout(3)
            try:
                s.connect((host, 80))
                outcome[host] = "CONNECTED"
            except OSError as exc:
                outcome[host] = f"blocked: {exc}"
            finally:
                s.close()
        try:
            socket.getaddrinfo("example.org", 80)
            outcome["dns"] = "RESOLVED"
        except OSError as exc:
            outcome["dns"] = f"blocked: {exc}"
        outcome["interfaces"] = sorted(os.listdir("/sys/class/net"))
        print("RESULT=" + json.dumps(outcome))
        """,
    )
    for target in ("1.1.1.1", "169.254.169.254", "168.63.129.16", "dns"):
        assert result[target].startswith("blocked"), result
    assert result["interfaces"] == ["lo"], result  # loopback only, no route out


def test_environment_has_no_credentials(sandbox, monkeypatch):
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "s3cret-azure-value")
    monkeypatch.setenv("CFDAGENT_LLM_API_KEY", "s3cret-llm-value")
    result = probe(
        sandbox,
        """
        import json, os
        pid1 = open("/proc/1/environ", "rb").read().decode("utf-8", "replace")
        print("RESULT=" + json.dumps({"environ": dict(os.environ), "pid1": pid1}))
        """,
    )
    blob = json.dumps(result)
    assert "s3cret" not in blob
    assert "AZURE_CLIENT_SECRET" not in blob and "CFDAGENT_LLM_API_KEY" not in blob


def test_root_filesystem_is_read_only_and_cannot_gain_privileges(sandbox):
    result = probe(
        sandbox,
        """
        import json, os, subprocess
        out = {}

        def attempt(label, function):
            try:
                function()
                out[label] = "ALLOWED"
            except OSError as exc:
                out[label] = f"refused: {exc}"

        attempt("write /etc", lambda: open("/etc/cfd-test", "w").close())
        attempt("write /usr/local", lambda: open("/usr/local/cfd-test", "w").close())
        attempt("write /opt", lambda: open("/opt/cfd-test", "w").close())
        attempt("write /tmp", lambda: open("/tmp/cfd-test", "w").close())
        attempt("write /job", lambda: open("/job/cfd-test", "w").close())
        status = open("/proc/self/status").read()
        caps = {k: v.strip() for k, v in (
            line.split(":", 1) for line in status.splitlines() if line.startswith("Cap")
        )}
        out["no_new_privs"] = status.split("NoNewPrivs:")[1].split()[0]
        out["caps"] = caps

        def run(argv):
            try:
                done = subprocess.run(argv, capture_output=True, text=True, timeout=20,
                                      input="")
                return [done.returncode, (done.stdout + done.stderr)[-200:]]
            except OSError as exc:
                return ["missing", str(exc)]

        out["su"] = run(["su", "-c", "id -u", "root"])
        out["sudo"] = run(["sudo", "-n", "id", "-u"])
        out["mount"] = run(["mount", "-t", "tmpfs", "none", "/mnt"])
        for line in open("/proc/mounts"):
            fields = line.split()
            if fields[1] in ("/", "/tmp", "/job"):
                out["mount " + fields[1]] = fields[3].split(",")[:6]
        print("RESULT=" + json.dumps(out))
        """,
    )
    for refused in ("write /etc", "write /usr/local", "write /opt"):
        assert result[refused].startswith("refused"), result
    assert result["write /tmp"] == "ALLOWED" and result["write /job"] == "ALLOWED", result
    assert "ro" in result["mount /"], result
    # /tmp is scratch space only: nothing in it can be executed, and no setuid or device nodes.
    for flag in ("rw", "noexec", "nosuid", "nodev"):
        assert flag in result["mount /tmp"], result
    assert result["no_new_privs"] == "1", result
    assert int(result["caps"]["CapEff"], 16) == 0, result
    assert int(result["caps"]["CapBnd"], 16) == 0, result
    for attempt in ("su", "sudo", "mount"):
        status = result[attempt][0]
        assert status == "missing" or status != 0, result  # never a success
        assert "0" != result[attempt][1].strip(), result  # `id -u` of root would print 0


def test_cannot_see_repo_or_benchmarks(sandbox):
    result = probe(
        sandbox,
        """
        import json, os
        found = []
        skip = {"/proc", "/sys", "/dev", "/opt", "/usr", "/lib", "/lib64", "/bin", "/sbin",
                "/var", "/etc", "/run", "/boot"}
        for top, dirs, files in os.walk("/", topdown=True):
            if top in skip:
                dirs[:] = []
                continue
            for entry in dirs + files:
                if entry in ("benchmarks", "pyproject.toml", ".git", "CLAUDE.md", "docker.sock"):
                    found.append(os.path.join(top, entry))
        out = {
            "found": found,
            "root": sorted(os.listdir("/")),
            "work": sorted(os.listdir("/work")) if os.path.isdir("/work") else None,
            "docker_socket": os.path.exists("/var/run/docker.sock"),
            "job": sorted(os.listdir("/job")),
        }
        print("RESULT=" + json.dumps(out))
        """,
    )
    assert result["found"] == [], result
    assert not result["docker_socket"], result
    # Only the run directory is visible at /job, and the image's /work is an empty directory.
    assert result["job"] == [".sandbox", "probe.log", "probe.py"], result
    assert not result["work"], result


def test_timeout_leaves_no_container(sandbox):
    sandbox.script(
        "slow.py",
        "import pathlib, time\npathlib.Path('started').write_text('x')\ntime.sleep(300)\n",
    )
    response = sandbox.client.call("run_script", {"script": "slow.py"}, timeout_s=20)
    assert response.ok is False
    assert "timed out" in response.error
    # The script really ran inside the container, and the container is gone now.
    assert (sandbox.run_dir.root / "started").is_file()
    assert sandbox.containers() == ""


def test_pids_limit_is_enforced(make_sandbox):
    sandbox = make_sandbox(pids=40)
    result = probe(
        sandbox,
        """
        import json, os, time
        children = []
        failure = None
        for _ in range(500):
            try:
                pid = os.fork()
            except OSError as exc:
                failure = str(exc)
                break
            if pid == 0:
                time.sleep(30)
                os._exit(0)
            children.append(pid)
        import signal
        for pid in children:
            os.kill(pid, signal.SIGKILL)
        print("RESULT=" + json.dumps({"forked": len(children), "failure": failure}))
        """,
    )
    assert result["failure"] is not None, result
    assert 0 < result["forked"] < 40, result


def test_memory_limit_is_enforced(make_sandbox):
    sandbox = make_sandbox(memory="256m")
    sandbox.script(
        "hog.py",
        "chunks = []\nfor _ in range(64):\n    chunks.append(bytearray(64 * 1024 * 1024))\n"
        "    chunks[-1][::4096] = b'x' * len(chunks[-1][::4096])\nprint('SURVIVED')\n",
    )
    response = sandbox.run_script("hog.py")
    assert response.ok is False
    assert "SURVIVED" not in json.dumps(response.model_dump())


def test_mpi_environment_is_ready_for_cap_drop_all(sandbox):
    result = probe(
        sandbox,
        """
        import json, os
        keys = ("OMPI_MCA_btl_vader_single_copy_mechanism", "OMPI_MCA_rmaps_base_oversubscribe",
                "HOME", "TMPDIR")
        out = {k: os.environ.get(k) for k in keys}
        out["shm"] = [line.split()[1:4] for line in open("/proc/mounts") if " /dev/shm " in line]
        out["tmp_options"] = [line.split()[3] for line in open("/proc/mounts") if " /tmp " in line]
        print("RESULT=" + json.dumps(out))
        """,
    )
    # CMA (single-copy shared memory) needs ptrace, which --cap-drop ALL removes.
    assert result["OMPI_MCA_btl_vader_single_copy_mechanism"] == "none", result
    assert result["shm"], result


def test_runtime_limits_beyond_memory_and_pids(sandbox):
    result = probe(
        sandbox,
        """
        import json, resource
        limits = {
            name: open("/sys/fs/cgroup/" + name).read().strip()
            for name in ("memory.max", "memory.swap.max", "pids.max")
        }
        limits["core"] = list(resource.getrlimit(resource.RLIMIT_CORE))
        print("RESULT=" + json.dumps(limits))
        """,
    )
    assert result["memory.swap.max"] == "0", result  # --memory-swap equals --memory: no swap
    assert result["core"] == [0, 0], result  # --ulimit core=0
    assert result["pids.max"] == str(sandbox.config.pids), result


@pytest.mark.parametrize("module", ["json", "pydantic", "cfdagent"])
def test_planted_module_in_the_run_directory_is_never_imported(sandbox, module):
    """The executor starts in the hostile run directory; python -P keeps it off sys.path."""
    marker = "IMPORTED_MARKER"
    plant = f"open('/job/{marker}', 'w').close()\nraise SystemExit(7)\n"
    if module == "cfdagent":
        sandbox.run_dir.write_text("cfdagent/__init__.py", plant)
    else:
        sandbox.run_dir.write_text(f"{module}.py", plant)
    sandbox.run_dir.write_text("fine.py", "print('fine')\n")
    response = sandbox.run_script("fine.py")
    assert response.ok, response
    assert not (sandbox.run_dir.root / marker).exists()


def test_a_symlink_left_by_a_script_is_refused_on_the_next_call(sandbox):
    sandbox.script(
        "evil.py",
        "import os\nos.symlink('/etc', 'case')\nos.makedirs('d', exist_ok=True)\n",
    )
    first = sandbox.run_script("evil.py")
    link = sandbox.run_dir.root / "case"
    if not link.is_symlink():
        pytest.skip("the shared mount does not surface container-made symlinks on this host")
    assert first.ok, first
    second = sandbox.client.call("check_dictionaries", {"case_dir": "case"}, timeout_s=120)
    assert second.ok is False and "symbolic link" in second.error
    # The host refuses it too.
    from cfdagent.rundir import UnsafePath

    with pytest.raises(UnsafePath):
        sandbox.run_dir.read_text("case/hostname")


CODESTREAM = """\
FoamFile { version 2.0; format ascii; class dictionary; object controlDict; }
application icoFoam;
trap #codeStream
{
    codeInclude
    #{
        #include <fstream>
    #};
    code
    #{
        std::ofstream marker("/job/EXECUTED_MARKER");
        marker << "the code ran" << std::endl;
        os << 1;
    #};
};
"""


@pytest.mark.xfail(
    strict=True,
    reason="Task 8 sets allowSystemOperations 0 in the image; until then #codeStream is compiled",
)
def test_codestream_dictionary_is_refused(sandbox):
    sandbox.run_dir.write_text("case/system/controlDict", CODESTREAM)
    sandbox.run_dir.write_text("case/system/fvSchemes", "FoamFile { version 2.0; }\n")
    sandbox.run_dir.write_text("case/system/fvSolution", "FoamFile { version 2.0; }\n")
    (sandbox.run_dir.root / "case" / "0").mkdir()
    (sandbox.run_dir.root / "case" / "constant").mkdir()
    response = sandbox.client.call("check_dictionaries", {"case_dir": "case"}, timeout_s=300)
    assert response.ok is False, response
    assert not (sandbox.run_dir.root / "EXECUTED_MARKER").exists()
    # The refusal is OpenFOAM's own, and says why.
    assert "allowSystemOperations" in response.error or "#codeStream" in response.error
    # Refused up front: today OpenFOAM still compiles the block (wmake) and only the noexec
    # /tmp keeps the library from loading; with allowSystemOperations off nothing is compiled.
    assert "wmake" not in response.error, response.error


def test_full_pipeline_with_four_mpi_ranks_in_the_sandbox(sandbox):
    """Geometry, mesh, case checks and a 4-rank parallel solve, every step in its own container."""
    work = sandbox.run_dir.root / "w"
    work.mkdir()
    for name in ("geometry.py", "mesh.py"):
        shutil.copyfile(CAVITY / name, work / name)
    call = sandbox.client.call

    def step(op, args, timeout_s=600):
        response = call(op, args, timeout_s=outer_timeout(timeout_s))
        assert response.ok, f"{op}: {response.error}"
        return response

    step("run_script", {"script": "w/geometry.py", "cwd": "w", "kind": "geometry"})
    step("check_geometry", {"workdir": "w", "expected_bbox": CAVITY_BBOX})
    step("run_script", {"script": "w/mesh.py", "cwd": "w", "kind": "mesh"})
    step("convert_mesh", {"msh": "w/mesh.msh", "case_dir": "w/case"})
    step("check_mesh", {"case_dir": "w/case", "expected_bbox": CAVITY_BBOX})

    case = work / "case"
    shutil.copytree(CAVITY / "case", case, dirs_exist_ok=True)
    control = (case / "system" / "controlDict").read_text(encoding="utf-8")
    control = re.sub(r"(?m)^endTime\s+40;", "endTime 0.1;", control)
    control = re.sub(r"(?m)^writeInterval\s+1000;", "writeInterval 10;", control)
    (case / "system" / "controlDict").write_text(control, encoding="utf-8")

    checked = step("check_dictionaries", {"case_dir": "w/case"})
    assert checked.data["checked"] >= 5
    trial = step("trial_run", {"case_dir": "w/case", "solver": "icoFoam", "steps": 3})
    assert trial.data["completed"] is True
    solved = step(
        "run_solver", {"case_dir": "w/case", "solver": "icoFoam", "n_procs": 4, "timeout_s": 500}
    )
    assert solved.data["end_time"] == pytest.approx(0.1)
    assert solved.data["solver_log"] == "w/case/log.icoFoam"
    log = sandbox.run_dir.read_text("w/case/log.icoFoam", max_bytes=5_000_000)
    assert re.search(r"nProcs\s*:\s*4", log), "the solver did not run on 4 MPI ranks"
    assert sandbox.containers() == ""  # every container was removed
