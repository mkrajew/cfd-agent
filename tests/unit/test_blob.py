"""Blob upload of a run directory, with a fake container client (no network, no Azure)."""

import io
import os
import shutil
import subprocess
import sys
import tarfile

import pytest
import zstandard

from cfdagent.rundir import RunDir
from cfdagent.storage import blob
from cfdagent.storage.blob import MAX_BLOB_BYTES, UploadReport, upload_run

RUN_ID = "20261112-143005-demo"
INF = float("inf")


class FakeContainer:
    """Records uploads; ``failures`` maps a blob name to how many calls fail before success."""

    def __init__(self, failures=None):
        self.blobs: dict[str, bytes] = {}
        self.calls: list[str] = []
        self.failures = dict(failures or {})

    def upload_blob(self, name, data, overwrite=False, **kwargs):
        self.calls.append(name)
        remaining = self.failures.get(name, 0)
        if remaining:
            self.failures[name] = remaining - 1
            raise ConnectionError("simulated failure")
        assert overwrite is True
        self.blobs[name] = data if isinstance(data, bytes) else data.read()


def make_link(link, target, *, directory=False) -> bool:
    """A symlink (a junction for a directory on Windows); False where none can be created."""
    try:
        os.symlink(target, link, target_is_directory=directory)
        return True
    except (OSError, NotImplementedError):
        pass
    if directory and sys.platform == "win32":
        done = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True
        )
        return done.returncode == 0
    return False


@pytest.fixture
def run(tmp_path):
    root = tmp_path / RUN_ID
    (root / "case" / "system").mkdir(parents=True)
    (root / "case" / "system" / "controlDict").write_bytes(b"application solver;\n")
    (root / "case" / "0").mkdir()
    (root / "case" / "0" / "U").write_bytes(b"internalField uniform (0 0 0);\n")
    (root / "geometry").mkdir()
    (root / "geometry" / "geometry.step").write_bytes(b"STEP")
    (root / "task.md").write_text("task\n")
    (root / "run_record.json").write_text("{}")
    return RunDir(root)


def upload(run, container, **kwargs):
    kwargs.setdefault("sleep", lambda seconds: None)
    return upload_run(run, "https://account.invalid", container_client=container, **kwargs)


def test_upload_names_files_under_run_id(run):
    container = FakeContainer()
    report = upload(run, container)
    assert isinstance(report, UploadReport)
    assert set(container.blobs) == {
        f"{RUN_ID}/task.md",
        f"{RUN_ID}/run_record.json",
        f"{RUN_ID}/geometry/geometry.step",
        f"{RUN_ID}/case.tar.zst",
    }
    assert container.blobs[f"{RUN_ID}/geometry/geometry.step"] == b"STEP"
    assert report.uploaded == 4
    assert report.skipped == [] and report.failed == []
    assert report.bytes == sum(len(v) for v in container.blobs.values())


def test_upload_archives_case_as_zstd(run):
    made = make_link(run.root / "case" / "link", run.root / "task.md")
    container = FakeContainer()
    report = upload(run, container)
    assert (report.skipped == ["case/link"]) == made
    assert not any("/case/" in name for name in container.blobs)  # no loose case files
    archive = container.blobs[f"{RUN_ID}/case.tar.zst"]
    assert archive[:4] == bytes.fromhex("28b52ffd")  # zstd frame magic
    stream = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(archive))
    with tarfile.open(fileobj=stream, mode="r|") as tar:
        members = {}
        for member in tar:
            assert member.isfile()  # no link, directory or device entries
            members[member.name] = tar.extractfile(member).read()
    assert members == {
        "case/system/controlDict": b"application solver;\n",
        "case/0/U": b"internalField uniform (0 0 0);\n",
    }


def test_upload_skips_symlink_and_oversize(run):
    made = make_link(run.root / "evil", run.root / "task.md")
    (run.root / "big.bin").write_bytes(b"x" * 2000)
    container = FakeContainer()
    report = upload(run, container, max_file_bytes=1000)
    assert f"{RUN_ID}/big.bin" not in container.blobs
    assert f"{RUN_ID}/evil" not in container.blobs
    assert sorted(report.skipped) == (["big.bin", "evil"] if made else ["big.bin"])
    assert report.failed == []
    assert f"{RUN_ID}/task.md" in container.blobs
    assert MAX_BLOB_BYTES == 100 * 1024 * 1024


def test_upload_skips_symlinked_case_directory(run, tmp_path):
    real = tmp_path / "elsewhere"
    real.mkdir()
    (real / "secret").write_text("do not upload")
    shutil.rmtree(run.root / "case")
    if not make_link(run.root / "case", real, directory=True):
        pytest.skip("this platform or user cannot create symlinks or junctions")
    container = FakeContainer()
    report = upload(run, container)
    assert "case" in report.skipped
    assert not any("case" in name or "secret" in name for name in container.blobs)


def test_oversize_archive_is_skipped_and_listed(run):
    # Incompressible content, so the compressed archive stays above the cap.
    (run.root / "case" / "noise").write_bytes(os.urandom(5000))
    container = FakeContainer()
    report = upload(run, container, max_file_bytes=1000)
    assert f"{RUN_ID}/case.tar.zst" not in container.blobs
    assert "case.tar.zst" in report.skipped
    assert report.failed == []


def test_upload_reports_failures_without_raising(run):
    delays = []
    flaky = f"{RUN_ID}/task.md"
    dead = f"{RUN_ID}/geometry/geometry.step"
    container = FakeContainer(failures={flaky: 2, dead: INF})
    report = upload(run, container, sleep=delays.append)
    assert report.failed == ["geometry/geometry.step"]
    assert container.calls.count(flaky) == 3  # two failures, then success
    assert flaky in container.blobs
    assert container.calls.count(dead) == 3  # three tries, then given up
    assert dead not in container.blobs
    assert report.uploaded == 3  # task.md, run_record.json, case.tar.zst
    assert len(delays) == 4 and all(0 < d <= 5 for d in delays)  # no sleep after the last try


def test_failed_archive_upload_is_reported(run):
    container = FakeContainer(failures={f"{RUN_ID}/case.tar.zst": INF})
    report = upload(run, container)
    assert report.failed == ["case.tar.zst"]


def test_unexpected_error_while_archiving_does_not_raise(run, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("compressor exploded")

    monkeypatch.setattr(blob.zstandard, "ZstdCompressor", boom)
    report = upload(run, FakeContainer())
    assert report.failed == ["case.tar.zst"]
    assert report.uploaded == 3


def test_client_creation_failure_does_not_raise(run, monkeypatch):
    def boom(*args, **kwargs):
        raise ValueError("bad account url")

    monkeypatch.setattr(blob, "ContainerClient", boom)
    monkeypatch.setattr(blob, "DefaultAzureCredential", lambda: object())
    report = upload_run(run, "not a url", sleep=lambda seconds: None)
    assert report.uploaded == 0
    assert report.failed
    assert "not a url" not in " ".join(report.failed)


def test_default_credential_and_container_name(run, monkeypatch):
    created = {}
    credential = object()

    def fake_client(account_url, container_name, credential=None, **kwargs):
        created.update(url=account_url, container=container_name, credential=credential)
        return FakeContainer()

    monkeypatch.setattr(blob, "ContainerClient", fake_client)
    monkeypatch.setattr(blob, "DefaultAzureCredential", lambda: credential)
    report = upload_run(run, "https://account.invalid", sleep=lambda seconds: None)
    assert report.uploaded == 4
    assert created == {
        "url": "https://account.invalid",
        "container": "runs",
        "credential": credential,
    }
    own = object()
    upload_run(run, "https://account.invalid", credential=own, container="other")
    assert created["credential"] is own and created["container"] == "other"


def test_report_never_contains_the_account_url_or_exception_text(run):
    container = FakeContainer(failures={f"{RUN_ID}/task.md": INF})
    report = upload(run, container)
    assert "account" not in repr(report)
    assert "simulated" not in repr(report)
