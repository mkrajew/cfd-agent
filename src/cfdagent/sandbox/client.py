"""Call executor ops: one fresh container (or, in tests, one plain subprocess) per call.

Flow of ``SandboxClient.call``: validate the op and its arguments (a bad call never starts a
container); write ``.sandbox/<call-id>.request.json``; start ``docker run`` (see
``sandbox.docker``) under a unique name ``cfd-<run-id>-<n>``; wait; read
``.sandbox/<call-id>.response.json`` with ``RunDir.read_json`` and a size limit. The run directory
is hostile, so a missing, oversized, garbled or symlinked response is not an error of the client:
it becomes a failed ``OpResponse`` that carries the tail of what the container printed.

A timeout (or ``KeyboardInterrupt``, or any other exception while waiting) runs ``docker kill
<name>``: killing only the ``docker run`` client would leave the container running. The client
process is then waited for, killed if it does not exit, and ``docker rm -f <name>`` removes a
container that was created but never started.

``mode="local"`` runs ``python -m cfdagent.executor`` as a subprocess of the caller. It is NOT
confined (no container, no network cut); it exists for the tests that already run inside the
image. The child gets only the scrubbed environment of ``SAFE_ENV_*``.

This module is host-side: it imports no tool, CAD or mesh code.
"""

from __future__ import annotations

import itertools
import os
import re
import subprocess
import sys
import tempfile
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

import cfdagent
from cfdagent.executor import protocol
from cfdagent.executor.protocol import OpRequest, OpResponse
from cfdagent.rundir import RunDir, RunDirError
from cfdagent.sandbox.config import SandboxConfig
from cfdagent.sandbox.docker import build_argv

# Environment a local-mode executor gets; the same lists as ``tools.process`` (a test compares
# them). Everything else, in particular cloud credentials and API keys, is dropped.
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

STDERR_TAIL_BYTES = 6000  # with the message prefix this stays under protocol.MAX_ERROR_CHARS
KILL_GRACE_S = 30.0
_COMMAND_TIMEOUT_S = 60.0
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}")


def _tail(handle, limit: int = STDERR_TAIL_BYTES) -> str:
    try:
        handle.flush()
        size = handle.seek(0, os.SEEK_END)
        handle.seek(max(0, size - limit))
        data = handle.read()
    except (OSError, ValueError):
        return ""
    return data.decode("utf-8", errors="replace").lstrip("�")


def _kill_process(proc: Any) -> None:
    """Kill a started process and, where the platform allows, its children; then reap it."""
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            import signal

            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        proc.kill()
        proc.wait(timeout=KILL_GRACE_S)
    except (OSError, subprocess.SubprocessError):
        pass


class SandboxClient:
    """Calls executor ops for one run. ``popen`` and ``run`` default to ``subprocess`` and are
    injectable so tests need no Docker."""

    def __init__(
        self,
        config: SandboxConfig,
        run_dir: RunDir,
        run_id: str,
        *,
        popen: Callable[..., Any] = subprocess.Popen,
        run: Callable[..., Any] = subprocess.run,
    ) -> None:
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            raise ValueError(
                f"run id {str(run_id)[:60]!r} cannot be part of a container name "
                "(letters, digits, '_', '.', '-'; at most 101 characters)"
            )
        self.config = config
        self.run_dir = run_dir
        self.run_id = run_id
        self._popen = popen
        self._run = run
        self._counter = itertools.count(1)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ internals

    def _command(self, name: str, op: str, request_rel: str) -> tuple[list[str], dict[str, Any]]:
        if self.config.mode == "docker":
            return build_argv(self.config, name, self.run_dir.root, op, request_rel), {}
        source_root = str(Path(cfdagent.__file__).resolve().parents[1])
        env = {
            key: value
            for key, value in os.environ.items()
            if key in SAFE_ENV_KEYS or key.startswith(SAFE_ENV_PREFIXES)
        }
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [source_root, env.get("PYTHONPATH")]))
        kwargs: dict[str, Any] = {"cwd": self.run_dir.root, "env": env}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        return [sys.executable, "-m", "cfdagent.executor", op, request_rel], kwargs

    def _command_quietly(self, argv: list[str]) -> None:
        try:
            self._run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_COMMAND_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    def _stop(self, proc: Any, name: str) -> None:
        """Stop the executor: kill the named container first, then make sure the client is gone."""
        docker = self.config.mode == "docker"
        try:
            if docker:  # the client exits by itself once its container is gone
                self._command_quietly(["docker", "kill", name])
                try:
                    proc.wait(timeout=KILL_GRACE_S)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            if proc.poll() is None:
                _kill_process(proc)
            if docker:
                self._command_quietly(["docker", "rm", "-f", name])

    # ------------------------------------------------------------------ public

    def call(self, op: str, args: BaseModel | dict[str, Any], timeout_s: float) -> OpResponse:
        """Run ``op`` in a fresh executor and return its response; see the module docstring.

        Never raises for problems of the call itself (unknown op, bad arguments, container that
        does not start, no or bad response, timeout); those are failed responses. A
        ``KeyboardInterrupt`` kills the container and propagates.
        """
        raw_args = args.model_dump(mode="json") if isinstance(args, BaseModel) else args
        try:
            model = protocol.validate_args(op, raw_args)
        except (protocol.UnknownOp, protocol.InvalidArgs) as exc:
            return protocol.failure(str(exc))

        with self._lock:
            number = next(self._counter)
        name = f"cfd-{self.run_id}-{number}"
        call_id = f"{number:05d}-{uuid.uuid4().hex[:8]}"
        request_rel = protocol.request_rel(call_id)
        response_rel = request_rel.replace(protocol.REQUEST_SUFFIX, protocol.RESPONSE_SUFFIX)

        request = OpRequest(op=op, args=model.model_dump(mode="json"))
        try:
            self.run_dir.write_text(
                request_rel, request.model_dump_json(), max_bytes=protocol.REQUEST_MAX_BYTES
            )
        except (RunDirError, OSError) as exc:
            return protocol.failure(
                f"cannot write the request into the run directory ({exc}); "
                "the run directory may have been tampered with",
                internal=True,
            )

        argv, extra = self._command(name, op, request_rel)
        with tempfile.TemporaryFile() as output:
            try:
                proc = self._popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    shell=False,
                    **extra,
                )
            except OSError as exc:
                return protocol.failure(
                    f"cannot start the executor ({argv[0]}): {exc}", internal=True
                )

            timed_out = False
            code: int | None = None
            try:
                code = proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._stop(proc, name)
            except BaseException:  # KeyboardInterrupt included: never leave the container behind
                self._stop(proc, name)
                raise
            tail = _tail(output)

        if timed_out:
            where = (
                f"the container {name} was killed"
                if self.config.mode == "docker"
                else ("the executor was killed")
            )
            return protocol.failure(
                f"{op} timed out after {timeout_s:g} s; {where}\n{tail}".rstrip(),
                data={"timed_out": True},
            )
        try:
            return OpResponse.model_validate(
                self.run_dir.read_json(response_rel, protocol.RESPONSE_MAX_BYTES)
            )
        except (RunDirError, OSError, ValidationError) as exc:
            reason = exc.__class__.__name__ if isinstance(exc, ValidationError) else str(exc)
            reason = reason[:300]
            return protocol.failure(
                f"the executor for {op} (exit code {code}) left no valid response: {reason}\n"
                f"{tail}".rstrip(),
                internal=True,
            )
