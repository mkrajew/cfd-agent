"""Safe access to the run directory.

The run directory is shared with executor containers that run agent-written code, so everything
in it is hostile input. ``RunDir`` is the only way the host (and the executor, for its own
``/job``) touches it:

* every path is run-directory-relative and checked: no absolute path, no ``..``, no backslash,
  no control character, and no symbolic link (or Windows junction) in any component;
* reads are size-limited, only regular files are opened (a FIFO would block, a device would
  never end) and the final component is opened with ``O_NOFOLLOW`` where the platform has it;
* writes are atomic (temporary file in the same directory, then ``os.replace``);
* walking and promoting skip symbolic links, FIFOs and devices and count what they skipped.

The checks run before the access, so a container that is still running could race them. The host
reads only after the container that wrote has exited, and ``O_NOFOLLOW`` closes the remaining
window for the final component.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from cfdagent.errors import AgentInputError

DEFAULT_READ_BYTES = 1_000_000
DEFAULT_WRITE_BYTES = 2_000_000
MAX_REL_CHARS = 1024
MAX_COMPONENT_CHARS = 255

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_STAGE = re.compile(r"[A-Za-z0-9_]{1,40}")


class RunDirError(AgentInputError):
    """Something in the run directory is not what the run needs (content an agent produced)."""


class UnsafePath(RunDirError):
    """A path escapes the run directory, is malformed, or goes through a symbolic link."""


class FileTooLarge(RunDirError):
    """A file or text exceeds the size limit of the access."""


def split_relative(rel: object) -> list[str]:
    """Check a run-directory-relative path and return its components (``.`` components dropped).

    Raises ``UnsafePath`` for anything that is not a plain relative POSIX-style path. This is the
    purely syntactic part of ``RunDir.path``; it needs no file system and is also used to validate
    op arguments before a container is started.
    """
    if not isinstance(rel, str):
        raise UnsafePath(f"a path must be a string, got {type(rel).__name__}")
    if not rel:
        raise UnsafePath("an empty path is not allowed; use '.' for the run directory itself")
    if len(rel) > MAX_REL_CHARS:
        raise UnsafePath(f"the path is longer than {MAX_REL_CHARS} characters")
    if _CONTROL.search(rel):
        raise UnsafePath("a path must not contain control characters")
    if "\\" in rel:
        raise UnsafePath(f"{rel!r}: use '/' as the path separator, not a backslash")
    if rel.startswith(("/", "~")):
        raise UnsafePath(f"{rel!r}: the path must be relative to the run directory")
    parts = rel.split("/")
    for part in parts:
        if part == "":
            raise UnsafePath(f"{rel!r}: empty path component (double or trailing slash)")
        if part == "..":
            raise UnsafePath(f"{rel!r}: '..' is not allowed; stay inside the run directory")
        if ":" in part:
            raise UnsafePath(f"{rel!r}: ':' is not allowed in a path component")
        if len(part) > MAX_COMPONENT_CHARS:
            raise UnsafePath(f"{rel!r}: a path component is longer than {MAX_COMPONENT_CHARS}")
    return [part for part in parts if part != "."]


def _is_link(path: Path) -> bool:
    """True for a symbolic link (also a dangling one) and for a Windows junction."""
    if os.path.islink(path):
        return True
    isjunction = getattr(os.path, "isjunction", None)
    return bool(isjunction and isjunction(path))


def _is_special_or_link(entry: os.DirEntry) -> bool:
    return entry.is_symlink() or bool(getattr(entry, "is_junction", lambda: False)())


class RunDir:
    """A run directory; see the module docstring. ``root`` is resolved once, at construction.

    ``skipped`` and ``skipped_paths`` describe what the last ``iter_files`` or ``promote`` left
    out (symbolic links, FIFOs, devices, unreadable entries); ``skipped_paths`` are relative.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()
        self.skipped = 0
        self.skipped_paths: list[str] = []

    # ------------------------------------------------------------------ paths

    def path(self, rel: str) -> Path:
        """The absolute path of ``rel`` inside the run directory.

        Raises ``UnsafePath`` if ``rel`` is malformed or escapes, or if any existing component
        (the last included) is a symbolic link or junction. The path need not exist.
        """
        current = self.root
        for part in split_relative(rel):
            current = current / part
            if _is_link(current):
                raise UnsafePath(
                    f"{rel!r}: {part!r} is a symbolic link; links are not allowed in the "
                    "run directory"
                )
        return current

    def relative(self, path: Path) -> str:
        """``path`` (absolute, inside the run directory) as a '/'-separated relative string."""
        try:
            rel = Path(path).relative_to(self.root)
        except ValueError:
            raise UnsafePath(f"{str(path)!r} is not inside the run directory") from None
        return rel.as_posix() or "."

    # ------------------------------------------------------------------ reading

    def _read_bytes(self, rel: str, max_bytes: int) -> bytes:
        path = self.path(rel)
        info = os.lstat(path)  # FileNotFoundError for a missing file
        if not stat.S_ISREG(info.st_mode):
            raise RunDirError(f"{rel!r} is not a regular file")
        if info.st_size > max_bytes:
            raise FileTooLarge(f"{rel!r} is {info.st_size} bytes; the limit is {max_bytes}")
        flags = os.O_RDONLY
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(path, flags)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise RunDirError(f"{rel!r} is not a regular file")
            with os.fdopen(fd, "rb", closefd=False) as handle:
                data = handle.read(max_bytes + 1)
        finally:
            os.close(fd)
        if len(data) > max_bytes:
            raise FileTooLarge(f"{rel!r} is larger than the limit of {max_bytes} bytes")
        return data

    def read_text(self, rel: str, max_bytes: int = DEFAULT_READ_BYTES) -> str:
        """Read a regular file as UTF-8 text (undecodable bytes become U+FFFD)."""
        return self._read_bytes(rel, max_bytes).decode("utf-8", errors="replace")

    def read_json(self, rel: str, max_bytes: int = DEFAULT_READ_BYTES) -> Any:
        """Read and parse a JSON file; a malformed or pathological document is a ``RunDirError``."""
        text = self.read_text(rel, max_bytes)
        try:
            return json.loads(text)
        except (ValueError, RecursionError) as exc:
            raise RunDirError(f"{rel!r} is not valid JSON ({type(exc).__name__})") from None

    # ------------------------------------------------------------------ writing

    def write_text(self, rel: str, text: str, max_bytes: int = DEFAULT_WRITE_BYTES) -> None:
        """Write ``text`` atomically, creating missing parent directories."""
        data = text.encode("utf-8")
        if len(data) > max_bytes:
            raise FileTooLarge(f"text for {rel!r} is {len(data)} bytes; the limit is {max_bytes}")
        target = self.path(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        # A parent created just now cannot be a link; one that existed was checked by path().
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            with open(temporary, "xb") as handle:
                handle.write(data)
            os.replace(temporary, target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    # ------------------------------------------------------------------ walking

    def _skip(self, path: Path) -> None:
        self.skipped += 1
        try:
            self.skipped_paths.append(path.relative_to(self.root).as_posix())
        except ValueError:  # pragma: no cover - paths come from walking the root
            self.skipped_paths.append(str(path))

    def _walk(self, directory: Path) -> Iterator[Path]:
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError:
            self._skip(directory)
            return
        for entry in entries:
            path = Path(entry.path)
            try:
                if _is_special_or_link(entry):
                    self._skip(path)
                elif entry.is_dir(follow_symlinks=False):
                    yield from self._walk(path)
                elif entry.is_file(follow_symlinks=False):
                    yield path
                else:  # FIFO, socket, device
                    self._skip(path)
            except OSError:
                self._skip(path)

    def iter_files(self) -> Iterator[Path]:
        """Every regular file under the run directory (absolute paths), in sorted order.

        Symbolic links, junctions, FIFOs, sockets and devices are skipped, never followed or
        opened; they are counted in ``skipped`` (listed in ``skipped_paths``).
        """
        self.skipped = 0
        self.skipped_paths = []
        yield from self._walk(self.root)

    # ------------------------------------------------------------------ attempts

    def attempt_dir(self, stage: str, attempt: int) -> Path:
        """A fresh, empty ``attempts/<stage>-<attempt>/``; an older one of that name is deleted."""
        if not isinstance(stage, str) or not _STAGE.fullmatch(stage):
            raise UnsafePath(f"stage name {stage!r} must be 1-40 letters, digits or underscores")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise UnsafePath(f"attempt must be an integer of at least 1, got {attempt!r}")
        path = self.path(f"attempts/{stage}-{attempt}")
        if path.is_dir() and not _is_link(path):
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
        path.mkdir(parents=True)
        return path

    def _copy_tree(self, source: Path, target: Path) -> None:
        target.mkdir()
        for entry in sorted(os.scandir(source), key=lambda e: e.name):
            path = Path(entry.path)
            try:
                if _is_special_or_link(entry):
                    self._skip(path)
                elif entry.is_dir(follow_symlinks=False):
                    self._copy_tree(path, target / entry.name)
                elif entry.is_file(follow_symlinks=False):
                    shutil.copyfile(path, target / entry.name, follow_symlinks=False)
                else:
                    self._skip(path)
            except OSError:
                self._skip(path)

    def promote(self, attempt_dir: Path | str, canonical: str) -> None:
        """Copy an attempt directory to ``canonical`` (replacing it), without any link or device.

        Only regular files and directories are copied; everything else is skipped and counted in
        ``skipped``. The copy is built next to the target and swapped in, so a failure leaves the
        previous ``canonical`` as it was.
        """
        source_rel = self.relative(Path(attempt_dir))
        source = self.path(source_rel)
        target = self.path(canonical)
        if target == self.root or source == target or source.is_relative_to(target):
            raise UnsafePath(f"cannot promote {source_rel!r} to {canonical!r}")
        if target.is_relative_to(source):
            raise UnsafePath(f"cannot promote {source_rel!r} into itself ({canonical!r})")
        if not source.is_dir():
            raise RunDirError(f"{source_rel!r} is not a directory")
        self.skipped = 0
        self.skipped_paths = []
        target.parent.mkdir(parents=True, exist_ok=True)
        tag = uuid.uuid4().hex[:8]
        staging = target.with_name(f".{target.name}.{tag}.new")
        retired = target.with_name(f".{target.name}.{tag}.old")
        swapped = False
        try:
            self._copy_tree(source, staging)
            if target.exists():
                os.replace(target, retired)
                swapped = True
            try:
                os.replace(staging, target)
            except BaseException:
                if swapped:
                    os.replace(retired, target)  # put the previous version back
                raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            if retired.is_dir():
                shutil.rmtree(retired, ignore_errors=True)
            else:
                retired.unlink(missing_ok=True)
