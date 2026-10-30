"""The executor protocol: request/response files, argument validation and op dispatch.

Everything here runs in-process or in a plain local subprocess, with no Docker and no OpenFOAM.
Ops that need OpenFOAM or CAD are covered by the integration and sandbox suites.
"""

import json
import math
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

import cfdagent
from cfdagent.errors import AgentInputError, ToolFailure
from cfdagent.executor import __main__ as executor_main
from cfdagent.executor import ops, protocol
from cfdagent.executor.__main__ import main
from cfdagent.rundir import RunDir
from cfdagent.tools import foam, process

BRIEF_OPS = {
    "run_script",
    "check_geometry",
    "convert_mesh",
    "check_mesh",
    "check_dictionaries",
    "trial_run",
    "run_solver",
    "run_utility",
}


@pytest.fixture
def run(tmp_path):
    root = tmp_path / "job"
    root.mkdir()
    return RunDir(root)


def _call(run, op, args, *, name=None, request_op=None):
    """Write a request file, run the executor entry point in-process, return (exit, response)."""
    rel = f".sandbox/{name or 'c1'}.request.json"
    run.write_text(rel, json.dumps({"op": request_op or op, "args": args}))
    code = main([op, rel], root=run.root)
    response_rel = rel.replace(".request.json", ".response.json")
    response = None
    if (run.root / response_rel).exists():
        response = protocol.OpResponse.model_validate(run.read_json(response_rel))
    return code, response


def test_protocol_roundtrip_and_unknown_op(run):
    request = protocol.OpRequest(op="run_script", args={"script": "a.py"})
    assert protocol.OpRequest.model_validate_json(request.model_dump_json()) == request
    response = protocol.OpResponse(ok=True, data={"x": 1})
    assert response.error is None and response.internal is False
    assert protocol.OpResponse.model_validate_json(response.model_dump_json()) == response

    code, answer = _call(run, "frobnicate", {})
    assert code == 0
    assert answer.ok is False and answer.internal is False
    assert "frobnicate" in answer.error
    assert "run_script" in answer.error  # the message names the ops that exist


def test_protocol_models_forbid_extra_fields_and_bound_error_length():
    with pytest.raises(ValidationError):
        protocol.OpRequest.model_validate({"op": "x", "args": {}, "extra": 1})
    with pytest.raises(ValidationError):
        protocol.OpResponse.model_validate({"ok": True, "pwned": 1})
    long = protocol.OpResponse(ok=False, error="e" * 100_000)
    assert len(long.error) <= protocol.MAX_ERROR_CHARS + 100


def test_registry_has_the_brief_ops_and_no_knowledge_ops_yet():
    assert set(ops.OPS) == set(protocol.ARG_MODELS) == BRIEF_OPS
    assert not {"search_knowledge", "read_knowledge"} & set(ops.OPS)


def test_adding_an_op_is_one_decorator_and_one_model(run, monkeypatch):
    monkeypatch.setitem(protocol.ARG_MODELS, "echo", _EchoArgs)

    @ops.operation("echo")
    def echo(args, jail):
        return {"said": args.text, "root": jail.root.name}

    try:
        code, answer = _call(run, "echo", {"text": "hi"})
    finally:
        ops.OPS.pop("echo", None)
    assert code == 0 and answer.ok
    assert answer.data == {"said": "hi", "root": "job"}


class _EchoArgs(protocol.ArgsModel):
    text: str


def test_operation_decorator_refuses_an_op_without_an_argument_model():
    with pytest.raises(KeyError):

        @ops.operation("no_such_model")
        def nothing(args, jail):  # pragma: no cover
            return {}


@pytest.mark.parametrize(
    ("op", "args"),
    [
        ("run_script", {}),  # missing script
        ("run_script", {"script": "a.py", "bogus": 1}),  # extra field
        ("run_script", {"script": 7}),
        ("run_script", {"script": "/etc/passwd"}),
        ("run_script", {"script": "../a.py"}),
        ("run_script", {"script": "a.py", "cwd": "C:\\x"}),
        ("run_script", {"script": "a.py", "timeout_s": -1}),
        ("run_script", {"script": "a.py", "timeout_s": 1e9}),
        ("run_script", {"script": "a.py", "kind": "shell"}),
        ("check_geometry", {"workdir": ".", "expected_bbox": [0, 0, 1]}),
        ("check_geometry", {"workdir": ".", "expected_bbox": "0,0,0,1,1,1"}),
        ("convert_mesh", {"msh": "m.msh"}),
        ("check_mesh", {"case_dir": "../case", "expected_bbox": [0, 0, 0, 1, 1, 1]}),
        ("check_dictionaries", {"case_dir": "/"}),
        ("trial_run", {"case_dir": "case", "solver": "icoFoam", "steps": 0}),
        ("trial_run", {"case_dir": "case", "solver": "icoFoam", "steps": True}),
        ("run_solver", {"case_dir": "case", "solver": "icoFoam", "n_procs": 5}),
        ("run_solver", {"case_dir": "case", "solver": "icoFoam", "n_procs": 0}),
        ("run_utility", {"argv": []}),
        ("run_utility", {"argv": "postProcess"}),
        ("run_utility", {"argv": ["postProcess"] * 1000}),
    ],
)
def test_invalid_arguments_are_a_failed_response_not_a_crash(run, op, args):
    code, answer = _call(run, op, args)
    assert code == 0
    assert answer.ok is False and answer.internal is False
    assert len(answer.error) <= protocol.MAX_ERROR_CHARS + 100


def test_validate_args_is_usable_on_the_host_without_the_toolchain():
    model = protocol.validate_args("run_solver", {"case_dir": "case", "solver": "icoFoam"})
    assert (model.n_procs, model.timeout_s) == (1, 7200)
    with pytest.raises(protocol.UnknownOp):
        protocol.validate_args("nope", {})
    with pytest.raises(protocol.InvalidArgs):
        protocol.validate_args("run_solver", {"case_dir": "../x", "solver": "icoFoam"})


def test_solver_and_rank_limits_match_the_tool_layer():
    assert protocol.MAX_RANKS == foam.MAX_RANKS
    assert ops.UTILITIES <= process.ALLOWED_EXECUTABLES
    assert not ops.UTILITIES & {"python", "mpirun"}
    assert not ops.UTILITIES & foam.SOLVERS


def test_run_script_runs_python_in_the_directory_and_returns_the_tail(run):
    run.write_text("w/hello.py", "import os\nprint('hello from', os.path.basename(os.getcwd()))\n")
    code, answer = _call(run, "run_script", {"script": "w/hello.py", "cwd": "w"})
    assert code == 0 and answer.ok, answer
    assert "hello from w" in answer.data["tail"]
    assert answer.data["returncode"] == 0 and answer.data["timed_out"] is False
    assert answer.data["log"] == "w/hello.log"
    assert (run.root / "w" / "hello.log").is_file()


def test_run_script_failure_is_ok_false_with_the_traceback_tail(run):
    run.write_text("bad.py", "raise RuntimeError('boom marker')\n")
    code, answer = _call(run, "run_script", {"script": "bad.py"})
    assert code == 0
    assert answer.ok is False and answer.internal is False
    assert "boom marker" in answer.data["tail"]
    assert "boom marker" in answer.error


def test_run_script_timeout_kills_the_script(run):
    run.write_text("slow.py", "import time\ntime.sleep(60)\n")
    code, answer = _call(run, "run_script", {"script": "slow.py", "timeout_s": 1})
    assert code == 0
    assert answer.ok is False
    assert answer.data["timed_out"] is True
    assert "timed out" in answer.error


def test_run_script_does_not_leak_credentials_to_the_script(run, monkeypatch):
    monkeypatch.setenv("AZURE_CLIENT_SECRET", "s3cret-value")
    monkeypatch.setenv("CFDAGENT_LLM_API_KEY", "k3y-value")
    run.write_text("env.py", "import os\nprint(sorted(os.environ))\nprint(os.environ)\n")
    _, answer = _call(run, "run_script", {"script": "env.py"})
    assert answer.ok
    assert "s3cret-value" not in answer.data["tail"] and "k3y-value" not in answer.data["tail"]
    assert "AZURE_CLIENT_SECRET" not in answer.data["tail"]


def test_run_script_stdout_noise_cannot_corrupt_the_response(run):
    # The response travels in a file. Whatever the script prints, even a forged response, is only
    # log text.
    forged = json.dumps({"ok": True, "data": {"forged": True}, "error": None})
    run.write_text("noisy.py", f"print({forged!r})\nprint('}}{{ not json')\n")
    code, answer = _call(run, "run_script", {"script": "noisy.py"})
    assert code == 0 and answer.ok
    assert answer.data.get("forged") is None


def test_script_that_overwrites_the_response_file_does_not_win(run):
    # The executor writes the response after the op has finished, replacing whatever is there.
    rel = ".sandbox/c1.response.json"
    forged = json.dumps({"ok": True, "data": {"forged": True}, "error": None})
    script = f"""
        import pathlib
        pathlib.Path({str(run.root / rel)!r}).write_text({forged!r}, encoding="utf-8")
    """
    run.write_text("evil.py", textwrap.dedent(script))
    code, answer = _call(run, "run_script", {"script": "evil.py"})
    assert code == 0
    assert answer.data.get("forged") is None
    assert answer.data["returncode"] == 0


def test_path_escape_is_refused_inside_the_executor_too(run, tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("print('escaped')\n", encoding="utf-8")
    # Bypass the model's syntactic check by writing the request file directly: the executor
    # validates again.
    rel = ".sandbox/raw.request.json"
    run.write_text(rel, json.dumps({"op": "run_script", "args": {"script": "../outside.py"}}))
    assert main(["run_script", rel], root=run.root) == 0
    answer = protocol.OpResponse.model_validate(run.read_json(".sandbox/raw.response.json"))
    assert answer.ok is False and "escaped" not in json.dumps(answer.model_dump())


def test_symlinked_working_directory_is_refused(run, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.py").write_text("print('escaped')\n", encoding="utf-8")
    try:
        os.symlink(outside, run.root / "linked", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this platform or user cannot create symlinks")
    code, answer = _call(run, "run_script", {"script": "linked/x.py", "cwd": "linked"})
    assert code == 0
    assert answer.ok is False and answer.internal is False
    assert "symbolic link" in answer.error


def test_agent_input_error_is_a_gate_style_failure(run, monkeypatch):
    def refuse(*_a, **_k):
        raise AgentInputError("the manifest is wrong: fix it")

    monkeypatch.setattr(foam, "run_solver", refuse)
    code, answer = _call(run, "run_solver", {"case_dir": ".", "solver": "icoFoam"})
    assert code == 0
    assert answer.ok is False and answer.internal is False
    assert answer.error == "the manifest is wrong: fix it"


def test_other_exceptions_are_internal_errors(run, monkeypatch):
    def broken(*_a, **_k):
        raise ToolFailure("foamDictionary exploded")

    monkeypatch.setattr(foam, "run_solver", broken)
    code, answer = _call(run, "run_solver", {"case_dir": ".", "solver": "icoFoam"})
    assert code == 0
    assert answer.ok is False and answer.internal is True
    assert "foamDictionary exploded" in answer.error

    def buggy(*_a, **_k):
        raise KeyError("x" * 50_000)

    monkeypatch.setattr(foam, "run_solver", buggy)
    _, answer = _call(run, "run_solver", {"case_dir": ".", "solver": "icoFoam"}, name="c2")
    assert answer.internal is True
    assert len(answer.error) <= protocol.MAX_ERROR_CHARS + 100


def test_solver_run_is_returned_as_plain_data(run, monkeypatch, tmp_path):
    (run.root / "case").mkdir()
    log = run.root / "case" / "log.icoFoam"
    summary = foam.SolverLogSummary(
        completed=True,
        diverged=False,
        fatal_error=None,
        end_time=0.25,
        final_residuals={"Ux": 1e-6, "p": math.nan},
        max_courant=0.4,
    )
    tool = process.ToolResult(True, 0, False, log, "End\n")
    fake = foam.SolverRun(tool, log, summary, case_end_time=0.25, delta_t=0.005)
    monkeypatch.setattr(foam, "run_solver", lambda *a, **k: fake)
    code, answer = _call(run, "run_solver", {"case_dir": "case", "solver": "icoFoam"})
    assert code == 0 and answer.ok, answer
    data = answer.data
    assert data["end_time"] == 0.25 and data["case_end_time"] == 0.25
    assert data["solver_log"] == "case/log.icoFoam"
    assert data["final_residuals"]["Ux"] == 1e-6 and data["final_residuals"]["p"] is None
    assert data["tail"] == "End\n" and data["completed"] is True


def test_failed_solver_run_reports_ok_false_with_the_reason(run, monkeypatch):
    (run.root / "case").mkdir()
    log = run.root / "case" / "log.icoFoam"
    summary = foam.SolverLogSummary(False, False, "bad dictionary", None, {}, None)
    tool = process.ToolResult(False, 1, False, log, "FOAM FATAL ERROR\n")
    fake = foam.SolverRun(tool, log, summary, case_end_time=None)
    monkeypatch.setattr(foam, "run_solver", lambda *a, **k: fake)
    _, answer = _call(run, "run_solver", {"case_dir": "case", "solver": "icoFoam"})
    assert answer.ok is False and answer.internal is False
    assert "bad dictionary" in answer.error
    assert answer.data["completed"] is False


def test_solver_name_must_be_a_solver(run):
    _, answer = _call(run, "run_solver", {"case_dir": ".", "solver": "mpirun"})
    assert answer.ok is False and answer.internal is False
    assert "solver" in answer.error


@pytest.mark.parametrize(
    "argv",
    [
        ["python", "-c", "print(1)"],
        ["bash", "-c", "id"],
        ["mpirun", "-np", "1", "id"],
        ["icoFoam"],
        ["/usr/bin/foamListTimes"],
        ["sh"],
        ["postProcess", "-case", "/etc"],
        ["postProcess", "-case", "../.."],
        ["foamDictionary", "~/x"],
        ["postProcess", "-case", "a\\b"],
    ],
)
def test_run_utility_allows_only_openfoam_utilities_inside_the_job(run, argv):
    _, answer = _call(run, "run_utility", {"argv": argv})
    assert answer.ok is False and answer.internal is False
    assert "utility" in answer.error or "path" in answer.error.lower()


def test_run_utility_runs_nothing_when_the_utility_is_not_installed(run):
    # On a host without OpenFOAM an allowlisted utility is "not installed", not an internal error.
    if process.shutil.which("foamListTimes"):
        pytest.skip("OpenFOAM is installed here")
    _, answer = _call(run, "run_utility", {"argv": ["foamListTimes"]})
    assert answer.ok is False and answer.internal is False


def test_unreadable_request_leaves_a_failed_response(run):
    rel = ".sandbox/garbled.request.json"
    run.write_text(rel, "{not json")
    assert main(["run_script", rel], root=run.root) == 0
    answer = protocol.OpResponse.model_validate(run.read_json(".sandbox/garbled.response.json"))
    assert answer.ok is False and answer.internal is False


def test_request_for_another_op_is_refused(run):
    code, answer = _call(run, "run_script", {"script": "a.py"}, request_op="run_solver")
    assert code == 0
    assert answer.ok is False and "do not match" in answer.error


@pytest.mark.parametrize(
    "rel",
    ["x.request.json", "../x.request.json", ".sandbox/x.json", ".sandbox/../x.request.json"],
)
def test_request_must_be_a_sandbox_request_file(run, rel):
    assert main(["run_script", rel], root=run.root) == 2
    assert not list(run.root.rglob("*.response.json"))


def test_response_is_written_atomically_and_size_bounded(run, monkeypatch):
    big = {"blob": "z" * (2 * protocol.RESPONSE_MAX_BYTES)}

    monkeypatch.setitem(protocol.ARG_MODELS, "big", protocol.ArgsModel)

    @ops.operation("big")
    def big_op(args, jail):
        return big

    try:
        code, answer = _call(run, "big", {})
    finally:
        ops.OPS.pop("big", None)
    assert code == 0
    assert answer.ok is False and answer.internal is True
    assert "too large" in answer.error
    assert [p.name for p in (run.root / ".sandbox").iterdir() if ".tmp" in p.name] == []


# --------------------------------------------------------------------------------------------
# Fix round 1
# --------------------------------------------------------------------------------------------


def test_numpy_scalars_in_a_result_become_plain_numbers(run, monkeypatch):
    np = pytest.importorskip("numpy")
    monkeypatch.setitem(protocol.ARG_MODELS, "numbers", protocol.ArgsModel)

    @ops.operation("numbers")
    def numbers(args, jail):
        return {
            "f": np.float64(1.5),
            "i": np.int64(3),
            "b": np.bool_(True),
            "nan": np.float64("nan"),
        }

    try:
        code, answer = _call(run, "numbers", {})
    finally:
        ops.OPS.pop("numbers", None)
    assert code == 0 and answer.ok, answer
    assert answer.data == {"f": 1.5, "i": 3, "b": True, "nan": None}


def test_result_that_is_not_json_safe_still_gets_a_response(run, monkeypatch):
    monkeypatch.setitem(protocol.ARG_MODELS, "odd", protocol.ArgsModel)

    @ops.operation("odd")
    def odd(args, jail):
        return {"thing": object()}

    try:
        code, answer = _call(run, "odd", {})
    finally:
        ops.OPS.pop("odd", None)
    assert code == 0  # a response was written
    assert answer.ok is False and answer.internal is True
    assert "serial" in answer.error.lower()


@pytest.mark.parametrize(
    ("op", "args"),
    [
        ("check_geometry", {"workdir": ".", "expected_bbox": [0, 0, 0, 1, 1, math.nan]}),
        ("check_geometry", {"workdir": ".", "expected_bbox": [0, 0, 0, math.inf, 1, 1]}),
        ("check_geometry", {"workdir": ".", "expected_bbox": [0] * 6, "rel_tol": 0}),
        ("check_geometry", {"workdir": ".", "expected_bbox": [0] * 6, "rel_tol": -1e-3}),
        ("check_geometry", {"workdir": ".", "expected_bbox": [0] * 6, "rel_tol": math.nan}),
        ("check_geometry", {"workdir": ".", "expected_bbox": [0] * 6, "expected_volume": math.nan}),
        ("check_geometry", {"workdir": ".", "expected_bbox": [0] * 6, "expected_volume": -1}),
        ("check_mesh", {"case_dir": "c", "expected_bbox": [0] * 6, "rel_tol": 0}),
        ("check_mesh", {"case_dir": "c", "expected_bbox": [0] * 6, "rel_tol": math.inf}),
        ("run_solver", {"case_dir": "c", "solver": "icoFoam", "timeout_s": math.nan}),
        ("run_script", {"script": "a.py", "timeout_s": math.inf}),
    ],
)
def test_non_finite_and_non_positive_numbers_are_rejected_in_the_arguments(op, args):
    with pytest.raises(protocol.InvalidArgs):
        protocol.validate_args(op, args)


def test_response_ok_must_be_a_real_boolean():
    for bad in ("yes", 1, 0, None, "true"):
        with pytest.raises(ValidationError):
            protocol.OpResponse.model_validate({"ok": bad})
    assert protocol.OpResponse.model_validate({"ok": True}).ok is True


def test_stdout_is_redirected_to_stderr_while_ops_run():
    code = (
        "import os, sys\n"
        "from cfdagent.executor.__main__ import _stdout_to_stderr\n"
        "_stdout_to_stderr()\n"
        "print('python-level')\n"
        "os.write(1, b'fd-level')\n"
        "sys.stderr.flush()\n"
    )
    source_root = str(Path(cfdagent.__file__).resolve().parents[1])
    env = {**os.environ, "PYTHONPATH": source_root}
    done = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, env=env
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == ""
    assert "python-level" in done.stderr and "fd-level" in done.stderr


_REFUSAL = (
    "--> FOAM FATAL IO ERROR: (openfoam-2512)\n"
    "Loading shared libraries using case-supplied code may have been disabled\n"
    "by default for security reasons.\n"
)


_BANNER = "allowSystemOperations : Disallowing user-supplied system call operations\n"
_MISSING = 'cannot find file "0/p"'


def test_code_refusal_hint_is_general_and_only_added_for_the_refusal():
    assert process.code_refusal_hint("nothing special") is None
    hint = process.code_refusal_hint(_REFUSAL)
    assert hint is not None
    assert "#eval" in hint and "plain numbers" in hint and "exprFixedValue" in hint
    # The generality rule: the hint names no benchmark case.
    for word in ("cavity", "pipe", "cylinder"):
        assert word not in hint.lower()
    assert process.code_refusal_hint("Executing user-supplied system calls is disabled") == hint


def test_the_solver_banner_is_not_a_refusal():
    """Every solver log prints the switch (here: off); that alone must never trigger the hint."""
    tail = f"Exec   : icoFoam\n{_BANNER}\n--> FOAM FATAL IO ERROR:\n{_MISSING}\n"
    assert process.code_refusal_hint(tail) is None
    assert process.code_refusal_hint("allowSystemOperations 1;") is None


def test_refused_case_code_gets_the_hint_once(run, monkeypatch):
    def refuse(*_a, **_k):
        raise AgentInputError(f"cannot read 'startTime':\n{_REFUSAL}")

    monkeypatch.setattr(foam, "trial_run", refuse)
    _, answer = _call(run, "trial_run", {"case_dir": ".", "solver": "icoFoam"})
    assert answer.ok is False and answer.internal is False
    assert "case-supplied code" in answer.error
    assert answer.error.count(process.CODE_REFUSAL_HINT) == 1
    # An error that has nothing to do with it gets no hint.
    monkeypatch.setattr(
        foam,
        "trial_run",
        lambda *a, **k: (_ for _ in ()).throw(AgentInputError("deltaT must be positive")),
    )
    _, other = _call(run, "trial_run", {"case_dir": ".", "solver": "icoFoam"}, name="c2")
    assert other.error == "deltaT must be positive"


def test_check_dictionaries_reads_files_against_the_case_from_a_scratch_directory(run, monkeypatch):
    for name in ("0/U", "constant/transportProperties", "system/controlDict"):
        run.write_text(f"case/{name}", "FoamFile { version 2.0; }\n")
    calls = []

    def fake_run_tool(argv, cwd, log_path, timeout_s=600.0):
        calls.append((list(argv), Path(cwd)))
        log_path.write_text("", encoding="utf-8")
        return process.ToolResult(True, 0, False, log_path, "")

    monkeypatch.setattr(ops, "run_tool", fake_run_tool)
    _, answer = _call(run, "check_dictionaries", {"case_dir": "case"})
    assert answer.ok, answer
    case = (run.root / "case").resolve()
    assert len(calls) == 3
    for argv, cwd in calls:
        assert argv[:3] == ["foamDictionary", "-case", str(case)]
        assert Path(argv[3]).is_absolute() and Path(argv[3]).parent.parent == case
        assert len(argv) == 4
        assert case not in cwd.parents and cwd != case  # the scratch directory, not the case


def test_a_failure_that_only_shows_the_banner_gets_no_hint(run, monkeypatch):
    message = _BANNER + _MISSING

    def fail(*_a, **_k):
        raise AgentInputError(message)

    monkeypatch.setattr(foam, "trial_run", fail)
    _, answer = _call(run, "trial_run", {"case_dir": ".", "solver": "icoFoam"})
    assert answer.error == message


def test_home_must_be_set_and_outside_the_job_directory(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    problem = executor_main.home_problem
    assert problem(str(tmp_path / "home"), job) is None
    assert "HOME" in problem(None, job)
    assert "HOME" in problem("", job)
    assert "HOME" in problem(str(job), job)
    assert "HOME" in problem(str(job / ".hidden" / "home"), job)
    assert "HOME" in problem(str(job / ".." / "job" / "x"), job)  # dots are resolved first
    assert problem(str(tmp_path / "job2"), job) is None  # same prefix, different directory


def test_the_executor_refuses_to_start_with_a_home_in_the_job_directory(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    source = str(Path(cfdagent.__file__).parents[1])
    env = {**os.environ, "HOME": str(job / "home"), "PYTHONPATH": source}
    done = subprocess.run(
        [sys.executable, "-P", "-m", "cfdagent.executor", "run_script", ".sandbox/c1.request.json"],
        cwd=job,
        env=env,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 2
    assert "HOME" in done.stderr
