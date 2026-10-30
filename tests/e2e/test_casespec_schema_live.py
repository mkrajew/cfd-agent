"""Live checks of the Planner against the real model service.

Manual, marker ``e2e``, costs a few cents per deployment. It needs ``CFDAGENT_LLM_ENDPOINT`` (and
a current ``az login`` for Entra authentication, or ``CFDAGENT_LLM_AUTH=api_key`` with
``CFDAGENT_LLM_API_KEY``). Without the endpoint the tests are skipped. Run:
``uv run pytest -m e2e tests/e2e/test_casespec_schema_live.py``.

Two questions are settled, on the primary model (reasoning effort set) and on the comparison
model (no effort):

* Does the service accept the strict ``CaseSpec`` schema and the request shape of phase 2, that
  is a conversation holding a calculator round trip (assistant ``tool_calls`` and a ``tool``
  message) with ``response_format`` and NO ``tools`` parameter? The fixed-history test sends
  exactly that and checks the reply validates.
* Does the whole Planner (``run_planner``: phase 1 with the calculator, phase 2, the gate) work
  against the live client? Whether the plan is sensible is judged by ``plan_gate`` only.
"""

import os
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from cfdagent.agents.casespec import CaseSpec
from cfdagent.agents.planner import run_planner
from cfdagent.agents.stage import StageContext
from cfdagent.agents.transcript import TranscriptWriter
from cfdagent.config import RunConfig
from cfdagent.llm.client import LLMClient
from cfdagent.llm.config import ModelConfig
from cfdagent.llm.cost import PRICING, CostTracker
from cfdagent.prompts import load_prompt
from cfdagent.record import RunRecord
from cfdagent.rundir import RunDir

pytestmark = pytest.mark.e2e

TASK = (
    "Plan a steady, laminar, incompressible flow of water through a straight rectangular duct "
    "2 m long, 0.1 m high and 0.01 m deep, with a mean inlet speed giving a Reynolds number of 100 "
    "based on the duct height. Report `outlet_velocity_profile` and `pressure_drop`."
)
DEPLOYMENTS = [("gpt-oss-120b", "medium"), ("Llama-3.3-70B-Instruct", None)]


@pytest.fixture(scope="module")
def client() -> LLMClient:
    if not os.environ.get("CFDAGENT_LLM_ENDPOINT"):
        pytest.skip("CFDAGENT_LLM_ENDPOINT is not set; the live tests need a model endpoint")
    return LLMClient(RunConfig.llm_settings_from_env())


def _model(deployment: str, effort: str | None, max_tokens: int) -> ModelConfig:
    return ModelConfig(deployment=deployment, reasoning_effort=effort, max_tokens=max_tokens)


@pytest.mark.parametrize(("deployment", "effort"), DEPLOYMENTS)
def test_casespec_strict_schema_is_accepted_after_a_tool_round_trip(client, deployment, effort):
    """The request shape of phase 2: tool history in the messages, a schema, no tools."""
    config = RunConfig.default()
    model = _model(deployment, effort, config.planner_structured_max_tokens)
    messages = [
        {"role": "system", "content": load_prompt("planner").text},
        {"role": "user", "content": f"Task from the user:\n\n{TASK}\n\nPlan the simulation."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "calculator",
                        "arguments": '{"expression": "100 * 1e-6 / 0.1"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "0.001"},
        {
            "role": "assistant",
            "content": "Plan: laminar duct flow, mean speed 0.001 m/s for nu = 1e-6 m^2/s.",
        },
        {
            "role": "user",
            "content": "Now write the CaseSpec: one JSON object that matches the required "
            "schema, with every field present (use null where a value does not apply). List "
            "every assumption in `assumptions`.",
        },
    ]
    result = client.chat(messages, model, response_schema=CaseSpec)  # no tools parameter
    assert isinstance(result.parsed, CaseSpec)
    assert CaseSpec.model_validate_json(result.content or "") == result.parsed


@pytest.mark.parametrize(("deployment", "effort"), DEPLOYMENTS)
def test_planner_runs_end_to_end_against_the_service(client, tmp_path, deployment, effort):
    config = RunConfig.default()
    config = config.model_copy(
        update={"models": {**config.models, "planner": _model(deployment, effort, 8192)}}
    )
    root = tmp_path / "run"
    root.mkdir()
    run = RunDir(root)
    cost = CostTracker(config.limits.max_cost_usd, PRICING[deployment])
    record = RunRecord(
        run_id="20261112-143005-live",
        task_set="ad hoc",
        task_sha256="a" * 64,
        started_at=datetime(2026, 11, 12, 14, 30, 5, tzinfo=UTC),
        status="running",
        cfdagent_version="0.0.1",
    )
    ctx = StageContext(
        run=run,
        config=config,
        backend=client,
        sandbox=SimpleNamespace(),  # the Planner never uses the sandbox
        cost=cost,
        record=record,
        transcript=TranscriptWriter(run, cost),
        started_at=time.monotonic(),
    )
    outcome = run_planner(ctx, TASK)
    assert outcome.ok, f"{outcome.reason}; errors: {outcome.errors}"
    assert isinstance(ctx.casespec, CaseSpec)
    assert (root / "casespec.json").is_file()
