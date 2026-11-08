"""Replay a recorded model conversation through the whole pipeline (real sandbox, real upload).

Usage, on the VM, from the host checkout (the host environment is enough: no toolchain):

    /opt/cfdagent-host/bin/python scripts/replay_scripted_run.py \\
        tests/fixtures/scripted/cavity_pipeline.json [--image IMAGE]

``ScriptedBackend`` plays all six agents from the JSON file, so no model is called and the only
cost is VM time. Everything else is real: executor containers in ``docker`` mode, the real Scorer
against the benchmark's reference data, and the upload of the run folder to the ``runs`` container.

Configuration comes from the environment that ``/etc/cfdagent/env`` provides: ``CFDAGENT_IMAGE``
(or ``--image``), ``CFDAGENT_STORAGE_ACCOUNT_URL`` (required: the upload must finish),
``CFDAGENT_RUNS_DIR`` and ``CFDAGENT_STATE_DIR``. The benchmark folder is found from this script's
own location, so the working directory does not matter.

The summary lists the run id, the status, the wall time per stage, the Scorer result, the last
``upload:`` line of ``run.log``, the size of the run directory and the read-only command that lists
the run in Blob storage. Exit status: 0 only when the status is ``completed``, the comparison
passed and the upload finished; 1 otherwise; 2 for a configuration problem.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlparse

from cfdagent.config import RunConfig
from cfdagent.llm.scripted import ScriptedBackend
from cfdagent.orchestrator import directory_size, run_task

REPO_ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_ID = "cavity-re100"
TASK_SET = "development"
IMAGE_ENV = "CFDAGENT_IMAGE"
STORAGE_URL_ENV = "CFDAGENT_STORAGE_ACCOUNT_URL"
RUNS_DIR_ENV = "CFDAGENT_RUNS_DIR"
STATE_DIR_ENV = "CFDAGENT_STATE_DIR"
CONTAINER = "runs"
LOG_TAIL_BYTES = 256 * 1024

_UPLOAD_LINE = re.compile(r"^\S+ upload[: ].*$", re.MULTILINE)
_UPLOAD_DONE = re.compile(r"^\S+ upload: done, \d+ file\(s\)$")


def last_upload_line(run_dir: Path) -> str | None:
    """The last ``<time> upload...`` line of ``run.log``: it tells how the upload ended."""
    try:
        with open(run_dir / "run.log", "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - LOG_TAIL_BYTES))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    lines = _UPLOAD_LINE.findall(text)
    return lines[-1].rstrip("\r") if lines else None


def size_text(count: int) -> str:
    for unit, size in (("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)):
        if count >= size:
            return f"{count / size:.1f} {unit}"
    return f"{count} B"


def account_name(url: str) -> str:
    """The storage account of a Blob endpoint URL (the first label of the host name)."""
    return (urlparse(url).hostname or "").split(".")[0]


def build_config(image: str) -> RunConfig:
    """The default run configuration with the docker-mode sandbox, tagged as a benchmark run."""
    return RunConfig.default(image=image).model_copy(
        update={"benchmark_id": BENCHMARK_ID, "task_set": TASK_SET}
    )


def stage_lines(record: Any) -> list[str]:
    lines = []
    for stage in record.stages:
        seconds = sum(attempt.seconds for attempt in stage.attempts)
        state = "ok" if stage.ok else "FAILED"
        lines.append(
            f"  {stage.name:<14} {seconds:8.1f} s  {len(stage.attempts)} attempt(s)  {state}"
        )
    return lines


def summary_lines(result: Any, upload: str | None, size: int, account: str) -> list[str]:
    record, comparison = result.record, result.comparison or {}
    lines = [
        f"run id: {result.run_id}",
        f"status: {result.status}",
        f"run directory: {result.run_dir} ({size_text(size)})",
        f"wall time: {record.wall_seconds:.1f} s, cost: ${record.cost_usd:.4f}",
        "stages (wall time of the attempts):",
        *stage_lines(record),
        f"Scorer: passed: {comparison.get('passed')}",
    ]
    for criterion in comparison.get("criteria", []):
        lines.append(
            f"  {criterion.get('quantity')}: passed={criterion.get('passed')} "
            f"error={criterion.get('error')} ({criterion.get('reason')})"
        )
    if result.status != "completed":
        lines.append(f"failed stage: {record.failed_stage}; reason: {record.failure_reason}")
    lines.append(f"upload: {upload if upload is not None else 'no upload line in run.log'}")
    lines.append(
        "blob listing (read-only): az storage blob list --auth-mode login "
        f"--account-name {account} --container-name {CONTAINER} --prefix {result.run_id}/"
    )
    return lines


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    run: Callable[..., Any] = run_task,
    load_backend: Callable[[Path], Any] = ScriptedBackend.from_json,
    size: Callable[[Path], int] = directory_size,
    out: TextIO | None = None,
) -> int:
    environ = os.environ if environ is None else environ
    out = out or sys.stdout
    parser = argparse.ArgumentParser(description="Replay a scripted conversation, real sandbox.")
    parser.add_argument("route", type=Path, help="JSON file of the scripted conversation")
    parser.add_argument("--image", help=f"executor image (default: ${IMAGE_ENV})")
    args = parser.parse_args(argv)

    image = args.image or environ.get(IMAGE_ENV)
    url = environ.get(STORAGE_URL_ENV)
    problem = None
    if not image:
        problem = f"give --image or set {IMAGE_ENV}"
    elif not url:
        problem = f"set {STORAGE_URL_ENV}: the upload must finish for this replay to pass"
    elif not args.route.is_file():
        problem = f"the route file {args.route} does not exist"
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return 2

    runs, state = environ.get(RUNS_DIR_ENV), environ.get(STATE_DIR_ENV)
    try:
        result = run(
            "ignored: a benchmark run uses the task file of the benchmark",
            build_config(image),
            backend=load_backend(args.route),
            run_root=Path(runs) if runs else None,
            upload=True,
            state_dir=Path(state) if state else None,
            benchmarks_root=REPO_ROOT / "benchmarks",
        )
    except Exception as exc:
        print(f"error: the run raised {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    upload = last_upload_line(result.run_dir)
    lines = summary_lines(result, upload, size(result.run_dir), account_name(url))
    print("\n".join(lines), file=out)
    uploaded = upload is not None and _UPLOAD_DONE.match(upload) is not None
    passed = (result.comparison or {}).get("passed") is True
    return 0 if result.status == "completed" and passed and uploaded else 1


if __name__ == "__main__":
    sys.exit(main())
