"""RunDir: every access to the shared run directory is confined, size-limited and symlink-free.

The run directory is hostile territory: executor containers write into it. Symlink tests are
skipped where the platform cannot create symlinks (an unprivileged Windows user).
"""

import json
import os
import subprocess
import sys

import pytest

from cfdagent.errors import AgentInputError
from cfdagent.rundir import MAX_DEPTH, FileTooLarge, RunDir, RunDirError, UnsafePath

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="NTFS junctions")
needs_fifo = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this platform")


def _symlink(link, target, *, directory=False):
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError):
        pytest.skip("this platform or user cannot create symlinks")


@pytest.fixture
def run(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    return RunDir(root)


def test_unsafe_path_is_an_agent_input_error():
    assert issubclass(UnsafePath, AgentInputError)
    assert issubclass(FileTooLarge, AgentInputError)
    assert issubclass(RunDirError, AgentInputError)


def test_path_resolves_inside_root(run):
    assert run.path("a/b.txt") == run.root / "a" / "b.txt"
    assert run.path("./a/./b.txt") == run.root / "a" / "b.txt"
    assert run.path(".") == run.root


@pytest.mark.parametrize(
    "rel",
    [
        "",
        "/etc/passwd",
        "../outside",
        "a/../../outside",
        "a/..",
        "a//b",
        "C:/Windows",
        "C:\\Windows",
        "a\\b",
        "\\\\server\\share",
        "~/x",
        "a\x00b",
        "a\nb",
        "x" * 5000,
        "y" * 300,
    ],
)
def test_path_escape_refused(run, rel):
    with pytest.raises(UnsafePath):
        run.path(rel)


@pytest.mark.parametrize("rel", [None, 3, b"a", ["a"]])
def test_path_wrong_type_refused(run, rel):
    with pytest.raises(UnsafePath):
        run.path(rel)


def test_path_escape_and_symlink_component_refused(run, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    _symlink(run.root / "link", outside, directory=True)
    with pytest.raises(UnsafePath, match="symbolic link"):
        run.path("link/secret.txt")
    with pytest.raises(UnsafePath):
        run.path("link")
    with pytest.raises(UnsafePath):
        run.read_text("link/secret.txt")
    with pytest.raises(UnsafePath):
        run.write_text("link/new.txt", "x")
    assert not (outside / "new.txt").exists()


def test_symlink_to_a_file_is_refused_for_every_access(run, tmp_path):
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    _symlink(run.root / "file.txt", tmp_path / "secret.txt")
    with pytest.raises(UnsafePath):
        run.read_text("file.txt")
    with pytest.raises(UnsafePath):
        run.read_json("file.txt")
    with pytest.raises(UnsafePath):
        run.write_text("file.txt", "overwrite")
    assert (tmp_path / "secret.txt").read_text(encoding="utf-8") == "secret"


def test_dangling_symlink_is_refused(run, tmp_path):
    _symlink(run.root / "dangling", tmp_path / "does-not-exist")
    with pytest.raises(UnsafePath):
        run.path("dangling")


@windows_only
def test_junction_component_is_refused(run, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(run.root / "junction"), str(outside)],
        capture_output=True,
        text=True,
    )
    if made.returncode != 0:
        pytest.skip(f"cannot create a junction: {made.stdout}{made.stderr}")
    with pytest.raises(UnsafePath):
        run.read_text("junction/secret.txt")


def test_missing_file_is_a_plain_file_not_found(run):
    with pytest.raises(FileNotFoundError):
        run.read_text("nope.txt")
    assert run.path("not/yet/there.txt") == run.root / "not" / "yet" / "there.txt"


def test_read_text_enforces_max_bytes(run):
    (run.root / "big.txt").write_bytes(b"x" * 101)
    assert run.read_text("big.txt", max_bytes=101) == "x" * 101
    with pytest.raises(FileTooLarge, match="100"):
        run.read_text("big.txt", max_bytes=100)
    with pytest.raises(FileTooLarge):
        run.read_json("big.txt", max_bytes=100)


def test_read_text_default_limit_is_one_megabyte(run):
    (run.root / "big.txt").write_bytes(b"x" * 1_000_001)
    with pytest.raises(FileTooLarge):
        run.read_text("big.txt")


def test_read_text_of_a_directory_is_refused(run):
    (run.root / "d").mkdir()
    with pytest.raises(RunDirError, match="regular file"):
        run.read_text("d")


def test_read_text_replaces_invalid_utf8(run):
    (run.root / "bin.txt").write_bytes(b"ok \xff\xfe end")
    text = run.read_text("bin.txt")
    assert text.startswith("ok ") and text.endswith(" end")


@needs_fifo
def test_read_of_a_fifo_is_refused_and_does_not_block(run):
    os.mkfifo(run.root / "pipe")
    with pytest.raises(RunDirError, match="regular file"):
        run.read_text("pipe")


def test_read_json_roundtrip_and_hostile_documents(run):
    run.write_text("data.json", json.dumps({"a": [1, 2]}))
    assert run.read_json("data.json") == {"a": [1, 2]}
    (run.root / "bad.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(RunDirError, match="JSON"):
        run.read_json("bad.json")
    (run.root / "deep.json").write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")
    with pytest.raises(RunDirError, match="JSON"):
        run.read_json("deep.json", max_bytes=1_000_000)
    (run.root / "digits.json").write_text("1" * 5000, encoding="utf-8")
    with pytest.raises(RunDirError, match="JSON"):
        run.read_json("digits.json")


def test_write_text_is_atomic_creates_parents_and_limits_size(run):
    run.write_text("a/b/c.txt", "hello")
    assert (run.root / "a" / "b" / "c.txt").read_text(encoding="utf-8") == "hello"
    run.write_text("a/b/c.txt", "again")
    assert run.read_text("a/b/c.txt") == "again"
    leftovers = [p.name for p in (run.root / "a" / "b").iterdir()]
    assert leftovers == ["c.txt"]
    with pytest.raises(FileTooLarge):
        run.write_text("big.txt", "x" * 11, max_bytes=10)
    assert not (run.root / "big.txt").exists()
    with pytest.raises(UnsafePath):
        run.write_text("../escape.txt", "x")


def test_fifo_skipped_and_symlink_skipped_by_iter_files(run, tmp_path):
    (run.root / "keep.txt").write_text("k", encoding="utf-8")
    (run.root / "sub").mkdir()
    (run.root / "sub" / "deep.txt").write_text("d", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("o", encoding="utf-8")
    (tmp_path / "outdir").mkdir()
    (tmp_path / "outdir" / "inside_outdir.txt").write_text("i", encoding="utf-8")
    expected_skipped = 0
    if hasattr(os, "mkfifo"):
        os.mkfifo(run.root / "pipe")
        expected_skipped += 1
    try:
        os.symlink(tmp_path / "outside.txt", run.root / "link.txt")
        os.symlink(tmp_path / "outdir", run.root / "linkdir", target_is_directory=True)
        expected_skipped += 2
    except (OSError, NotImplementedError):
        pass
    if expected_skipped == 0:
        pytest.skip("neither FIFOs nor symlinks can be created here")
    found = sorted(p.relative_to(run.root).as_posix() for p in run.iter_files())
    assert found == ["keep.txt", "sub/deep.txt"]
    assert run.skipped == expected_skipped
    assert len(run.skipped_paths) == expected_skipped


def test_iter_files_yields_absolute_paths_under_root_and_resets_the_count(run):
    (run.root / "a.txt").write_text("a", encoding="utf-8")
    assert list(run.iter_files()) == [run.root / "a.txt"]
    assert run.skipped == 0


def test_attempt_dir_is_fresh_each_time(run):
    first = run.attempt_dir("geometry", 1)
    assert first == run.root / "attempts" / "geometry-1"
    assert first.is_dir()
    (first / "stale.txt").write_text("old", encoding="utf-8")
    second = run.attempt_dir("geometry", 1)
    assert second == first
    assert list(second.iterdir()) == []
    assert run.attempt_dir("geometry", 2) == run.root / "attempts" / "geometry-2"


@pytest.mark.parametrize(
    ("stage", "attempt"),
    [("../x", 1), ("a/b", 1), ("", 1), ("geometry", 0), ("geometry", -1), ("geometry", True)],
)
def test_attempt_dir_rejects_bad_arguments(run, stage, attempt):
    with pytest.raises(UnsafePath):
        run.attempt_dir(stage, attempt)


def test_attempt_dir_refuses_a_symlinked_attempts_folder(run, tmp_path):
    (tmp_path / "elsewhere").mkdir()
    _symlink(run.root / "attempts", tmp_path / "elsewhere", directory=True)
    with pytest.raises(UnsafePath):
        run.attempt_dir("geometry", 1)


def test_promote_copies_the_attempt_to_the_canonical_folder(run):
    attempt = run.attempt_dir("mesh", 1)
    (attempt / "mesh.py").write_text("print(1)", encoding="utf-8")
    (attempt / "case" / "system").mkdir(parents=True)
    (attempt / "case" / "system" / "controlDict").write_text("x", encoding="utf-8")
    run.promote(attempt, "mesh")
    assert (run.root / "mesh" / "mesh.py").read_text(encoding="utf-8") == "print(1)"
    assert (run.root / "mesh" / "case" / "system" / "controlDict").is_file()
    # A later promotion replaces the earlier one completely.
    attempt2 = run.attempt_dir("mesh", 2)
    (attempt2 / "other.txt").write_text("y", encoding="utf-8")
    run.promote(attempt2, "mesh")
    assert sorted(p.name for p in (run.root / "mesh").iterdir()) == ["other.txt"]


def test_promote_does_not_copy_symlinks(run, tmp_path):
    attempt = run.attempt_dir("mesh", 1)
    (attempt / "real.txt").write_text("r", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    (tmp_path / "secretdir").mkdir()
    (tmp_path / "secretdir" / "s.txt").write_text("s", encoding="utf-8")
    made = 0
    try:
        os.symlink(tmp_path / "secret.txt", attempt / "link.txt")
        os.symlink(tmp_path / "secretdir", attempt / "linkdir", target_is_directory=True)
        made = 2
    except (OSError, NotImplementedError):
        pass
    if hasattr(os, "mkfifo"):
        os.mkfifo(attempt / "pipe")
        made += 1
    if made == 0:
        pytest.skip("neither FIFOs nor symlinks can be created here")
    run.promote(attempt, "mesh")
    names = sorted(p.name for p in (run.root / "mesh").rglob("*"))
    assert names == ["real.txt"]
    assert run.skipped == made


def test_promote_refuses_unsafe_arguments(run, tmp_path):
    attempt = run.attempt_dir("mesh", 1)
    with pytest.raises(UnsafePath):
        run.promote(attempt, "../escape")
    with pytest.raises(UnsafePath):
        run.promote(tmp_path, "mesh")  # not inside the run directory
    with pytest.raises(UnsafePath):
        run.promote(attempt, "attempts/mesh-1/inner")  # into the source itself
    with pytest.raises(UnsafePath):
        run.promote(attempt, ".")  # would replace the whole run directory
    assert attempt.is_dir()


# --------------------------------------------------------------------------------------------
# Fix round 1: depth cap, no recursion, no host paths in messages
# --------------------------------------------------------------------------------------------

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="deep relative trees need POSIX")


def _deep_tree(top, depth):
    """Create top/d/d/d/... (depth levels) with relative mkdir, so path length never matters."""
    here = os.getcwd()
    os.chdir(top)
    try:
        for _ in range(depth):
            os.mkdir("d")
            os.chdir("d")
            with open("f.txt", "w", encoding="utf-8") as handle:
                handle.write("x")
    finally:
        os.chdir(here)


@posix_only
def test_iter_files_caps_the_depth_and_does_not_recurse(run):
    (run.root / "top.txt").write_text("t", encoding="utf-8")
    _deep_tree(run.root, 400)  # several times the cap
    found = list(run.iter_files())
    assert run.root / "top.txt" in found
    assert len(found) == 1 + MAX_DEPTH  # files down to the cap, nothing deeper
    assert run.skipped >= 1 and run.skipped_paths


@posix_only
def test_promote_caps_the_depth_and_does_not_recurse(run):
    attempt = run.attempt_dir("mesh", 1)
    (attempt / "top.txt").write_text("t", encoding="utf-8")
    _deep_tree(attempt, 400)
    run.promote(attempt, "mesh")
    copied = [p for p in (run.root / "mesh").rglob("f.txt")]
    assert 0 < len(copied) <= MAX_DEPTH
    assert (run.root / "mesh" / "top.txt").is_file()
    assert run.skipped >= 1


@posix_only
def test_attempt_dir_removes_a_very_deep_tree(run):
    first = run.attempt_dir("geometry", 1)
    _deep_tree(first, 500)
    second = run.attempt_dir("geometry", 1)
    assert list(second.iterdir()) == []


def test_residual_recursion_error_becomes_a_run_dir_error(run, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RecursionError

    monkeypatch.setattr(os, "scandir", boom)
    with pytest.raises(RunDirError, match="too deep"):
        list(run.iter_files())


def test_symlinked_attempt_directory_is_refused_and_not_deleted(run, tmp_path):
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "keep.txt").write_text("keep", encoding="utf-8")
    (run.root / "attempts").mkdir()
    _symlink(run.root / "attempts" / "geometry-1", precious, directory=True)
    with pytest.raises(UnsafePath):
        run.attempt_dir("geometry", 1)
    assert (precious / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_messages_do_not_carry_host_paths(run, tmp_path):
    host = str(run.root)
    with pytest.raises(FileNotFoundError) as missing:
        run.read_text("nope.txt")
    assert host not in str(missing.value) and "nope.txt" in str(missing.value)
    with pytest.raises(UnsafePath) as outside:
        run.relative(tmp_path / "elsewhere")
    assert str(tmp_path) not in str(outside.value)
    (run.root / "d").mkdir()
    with pytest.raises(RunDirError) as directory:
        run.read_text("d")
    assert host not in str(directory.value)
