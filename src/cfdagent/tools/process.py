"""Allowlisted process runner used by every tool that launches an external program.

Only bare executable names from ``ALLOWED_EXECUTABLES`` can be run; there is no shell. stdout and
stderr are merged into one log file. Network and credential sandboxing is out of scope here.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
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
    }
)

TAIL_LINES = 50


class ToolNotAllowed(Exception):
    """The requested executable is not on the allowlist."""


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    returncode: int | None
    timed_out: bool
    log_path: Path
    tail: str  # last 50 log lines


def _read_tail(log_path: Path) -> str:
    with open(log_path, encoding="utf-8", errors="replace") as f:
        return "".join(deque(f, maxlen=TAIL_LINES))


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
    ``returncode=None``. An allowlisted executable that is not installed gives ``ok=False`` with a
    message in the log.
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
    log_path.parent.mkdir(parents=True, exist_ok=True)

    exe = _resolve(name)
    if exe is None:
        log_path.write_text(
            f"executable {name!r} not found on PATH; it is allowlisted but not installed\n",
            encoding="utf-8",
        )
        return ToolResult(False, None, False, log_path, _read_tail(log_path))

    popen_kwargs: dict = {}
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    timed_out = False
    with open(log_path, "wb") as log:
        proc = subprocess.Popen(
            [exe, *argv[1:]],
            cwd=cwd,
            # A shell updates PWD on cd; without this OpenFOAM utilities warn about a stale PWD.
            env={**os.environ, "PWD": str(cwd)},
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            shell=False,
            **popen_kwargs,
        )
        try:
            returncode: int | None = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            returncode = None
            _kill_tree(proc)

    if timed_out:
        with open(log_path, "a", encoding="utf-8") as log:
            log.write(f"\n[cfdagent] timed out after {timeout_s} s; process killed\n")

    return ToolResult(
        ok=(returncode == 0 and not timed_out),
        returncode=returncode,
        timed_out=timed_out,
        log_path=log_path,
        tail=_read_tail(log_path),
    )
