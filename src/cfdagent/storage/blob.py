"""Upload of a run directory to Azure Blob Storage.

Layout (design 6.9): container ``runs``, one folder per run, ``<run-id>/<relative path>`` where
the run ID is the name of the run directory. ``case/`` is archived as ``case.tar.zst`` (the
OpenFOAM case holds thousands of small files); everything else is uploaded as it is.

The run directory is hostile territory (executor containers wrote into it), so the walk is the
``RunDir`` walk: only regular files, no link, FIFO or device is followed or opened, directories
nested too deep are skipped. A file over the size cap is skipped and listed. The archive is
compressed as a stream into a temporary file outside the run directory and dropped when it grows
beyond the cap.

``upload_run`` never raises: it is called from a ``finally`` after a failed run, and what was
produced so far should still reach the storage. Each blob is tried three times with a short
backoff; whatever still fails is listed in the report. The report and its messages carry only
relative paths and exception class names, never the account URL or SDK error text.
"""

from __future__ import annotations

import os
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

from cfdagent.rundir import RunDir

MAX_BLOB_BYTES = 100 * 1024 * 1024
ATTEMPTS = 3
BACKOFF_S = (0.5, 1.0)  # pause before the 2nd and the 3rd try
ARCHIVE_NAME = "case.tar.zst"
CASE_DIR = "case"
_COMPRESSION_LEVEL = 3


@dataclass
class UploadReport:
    """What happened to the files of one run; paths are relative to the run directory."""

    uploaded: int = 0
    skipped: list[str] = field(default_factory=list)
    bytes: int = 0
    failed: list[str] = field(default_factory=list)


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


def _with_retries(action: Callable[[], None], sleep: Callable[[float], None]) -> bool:
    """Run ``action`` up to ``ATTEMPTS`` times; True once it succeeds."""
    for attempt in range(ATTEMPTS):
        try:
            action()
            return True
        except Exception:  # any SDK or file error is a failed try, never a crash
            if attempt < ATTEMPTS - 1:
                sleep(BACKOFF_S[attempt])
    return False


def _build_archive(
    run: RunDir, case_files: list[str], cap: int, report: UploadReport
) -> IO[bytes] | None:
    """Compress the case files into a temporary file; None if it exceeds ``cap``.

    A case file that cannot be opened is skipped (and listed) before anything of it is written.
    The temporary file is positioned at the start; the caller closes it.
    """
    temporary = tempfile.TemporaryFile()
    try:
        sink = _CappedSink(temporary, cap)
        writer = zstandard.ZstdCompressor(level=_COMPRESSION_LEVEL).stream_writer(
            sink, closefd=False
        )
        with tarfile.open(fileobj=writer, mode="w|") as tar:
            for rel in case_files:
                try:
                    handle, size = _open_regular(str(run.path(rel)))
                except OSError:
                    report.skipped.append(rel)
                    continue
                with handle:
                    info = tarfile.TarInfo(rel)
                    info.size = size
                    info.mode = 0o644
                    info.mtime = int(os.fstat(handle.fileno()).st_mtime)
                    tar.addfile(info, handle)
        writer.close()
        temporary.seek(0)
        return temporary
    except _TooLarge:
        temporary.close()
        return None
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
    max_file_bytes: int = MAX_BLOB_BYTES,
) -> UploadReport:
    """Upload ``run`` to ``<container>/<run-id>/``; see the module docstring. Never raises.

    ``credential`` defaults to ``DefaultAzureCredential`` (managed identity on the VM, the
    developer's login elsewhere). ``container_client``, ``sleep`` and ``max_file_bytes`` exist
    for tests.
    """
    report = UploadReport()
    try:
        client = container_client
        if client is None:
            try:
                if credential is None:
                    credential = DefaultAzureCredential()
                client = ContainerClient(account_url, container, credential=credential)
            except Exception as exc:
                report.failed.append(f"(storage client) {type(exc).__name__}")
                return report
        _upload(run, client, sleep, max_file_bytes, report)
    except Exception as exc:  # the contract is: report, never raise
        report.failed.append(f"(upload) {type(exc).__name__}")
    return report


def _upload(
    run: RunDir,
    client: Any,
    sleep: Callable[[float], None],
    cap: int,
    report: UploadReport,
) -> None:
    run_id = run.root.name
    files = [run.relative(path) for path in run.iter_files()]
    report.skipped.extend(run.skipped_paths)  # links, FIFOs, devices, too deep
    case_prefix = f"{CASE_DIR}/"
    case_files = [rel for rel in files if rel.startswith(case_prefix)]
    loose_files = [rel for rel in files if not rel.startswith(case_prefix)]

    for rel in loose_files:
        path = str(run.path(rel))
        try:
            size = os.lstat(path).st_size
        except OSError:
            report.failed.append(rel)
            continue
        if size > cap:
            report.skipped.append(rel)
            continue

        def action(path: str = path, name: str = f"{run_id}/{rel}") -> None:
            handle, length = _open_regular(path)
            with handle:
                client.upload_blob(name, handle, overwrite=True, length=length)

        if _with_retries(action, sleep):
            report.uploaded += 1
            report.bytes += size
        else:
            report.failed.append(rel)

    if case_files:
        _upload_case(run, client, case_files, cap, sleep, report)


def _upload_case(
    run: RunDir,
    client: Any,
    case_files: list[str],
    cap: int,
    sleep: Callable[[float], None],
    report: UploadReport,
) -> None:
    try:
        archive = _build_archive(run, case_files, cap, report)
    except Exception:  # a half-written archive is useless; report and go on
        report.failed.append(ARCHIVE_NAME)
        return
    if archive is None:
        report.skipped.append(ARCHIVE_NAME)
        return
    with archive:
        size = os.fstat(archive.fileno()).st_size
        name = f"{run.root.name}/{ARCHIVE_NAME}"

        def action() -> None:
            archive.seek(0)
            client.upload_blob(name, archive, overwrite=True, length=size)

        if _with_retries(action, sleep):
            report.uploaded += 1
            report.bytes += size
        else:
            report.failed.append(ARCHIVE_NAME)
