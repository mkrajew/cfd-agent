"""The error channel: agent-caused failures become gate errors, programmer errors propagate."""

import pytest

from cfdagent.errors import AgentInputError, ToolFailure, as_gate
from cfdagent.gates import GateResult
from cfdagent.manifest import ManifestError
from cfdagent.tools.mesh import MeshError
from cfdagent.tools.post import PostProcessingError, latest_time_dir


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


def test_tool_failure_is_not_an_agent_input_error():
    assert not issubclass(ToolFailure, AgentInputError)


def test_as_gate_lets_tool_failure_through():
    def fn() -> GateResult:
        raise ToolFailure("a utility timed out")

    with pytest.raises(ToolFailure, match="timed out"):
        as_gate(fn)


def test_post_processing_error_from_latest_time_dir_becomes_a_gate_failure(tmp_path):
    gate = as_gate(lambda: GateResult(True, [], data=latest_time_dir(tmp_path)))
    assert not gate.ok
    assert "no time directory" in gate.errors[0]
