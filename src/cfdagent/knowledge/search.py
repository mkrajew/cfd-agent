"""Lexical search over, and read access to, a tree of OpenFOAM tutorials.

The tree is ``$FOAM_TUTORIALS`` inside the image. It is read-only reference material, so nothing
here writes. Nothing from ``benchmarks/`` is ever part of it.

``search_tutorials`` is deliberately simple (no index, no embeddings, design 6.3): a file matches
a query when every term of the query occurs, case-insensitively, in its relative path (directory
and file names) or in its text. It looks at each regular file of at most ``MAX_FILE_BYTES`` bytes
that is not binary, and never follows a symbolic link. A match gives one ``Hit`` per file, ordered
by how many terms the path contains, then by how many terms the best line contains, then by path
and line, so the result is the same on every run.

``read_tutorial`` returns the text of one file below the root, or the listing of a directory. It
refuses a path that leaves the root (``..``, an absolute path elsewhere) and any path that goes
through a symbolic link.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path, PurePath
from typing import NamedTuple

from cfdagent.errors import AgentInputError, ToolFailure

MAX_FILE_BYTES = 200 * 1024  # larger files are not searched
MAX_FILES = 100_000  # a tutorial tree has a few thousand; stop at a runaway tree
MAX_QUERY_CHARS = 200
MAX_TERMS = 8
MAX_HIT_CHARS = 200
MAX_PATH_CHARS = 1024
MAX_LISTING_ENTRIES = 200
BINARY_PROBE_BYTES = 8192
NAME_ONLY_TEXT = "(the file name matches)"
_SEPARATORS = ("/", chr(92))


class Hit(NamedTuple):
    """One matching file. ``line`` is 1-based, or 0 when only the path matches."""

    path: str
    line: int
    text: str


def _is_link(path: str) -> bool:
    return os.path.islink(path) or os.path.isjunction(path)


def _root_dir(root: Path) -> Path:
    resolved = Path(root).resolve()
    if not resolved.is_dir():
        raise ToolFailure(f"the tutorial directory {str(root)!r} does not exist in this image")
    return resolved


def _terms(query: str) -> list[str]:
    if not isinstance(query, str):
        raise AgentInputError("the query must be text")
    if len(query) > MAX_QUERY_CHARS:
        raise AgentInputError(f"the query is longer than {MAX_QUERY_CHARS} characters")
    terms = list(dict.fromkeys(query.lower().split()))
    if not terms:
        raise AgentInputError("the query is empty; give one or more words to search for")
    if len(terms) > MAX_TERMS:
        raise AgentInputError(f"the query has more than {MAX_TERMS} words; use fewer, shorter ones")
    return terms


def _files(root: Path):
    """Relative POSIX path and absolute path of every regular file, in sorted order; links and
    special files are skipped, never followed."""
    stack = [root]
    count = 0
    while stack:
        directory = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError:
            continue
        subdirectories = []
        for entry in entries:
            try:
                if entry.is_symlink() or os.path.isjunction(entry.path):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    subdirectories.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    count += 1
                    if count > MAX_FILES:
                        return
                    yield Path(entry.path).relative_to(root).as_posix(), entry.path
            except OSError:
                continue
        stack.extend(reversed(subdirectories))


def _read_text(path: str) -> str | None:
    """The text of a small, non-binary file, or ``None``."""
    try:
        if os.stat(path).st_size > MAX_FILE_BYTES:
            return None
        with open(path, "rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
    except OSError:
        return None
    if len(data) > MAX_FILE_BYTES or b"\x00" in data[:BINARY_PROBE_BYTES]:
        return None
    return data.decode("utf-8", "replace")


def _best_line(text: str, terms: list[str]) -> tuple[int, int, str]:
    """``(terms in the line, line number, line)`` of the line with the most terms (the first of
    equals); ``(0, 0, "")`` if no line holds a term."""
    best = (0, 0, "")
    for number, line in enumerate(text.splitlines(), start=1):
        lowered = line.lower()
        found = sum(1 for term in terms if term in lowered)
        if found > best[0]:
            best = (found, number, line)
    return best


def search_tutorials(query: str, root: Path, max_hits: int = 10) -> list[Hit]:
    """Files below ``root`` that contain every word of ``query``; see the module docstring.

    An empty, too long or too complex query raises ``AgentInputError``; a ``root`` that is not a
    directory raises ``ToolFailure`` (the image is wrong, not the agent).
    """
    if isinstance(max_hits, bool) or not isinstance(max_hits, int) or max_hits < 1:
        raise ValueError(f"max_hits must be an integer of at least 1, got {max_hits!r}")
    terms = _terms(query)
    base = _root_dir(root)
    ranked: list[tuple[int, int, str, Hit]] = []
    for rel, absolute in _files(base):
        lowered_path = rel.lower()
        in_path = {term for term in terms if term in lowered_path}
        text = _read_text(absolute)
        if text is None:
            continue
        lowered_text = text.lower()
        if not all(term in lowered_path or term in lowered_text for term in terms):
            continue
        found, number, line = _best_line(text, terms)
        shown = " ".join(line.split())[:MAX_HIT_CHARS] if number else NAME_ONLY_TEXT
        ranked.append((-len(in_path), -found, rel, Hit(rel, number, shown)))
    ranked.sort(key=lambda item: item[:3])
    return [item[3] for item in ranked[:max_hits]]


def _parts_below(path: str, root: Path) -> list[str]:
    """The components of ``path`` below ``root``; refuses anything that is not below it."""
    if not isinstance(path, str) or not path.strip():
        raise AgentInputError("give a path relative to the tutorial directory")
    if len(path) > MAX_PATH_CHARS or "\x00" in path:
        raise AgentInputError("the path is too long or contains a control character")
    if PurePath(path).is_absolute() or path.startswith(_SEPARATORS):
        for base in (root, Path(os.path.abspath(root))):
            try:
                return list(Path(path).relative_to(base).parts)
            except ValueError:
                continue
        raise AgentInputError(
            "the path is outside the tutorial directory; use a path relative to it, as the "
            "search results show them"
        )
    if chr(92) in path:
        raise AgentInputError("use '/' as the path separator")
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise AgentInputError("the path must stay inside the tutorial directory (no '..')")
    return parts


def _listing(directory: str) -> str:
    try:
        entries = sorted(os.scandir(directory), key=lambda e: e.name)
    except OSError:
        raise AgentInputError("that directory cannot be listed") from None
    lines = []
    for entry in entries:
        if entry.is_symlink() or os.path.isjunction(entry.path):
            continue
        suffix = "/" if entry.is_dir(follow_symlinks=False) else ""
        lines.append(entry.name + suffix)
    shown = lines[:MAX_LISTING_ENTRIES]
    if len(lines) > len(shown):
        shown.append(f"[{len(lines) - len(shown)} more entries not shown]")
    return "\n".join(shown) if shown else "(empty directory)"


def read_tutorial(path: str, root: Path, max_bytes: int = 20000) -> str:
    """The text of the file ``path`` (relative to ``root``), at most ``max_bytes`` bytes of it, or
    the names in a directory. Raises ``AgentInputError`` for a path outside ``root``, through a
    symbolic link, missing, binary or not a regular file; ``ValueError`` for ``max_bytes < 1``."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError(f"max_bytes must be an integer of at least 1, got {max_bytes!r}")
    base = _root_dir(root)
    parts = _parts_below(path, base)
    current = str(base)
    for part in parts:
        current = os.path.join(current, part)
        if _is_link(current):
            raise AgentInputError(f"{path!r} goes through a symbolic link, which is not followed")
    try:
        status = os.lstat(current)
    except FileNotFoundError:
        raise AgentInputError(f"{path!r} does not exist in the tutorial directory") from None
    except OSError:
        raise AgentInputError(f"{path!r} cannot be used as a path") from None
    if stat.S_ISDIR(status.st_mode):
        return _listing(current)
    if not stat.S_ISREG(status.st_mode):
        raise AgentInputError(f"{path!r} is not a regular file")
    with open(current, "rb") as handle:
        data = handle.read(max_bytes + 1)
    if b"\x00" in data[:BINARY_PROBE_BYTES]:
        raise AgentInputError(f"{path!r} is binary; it cannot be shown")
    if len(data) > max_bytes:
        text = data[:max_bytes].decode("utf-8", "ignore")
        return text + f"\n[file truncated: only the first {max_bytes} bytes are shown]"
    return data.decode("utf-8", "replace")
