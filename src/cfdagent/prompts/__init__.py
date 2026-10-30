"""Loader for the cloud-agent system prompts (``prompts/<agent>.md``).

A prompt is versioned by content: the version is the first 12 hex digits of the SHA-256 of the
file's bytes, so any edit changes it and the run record can say exactly which text an agent saw.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent
_AGENT_NAME = re.compile(r"[a-z][a-z0-9_]{0,39}")


@dataclass(frozen=True)
class Prompt:
    text: str
    version: str  # first 12 hex digits of the SHA-256 of the file


def load_prompt(agent: str, prompts_dir: Path | str | None = None) -> Prompt:
    """Read ``<prompts_dir>/<agent>.md`` (default: this package's directory).

    ``agent`` must be a plain lower-case identifier, so a name can never point outside the
    directory. A missing file raises ``FileNotFoundError``; a file that is not UTF-8 raises
    ``UnicodeDecodeError`` (both are programming errors, not model behaviour).
    """
    if not isinstance(agent, str) or not _AGENT_NAME.fullmatch(agent):
        raise ValueError(f"agent name {agent!r} must be 1-40 lower-case letters, digits or _")
    directory = Path(prompts_dir) if prompts_dir is not None else PROMPTS_DIR
    path = directory / f"{agent}.md"
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise FileNotFoundError(
            f"no prompt for agent {agent!r}: missing file {path.name}"
        ) from None
    return Prompt(text=data.decode("utf-8"), version=hashlib.sha256(data).hexdigest()[:12])
