"""Entry point of an executor container: ``python -m cfdagent.executor <op> <request-file>``.

Reads ``<job>/.sandbox/<id>.request.json``, runs the op, writes ``<id>.response.json`` atomically.
The job directory is the current directory (``/job`` in the container). Exit code 0 means a
response was written (whether the op succeeded or not), 2 a bad command line (no response is
possible; also 2 when HOME is unset or inside the job directory, see ``home_problem``), 1 that
the response could not be written. Diagnostics go to stderr.

While ops run, file descriptor 1 is pointed at stderr: OpenCASCADE and OpenFOAM libraries print
to stdout, and nothing on stdout is ever parsed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from pydantic import ValidationError

from cfdagent.executor import ops, protocol
from cfdagent.rundir import RunDir, RunDirError


def home_problem(home: str | None, job_root: Path) -> str | None:
    """Why ``home`` cannot be this process's HOME; None if it is fine.

    OpenFOAM reads ``$HOME/.OpenFOAM/controlDict`` and that file overrides the site-wide
    ``allowSystemOperations 0``. The job directory is hostile (agent code writes into it), so HOME
    must be set (OpenFOAM falls back to the working directory otherwise) and must not be the job
    directory or lie under it.
    """
    if not home:
        return "HOME is not set; OpenFOAM would look for its user files in the job directory"
    job = job_root.resolve()
    resolved = Path(home).resolve()
    if resolved == job or job in resolved.parents:
        return f"HOME ({home}) is inside the job directory, which agent code can write to"
    return None


def _respond(op: str, request_rel: str, jail: RunDir) -> protocol.OpResponse:
    try:
        raw = jail.read_json(request_rel, protocol.REQUEST_MAX_BYTES)
        request = protocol.OpRequest.model_validate(raw)
    except (RunDirError, OSError) as exc:
        return protocol.failure(f"cannot read the request {request_rel!r}: {exc}")
    except ValidationError:
        return protocol.failure(f"the request {request_rel!r} is not a valid OpRequest")
    if request.op != op:
        return protocol.failure(
            f"the request is for op {request.op[:80]!r} but the command asked for {op[:80]!r}; "
            "they do not match"
        )
    return ops.execute(request, jail)


def main(argv: list[str], root: Path | None = None) -> int:
    """Run one call: ``argv`` is ``[op, request_rel]``; ``root`` defaults to the cwd."""
    if len(argv) != 2:
        print("usage: python -m cfdagent.executor <op> <request-file>", file=sys.stderr)
        return 2
    op, request_rel = argv
    response_rel = protocol.response_rel_for(request_rel)
    if response_rel is None:
        print(
            f"the request file must be {protocol.SANDBOX_DIR}/<id>{protocol.REQUEST_SUFFIX}, "
            f"got {request_rel!r}",
            file=sys.stderr,
        )
        return 2
    jail = RunDir(root if root is not None else Path.cwd())
    response = _respond(op, request_rel, jail)
    try:
        text = response.model_dump_json()
    except Exception as exc:  # a result that is not JSON-safe must not leave the host waiting
        text = protocol.failure(
            f"the result of {op} could not be serialised to JSON ({type(exc).__name__})",
            internal=True,
        ).model_dump_json()
    if len(text.encode("utf-8")) > protocol.RESPONSE_MAX_BYTES:
        text = protocol.failure(
            f"the response of {op} is too large (over {protocol.RESPONSE_MAX_BYTES} bytes)",
            internal=True,
        ).model_dump_json()
    try:
        jail.write_text(response_rel, text, max_bytes=protocol.RESPONSE_MAX_BYTES)
    except (RunDirError, OSError) as exc:
        print(f"cannot write the response {response_rel!r}: {exc}", file=sys.stderr)
        return 1
    return 0


def _stdout_to_stderr() -> None:
    sys.stdout.flush()
    os.dup2(2, 1)  # child processes and C libraries inherit this too
    sys.stdout = sys.stderr


if __name__ == "__main__":
    _problem = home_problem(os.environ.get("HOME"), Path.cwd())
    if _problem is not None:
        print(f"the executor does not start: {_problem}", file=sys.stderr)
        sys.exit(2)
    _stdout_to_stderr()
    sys.exit(main(sys.argv[1:]))
