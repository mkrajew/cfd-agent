"""Entry point of an executor container: ``python -m cfdagent.executor <op> <request-file>``.

Reads ``<job>/.sandbox/<id>.request.json``, runs the op, writes ``<id>.response.json`` atomically.
The job directory is the current directory (``/job`` in the container). Exit code 0 means a
response was written (whether the op succeeded or not), 2 a bad command line (no response is
possible), 1 that the response could not be written. Diagnostics go to stderr.

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
    text = response.model_dump_json()
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
    _stdout_to_stderr()
    sys.exit(main(sys.argv[1:]))
