"""The command line: ``cfdagent run``, ``status``, ``logs`` and ``list``.

The command line is a thin front end of ``cfdagent.orchestrator.run_task``. It reads only the
files the orchestrator writes into a run directory (``status.json``, ``run_record.json``,
``run.log``) and writes nothing into a run directory itself (blind evaluation: the benchmark id
and the task set never reach a file an agent can read).

Environment: ``CFDAGENT_RUNS_DIR`` is the directory that holds the run directories (default
``~/runs``). The model endpoint and the storage account are read by the orchestrator (see its
module docstring).

``run`` exits with 0 (completed), 1 (failed), 130 (interrupted) or 2 (usage error: no message
about the run is printed then, only what was wrong, and no traceback).

``run --detach`` computes the run id first (``orchestrator.new_run_id``), starts
``python -m cfdagent run ... --run-id <id>`` in a new session, prints the id and returns. The
child's own output goes to a launcher log ``<runs>/.launch-<id>.log`` next to, not inside, the
run directory: the orchestrator refuses an existing run directory and the sandbox mounts only
the run directory. ``status`` and ``logs`` fall back to the launcher log until the run directory
exists.

Retention. After a run that was asked to upload (also in the detached child), only the newest 10
run directories stay on the disk. An older one is deleted only when ``run.log`` says the upload
was complete (``upload: done``) and the run is not live; a run whose upload failed, was skipped or
was incomplete is never deleted, so the directory count can exceed 10. Links are never followed.

``status`` and ``list`` show a record in state ``running`` as ``interrupted`` when its process is
gone or its heartbeat is older than five minutes.

Importing this module is cheap on purpose: the orchestrator (and with it the agents, the sandbox
client and the storage layer) is imported inside ``run`` only.
"""

from __future__ import annotations

import codecs
import math
import os
import re
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from cfdagent.config import is_run_id
from cfdagent.record import RECORD_FILE, read_record
from cfdagent.rundir import RunDir, RunDirError, _remove_tree

RUNS_DIR_ENV = "CFDAGENT_RUNS_DIR"
STATUS_FILE = "status.json"
LOG_FILE = "run.log"
LAUNCHER_PREFIX = ".launch-"
MAX_TASK_BYTES = 64 * 1024
KEEP_RUNS = 10
HEARTBEAT_STALE_S = 300.0
POLL_INTERVAL_S = 1.0
START_GRACE_S = 60  # how long ``logs --follow`` waits for a detached run to appear
ORPHAN_LAUNCHER_LOG_S = 24 * 3600.0
LOG_TAIL_BYTES = 256 * 1024
READ_CHUNK = 64 * 1024
FINISHED_STATES = ("completed", "failed", "interrupted")
STATES = ("running", *FINISHED_STATES)

_UPLOAD_DONE = re.compile(r"^\S+ upload: done, \d+ file\(s\)\r?$", re.MULTILINE)
_LAUNCHER_LOG = re.compile(r"\.launch-(?P<run_id>\d{8}-\d{6}-[a-z0-9-]+)\.log")

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Run a CFD task with LLM agents and look at the runs.",
)


class Effort(StrEnum):
    low = "low"
    medium = "medium"
    high = "high"


# ---------------------------------------------------------------------------------- small helpers


def runs_root() -> Path:
    """The directory of the run directories: ``$CFDAGENT_RUNS_DIR`` or ``~/runs``."""
    value = os.environ.get(RUNS_DIR_ENV)
    return Path(value) if value else Path.home() / "runs"


def _usage_error(message: str) -> NoReturn:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(2)


def _warn(message: str) -> None:
    typer.echo(f"warning: {message}", err=True)


def _check_run_id(run_id: str) -> None:
    if not is_run_id(run_id):
        _usage_error(f"invalid run id {run_id[:80]!r}: expected YYYYMMDD-HHMMSS-<slug>")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _is_link(path: Path) -> bool:
    """True for a symbolic link (also a dangling one) and for a Windows junction."""
    if os.path.islink(path):
        return True
    isjunction = getattr(os.path, "isjunction", None)
    return bool(isjunction and isjunction(path))


def _is_regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _is_real_dir(path: Path) -> bool:
    """A directory that is neither a link nor a junction."""
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode) and not _is_link(path)
    except OSError:
        return False


def _money(value: float | None) -> str:
    return "n/a" if value is None else f"${value:.4f}"


# ---------------------------------------------------------------------------------- process check


def pid_alive(pid: object) -> bool:
    """True when a process with this id is running. Never signals the process on Windows."""
    if isinstance(pid, bool) or not isinstance(pid, int) or not 0 < pid <= 2**31 - 1:
        return False
    if sys.platform == "win32":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # the process exists; it belongs to another user
    except OSError:
        return False
    return True


def _pid_alive_windows(pid: int) -> bool:
    # ``os.kill(pid, 0)`` would terminate the process on Windows: ask for its exit code instead.
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    access_denied = 5
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return ctypes.get_last_error() == access_denied
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


# ---------------------------------------------------------------------------------- reading a run


@dataclass(frozen=True)
class RunView:
    """What the files of one run directory say."""

    run_id: str
    state: str  # running | completed | failed | interrupted | unknown
    stage: str | None
    cost_usd: float | None
    started: datetime | None
    finished: datetime | None
    reason: str | None


def _read_dict(run: RunDir, name: str) -> dict[str, Any] | None:
    try:
        data = run.read_json(name)
    except (RunDirError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _heartbeat_is_stale(value: object) -> bool:
    if not isinstance(value, str):
        return True
    try:
        beat = datetime.fromisoformat(value)
    except ValueError:
        return True
    if beat.tzinfo is None:
        beat = beat.replace(tzinfo=UTC)
    return (_utcnow() - beat).total_seconds() > HEARTBEAT_STALE_S


def _live_state(status: dict[str, Any]) -> str | None:
    """The state in ``status.json``; ``running`` becomes ``interrupted`` for a dead run."""
    state = status.get("state")
    if state not in STATES:
        return None
    if state == "running" and (
        not pid_alive(status.get("pid")) or _heartbeat_is_stale(status.get("heartbeat"))
    ):
        return "interrupted"
    return state


def view_of(path: Path) -> RunView:
    """Read ``status.json`` and ``run_record.json`` of a run directory; both may be missing."""
    run = RunDir(path)
    status = _read_dict(run, STATUS_FILE)
    try:
        record = read_record(run)
    except (RunDirError, OSError, ValueError):
        record = None
    state = _live_state(status) if status is not None else None
    if state is None and record is not None and record.status != "running":
        state = record.status
    stage = status.get("stage") if status is not None else None
    if not isinstance(stage, str):
        stage = record.failed_stage if record is not None else None
    return RunView(
        run_id=path.name,
        state=state or "unknown",
        stage=stage,
        cost_usd=record.cost_usd if record is not None else None,
        started=record.started_at if record is not None else None,
        finished=record.finished_at if record is not None else None,
        reason=record.failure_reason if record is not None else None,
    )


def _launcher_log(root: Path, run_id: str) -> Path:
    return root / f"{LAUNCHER_PREFIX}{run_id}.log"


def _has_files_of_a_run(path: Path) -> bool:
    return _is_regular_file(path / STATUS_FILE) or _is_regular_file(path / RECORD_FILE)


def run_directories(root: Path) -> list[Path]:
    """Run directories under ``root``, newest first: real directories (no links) with a run id
    as name that hold a status or a record file."""
    found = []
    try:
        with os.scandir(root) as entries:
            names = [entry.name for entry in entries]
    except OSError:
        return []
    for name in names:
        if not is_run_id(name):
            continue
        path = root / name
        if _is_real_dir(path) and _has_files_of_a_run(path):
            found.append(path)
    return sorted(found, key=lambda p: p.name, reverse=True)


# ---------------------------------------------------------------------------------- retention


def _upload_is_complete(path: Path) -> bool:
    log = path / LOG_FILE
    if not _is_regular_file(log):
        return False
    try:
        size = os.lstat(log).st_size
        with open(log, "rb") as handle:
            handle.seek(max(0, size - LOG_TAIL_BYTES))
            tail = handle.read(LOG_TAIL_BYTES)
    except OSError:
        return False
    return _UPLOAD_DONE.search(tail.decode("utf-8", errors="replace")) is not None


def _is_live(path: Path) -> bool:
    status = _read_dict(RunDir(path), STATUS_FILE)
    return status is not None and status.get("state") == "running" and pid_alive(status.get("pid"))


def _remove_stale_launcher_logs(root: Path) -> None:
    try:
        with os.scandir(root) as entries:
            names = [entry.name for entry in entries]
    except OSError:
        return
    for name in names:
        match = _LAUNCHER_LOG.fullmatch(name)
        if match is None:
            continue
        path = root / name
        if not _is_regular_file(path) or os.path.lexists(root / match["run_id"]):
            continue
        try:
            # A detached run creates its directory a moment after the log; the log of a start that
            # failed is useful for a while, so only a stale one goes.
            if time.time() - os.lstat(path).st_mtime > ORPHAN_LAUNCHER_LOG_S:
                path.unlink()
        except OSError:
            continue


def apply_retention(
    root: Path, *, keep: int = KEEP_RUNS, protect: set[str] | frozenset[str] = frozenset()
) -> None:
    """Delete run directories beyond the newest ``keep``; see the module docstring.

    Never raises: a directory that cannot be removed is a warning on stderr.
    """
    root = Path(root)
    for path in run_directories(root)[keep:]:
        if path.name in protect or not _upload_is_complete(path) or _is_live(path):
            continue
        try:
            _remove_tree(path)
        except (OSError, RecursionError) as exc:
            _warn(f"retention could not remove {path.name} ({type(exc).__name__})")
            continue
        try:
            _launcher_log(root, path.name).unlink(missing_ok=True)
        except OSError:
            pass
    _remove_stale_launcher_logs(root)


# ---------------------------------------------------------------------------------- run


def _registry() -> dict:
    from cfdagent.bench.registry import load_registry

    return load_registry()


def _read_task_file(path: Path) -> str:
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_TASK_BYTES + 1)
    except OSError as exc:
        _usage_error(
            f"cannot read the task file {str(path)!r}: {exc.strerror or type(exc).__name__}"
        )
    if len(data) > MAX_TASK_BYTES:
        _usage_error(f"the task file {str(path)!r} is larger than {MAX_TASK_BYTES // 1024} KiB")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        _usage_error(f"the task file {str(path)!r} is not UTF-8 text")
    if not text.strip():
        _usage_error(f"the task file {str(path)!r} is empty")
    return text


def _benchmark_task(benchmark_id: str) -> str:
    from cfdagent.bench.registry import RegistryError

    try:
        registry = _registry()
    except RegistryError as exc:
        _usage_error(f"the benchmark registry cannot be loaded: {exc}")
    entry = registry.get(benchmark_id)
    if entry is None:
        known = ", ".join(sorted(registry)) or "(none)"
        _usage_error(f"unknown benchmark {benchmark_id!r}; known benchmarks: {known}")
    return _read_task_file(entry.root / entry.task_file)


def _build_config(model: str | None, effort: Effort | None, benchmark_id: str | None) -> Any:
    from cfdagent.config import DEFAULT_DEPLOYMENT, RunConfig
    from cfdagent.llm.cost import PRICING

    config = RunConfig.default()
    if model is not None and model not in PRICING:
        _usage_error(
            f"no price is known for the model {model!r}; known models: {', '.join(PRICING)}"
        )
    if model is not None or effort is not None:
        changes: dict[str, Any] = {}
        if model is not None:
            changes["deployment"] = model
        if effort is not None:
            changes["reasoning_effort"] = effort.value
        elif model != DEFAULT_DEPLOYMENT:
            changes["reasoning_effort"] = None  # the default effort belongs to the default model
        models = {name: cfg.model_copy(update=changes) for name, cfg in config.models.items()}
        config = config.model_copy(update={"models": models})
    if benchmark_id is not None:
        config = config.model_copy(update={"benchmark_id": benchmark_id, "task_set": "development"})
    return config


def _child_command(
    task_file: Path | None,
    benchmark: str | None,
    model: str | None,
    effort: Effort | None,
    no_upload: bool,
    run_id: str,
) -> list[str]:
    argv = [sys.executable, "-m", "cfdagent", "run"]
    if task_file is not None:
        argv.append(str(task_file.resolve()))
    if benchmark is not None:
        argv += ["--benchmark", benchmark]
    if model is not None:
        argv += ["--model", model]
    if effort is not None:
        argv += ["--reasoning-effort", effort.value]
    if no_upload:
        argv.append("--no-upload")
    argv += ["--run-id", run_id]
    return argv


def _detach(argv: list[str], root: Path, run_id: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(root / run_id):
        _usage_error(f"the run directory for run {run_id} already exists")
    launcher = _launcher_log(root, run_id)
    try:
        handle = open(launcher, "xb")  # exclusive: never reuse a log of an earlier start
    except FileExistsError:
        _usage_error(f"a run with the id {run_id} was started already")
    except OSError as exc:
        typer.echo(f"error: cannot create the launcher log ({type(exc).__name__})", err=True)
        raise typer.Exit(1) from None
    options: dict[str, Any] = {"stdin": subprocess.DEVNULL, "close_fds": True}
    if sys.platform == "win32":
        options["creationflags"] = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        )
    else:
        options["start_new_session"] = True
    try:
        try:
            subprocess.Popen(argv, stdout=handle, stderr=handle, **options)
        finally:
            handle.close()
    except OSError as exc:
        launcher.unlink(missing_ok=True)
        typer.echo(f"error: could not start the run ({type(exc).__name__}: {exc})", err=True)
        raise typer.Exit(1) from None


@app.command()
def run(
    task_file: Annotated[
        Path | None, typer.Argument(metavar="[TASK_FILE]", help="Markdown file with the task.")
    ] = None,
    benchmark: Annotated[
        str | None, typer.Option("--benchmark", help="Run a registered benchmark's task.")
    ] = None,
    model: Annotated[str | None, typer.Option("--model", help="Deployment for all agents.")] = None,
    reasoning_effort: Annotated[
        Effort | None, typer.Option("--reasoning-effort", help="Reasoning effort for all agents.")
    ] = None,
    detach: Annotated[
        bool, typer.Option("--detach", help="Start in the background, print the run id, return.")
    ] = False,
    no_upload: Annotated[
        bool, typer.Option("--no-upload", help="Keep the results on this machine only.")
    ] = False,
    run_id: Annotated[str | None, typer.Option("--run-id", hidden=True)] = None,
) -> None:
    """Run one task through the whole pipeline."""
    if task_file is None and benchmark is None:
        _usage_error("give a task file or --benchmark ID")
    if task_file is not None and benchmark is not None:
        _usage_error("give a task file or --benchmark, not both")
    if run_id is not None:
        _check_run_id(run_id)

    task = _read_task_file(task_file) if task_file is not None else _benchmark_task(str(benchmark))
    config = _build_config(model, reasoning_effort, benchmark)
    root = runs_root()

    if detach:
        from cfdagent.config import RunConfig

        try:
            RunConfig.llm_settings_from_env()  # fail now, not in a background process
        except ValueError as exc:
            _usage_error(str(exc))
        from cfdagent import orchestrator

        new_id = run_id or orchestrator.new_run_id(task)
        argv = _child_command(task_file, benchmark, model, reasoning_effort, no_upload, new_id)
        _detach(argv, root, new_id)
        typer.echo(new_id)
        typer.echo(
            f"started in the background; follow it with: cfdagent logs {new_id} --follow",
            err=True,
        )
        return

    from cfdagent import orchestrator

    try:
        result = orchestrator.run_task(
            task, config, run_root=root, upload=not no_upload, run_id=run_id
        )
    except (orchestrator.RunExistsError, ValueError) as exc:
        _usage_error(str(exc))

    record = result.record
    typer.echo(f"run {result.run_id}: {result.status}")
    typer.echo(f"cost: {_money(record.cost_usd)}")
    typer.echo(f"run directory: {result.run_dir}")
    if result.status == "failed":
        typer.echo(f"failed at stage: {record.failed_stage or 'n/a'}")
    if record.failure_reason and result.status != "completed":
        typer.echo(f"reason: {record.failure_reason}")

    if not no_upload:
        try:
            apply_retention(root, protect={result.run_id})
        except Exception as exc:  # cleaning up must never change the outcome of a run
            _warn(f"retention failed ({type(exc).__name__})")

    raise typer.Exit({"completed": 0, "failed": 1}.get(result.status, 130))


# ---------------------------------------------------------------------------------- status


def _existing_run(root: Path, run_id: str) -> Path | None:
    path = root / run_id
    return path if _is_real_dir(path) else None


@app.command()
def status(run_id: Annotated[str, typer.Argument(help="Run id.")]) -> None:
    """Show the state, stage and cost of a run."""
    _check_run_id(run_id)
    root = runs_root()
    path = _existing_run(root, run_id)
    if path is None:
        launcher = _launcher_log(root, run_id)
        if _is_regular_file(launcher):
            typer.echo(f"run: {run_id}")
            typer.echo(f"state: not started (see launcher log: {launcher})")
            return
        _usage_error(f"no run {run_id} in {root}")
    view = view_of(path)
    typer.echo(f"run: {view.run_id}")
    typer.echo(f"state: {view.state}")
    typer.echo(f"stage: {view.stage or '-'}")
    typer.echo(f"cost: {_money(view.cost_usd)}")
    if view.started is not None:
        typer.echo(f"started: {view.started.isoformat(timespec='seconds')}")
    if view.finished is not None:
        typer.echo(f"finished: {view.finished.isoformat(timespec='seconds')}")
    if view.reason:
        typer.echo(f"failure: {view.reason}")


# ---------------------------------------------------------------------------------- logs


def _drain(path: Path, offset: int, decoder: codecs.IncrementalDecoder) -> int:
    """Print what ``path`` holds beyond ``offset``; returns the new offset. A file that is
    missing, not a regular file or shorter than ``offset`` (rewritten) is handled quietly."""
    try:
        info = os.lstat(path)
    except OSError:
        return offset
    if not stat.S_ISREG(info.st_mode):
        return offset
    if info.st_size < offset:
        offset = 0
        decoder.reset()
    try:
        with open(path, "rb") as handle:
            handle.seek(offset)
            while chunk := handle.read(READ_CHUNK):
                offset += len(chunk)
                text = decoder.decode(chunk)
                if text:
                    typer.echo(text, nl=False)
    except OSError:
        pass
    return offset


def _new_decoder() -> codecs.IncrementalDecoder:
    return codecs.getincrementaldecoder("utf-8")(errors="replace")


@app.command()
def logs(
    run_id: Annotated[str, typer.Argument(help="Run id.")],
    follow: Annotated[
        bool, typer.Option("--follow", help="Wait for new lines until it ends.")
    ] = False,
) -> None:
    """Print the log of a run; with --follow keep printing until the run has ended."""
    _check_run_id(run_id)
    root = runs_root()
    launcher = _launcher_log(root, run_id)
    if _existing_run(root, run_id) is None and not _is_regular_file(launcher):
        _usage_error(f"no run {run_id} in {root}")
    if not follow:
        path = root / run_id / LOG_FILE
        if _existing_run(root, run_id) is None:
            path = launcher
        _drain(path, 0, _new_decoder())
        return

    source: Path | None = None
    offset = 0
    decoder = _new_decoder()
    waits = 0
    max_waits = math.ceil(START_GRACE_S / POLL_INTERVAL_S)
    try:
        while True:
            run_path = _existing_run(root, run_id)
            view = view_of(run_path) if run_path is not None else None
            # The state is read before the drain, so the drain after it sees every final line.
            finished = view is not None and view.state in FINISHED_STATES
            target = run_path / LOG_FILE if run_path is not None else launcher
            if target != source:
                source, offset, decoder = target, 0, _new_decoder()
            offset = _drain(target, offset, decoder)
            if finished:
                return
            if view is None or view.state == "unknown":
                waits += 1
                if waits > max_waits:
                    typer.echo("error: the run did not start; see the launcher log above", err=True)
                    raise typer.Exit(1)
            _sleep(POLL_INTERVAL_S)
    except KeyboardInterrupt:
        raise typer.Exit(130) from None


# ---------------------------------------------------------------------------------- list


@app.command("list")
def list_runs(
    limit: Annotated[int, typer.Option("--limit", min=1, help="How many runs to show.")] = 10,
) -> None:
    """List the most recent runs."""
    root = runs_root()
    paths = run_directories(root)[:limit]
    if not paths:
        typer.echo(f"no runs in {root}")
        return
    rows = [view_of(path) for path in paths]
    width = max(len("RUN ID"), *(len(view.run_id) for view in rows))
    typer.echo(f"{'RUN ID':<{width}}  {'STATUS':<11}  {'STAGE':<14}  COST")
    for view in rows:
        typer.echo(
            f"{view.run_id:<{width}}  {view.state:<11}  {(view.stage or '-'):<14}  "
            f"{_money(view.cost_usd)}"
        )
