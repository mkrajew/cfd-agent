"""Executor ops against the real OpenFOAM utilities and solvers (inside the Docker image).

The client runs in ``local`` mode: the executor is a plain subprocess of the test, which already
runs inside the container. Confinement itself is covered by ``tests/sandbox``.
"""

import os
import shutil

import pytest
from foam_cases import build_case, needs_4_threads

from cfdagent.rundir import RunDir
from cfdagent.sandbox.client import SandboxClient
from cfdagent.sandbox.config import SandboxConfig

pytestmark = pytest.mark.integration


@pytest.fixture
def job(tmp_path):
    root = tmp_path / "job"
    root.mkdir()
    run_dir = RunDir(root)
    config = SandboxConfig(mode="local", allow_local=True, image="local-unused")
    client = SandboxClient(config, run_dir, "20260101-000000-ops")
    build_case(root, end_time=0.25)
    return client, run_dir


def test_check_dictionaries_accepts_a_good_case(job):
    client, _ = job
    response = client.call("check_dictionaries", {"case_dir": "case"}, timeout_s=300)
    assert response.ok, response.error
    assert response.data["checked"] >= 6


def test_check_dictionaries_names_the_file_that_does_not_parse(job):
    client, run_dir = job
    run_dir.write_text("case/system/fvSchemes", "FoamFile { version 2.0;\n")  # unbalanced brace
    response = client.call("check_dictionaries", {"case_dir": "case"}, timeout_s=300)
    assert response.ok is False and response.internal is False
    assert "system/fvSchemes" in response.error


def test_run_utility_runs_an_allowed_utility_and_refuses_others(job):
    client, run_dir = job
    listed = client.call("run_utility", {"argv": ["foamListTimes"], "cwd": "case"}, timeout_s=120)
    assert listed.ok, listed.error
    assert (run_dir.root / "case" / "log.foamListTimes").is_file()
    refused = client.call("run_utility", {"argv": ["python", "-c", "1"]}, timeout_s=120)
    assert refused.ok is False and refused.internal is False


def test_trial_run_and_serial_solve_through_the_executor(job):
    client, run_dir = job
    trial = client.call("trial_run", {"case_dir": "case", "solver": "icoFoam"}, timeout_s=300)
    assert trial.ok, trial.error
    assert trial.data["completed"] is True
    assert not (run_dir.root / "case" / "0.015").exists()  # the trial ran in a scratch copy
    solved = client.call("run_solver", {"case_dir": "case", "solver": "icoFoam"}, timeout_s=300)
    assert solved.ok, solved.error
    assert solved.data["end_time"] == pytest.approx(0.25)
    assert solved.data["solver_log"] == "case/log.icoFoam"


def test_solver_failure_is_a_gate_style_failure_not_an_internal_error(job):
    client, run_dir = job
    (run_dir.root / "case" / "system" / "fvSolution").unlink()
    response = client.call("run_solver", {"case_dir": "case", "solver": "icoFoam"}, timeout_s=300)
    assert response.ok is False and response.internal is False
    assert response.data["completed"] is False


@needs_4_threads
def test_parallel_solve_through_the_executor(job):
    client, _ = job
    solved = client.call(
        "run_solver", {"case_dir": "case", "solver": "icoFoam", "n_procs": 4}, timeout_s=300
    )
    assert solved.ok, solved.error
    assert solved.data["end_time"] == pytest.approx(0.25)


def test_unknown_case_directory_is_a_failed_op(job):
    client, run_dir = job
    shutil.rmtree(run_dir.root / "case")
    response = client.call("check_dictionaries", {"case_dir": "case"}, timeout_s=120)
    assert response.ok is False and response.internal is False


def test_check_dictionaries_resolves_system_includes_and_case_local_functions(job):
    """Includes and function objects are resolved against the case, not the scratch directory."""
    client, run_dir = job
    run_dir.write_text("case/system/shared", "FoamFile { version 2.0; }\nshared 42;\n")
    run_dir.write_text("case/system/myFunc", "FoamFile { version 2.0; }\ntype probes;\n")
    control = run_dir.read_text("case/system/controlDict")
    run_dir.write_text(
        "case/system/controlDict",
        control + '#include "<system>/shared"\nfunctions { #includeFunc myFunc #includeFunc '
        'solverInfo }\nvalue #eval "1 + 2";\n',
    )
    response = client.call("check_dictionaries", {"case_dir": "case"}, timeout_s=300)
    assert response.ok, response.error


MARKER = "EXECUTED_MARKER"
CODE_STREAM = (
    "trap #codeStream\n{\n    codeInclude #{ #include <fstream> #};\n"
    f'    code #{{ std::ofstream("/tmp/{MARKER}") << "ran"; os << 1; #}};\n}};\n'
)
HOSTILE_CASES = {
    # file to change, text to append, ops that must refuse it
    "codestream in controlDict": (
        "system/controlDict",
        CODE_STREAM,
        ("check_dictionaries", "trial_run", "run_solver"),
    ),
    "calc in a 0/ file": (
        "0/p",
        f'trap #calc "system(\\"touch /tmp/{MARKER}\\")";\n',
        ("check_dictionaries", "trial_run", "run_solver"),
    ),
    "coded patch type": (
        "0/p",
        "boundaryField { movingWall { type codedFixedValue; name probe; "
        f'code #{{ std::ofstream("/tmp/{MARKER}") << "ran"; #}}; }} }}\n',
        ("trial_run", "run_solver"),
    ),
    "info switch set in the case": (
        "system/controlDict",
        "InfoSwitches { allowSystemOperations 1; }\n" + CODE_STREAM,
        ("check_dictionaries", "trial_run", "run_solver"),
    ),
}
HOSTILE_PARAMS = [
    pytest.param(name, op, id=f"{name}-{op}")
    for name, (_, _, ops) in HOSTILE_CASES.items()
    for op in ops
]


@pytest.mark.parametrize(("name", "op"), HOSTILE_PARAMS)
def test_case_supplied_code_is_refused_with_a_general_hint(job, name, op):
    client, run_dir = job
    target, text, _ = HOSTILE_CASES[name]
    path = f"case/{target}"
    run_dir.write_text(path, run_dir.read_text(path) + text)
    args = (
        {"case_dir": "case"}
        if op == "check_dictionaries"
        else {
            "case_dir": "case",
            "solver": "icoFoam",
        }
    )
    response = client.call(op, args, timeout_s=300)
    assert response.ok is False and response.internal is False, response
    assert "case-supplied code" in response.error, response.error
    assert "plain numbers" in response.error and "#eval" in response.error, response.error
    assert not os.path.exists(f"/tmp/{MARKER}")
    assert not list(run_dir.root.rglob("dynamicCode")), "OpenFOAM prepared code for compiling"
    assert not list(run_dir.root.rglob(MARKER))
