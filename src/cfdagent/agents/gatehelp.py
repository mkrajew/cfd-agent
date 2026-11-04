"""Small helpers that the stage gates share: bounded text for messages and the manifest read.

Host-side, like the stages: no tool, CAD or mesh code.
"""

from __future__ import annotations

from pydantic import ValidationError

from cfdagent.agents.stage import StageContext
from cfdagent.manifest import MANIFEST_FILE_NAME, BoundaryManifest
from cfdagent.rundir import FileTooLarge, RunDirError

MANIFEST_MAX_BYTES = 200_000  # a manifest of a real geometry is a few kilobytes
_MAX_NAMES_SHOWN = 12
_MAX_NAME_CHARS = 40


def last_bytes(text: str, limit: int) -> str:
    """The end of ``text`` (at most ``limit`` UTF-8 bytes), where a traceback keeps its cause."""
    data = text.encode("utf-8", "replace")
    if len(data) <= limit:
        return text
    return "[earlier output omitted]\n" + data[-limit:].decode("utf-8", "ignore")


def names(items: list[str] | set[str]) -> str:
    """A bounded, readable list of names (the plan's names have no length limit)."""
    ordered = sorted(items)
    shown = []
    for name in ordered[:_MAX_NAMES_SHOWN]:
        text = name if len(name) <= _MAX_NAME_CHARS else name[:_MAX_NAME_CHARS] + "..."
        shown.append(f"`{text}`")
    more = f" and {len(ordered) - _MAX_NAMES_SHOWN} more" if len(ordered) > _MAX_NAMES_SHOWN else ""
    return ", ".join(shown) + more


def read_manifest(ctx: StageContext, rel: str) -> tuple[BoundaryManifest | None, list[str]]:
    """The manifest, read with a size limit; ``(None, [])`` if it is missing or does not parse
    (the sandbox gate describes that), ``(None, [problem])`` if it is too large."""
    try:
        data = ctx.run.read_json(rel, MANIFEST_MAX_BYTES)
        return BoundaryManifest.model_validate(data), []
    except FileTooLarge:
        return None, [
            f"{MANIFEST_FILE_NAME} is larger than {MANIFEST_MAX_BYTES} bytes; a manifest names a "
            "handful of patches with a few locators each. Write a much smaller file"
        ]
    except (RunDirError, ValidationError, FileNotFoundError):
        return None, []
