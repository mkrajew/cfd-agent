"""The error channel: agent-caused failures become gate errors, programmer errors propagate."""

import pytest

from cfdagent.errors import AgentInputError, as_gate
from cfdagent.gates import GateResult
from cfdagent.manifest import ManifestError
from cfdagent.tools.mesh import MeshError
from cfdagent.tools.post import PostProcessingError


def test_as_gate_converts_agent_input_error():
    def fn() -> GateResult:
        raise AgentInputError("the agent wrote nonsense")

    assert as_gate(fn) == GateResult(False, ["the agent wrote nonsense"])


@pytest.mark.parametrize("exc_type", [MeshError, ManifestError, PostProcessingError])
def test_known_agent_input_errors_become_gate_errors(exc_type):
    assert issubclass(exc_type, AgentInputError)

    def fn() -> GateResult:
        raise exc_type("bad content")

    gate = as_gate(fn)
    assert not gate.ok
    assert gate.errors == ["bad content"]


def test_as_gate_returns_the_result_unchanged():
    expected = GateResult(True, [], data={"n": 1})
    assert as_gate(lambda: expected) is expected


def test_as_gate_propagates_programmer_error():
    def fn() -> GateResult:
        raise KeyError("programmer mistake")

    with pytest.raises(KeyError):
        as_gate(fn)
