"""Live check that the real model service accepts the CaseSpec strict schema.

Manual, marker ``e2e``, costs a few cents: one structured call per deployment. It needs
``CFDAGENT_LLM_ENDPOINT`` (and a current ``az login`` for Entra authentication, or
``CFDAGENT_LLM_AUTH=api_key`` with ``CFDAGENT_LLM_API_KEY``). Without the endpoint the tests are
skipped. Run: ``uv run pytest -m e2e tests/e2e/test_casespec_schema_live.py``.

What it settles: a reply built from ``strict_schema(CaseSpec.model_json_schema())`` must come back
as valid JSON that ``CaseSpec`` accepts, on the primary model (reasoning effort set) and on the
comparison model (no effort). Whether the plan is sensible is not judged here, only the schema
round trip, so the gate is not applied.
"""

import os

import pytest

from cfdagent.agents.casespec import CaseSpec
from cfdagent.config import RunConfig
from cfdagent.llm.client import LLMClient
from cfdagent.llm.config import ModelConfig
from cfdagent.prompts import load_prompt

pytestmark = pytest.mark.e2e

TASK = (
    "Plan a steady, laminar, incompressible flow of water through a straight rectangular duct "
    "2 m long, 0.1 m high and 0.01 m deep, with a mean inlet speed giving a Reynolds number of 100 "
    "based on the duct height. Report `outlet_velocity_profile` and `pressure_drop`."
)


@pytest.fixture(scope="module")
def client() -> LLMClient:
    if not os.environ.get("CFDAGENT_LLM_ENDPOINT"):
        pytest.skip("CFDAGENT_LLM_ENDPOINT is not set; the live schema test needs a model endpoint")
    return LLMClient(RunConfig.llm_settings_from_env())


@pytest.mark.parametrize(
    "deployment, effort",
    [("gpt-oss-120b", "medium"), ("Llama-3.3-70B-Instruct", None)],
)
def test_casespec_strict_schema_is_accepted_by_the_service(client, deployment, effort):
    config = RunConfig.default()
    model = ModelConfig(
        deployment=deployment,
        reasoning_effort=effort,
        max_tokens=config.planner_structured_max_tokens,
    )
    messages = [
        {"role": "system", "content": load_prompt("planner").text},
        {
            "role": "user",
            "content": f"{TASK}\n\nAnswer with the CaseSpec as one JSON object.",
        },
    ]
    result = client.chat(messages, model, response_schema=CaseSpec)
    assert isinstance(result.parsed, CaseSpec)
    assert CaseSpec.model_validate_json(result.content or "") == result.parsed
