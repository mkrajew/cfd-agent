"""The ops an executor container can run, and the dispatcher that runs one.

Each op takes the validated argument model of ``executor.protocol`` and the ``RunDir`` of the
job directory, and returns a plain ``dict``. Two reserved keys steer the response: ``ok`` (default
True) and ``error``; everything else is the op's data. Every path argument is run-directory
relative and is validated again here (no absolute path, no ``..``, no symbolic link in any
component, see ``rundir.RunDir.path``): an earlier call may have run agent-written code that left
links behind. An op never touches anything outside the job directory.

Failure mapping (``execute``): ``AgentInputError`` (the agent's content is wrong) becomes a
gate-style failure, ``ok=False``; any other exception is a tool or environment failure and
becomes ``ok=False, internal=True``.

Adding an op: a model with ``@op_args("name")`` in ``executor.protocol`` and a function with
``@operation("name")`` here.
"""

from __future__ import annotations

import dataclasses
import math
import os
import sys
import tempfile
import time
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cfdagent.errors import AgentInputError
from cfdagent.executor import protocol
from cfdagent.executor.protocol import OpRequest, OpResponse
from cfdagent.gates import GateResult
from cfdagent.manifest import MANIFEST_FILE_NAME, BoundaryManifest
from cfdagent.rundir import RunDir, split_relative
from cfdagent.tools import foam as foam_tools
from cfdagent.tools import geometry as geometry_tools
from cfdagent.tools import mesh as mesh_tools
from cfdagent.tools.process import ALLOWED_EXECUTABLES, ToolResult, run_tool

OPS: dict[str, Callable[[Any, RunDir], dict[str, Any]]] = {}

# OpenFOAM utilities an agent may run through ``run_utility``. The solvers have their own op (with
# limits), ``python`` has ``run_script`` and ``mpirun`` is only used by ``run_solver``.
UTILITIES: frozenset[str] = frozenset(
    {"postProcess", "foamListTimes", "checkMesh", "foamDictionary", "changeDictionary"}
)
assert UTILITIES <= ALLOWED_EXECUTABLES

_UTILITY_TIMEOUT_S = 600.0
_DICTIONARY_FILE_TIMEOUT_S = 60.0
_DICTIONARY_TOTAL_S = 300.0
_DICTIONARY_MAX_FILES = 200
_DICTIONARY_DIRS = ("0", "constant", "system")
_MAX_ERROR_LINE = 2000


def operation(name: str):
    """Decorator: register the function as the implementation of op ``name``.

    The op's argument model must already be registered in ``protocol.ARG_MODELS``.
    """
    if name not in protocol.ARG_MODELS:
        raise KeyError(f"op {name!r} has no argument model; register it with @op_args first")

    def register(function: Callable[[Any, RunDir], dict[str, Any]]):
        OPS[name] = function
        return function

    return register


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------


def _join(rel: str, name: str) -> str:
    parts = [*split_relative(rel), name]
    return "/".join(parts)


def _directory(jail: RunDir, rel: str) -> Path:
    path = jail.path(rel)
    if not path.is_dir():
        raise AgentInputError(f"{rel!r} is not a directory in the run directory")
    return path


def _file(jail: RunDir, rel: str) -> Path:
    path = jail.path(rel)
    if not path.is_file():
        raise AgentInputError(f"{rel!r} does not exist (or is not a regular file)")
    return path


def _no_links(jail: RunDir, directory_rel: str, names: tuple[str, ...]) -> None:
    """Refuse symbolic links among files and folders that a tool is about to read or write."""
    for name in names:
        jail.path(_join(directory_rel, name))


def _plain(value: Any) -> Any:
    """JSON-safe copy: dataclasses and tuples become dicts and lists, NaN and inf become None."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        value = dataclasses.asdict(value)
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Path):
        return str(value)
    return value


def _clip(text: str, limit: int = _MAX_ERROR_LINE) -> str:
    return text if len(text) <= limit else text[:limit] + " [...]"


def _gate(gate: GateResult) -> dict[str, Any]:
    return {
        "ok": gate.ok,
        "errors": [_clip(str(e)) for e in gate.errors[:50]],
        "report": _plain(gate.data),
        "error": None if gate.ok else "\n".join(_clip(str(e)) for e in gate.errors[:50]),
    }


def _tool_problem(result: ToolResult, label: str, timeout_s: float | None = None) -> str | None:
    if result.ok:
        return None
    if result.timed_out:
        limit = f" after {timeout_s:g} s" if timeout_s is not None else ""
        return f"{label} timed out{limit} and was killed\n{result.tail}"
    code = "did not start" if result.returncode is None else f"exited with code {result.returncode}"
    return f"{label} {code}\n{result.tail}"


def _tool(jail: RunDir, result: ToolResult, label: str, timeout_s: float | None = None) -> dict:
    return {
        "ok": result.ok,
        "returncode": result.returncode,
        "timed_out": result.timed_out,
        "tail": result.tail,
        "log": jail.relative(result.log_path),
        "error": _tool_problem(result, label, timeout_s),
    }


def _manifest(jail: RunDir, explicit: str | None, case_dir_rel: str) -> BoundaryManifest:
    """Load the boundary manifest: the explicit path, else the one beside ``case_dir``."""
    if explicit is None:
        parents = split_relative(case_dir_rel)[:-1]
        explicit = "/".join([*parents, MANIFEST_FILE_NAME])
    path = _file(jail, explicit)
    jail.read_text(explicit, max_bytes=200_000)  # size limit and regular-file check
    return BoundaryManifest.from_file(path)


def _check_solver(solver: str) -> None:
    if solver not in foam_tools.SOLVERS:
        raise AgentInputError(
            f"{solver!r} is not a solver; the solvers are {sorted(foam_tools.SOLVERS)}"
        )


def _solver_data(jail: RunDir, run: foam_tools.SolverRun, *, logs: bool) -> dict[str, Any]:
    summary = run.summary
    tool = run.tool
    reason = None
    if not run.ok:
        reason = (
            _tool_problem(tool, "the solver step")
            or summary.failure_reason
            or foam_tools.reach_problem(summary, run.case_end_time, run.delta_t, run.time_precision)
        )
        if summary.failure_reason and reason != summary.failure_reason:
            reason = f"{summary.failure_reason}\n{reason}"
    data: dict[str, Any] = {
        "ok": run.ok,
        "error": reason,
        "returncode": tool.returncode,
        "timed_out": tool.timed_out,
        "tail": tool.tail,
        "completed": summary.completed,
        "diverged": summary.diverged,
        "fatal_error": summary.fatal_error,
        "end_time": summary.end_time,
        "first_time": summary.first_time,
        "last_step": summary.last_step,
        "final_residuals": summary.final_residuals,
        "max_courant": summary.max_courant,
        "converged_at": summary.converged_at,
        "case_end_time": run.case_end_time,
        "delta_t": run.delta_t,
    }
    if logs:  # the trial run's scratch case is gone; only a real run has logs to point to
        data["solver_log"] = jail.relative(run.solver_log)
        data["tool_log"] = jail.relative(tool.log_path)
    return _plain(data)


# --------------------------------------------------------------------------------------------
# Ops
# --------------------------------------------------------------------------------------------


@operation("run_script")
def run_script(args: protocol.RunScriptArgs, jail: RunDir) -> dict[str, Any]:
    """Run ``python <script>`` with ``cwd``; the output goes to a log file in ``cwd``.

    ``kind="geometry"`` or ``"mesh"`` applies that stage's output checks (stale outputs are
    removed first; the expected files must exist, the mesh in MSH 2.2 ASCII). Without ``kind`` the
    script is simply run (post-processing and analysis scripts); its log is ``<script>.log``.
    """
    script = _file(jail, args.script)
    cwd = _directory(jail, args.cwd)
    if args.kind == "geometry":
        _no_links(jail, args.cwd, (geometry_tools.STEP_FILE_NAME, geometry_tools.GEOMETRY_LOG_NAME))
        result = geometry_tools.run_geometry_script(script, cwd, args.timeout_s)
    elif args.kind == "mesh":
        _no_links(jail, args.cwd, (mesh_tools.MESH_FILE_NAME, mesh_tools.MESH_LOG_NAME))
        result = mesh_tools.run_mesh_script(script, cwd, args.timeout_s)
    else:
        log_name = f"{script.stem}.log"
        _no_links(jail, args.cwd, (log_name,))
        result = run_tool(["python", str(script)], cwd, cwd / log_name, args.timeout_s)
    return _tool(jail, result, f"the script {args.script!r}", args.timeout_s)


@operation("check_geometry")
def check_geometry(args: protocol.CheckGeometryArgs, jail: RunDir) -> dict[str, Any]:
    workdir = _directory(jail, args.workdir)
    _no_links(jail, args.workdir, (geometry_tools.STEP_FILE_NAME, MANIFEST_FILE_NAME))
    gate = geometry_tools.check_geometry(
        workdir, tuple(args.expected_bbox), args.expected_volume, args.rel_tol
    )
    return _gate(gate)


@operation("convert_mesh")
def convert_mesh(args: protocol.ConvertMeshArgs, jail: RunDir) -> dict[str, Any]:
    """``gmshToFoam`` into ``case_dir`` (which must hold no mesh yet), then set the patch types."""
    msh = _file(jail, args.msh)
    case_dir = jail.path(args.case_dir)
    manifest = _manifest(jail, args.manifest, args.case_dir)
    _no_links(
        jail,
        args.case_dir,
        ("constant", "constant/polyMesh", mesh_tools.CONVERT_LOG_NAME, mesh_tools.DICT_LOG_NAME),
    )
    result = mesh_tools.convert_to_foam(msh, case_dir)
    if not result.ok:
        return _tool(jail, result, "gmshToFoam")
    mesh_tools.apply_patch_types(case_dir, manifest)
    return _tool(jail, result, "gmshToFoam")


@operation("check_mesh")
def check_mesh(args: protocol.CheckMeshArgs, jail: RunDir) -> dict[str, Any]:
    case_dir = _directory(jail, args.case_dir)
    manifest = _manifest(jail, args.manifest, args.case_dir)
    _no_links(
        jail,
        args.case_dir,
        ("constant", "constant/polyMesh", mesh_tools.CHECKMESH_LOG_NAME),
    )
    gate = mesh_tools.check_mesh(case_dir, manifest, tuple(args.expected_bbox), args.rel_tol)
    return _gate(gate)


@operation("check_dictionaries")
def check_dictionaries(args: protocol.CheckDictionariesArgs, jail: RunDir) -> dict[str, Any]:
    """Parse every file directly inside ``0/``, ``constant/`` and ``system/`` with foamDictionary.

    A file that does not parse is reported with the start of foamDictionary's message. The
    dictionaries are read by OpenFOAM, which can execute code embedded in them (``#codeStream``,
    ``#calc``); the image must have ``allowSystemOperations`` off and this runs only in the
    sandbox. foamDictionary runs in a scratch directory, so nothing is written to the case.
    """
    _directory(jail, args.case_dir)
    errors: list[str] = []
    files: list[tuple[str, Path]] = []
    for folder in _DICTIONARY_DIRS:
        folder_rel = _join(args.case_dir, folder)
        folder_path = jail.path(folder_rel)
        if not folder_path.is_dir():
            errors.append(f"the case has no {folder}/ directory")
            continue
        for entry in sorted(os.scandir(folder_path), key=lambda e: e.name):
            if entry.name.startswith("."):
                continue
            entry_rel = f"{folder_rel}/{entry.name}"
            try:
                path = jail.path(entry_rel)
            except AgentInputError as exc:
                errors.append(_clip(str(exc)))
                continue
            if path.is_file():
                files.append((f"{folder}/{entry.name}", path))
    if len(files) > _DICTIONARY_MAX_FILES:
        errors.append(
            f"the case has {len(files)} files in 0/, constant/ and system/; "
            f"at most {_DICTIONARY_MAX_FILES} are checked"
        )
        files = files[:_DICTIONARY_MAX_FILES]

    deadline = time.monotonic() + _DICTIONARY_TOTAL_S
    with tempfile.TemporaryDirectory(prefix="cfdagent-dicts-") as tmp:
        scratch = Path(tmp)
        for index, (label, path) in enumerate(files):
            if time.monotonic() > deadline:
                errors.append(f"time limit reached; {len(files) - index} file(s) not checked")
                break
            result = run_tool(
                ["foamDictionary", str(path)],
                cwd=scratch,
                log_path=scratch / f"{index}.log",
                timeout_s=_DICTIONARY_FILE_TIMEOUT_S,
            )
            if not result.ok:
                reason = "timed out" if result.timed_out else "cannot be parsed"
                errors.append(_clip(f"{label} {reason}:\n{result.tail}", 3000))
    return {
        "ok": not errors,
        "errors": errors,
        "checked": len(files),
        "error": "\n".join(errors) if errors else None,
    }


@operation("trial_run")
def trial_run(args: protocol.TrialRunArgs, jail: RunDir) -> dict[str, Any]:
    case_dir = _directory(jail, args.case_dir)
    _check_solver(args.solver)
    run = foam_tools.trial_run(case_dir, args.solver, args.steps)
    return _solver_data(jail, run, logs=False)


@operation("run_solver")
def run_solver(args: protocol.RunSolverArgs, jail: RunDir) -> dict[str, Any]:
    case_dir = _directory(jail, args.case_dir)
    _check_solver(args.solver)
    _no_links(jail, args.case_dir, (f"log.{args.solver}", "log.decomposePar", "log.reconstructPar"))
    run = foam_tools.run_solver(case_dir, args.solver, args.n_procs, args.timeout_s)
    return _solver_data(jail, run, logs=True)


def _check_utility_argument(jail: RunDir, cwd_rel: str, cwd: Path, argument: str) -> None:
    if "\x00" in argument or "\\" in argument or argument.startswith(("/", "~")):
        raise AgentInputError(
            f"argument {_clip(argument, 80)!r} is not allowed: paths must be relative to the "
            "working directory, with '/' separators, and stay inside the run directory"
        )
    if ".." in argument.split("/"):
        raise AgentInputError(
            f"argument {_clip(argument, 80)!r} is not allowed: a path must stay inside the run "
            "directory (no '..')"
        )
    if "/" in argument or os.path.lexists(cwd / argument):
        jail.path(_join(cwd_rel, argument))  # no symbolic link in a path the utility will open


@operation("run_utility")
def run_utility(args: protocol.RunUtilityArgs, jail: RunDir) -> dict[str, Any]:
    """Run an allowlisted OpenFOAM utility with ``cwd``; the log is ``log.<utility>`` there."""
    name = args.argv[0]
    if name not in UTILITIES:
        raise AgentInputError(
            f"{_clip(name, 80)!r} is not an allowed OpenFOAM utility; allowed: {sorted(UTILITIES)}"
        )
    cwd = _directory(jail, args.cwd)
    for argument in args.argv[1:]:
        _check_utility_argument(jail, args.cwd, cwd, argument)
    log_name = f"log.{name}"
    _no_links(jail, args.cwd, (log_name,))
    result = run_tool(list(args.argv), cwd, cwd / log_name, _UTILITY_TIMEOUT_S)
    return _tool(jail, result, name, _UTILITY_TIMEOUT_S)


# --------------------------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------------------------


def execute(request: OpRequest, jail: RunDir) -> OpResponse:
    """Run the op named in ``request``; never raises (except for ``KeyboardInterrupt``)."""
    try:
        model = protocol.validate_args(request.op, request.args)
    except (protocol.UnknownOp, protocol.InvalidArgs) as exc:
        return protocol.failure(str(exc))
    function = OPS.get(request.op)
    if function is None:
        return protocol.failure(f"op {request.op!r} has no implementation in this executor")
    try:
        out = dict(function(model, jail))
    except AgentInputError as exc:
        return protocol.failure(str(exc))
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        return protocol.failure(f"{type(exc).__name__}: {exc}", internal=True)
    ok = bool(out.pop("ok", True))
    error = out.pop("error", None)
    if not ok and not error:
        error = f"{request.op} failed"
    return OpResponse(ok=ok, data=_plain(out), error=error if not ok else None)
