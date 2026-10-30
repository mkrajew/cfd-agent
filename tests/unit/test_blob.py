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
        self.body_types: dict[str, type] = {}
        self.failures = dict(failures or {})

    def upload_blob(self, name, data, overwrite=False, **kwargs):
        self.calls.append(name)
        remaining = self.failures.get(name, 0)
        if remaining:
            self.failures[name] = remaining - 1
            raise ConnectionError("simulated failure")
        assert overwrite is True
        self.body_types[name] = type(data)
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
        created.update(
            url=account_url, container=container_name, credential=credential, options=kwargs
        )
        return FakeContainer()

    monkeypatch.setattr(blob, "ContainerClient", fake_client)
    monkeypatch.setattr(blob, "DefaultAzureCredential", lambda: credential)
    report = upload_run(run, "https://account.invalid", sleep=lambda seconds: None)
    assert report.uploaded == 4
    options = created.pop("options")
    assert created == {
        "url": "https://account.invalid",
        "container": "runs",
        "credential": credential,
    }
    assert options["retry_total"] == 1  # our own retry loop is the only multiplier
    assert 0 < options["connection_timeout"] <= 30 and 0 < options["read_timeout"] <= 300
    own = object()
    upload_run(run, "https://account.invalid", credential=own, container="other")
    assert created["credential"] is own and created["container"] == "other"


def test_report_never_contains_the_account_url_or_exception_text(run):
    container = FakeContainer(failures={f"{RUN_ID}/task.md": INF})
    report = upload(run, container)
    assert "account" not in repr(report)
    assert "simulated" not in repr(report)


# --------------------------------------------------------------------------- fix round 1


def write_or_skip(path, data=b"data"):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except (OSError, ValueError):
        pytest.skip("this file system cannot hold that file name")


def decode_archive(blob_bytes) -> dict[str, bytes]:
    stream = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(blob_bytes))
    with tarfile.open(fileobj=stream, mode="r|") as tar:
        return {m.name: tar.extractfile(m).read() for m in tar if m.isfile()}


ODD_LOOSE_NAMES = ["m:n.txt", "back\\slash.txt", "new\nline.txt", "~lead.txt", "tab\there.txt"]
ALL_BLOBS = {
    f"{RUN_ID}/task.md",
    f"{RUN_ID}/run_record.json",
    f"{RUN_ID}/geometry/geometry.step",
    f"{RUN_ID}/case.tar.zst",
}


@pytest.mark.parametrize("name", ODD_LOOSE_NAMES)
def test_odd_loose_file_name_is_skipped_and_the_rest_is_uploaded(run, name):
    if sys.platform == "win32" and "\\" in name:
        pytest.skip("a backslash is a path separator on Windows")
    write_or_skip(run.root / name)
    container = FakeContainer()
    report = upload(run, container)
    assert report.failed == []
    assert len(report.skipped) == 1
    assert "\n" not in report.skipped[0] and "\t" not in report.skipped[0]
    assert set(container.blobs) == ALL_BLOBS


def test_odd_case_file_name_is_skipped_and_the_archive_is_still_uploaded(run):
    write_or_skip(run.root / "case" / "0" / "p:q")
    container = FakeContainer()
    report = upload(run, container)
    assert report.skipped == ["case/0/p:q"] and report.failed == []
    members = decode_archive(container.blobs[f"{RUN_ID}/case.tar.zst"])
    assert set(members) == {"case/system/controlDict", "case/0/U"}
    assert f"{RUN_ID}/task.md" in container.blobs


def test_over_long_path_is_skipped(run):
    deep = run.root.joinpath(*(["d" * 200] * 6))  # 1200+ characters of relative path
    write_or_skip(deep / "file.txt")
    container = FakeContainer()
    report = upload(run, container)
    assert len(report.skipped) == 1 and report.failed == []
    assert set(container.blobs) == ALL_BLOBS


def swap_after_listing(run, victim, make_replacement):
    """Make ``run.iter_files`` replace ``victim`` once the listing is complete."""
    original = run.iter_files

    def listing():
        yield from original()
        (run.root / victim).unlink()
        make_replacement(run.root / victim)

    run.iter_files = listing


def link_to_task(run):
    def replace(path):
        if not make_link(path, run.root / "task.md"):
            pytest.skip("this platform or user cannot create symlinks")

    return replace


def test_loose_file_swapped_for_a_symlink_after_listing_is_skipped(run):
    swap_after_listing(run, "geometry/geometry.step", link_to_task(run))
    container = FakeContainer()
    report = upload(run, container)
    assert report.skipped == ["geometry/geometry.step"] and report.failed == []
    assert set(container.blobs) == ALL_BLOBS - {f"{RUN_ID}/geometry/geometry.step"}


def test_case_file_swapped_for_a_symlink_after_listing_is_left_out_of_the_archive(run):
    swap_after_listing(run, "case/0/U", link_to_task(run))
    container = FakeContainer()
    report = upload(run, container)
    assert report.skipped == ["case/0/U"] and report.failed == []
    members = decode_archive(container.blobs[f"{RUN_ID}/case.tar.zst"])
    assert set(members) == {"case/system/controlDict"}


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this platform")
def test_file_swapped_for_a_fifo_after_listing_is_skipped_without_blocking(run):
    swap_after_listing(run, "task.md", lambda path: os.mkfifo(path))
    container = FakeContainer()
    report = upload(run, container)
    assert report.skipped == ["task.md"] and report.failed == []
    assert f"{RUN_ID}/run_record.json" in container.blobs


def test_path_resolving_to_a_symlink_is_never_followed(run, monkeypatch):
    link = run.root.parent / "linkdir"
    if not make_link(link, run.root / "task.md"):
        pytest.skip("this platform or user cannot create symlinks")
    real_path = run.path

    def tricked(rel):
        return link if rel in ("geometry/geometry.step", "case/0/U") else real_path(rel)

    monkeypatch.setattr(run, "path", tricked)
    container = FakeContainer()
    report = upload(run, container)
    assert sorted(report.skipped) == ["case/0/U", "geometry/geometry.step"]
    assert f"{RUN_ID}/geometry/geometry.step" not in container.blobs
    members = decode_archive(container.blobs[f"{RUN_ID}/case.tar.zst"])
    assert set(members) == {"case/system/controlDict"}


def test_absent_or_empty_case_directory_gives_no_archive_and_no_failure(run):
    shutil.rmtree(run.root / "case")
    container = FakeContainer()
    report = upload(run, container)
    assert report.failed == [] and report.skipped == []
    assert f"{RUN_ID}/case.tar.zst" not in container.blobs
    (run.root / "case").mkdir()
    container = FakeContainer()
    report = upload(run, container)
    assert report.failed == [] and report.skipped == []
    assert f"{RUN_ID}/case.tar.zst" not in container.blobs


def test_keyboard_interrupt_propagates(run):
    class Interrupting(FakeContainer):
        def upload_blob(self, name, data, **kwargs):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        upload(run, Interrupting())


def test_upload_blob_receives_a_file_object_not_bytes(run):
    container = FakeContainer()
    upload(run, container)
    assert container.body_types
    assert all(kind is not bytes for kind in container.body_types.values())


def test_local_errors_are_not_retried(run, monkeypatch):
    delays = []
    real_open = blob._open_regular

    def refuse(path):
        if path.endswith("task.md"):
            raise PermissionError("denied")
        return real_open(path)

    monkeypatch.setattr(blob, "_open_regular", refuse)
    container = FakeContainer()
    report = upload(run, container, sleep=delays.append)
    assert "task.md" in report.skipped and report.failed == []
    assert delays == [] and f"{RUN_ID}/task.md" not in container.calls


def many_files(run, count=10):
    for i in range(count):
        (run.root / f"f{i:02d}.txt").write_bytes(b"x")


def test_circuit_breaker_stops_after_five_consecutive_failures(run):
    many_files(run)

    class Dead(FakeContainer):
        def upload_blob(self, name, data, **kwargs):
            self.calls.append(name)
            raise ConnectionError("dead endpoint")

    container = Dead()
    report = upload(run, container, sleep=lambda seconds: None)
    assert report.stopped_early is True
    assert report.uploaded == 0
    assert len(container.calls) == 5 * 3  # five blobs, three tries each, then stop
    assert report.reasons["ConnectionError"] == 15
    assert report.reasons["CircuitBreaker"] == len(report.failed) - 5
    expected = {"task.md", "run_record.json", "geometry/geometry.step", "case.tar.zst"}
    expected |= {f"f{i:02d}.txt" for i in range(10)}
    assert set(report.failed) == expected  # nothing silently dropped
    assert "dead endpoint" not in repr(report)


def test_success_resets_the_failure_streak(run):
    many_files(run)
    failing = {f"{RUN_ID}/f{i:02d}.txt": INF for i in (0, 1, 2, 3, 5, 6, 7, 8)}
    report = upload(run, FakeContainer(failures=failing))
    assert report.stopped_early is False
    assert len(report.failed) == 8


def test_deadline_stops_the_upload_and_lists_the_rest(run):
    many_files(run)
    now = [0.0]

    class Slow(FakeContainer):
        def upload_blob(self, name, data, **kwargs):
            now[0] += 6.0
            return super().upload_blob(name, data, **kwargs)

    container = Slow()
    report = upload(run, container, deadline_s=10.0, clock=lambda: now[0])
    assert report.stopped_early is True
    assert report.uploaded == 2
    assert report.reasons["DeadlineExceeded"] == len(report.failed) > 0
    assert len(container.blobs) == 2
