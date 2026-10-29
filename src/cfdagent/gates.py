"""Result type shared by every deterministic stage gate."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GateResult:
    """Outcome of a deterministic stage gate; ``errors`` is empty when ``ok``.

    ``data`` optionally carries a gate-specific payload (for example a parsed report).
    """

    ok: bool
    errors: list[str]
    data: Any = None
