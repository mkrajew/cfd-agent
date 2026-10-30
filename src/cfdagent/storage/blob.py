"""Upload of a run directory to Azure Blob Storage.

Layout (design 6.9): container ``runs``, one folder per run, ``<run-id>/<relative path>`` where
the run ID is the name of the run directory. ``case/`` is archived as ``case.tar.zst`` (the
OpenFOAM case holds thousands of small files); everything else is uploaded as it is.

The run directory is hostile territory (executor containers wrote into it), so the walk is the
``RunDir`` walk: only regular files, no link, FIFO or device is followed or opened, directories
nested too deep are skipped. A file that cannot be safely resolved or opened (an odd name, a
file swapped for a link after the listing, no permission) is skipped and listed, and the upload
goes on. A file over the size cap (measured on the opened file) is skipped and listed. The
archive is compressed as a stream into a temporary file outside the run directory and dropped
when it grows beyond the cap.

``upload_run`` never raises (except for KeyboardInterrupt and the like): it is called from a
``finally`` after a failed run, and what was produced so far should still reach the storage.
Only storage errors are retried: each blob is tried three times with a short backoff, local
errors are not. Five blobs failing in a row (a dead endpoint) or an overall deadline stop the
upload; everything not uploaded is then listed in ``failed``. The report carries only relative
paths and exception class names, never the account URL or SDK error text.
"""

from __future__ import annotations

import os
import re
import stat
import tarfile
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import IO, Any

import zstandard
from azure.identity import DefaultAzureCredential
from azure.storage.blob import ContainerClient

from cfdagent.rundir import RunDir, RunDirError

MAX_BLOB_BYTES = 100 * 1024 * 1024
ATTEMPTS = 3
BACKOFF_S = (0.5, 1.0)  # pause before the 2nd and the 3rd try
MAX_CONSECUTIVE_FAILURES = 5
DEADLINE_S = 600.0
CONNECTION_TIMEOUT_S = 20
READ_TIMEOUT_S = 120
ARCHIVE_NAME = "case.tar.zst"
CASE_DIR = "case"
_COMPRESSION_LEVEL = 3
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


@dataclass
class UploadReport:
    """What happened to the files of one run; paths are relative to the run directory.

    ``reasons`` counts failed storage calls by exception class name, plus ``CircuitBreaker`` or
    ``DeadlineExceeded`` (number of blobs not attempted) when ``stopped_early``.
    """

    uploaded: int = 0
    skipped: list[str] = field(default_factory=list)
    bytes: int = 0
    failed: list[str] = field(default_factory=list)
    reasons: dict[str, int] = field(default_factory=dict)
    stopped_early: bool = False


def display(rel: str) -> str:
    """A path safe to print and to keep in a report (no control characters, no lone surrogates)."""
    text = rel.encode("utf-8", "backslashreplace").decode("utf-8")
    return _CONTROL.sub(lambda m: f"\\x{ord(m.group()):02x}", text)


class _TooLarge(Exception):
    """The compressed archive grew beyond the size cap."""


class _CappedSink:
    """A write-only wrapper that refuses to take more than ``cap`` bytes."""

    def __init__(self, sink: IO[bytes], cap: int) -> None:
        self._sink = sink
        self._cap = cap
        self.size = 0

    def write(self, data: bytes) -> int:
        self.size += len(data)
        if self.size > self._cap:
            raise _TooLarge
        return self._sink.write(data)

    def flush(self) -> None:
        self._sink.flush()


def _open_regular(path: str) -> tuple[IO[bytes], int]:
    """Open a regular file without following a final link; returns the file and its size."""
    if not stat.S_ISREG(os.lstat(path).st_mode):
        raise OSError("not a regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("not a regular file")
        return os.fdopen(fd, "rb"), info.st_size
    except BaseException:
        os.close(fd)
        raise


def _open_in_run(run: RunDir, rel: str) -> tuple[IO[bytes], int]:
    """``_open_regular`` for a run-relative path that ``RunDir`` accepts (no link on the way)."""
    return _open_regular(str(run.path(rel)))


@dataclass
class _State:
    """Everything one upload shares: client, clock, limits, report and the failure streak."""

    client: Any
    sleep: Callable[[float], None]
    clock: Callable[[], float]
    deadline_at: float
    cap: int
    report: UploadReport
    streak: int = 0
    stop_reason: str | None = None

    def should_stop(self) -> bool:
        if self.stop_reason is None:
            if self.streak >= MAX_CONSECUTIVE_FAILURES:
                self.stop_reason = "CircuitBreaker"
            elif self.clock() >= self.deadline_at:
                self.stop_reason = "DeadlineExceeded"
        return self.stop_reason is not None

    def count(self, reason: str, n: int = 1) -> None:
        self.report.reasons[reason] = self.report.reasons.get(reason, 0) + n

    def give_up(self, names: list[str]) -> None:
        """List ``names`` as failed because the upload stopped early."""
        self.report.stopped_early = True
        self.report.failed.extend(names)
        self.count(self.stop_reason or "Stopped", len(names))

    def put(
        self, blob_name: str, open_source: Callable[[], tuple[IO[bytes], int]]
    ) -> tuple[str, int]:
        """Upload one blob; returns ``(outcome, size)``.

        Outcomes: ``ok``; ``skipped`` (cannot be opened safely, or over the cap: not retried);
        ``failed`` (storage errors on every try); ``stopped`` (circuit breaker or deadline).
        Only exceptions of the storage call are retried and counted.
        """
        for attempt in range(ATTEMPTS):
            if self.should_stop():
                return "stopped", 0
            try:
                handle, size = open_source()
            except (RunDirError, OSError):
                return "skipped", 0
            with handle:
                if size > self.cap:
                    return "skipped", 0
                try:
                    self.client.upload_blob(blob_name, handle, overwrite=True, length=size)
                    return "ok", size
                except Exception as exc:  # any SDK or network error is a failed try
                    self.count(type(exc).__name__)
            if attempt < ATTEMPTS - 1:
                self.sleep(BACKOFF_S[attempt])
        return "failed", 0


def _build_archive(
    run: RunDir, case_files: list[str], cap: int, report: UploadReport
) -> IO[bytes] | None:
    """Compress the case files into a temporary file.

    Returns None if no file could be added; raises ``_TooLarge`` if the compressed archive
    exceeds ``cap``. A case file that cannot be opened safely is skipped (and listed) before
    anything of it is written. The temporary file is positioned at the start; the caller closes
    it.
    """
    temporary = tempfile.TemporaryFile()
    try:
        sink = _CappedSink(temporary, cap)
        writer = zstandard.ZstdCompressor(level=_COMPRESSION_LEVEL).stream_writer(
            sink, closefd=False
        )
        members = 0
        with tarfile.open(fileobj=writer, mode="w|") as tar:
            for rel in case_files:
                try:
                    handle, size = _open_in_run(run, rel)
                except (RunDirError, OSError):
                    report.skipped.append(display(rel))
                    continue
                with handle:
                    info = tarfile.TarInfo(rel)
                    info.size = size
                    info.mode = 0o644
                    info.mtime = int(os.fstat(handle.fileno()).st_mtime)
                    tar.addfile(info, handle)
                    members += 1
        writer.close()
        if members == 0:
            temporary.close()
            return None
        temporary.seek(0)
        return temporary
    except BaseException:
        temporary.close()
        raise


def upload_run(
    run: RunDir,
    account_url: str,
    credential: Any = None,
    container: str = "runs",
    *,
    container_client: Any = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    deadline_s: float = DEADLINE_S,
    max_file_bytes: int = MAX_BLOB_BYTES,
) -> UploadReport:
    """Upload ``run`` to ``<container>/<run-id>/``; see the module docstring.

    ``credential`` defaults to ``DefaultAzureCredential`` (managed identity on the VM, the
    developer's login elsewhere). The client is built with a single SDK retry and short network
    timeouts, so that our own retry loop is the only multiplier. ``container_client``,
    ``sleep``, ``clock``, ``deadline_s`` and ``max_file_bytes`` exist for tests.
    """
    report = UploadReport()
    try:
        client = container_client
        if client is None:
            try:
                if credential is None:
                    credential = DefaultAzureCredential()
                client = ContainerClient(
                    account_url,
                    container,
                    credential=credential,
                    retry_total=1,
                    connection_timeout=CONNECTION_TIMEOUT_S,
                    read_timeout=READ_TIMEOUT_S,
                )
            except Exception as exc:
                report.failed.append("(storage client)")
                report.reasons[type(exc).__name__] = 1
                return report
        state = _State(client, sleep, clock, clock() + deadline_s, max_file_bytes, report)
        _upload(run, state)
    except Exception as exc:  # the contract is: report, never raise
        report.failed.append("(upload)")
        report.reasons[type(exc).__name__] = report.reasons.get(type(exc).__name__, 0) + 1
    return report


def _upload(run: RunDir, state: _State) -> None:
    report = state.report
    run_id = run.root.name
    files = [run.relative(path) for path in run.iter_files()]
    report.skipped.extend(display(rel) for rel in run.skipped_paths)  # links, FIFOs, too deep
    case_prefix = f"{CASE_DIR}/"
    case_files = [rel for rel in files if rel.startswith(case_prefix)]
    loose_files = [rel for rel in files if not rel.startswith(case_prefix)]

    for index, rel in enumerate(loose_files):
        outcome, size = state.put(f"{run_id}/{rel}", lambda rel=rel: _open_in_run(run, rel))
        if outcome == "ok":
            report.uploaded += 1
            report.bytes += size
            state.streak = 0
        elif outcome == "skipped":
            report.skipped.append(display(rel))
        elif outcome == "failed":
            report.failed.append(display(rel))
            state.streak += 1
        else:  # stopped: this file and everything after it
            rest = [display(r) for r in loose_files[index:]]
            state.give_up(rest + ([ARCHIVE_NAME] if case_files else []))
            return

    if case_files:
        _upload_case(run, state, case_files)


def _upload_case(run: RunDir, state: _State, case_files: list[str]) -> None:
    report = state.report
    if state.should_stop():
        state.give_up([ARCHIVE_NAME])
        return
    try:
        archive = _build_archive(run, case_files, state.cap, report)
    except _TooLarge:
        report.skipped.append(ARCHIVE_NAME)
        return
    except Exception as exc:  # a half-written archive is useless; report and go on
        report.failed.append(ARCHIVE_NAME)
        state.count(type(exc).__name__)
        return
    if archive is None:
        return  # no readable file in case/: nothing to archive
    with archive:

        def open_archive() -> tuple[IO[bytes], int]:
            archive.seek(0)
            return _Borrowed(archive), os.fstat(archive.fileno()).st_size

        outcome, size = state.put(f"{run.root.name}/{ARCHIVE_NAME}", open_archive)
    if outcome == "ok":
        report.uploaded += 1
        report.bytes += size
    elif outcome == "skipped":
        report.skipped.append(ARCHIVE_NAME)
    elif outcome == "failed":
        report.failed.append(ARCHIVE_NAME)
    else:
        state.give_up([ARCHIVE_NAME])


class _Borrowed:
    """A file object that ``with`` does not close, so the archive survives a retry."""

    def __init__(self, handle: IO[bytes]) -> None:
        self._handle = handle

    def read(self, size: int = -1) -> bytes:
        return self._handle.read(size)

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._handle.seek(offset, whence)

    def tell(self) -> int:
        return self._handle.tell()

    def __enter__(self) -> _Borrowed:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None
