"""Unit tests for the LLM client. No network: httpx2.MockTransport replays recorded responses."""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import pytest
from azure.core.credentials import AccessToken
from pydantic import BaseModel

from cfdagent.llm.client import (
    UNPARSED_KEY,
    LLMClient,
    StructuredOutputError,
    ToolCall,
    Usage,
    strict_schema,
)
from cfdagent.llm.config import LLMSettings, ModelConfig

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "llm"
ENDPOINT = "https://example-account.services.ai.azure.com/openai/v1/"
MESSAGES = [{"role": "user", "content": "hi"}]


class FakeCredential:
    def get_token(self, *scopes, **kwargs) -> AccessToken:
        self.scopes = scopes
        return AccessToken("fake-token", int(datetime.now(UTC).timestamp()) + 3600)


class Verdict(BaseModel):
    solver: str
    steady: bool
    reynolds: float


def make_client(fixture: str):
    """Return (client, requests) where requests collects every request sent."""
    body = (FIXTURES / fixture).read_text(encoding="utf-8")
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, content=body, headers={"content-type": "application/json"})

    client = LLMClient(
        LLMSettings(endpoint=ENDPOINT),
        credential=FakeCredential(),
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    )
    return client, requests


def sent_json(request: httpx2.Request) -> dict:
    return json.loads(request.content)


def test_sends_deployment_as_model():
    client, requests = make_client("chat_plain.json")
    client.chat(MESSAGES, ModelConfig(deployment="my-deployment"))
    assert sent_json(requests[0])["model"] == "my-deployment"
    assert str(requests[0].url) == ENDPOINT + "chat/completions"


def test_sends_reasoning_effort_when_set():
    client, requests = make_client("chat_plain.json")
    client.chat(MESSAGES, ModelConfig(deployment="d", reasoning_effort="high"))
    assert sent_json(requests[0])["reasoning_effort"] == "high"


def test_omits_reasoning_effort_when_none():
    client, requests = make_client("chat_plain.json")
    client.chat(MESSAGES, ModelConfig(deployment="d"))
    body = sent_json(requests[0])
    assert "reasoning_effort" not in body
    assert "temperature" not in body
    assert "top_p" not in body


def test_bearer_token_from_credential():
    client, requests = make_client("chat_plain.json")
    client.chat(MESSAGES, ModelConfig(deployment="d"))
    assert requests[0].headers["Authorization"] == "Bearer fake-token"
    assert client.credential.scopes == ("https://cognitiveservices.azure.com/.default",)


def test_parses_tool_calls():
    client, requests = make_client("chat_tool_call.json")
    tools = [
        {
            "type": "function",
            "function": {
                "name": "calculator",
                "parameters": {
                    "type": "object",
                    "properties": {"expression": {"type": "string"}},
                    "required": ["expression"],
                },
            },
        }
    ]
    result = client.chat(MESSAGES, ModelConfig(deployment="d"), tools=tools)
    assert result.tool_calls == [
        ToolCall(id="call_fixture_1", name="calculator", arguments={"expression": "1*1/100"})
    ]
    assert result.content is None
    assert sent_json(requests[0])["tools"] == tools


def test_structured_output_parsed():
    client, requests = make_client("chat_structured.json")
    result = client.chat(MESSAGES, ModelConfig(deployment="d"), response_schema=Verdict)
    assert result.parsed == Verdict(solver="icoFoam", steady=False, reynolds=100.0)
    fmt = sent_json(requests[0])["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["name"] == "Verdict"
    assert fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["schema"]["additionalProperties"] is False


def test_invalid_structured_output_raises():
    client, _ = make_client("chat_structured_invalid.json")
    with pytest.raises(StructuredOutputError) as info:
        client.chat(MESSAGES, ModelConfig(deployment="d"), response_schema=Verdict)
    assert "icoFoam and the flow is transient" in str(info.value)
    assert info.value.raw_text.startswith("Sure!")


def test_usage_parsed():
    client, _ = make_client("chat_plain.json")
    result = client.chat(MESSAGES, ModelConfig(deployment="d"))
    assert result.usage == Usage(prompt_tokens=11, completion_tokens=42, reasoning_tokens=30)
    assert result.content == "Hello."


def test_usage_without_reasoning_details():
    client, _ = make_client("chat_tool_call.json")
    result = client.chat(MESSAGES, ModelConfig(deployment="d"))
    assert result.usage.reasoning_tokens is None


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("CFDAGENT_LLM_ENDPOINT", ENDPOINT)
    monkeypatch.delenv("CFDAGENT_LLM_AUTH", raising=False)
    assert LLMSettings.from_env() == LLMSettings(endpoint=ENDPOINT)
    monkeypatch.setenv("CFDAGENT_LLM_AUTH", "api_key")
    assert LLMSettings.from_env().auth == "api_key"


def test_settings_from_env_requires_endpoint(monkeypatch):
    monkeypatch.delenv("CFDAGENT_LLM_ENDPOINT", raising=False)
    with pytest.raises(ValueError, match="CFDAGENT_LLM_ENDPOINT"):
        LLMSettings.from_env()


def test_settings_timeout_and_retries_defaults_and_passthrough():
    settings = LLMSettings(endpoint=ENDPOINT)
    assert settings.timeout == 120.0
    assert settings.max_retries == 2
    client = LLMClient(
        LLMSettings(endpoint=ENDPOINT, timeout=7.5, max_retries=0), credential=FakeCredential()
    )
    assert client.openai_client.timeout == 7.5
    assert client.openai_client.max_retries == 0


def make_tool_client(arguments) -> LLMClient:
    body = json.loads((FIXTURES / "chat_tool_call.json").read_text(encoding="utf-8"))
    body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = arguments
    transport = httpx2.MockTransport(lambda request: httpx2.Response(200, json=body))
    return LLMClient(
        LLMSettings(endpoint=ENDPOINT),
        credential=FakeCredential(),
        http_client=httpx2.Client(transport=transport),
    )


@pytest.mark.parametrize("arguments", ["{not json", "[1]", '"x"', "null", "3"])
def test_malformed_or_non_object_tool_arguments_use_sentinel(arguments):
    result = make_tool_client(arguments).chat(MESSAGES, ModelConfig(deployment="d"))
    (call,) = result.tool_calls
    assert call.name == "calculator"
    assert call.arguments == {UNPARSED_KEY: arguments}


# --- strict schema generation ---


class Inner(BaseModel):
    a: int
    b: str = "x"


class Outer(BaseModel):
    inner: Inner
    items: list[Inner]
    maybe: float | None = None
    properties: int = 0  # a field that is literally named like the schema keyword


def object_nodes(node, path="$"):
    """Yield (path, node) for every object schema found anywhere below `node`."""
    if isinstance(node, dict):
        if node.get("type") == "object":
            yield path, node
        for key, value in node.items():
            if key == "properties":
                for name, sub in value.items():
                    yield from object_nodes(sub, f"{path}.{name}")
            elif key in ("$defs", "definitions"):
                for name, sub in value.items():
                    yield from object_nodes(sub, f"{path}#{name}")
            elif key in ("items", "additionalProperties"):
                yield from object_nodes(value, f"{path}[]")
            elif key in ("anyOf", "allOf", "oneOf", "prefixItems"):
                for i, sub in enumerate(value):
                    yield from object_nodes(sub, f"{path}|{i}")


def test_strict_schema_every_object_is_closed_and_fully_required():
    schema = strict_schema(Outer.model_json_schema())
    objects = dict(object_nodes(schema))
    assert len(objects) == 2  # the root object and Inner under $defs
    for path, node in objects.items():
        assert node["additionalProperties"] is False, path
        assert node["required"] == list(node["properties"]), path
    assert schema["required"] == ["inner", "items", "maybe", "properties"]
    assert schema["$defs"]["Inner"]["required"] == ["a", "b"]


def test_strict_schema_field_named_properties_is_not_treated_as_an_object():
    schema = strict_schema(Outer.model_json_schema())
    assert "additionalProperties" not in schema["properties"]
    assert schema["properties"]["properties"]["type"] == "integer"
    assert "required" not in schema["properties"]["properties"]


def test_strict_schema_does_not_mutate_input():
    original = Outer.model_json_schema()
    snapshot = json.dumps(original, sort_keys=True)
    strict_schema(original)
    assert json.dumps(original, sort_keys=True) == snapshot
