"""Unit tests for the LLM client. No network: httpx2.MockTransport replays recorded responses."""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import pytest
from azure.core.credentials import AccessToken
from pydantic import BaseModel

from cfdagent.llm.client import LLMClient, StructuredOutputError, ToolCall, Usage
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
