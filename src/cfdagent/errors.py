"""One error channel for failures caused by agent-authored content.

Rule: an exception derived from ``AgentInputError`` means "the content an agent wrote (a script,
a manifest, a case file) is wrong". The stage records it as a gate failure and the agent gets the
message back so it can fix its work (``as_gate``). Any other exception (``KeyError``,
``TypeError``, an ``OSError`` in our own code, ...) is a programmer or environment error: it must
propagate, and the orchestrator records it as ``internal_error`` rather than blaming the agent.

Raise an ``AgentInputError`` subclass only for problems the agent can fix; never use one to hide
a bug in this code base.
"""

from __future__ import annotations

from collections.abc import Callable

from cfdagent.gates import GateResult


class AgentInputError(Exception):
    """Base class for failures caused by content an agent wrote."""


def as_gate(fn: Callable[[], GateResult]) -> GateResult:
    """Call ``fn``; turn an ``AgentInputError`` into ``GateResult(False, [str(exc)])``.

    Every other exception propagates unchanged.
    """
    try:
        return fn()
    except AgentInputError as exc:
        return GateResult(False, [str(exc)])
