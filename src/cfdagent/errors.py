"""Two kinds of failure, kept apart so the right party is blamed.

``AgentInputError``: the content an agent wrote (a script, a manifest, a case file) is wrong. The
stage records it as a gate failure and the agent gets the message back so it can fix its work
(``as_gate``). Raise a subclass only for problems the agent can fix.

``ToolFailure``: not the agent's fault. A caller broke a precondition (for example converting into
a case directory that already holds a mesh) or a tool or the environment failed (a utility timed
out or exited abnormally on a file we wrote ourselves). ``as_gate`` never converts it: it
propagates and the orchestrator records an ``internal_error``.

Any other exception (``KeyError``, ``TypeError``, an ``OSError`` in our own code, ...) is a
programmer error and propagates in the same way. Never use either class to hide a bug.
"""

from __future__ import annotations

from collections.abc import Callable

from cfdagent.gates import GateResult


class AgentInputError(Exception):
    """Base class for failures caused by content an agent wrote."""


class ToolFailure(RuntimeError):
    """A caller bug or a tool or environment failure; never blamed on the agent."""


def as_gate(fn: Callable[[], GateResult]) -> GateResult:
    """Call ``fn``; turn an ``AgentInputError`` into ``GateResult(False, [str(exc)])``.

    Every other exception, including ``ToolFailure``, propagates unchanged.
    """
    try:
        return fn()
    except AgentInputError as exc:
        return GateResult(False, [str(exc)])
