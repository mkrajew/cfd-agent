"""Unit tests for the LLM client. No network: httpx2.MockTransport replays recorded responses."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx2
import openai
import pytest
from azure.core.credentials import AccessToken
from azure.core.exceptions import ClientAuthenticationError
from pydantic import BaseModel

from cfdagent.llm.client import (
    UNPARSED_KEY,
    ChatResult,
    EmptyChoicesError,
    LLMClient,
    LLMError,
    LLMTransportError,
    MalformedReplyError,
    SchemaError,
    StructuredOutputError,
    ToolCall,
    TruncatedOutputError,
    Usage,
    assistant_message,
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


# --- strict schema: maps are rejected, Optional stays nullable ---


class Leaf(BaseModel):
    note: str | None = None
    value: float


class Wrapper(BaseModel):
    leaves: list[Leaf]
    first: Leaf | None = None


def test_optional_stays_nullable():
    schema = strict_schema(Wrapper.model_json_schema())
    leaf = schema["$defs"]["Leaf"]
    assert {"type": "null"} in leaf["properties"]["note"]["anyOf"]
    assert {"type": "null"} in schema["properties"]["first"]["anyOf"]
    assert leaf["required"] == ["note", "value"]
    assert schema["properties"]["leaves"]["items"] == {"$ref": "#/$defs/Leaf"}


class WithMap(BaseModel):
    name: str
    extras: dict[str, str]


class MapInside(BaseModel):
    items: list[WithMap]


class OptionalMap(BaseModel):
    extras: dict[str, float] | None = None


class FreeForm(BaseModel):
    blob: dict


@pytest.mark.parametrize("model", [WithMap, MapInside, OptionalMap, FreeForm])
def test_map_in_llm_schema_is_rejected(model):
    with pytest.raises(SchemaError) as info:
        strict_schema(model.model_json_schema())
    assert not isinstance(info.value, LLMError)  # a programming error, never retried
    assert "list of" in str(info.value)


def test_map_error_names_the_field_and_chat_raises_before_sending():
    client, requests = make_client("chat_plain.json")
    with pytest.raises(SchemaError, match="extras"):
        client.chat(MESSAGES, ModelConfig(deployment="d"), response_schema=WithMap)
    assert requests == []


class Empty(BaseModel):
    pass


def test_model_without_fields_is_still_accepted():
    schema = strict_schema(Empty.model_json_schema())
    assert schema["additionalProperties"] is False


# --- response handling ---


def body_from(fixture: str, mutate) -> dict:
    body = json.loads((FIXTURES / fixture).read_text(encoding="utf-8"))
    mutate(body)
    return body


def client_for(body: dict) -> LLMClient:
    transport = httpx2.MockTransport(lambda request: httpx2.Response(200, json=body))
    return LLMClient(
        LLMSettings(endpoint=ENDPOINT),
        credential=FakeCredential(),
        http_client=httpx2.Client(transport=transport),
    )


def test_finish_reason_is_exposed():
    client, _ = make_client("chat_plain.json")
    assert client.chat(MESSAGES, ModelConfig(deployment="d")).finish_reason == "stop"
    client, _ = make_client("chat_tool_call.json")
    assert client.chat(MESSAGES, ModelConfig(deployment="d")).finish_reason == "tool_calls"


def test_finish_reason_length_is_typed():
    def cut(body):
        choice = body["choices"][0]
        choice["finish_reason"] = "length"
        choice["message"]["content"] = '{"solver": "icoFoam", "stea'

    client = client_for(body_from("chat_structured.json", cut))
    with pytest.raises(TruncatedOutputError) as info:
        client.chat(MESSAGES, ModelConfig(deployment="d"), response_schema=Verdict)
    assert isinstance(info.value, LLMError)
    assert info.value.finish_reason == "length"
    assert info.value.raw_text.startswith('{"solver"')
    assert info.value.usage == Usage(prompt_tokens=30, completion_tokens=18, reasoning_tokens=None)


def test_truncated_without_schema_is_not_an_error():
    def cut(body):
        body["choices"][0]["finish_reason"] = "length"

    result = client_for(body_from("chat_plain.json", cut)).chat(
        MESSAGES, ModelConfig(deployment="d")
    )
    assert result.finish_reason == "length"


def test_empty_content_with_length_is_truncated():
    def cut(body):
        choice = body["choices"][0]
        choice["finish_reason"] = "length"
        choice["message"]["content"] = None

    client = client_for(body_from("chat_structured.json", cut))
    with pytest.raises(TruncatedOutputError) as info:
        client.chat(MESSAGES, ModelConfig(deployment="d"), response_schema=Verdict)
    assert info.value.raw_text == ""


def test_structured_output_error_stays_compatible_and_carries_usage():
    client, _ = make_client("chat_structured_invalid.json")
    with pytest.raises(StructuredOutputError) as info:
        client.chat(MESSAGES, ModelConfig(deployment="d"), response_schema=Verdict)
    assert isinstance(info.value, LLMError)
    assert info.value.usage == Usage(prompt_tokens=30, completion_tokens=14, reasoning_tokens=None)
    assert StructuredOutputError("m", "raw").raw_text == "raw"


def test_empty_choices_is_typed():
    client = client_for(body_from("chat_plain.json", lambda body: body.update(choices=[])))
    with pytest.raises(EmptyChoicesError) as info:
        client.chat(MESSAGES, ModelConfig(deployment="d"))
    assert isinstance(info.value, LLMError)
    assert info.value.usage == Usage(prompt_tokens=11, completion_tokens=42, reasoning_tokens=30)


def test_missing_usage_is_unknown_not_zero():
    client = client_for(body_from("chat_plain.json", lambda body: body.pop("usage")))
    result = client.chat(MESSAGES, ModelConfig(deployment="d"))
    assert result.usage is None
    assert result.content == "Hello."


def test_assistant_message_echoes_tool_calls():
    client, _ = make_client("chat_tool_call.json")
    result = client.chat(MESSAGES, ModelConfig(deployment="d"))
    assert assistant_message(result) == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_fixture_1",
                "type": "function",
                "function": {"name": "calculator", "arguments": '{"expression": "1*1/100"}'},
            }
        ],
    }


def test_assistant_message_echoes_unparsed_arguments_verbatim():
    result = make_tool_client("{not json").chat(MESSAGES, ModelConfig(deployment="d"))
    (call,) = assistant_message(result)["tool_calls"]
    assert call["function"]["arguments"] == "{not json"


def test_assistant_message_plain_reply_has_no_tool_calls_key():
    result = ChatResult(content="Hello.", tool_calls=[], usage=None, parsed=None, raw={})
    assert assistant_message(result) == {"role": "assistant", "content": "Hello."}
    empty = ChatResult(content=None, tool_calls=[], usage=None, parsed=None, raw={})
    assert assistant_message(empty) == {"role": "assistant", "content": ""}


# --- transport errors, auth modes ---


def status_client(status: int, attempts: list, max_retries: int = 2) -> LLMClient:
    def handler(request: httpx2.Request) -> httpx2.Response:
        attempts.append(request)
        return httpx2.Response(
            status, json={"error": {"message": "nope"}}, headers={"retry-after-ms": "1"}
        )

    return LLMClient(
        LLMSettings(endpoint=ENDPOINT, max_retries=max_retries),
        credential=FakeCredential(),
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    )


def test_status_error_429_retries_then_raises():
    attempts: list = []
    with pytest.raises(LLMTransportError) as info:
        status_client(429, attempts).chat(MESSAGES, ModelConfig(deployment="d"))
    assert isinstance(info.value, LLMError)
    assert info.value.status_code == 429
    assert info.value.retryable is True
    assert isinstance(info.value.__cause__, openai.RateLimitError)
    assert "nope" in str(info.value)
    assert len(attempts) == 3  # first try plus two retries


def test_status_error_400_is_not_retried():
    attempts: list = []
    with pytest.raises(LLMTransportError) as info:
        status_client(400, attempts).chat(MESSAGES, ModelConfig(deployment="d"))
    assert info.value.status_code == 400
    assert info.value.retryable is False
    assert isinstance(info.value.__cause__, openai.BadRequestError)
    assert len(attempts) == 1


@pytest.mark.parametrize(
    ("status", "retryable"), [(401, False), (403, False), (404, False), (408, True), (500, True)]
)
def test_status_error_retryable_flag(status, retryable):
    with pytest.raises(LLMTransportError) as info:
        status_client(status, [], max_retries=0).chat(MESSAGES, ModelConfig(deployment="d"))
    assert (info.value.status_code, info.value.retryable) == (status, retryable)


def raising_client(exc_type, text: str) -> LLMClient:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise exc_type(text, request=request)

    return LLMClient(
        LLMSettings(endpoint=ENDPOINT, max_retries=0),
        credential=FakeCredential(),
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    )


def test_connection_error_is_transport_error_and_retryable():
    client = raising_client(httpx2.ConnectError, "boom")
    with pytest.raises(LLMTransportError) as info:
        client.chat(MESSAGES, ModelConfig(deployment="d"))
    assert info.value.status_code is None
    assert info.value.retryable is True
    assert isinstance(info.value.__cause__, openai.APIConnectionError)


def test_timeout_is_transport_error_and_retryable():
    client = raising_client(httpx2.ReadTimeout, "slow")
    with pytest.raises(LLMTransportError) as info:
        client.chat(MESSAGES, ModelConfig(deployment="d"))
    assert info.value.retryable is True
    assert isinstance(info.value.__cause__, openai.APITimeoutError)


class FailingCredential:
    def get_token(self, *scopes, **kwargs):
        raise ClientAuthenticationError("no login")


def test_authentication_failure_is_transport_error_not_retryable():
    client = LLMClient(
        LLMSettings(endpoint=ENDPOINT, max_retries=0),
        credential=FailingCredential(),
        http_client=httpx2.Client(transport=httpx2.MockTransport(lambda r: httpx2.Response(200))),
    )
    with pytest.raises(LLMTransportError) as info:
        client.chat(MESSAGES, ModelConfig(deployment="d"))
    assert info.value.status_code is None
    assert info.value.retryable is False
    assert isinstance(info.value.__cause__, ClientAuthenticationError)


def test_retry_cost_caution_is_documented():
    assert "max_retries=1" in (LLMClient.__doc__ or "")


def test_api_key_mode_reads_env(monkeypatch):
    monkeypatch.setenv("CFDAGENT_TEST_KEY", "secret-from-env")
    body = (FIXTURES / "chat_plain.json").read_text(encoding="utf-8")
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, content=body, headers={"content-type": "application/json"})

    client = LLMClient(
        LLMSettings(endpoint=ENDPOINT, auth="api_key", api_key_env="CFDAGENT_TEST_KEY"),
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    )
    client.chat(MESSAGES, ModelConfig(deployment="d"))
    assert seen[0].headers["Authorization"] == "Bearer secret-from-env"
    assert client.credential is None


def test_api_key_mode_requires_env(monkeypatch):
    monkeypatch.delenv("CFDAGENT_TEST_KEY", raising=False)
    with pytest.raises(ValueError, match="CFDAGENT_TEST_KEY"):
        LLMClient(LLMSettings(endpoint=ENDPOINT, auth="api_key", api_key_env="CFDAGENT_TEST_KEY"))


class ExpiringCredential:
    """The first token is about to expire, the second is fresh."""

    def __init__(self):
        self.calls = 0

    def get_token(self, *scopes, **kwargs) -> AccessToken:
        self.calls += 1
        now = int(datetime.now(UTC).timestamp())
        if self.calls == 1:
            return AccessToken("token-1", now + 10)
        return AccessToken("token-2", now + 3600)


def test_token_provider_refreshes():
    body = (FIXTURES / "chat_plain.json").read_text(encoding="utf-8")
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, content=body, headers={"content-type": "application/json"})

    credential = ExpiringCredential()
    client = LLMClient(
        LLMSettings(endpoint=ENDPOINT),
        credential=credential,
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    )
    for _ in range(3):
        client.chat(MESSAGES, ModelConfig(deployment="d"))
    assert [r.headers["Authorization"] for r in seen] == [
        "Bearer token-1",
        "Bearer token-2",
        "Bearer token-2",
    ]
    assert credential.calls == 2


# --- usage plausibility ---


def usage_body(usage) -> dict:
    return body_from("chat_plain.json", lambda body: body.update(usage=usage))


@pytest.mark.parametrize(
    "usage",
    [
        {},
        {"prompt_tokens": None, "completion_tokens": None},
        {"prompt_tokens": 5},
        {"completion_tokens": 5},
        {"prompt_tokens": 5, "completion_tokens": None},
        {"prompt_tokens": -1, "completion_tokens": 5},
        {"prompt_tokens": 5, "completion_tokens": -5},
        {"prompt_tokens": "5", "completion_tokens": 5},
        {"prompt_tokens": 5, "completion_tokens": 5.0},
        {"prompt_tokens": True, "completion_tokens": 5},
    ],
)
def test_implausible_usage_is_unknown(usage):
    result = client_for(usage_body(usage)).chat(MESSAGES, ModelConfig(deployment="d"))
    assert result.usage is None


def test_zero_token_counts_are_real_usage():
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    result = client_for(usage_body(usage)).chat(MESSAGES, ModelConfig(deployment="d"))
    assert result.usage == Usage(0, 0, None)


def test_bad_reasoning_tokens_become_none_but_usage_stays():
    usage = {
        "prompt_tokens": 5,
        "completion_tokens": 6,
        "completion_tokens_details": {"reasoning_tokens": -3},
    }
    result = client_for(usage_body(usage)).chat(MESSAGES, ModelConfig(deployment="d"))
    assert result.usage == Usage(5, 6, None)


def test_implausible_usage_on_an_error_reply_is_none():
    body = body_from("chat_plain.json", lambda b: b.update(choices=[], usage={}))
    with pytest.raises(EmptyChoicesError) as info:
        client_for(body).chat(MESSAGES, ModelConfig(deployment="d"))
    assert info.value.usage is None


# --- malformed replies, content filter ---


def test_choice_without_message_is_typed():
    def drop(body):
        body["choices"][0]["message"] = None

    with pytest.raises(MalformedReplyError) as info:
        client_for(body_from("chat_plain.json", drop)).chat(MESSAGES, ModelConfig(deployment="d"))
    assert isinstance(info.value, LLMError)


def test_content_filter_is_named_in_the_structured_error():
    def filtered(body):
        body["choices"][0]["finish_reason"] = "content_filter"
        body["choices"][0]["message"]["content"] = None

    client = client_for(body_from("chat_structured.json", filtered))
    with pytest.raises(StructuredOutputError, match="content filter"):
        client.chat(MESSAGES, ModelConfig(deployment="d"), response_schema=Verdict)


# --- strict schema: defaults and Any ---


class WithDefaults(BaseModel):
    a: int = 3
    b: list[int] = []
    c: str | None = None


class WithObject(BaseModel):
    anything: object


class WithObjectList(BaseModel):
    values: list[object]


class WithTypingAny(BaseModel):
    value: Any


def test_default_keywords_are_stripped_but_defaults_still_apply():
    schema = strict_schema(WithDefaults.model_json_schema())
    for name, sub in schema["properties"].items():
        assert "default" not in sub, name
    assert schema["required"] == ["a", "b", "c"]
    assert WithDefaults.model_validate_json("{}") == WithDefaults()


@pytest.mark.parametrize("model", [WithObject, WithObjectList, WithTypingAny])
def test_any_typed_field_is_rejected(model):
    with pytest.raises(SchemaError, match="no type"):
        strict_schema(model.model_json_schema())


def test_enums_refs_and_defaults_are_not_mistaken_for_any():
    class Mode(BaseModel):
        kind: Literal["a", "b"] = "a"
        leaf: Leaf | None = None
        nested: Leaf = Leaf(value=1.0)

    strict_schema(Mode.model_json_schema())
