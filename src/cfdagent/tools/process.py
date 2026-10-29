"""Allowlisted process runner used by every tool that launches an external program.

Only bare executable names from ``ALLOWED_EXECUTABLES`` can be run; there is no shell. stdout and
stderr are merged into one log file. The child gets a scrubbed environment (``SAFE_ENV_KEYS`` and
``SAFE_ENV_PREFIXES`` plus ``PWD``), so credentials in the parent environment do not reach it.

The allowlist is not a security boundary: ``python`` and ``mpirun`` can run anything. Confinement
(no network, no credentials, bounded resources) is the job of the container sandbox, not of this
module.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

ALLOWED_EXECUTABLES: frozenset[str] = frozenset(
    {
        "python",
        "gmshToFoam",
        "checkMesh",
        "foamDictionary",
        "changeDictionary",
        "icoFoam",
        "simpleFoam",
        "pimpleFoam",
        "decomposePar",
        "reconstructPar",
        "postProcess",
        "foamListTimes",
        "mpirun",
    }
)

TAIL_LINES = 50
TAIL_MAX_BYTES = 8000

# Environment passed to the child. Everything else (cloud credentials, API keys, ...) is dropped.
# SYSTEMROOT, PATHEXT, TEMP, TMP and COMSPEC are needed to start a process on Windows.
SAFE_ENV_KEYS: tuple[str, ...] = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "USER",
    "LD_LIBRARY_PATH",
    "SYSTEMROOT",
    "PATHEXT",
    "TEMP",
    "TMP",
    "COMSPEC",
)
SAFE_ENV_PREFIXES: tuple[str, ...] = (
    "FOAM_",
    "WM_",
    "MPI_",
    "OMPI_",
    "OPAL_",
    "PYTHON",
    "VTK_",
    "PYVISTA_",
    "MPL",
)


class ToolNotAllowed(Exception):
    """The requested executable is not on the allowlist."""


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    returncode: int | None
    timed_out: bool
    log_path: Path
    tail: str  # last 50 log lines, at most TAIL_MAX_BYTES bytes (UTF-8)


def _trim_to_char_boundary(data: bytes) -> bytes:
    """Drop leading UTF-8 continuation bytes left by cutting in the middle of a character."""
    start = 0
    while start < len(data) and (data[start] & 0xC0) == 0x80:
        start += 1
    return data[start:]


def _read_tail(log_path: Path) -> str:
    """Last ``TAIL_LINES`` lines of the log, at most ``TAIL_MAX_BYTES`` bytes when encoded.

    When the byte cap cuts into a line, that broken first line is dropped, unless the kept bytes
    contain no complete line at all (one huge line): then its end is kept.
    """
    with open(log_path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        # A few spare bytes so a cut in the middle of a character can be repaired.
        f.seek(max(0, size - TAIL_MAX_BYTES - 4))
        data = f.read()
    truncated = size > len(data)
    if truncated:
        data = _trim_to_char_boundary(data)
    # Lines are split on LF only; CRLF is normalised to LF.
    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])
    if truncated and len(lines) > 1:
        lines = lines[1:]
    text = "".join(deque(lines, maxlen=TAIL_LINES))
    encoded = text.encode("utf-8")
    if len(encoded) > TAIL_MAX_BYTES:
        text = _trim_to_char_boundary(encoded[-TAIL_MAX_BYTES:]).decode("utf-8", errors="replace")
    return text


def fail_after_run(result: ToolResult, problem: str) -> ToolResult:
    """Turn a finished run into a failure: append ``[cfdagent] <problem>`` to the log.

    Returns ``result`` with ``ok=False`` and a ``tail`` re-read from the log (so it shows the
    reason and obeys the same line and byte caps as every other tail).
    """
    with open(result.log_path, "a", encoding="utf-8") as log:
        log.write(f"\n[cfdagent] {problem}\n")
    return replace(result, ok=False, tail=_read_tail(result.log_path))


def _child_env(cwd: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key in SAFE_ENV_KEYS or key.startswith(SAFE_ENV_PREFIXES)
    }
    # A shell updates PWD on cd; without this OpenFOAM utilities warn about a stale PWD.
    env["PWD"] = str(cwd)
    return env


def _not_ok(log_path: Path, message: str, *, write_log: bool = True) -> ToolResult:
    """Result for an environment problem; the reason goes to the log where that is possible."""
    if write_log:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(message, encoding="utf-8")
        except OSError:
            pass  # the reason is still in ``tail``
    return ToolResult(False, None, False, log_path, message)


def _resolve(name: str) -> str | None:
    if name == "python":
        return sys.executable
    return shutil.which(name)


def _kill_tree(proc: subprocess.Popen) -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        import signal

        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    proc.kill()
    proc.wait()


def run_tool(
    argv: Sequence[str], cwd: Path, log_path: Path, timeout_s: float = 600.0
) -> ToolResult:
    """Run an allowlisted executable without a shell and log its merged output.

    ``argv[0]`` must be a bare allowlisted name (a path is rejected). On timeout the process (and,
    where the platform allows, its children) is killed and the result has ``timed_out=True`` and
    ``returncode=None``. An allowlisted executable that is not installed, a ``cwd`` that is not a
    directory (it is never created), a program that cannot be started and an unwritable log all
    give ``ok=False`` with the reason in ``tail`` (and in the log when it can be written). Only a
    bad ``argv`` raises ``ToolNotAllowed`` (a programmer error). Any exception while waiting,
    ``KeyboardInterrupt`` included, kills the child's process group before it propagates.
    """
    if not argv:
        raise ToolNotAllowed("empty command")
    name = argv[0]
    if name not in ALLOWED_EXECUTABLES:
        raise ToolNotAllowed(
            f"executable {name!r} is not allowed; allowed: {sorted(ALLOWED_EXECUTABLES)}"
        )

    cwd = Path(cwd).resolve()
    log_path = Path(log_path)

    # Checked before anything is created: a log inside a missing cwd must not conjure the cwd up.
    if not cwd.is_dir():
        message = f"cwd {str(cwd)!r} does not exist or is not a directory\n"
        inside_cwd = log_path.resolve().is_relative_to(cwd)
        return _not_ok(log_path, message, write_log=not inside_cwd)

    exe = _resolve(name)
    if exe is None:
        return _not_ok(
            log_path,
            f"executable {name!r} not found on PATH; it is allowlisted but not installed\n",
        )

    popen_kwargs: dict = {}
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = open(log_path, "wb")
    except OSError as exc:
        message = f"cannot write the log file {str(log_path)!r}: {exc}\n"
        return _not_ok(log_path, message, write_log=False)

    timed_out = False
    returncode: int | None = None
    spawn_error: str | None = None
    with log:
        try:
            proc = subprocess.Popen(
                [exe, *argv[1:]],
                cwd=cwd,
                env=_child_env(cwd),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                shell=False,
                **popen_kwargs,
            )
        except OSError as exc:
            spawn_error = f"cannot start {name!r}: {exc}\n"
        else:
            finished = False
            try:
                try:
                    returncode = proc.wait(timeout=timeout_s)
                    finished = True
                except subprocess.TimeoutExpired:
                    timed_out = True
            finally:
                # Timeout, KeyboardInterrupt or any other exception: never leave the child (or its
                # children) running behind us.
                if not finished:
                    _kill_tree(proc)

    if spawn_error is not None:
        return _not_ok(log_path, spawn_error)

    if timed_out:
        with open(log_path, "a", encoding="utf-8") as log_file:
            log_file.write(f"\n[cfdagent] timed out after {timeout_s} s; process killed\n")

    return ToolResult(
        ok=(returncode == 0 and not timed_out),
        returncode=returncode,
        timed_out=timed_out,
        log_path=log_path,
        tail=_read_tail(log_path),
    )
