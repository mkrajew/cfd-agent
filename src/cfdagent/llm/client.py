"""Thin chat client over the OpenAI v1 route of Azure AI Foundry, with Entra ID authentication."""

import json
import os
from dataclasses import dataclass
from typing import Any

import httpx2
import openai
from azure.core.credentials import TokenCredential
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from pydantic import BaseModel, ValidationError

from cfdagent.llm.config import LLMSettings, ModelConfig

TOKEN_SCOPE = "https://cognitiveservices.azure.com/.default"


UNPARSED_KEY = "_unparsed"


@dataclass(frozen=True)
class ToolCall:
    """One tool call requested by the model.

    `arguments` is always a dict. If the model's argument string is not valid JSON, or is valid
    JSON that is not an object (a list, string, number or null), it is returned as
    `{UNPARSED_KEY: <raw argument string>}` so that callers can report it back to the model.
    """

    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int | None


@dataclass(frozen=True)
class ChatResult:
    content: str | None
    tool_calls: list[ToolCall]
    usage: Usage
    parsed: BaseModel | None
    raw: dict


class StructuredOutputError(Exception):
    """The reply did not validate against the requested schema. `raw_text` holds the reply."""

    def __init__(self, message: str, raw_text: str):
        super().__init__(f"{message}; raw reply: {raw_text!r}")
        self.raw_text = raw_text


_SCHEMA_MAP_KEYWORDS = ("$defs", "definitions")  # keyword -> {name: schema}
_SCHEMA_LIST_KEYWORDS = ("anyOf", "allOf", "oneOf", "prefixItems")  # keyword -> [schema, ...]


def strict_schema(node: Any) -> Any:
    """Return a copy of a JSON schema made acceptable to strict structured-output mode.

    Strict mode needs every object to be closed (`additionalProperties: false`) and to list all
    of its properties in `required`. Pydantic omits defaulted and `Optional` fields from
    `required`; here they are required too, so a model must always emit them (use a nullable type
    to let the model say "no value"). Defaults still apply when the reply is validated.
    Only schema keywords are followed, so a field that happens to be named `properties` or
    `items` is not mistaken for a keyword.
    """
    if not isinstance(node, dict):
        return node
    out = dict(node)
    props = out.get("properties")
    if isinstance(props, dict):
        out["properties"] = {name: strict_schema(sub) for name, sub in props.items()}
        out["required"] = list(props)
        out["additionalProperties"] = False
    elif out.get("type") == "object":
        out["additionalProperties"] = False
        out.setdefault("required", [])
    for key in _SCHEMA_MAP_KEYWORDS:
        if isinstance(out.get(key), dict):
            out[key] = {name: strict_schema(sub) for name, sub in out[key].items()}
    for key in _SCHEMA_LIST_KEYWORDS:
        if isinstance(out.get(key), list):
            out[key] = [strict_schema(sub) for sub in out[key]]
    if isinstance(out.get("items"), dict):
        out["items"] = strict_schema(out["items"])
    return out


class LLMClient:
    def __init__(
        self,
        settings: LLMSettings,
        *,
        credential: TokenCredential | None = None,
        http_client: httpx2.Client | None = None,
    ):
        self.settings = settings
        self.credential: TokenCredential | None = None
        if settings.auth == "entra":
            self.credential = credential or DefaultAzureCredential()
            api_key: Any = get_bearer_token_provider(self.credential, TOKEN_SCOPE)
        else:
            api_key = os.environ.get(settings.api_key_env)
            if not api_key:
                raise ValueError(f"{settings.api_key_env} is not set")
        self.openai_client = openai.OpenAI(
            base_url=settings.endpoint,
            api_key=api_key,
            http_client=http_client,
            timeout=settings.timeout,
            max_retries=settings.max_retries,
        )

    def chat(
        self,
        messages: list[dict],
        model: ModelConfig,
        *,
        tools: list[dict] | None = None,
        response_schema: type[BaseModel] | None = None,
    ) -> ChatResult:
        params: dict[str, Any] = {
            "model": model.deployment,
            "messages": messages,
            "max_completion_tokens": model.max_tokens,
        }
        if model.temperature is not None:
            params["temperature"] = model.temperature
        if model.top_p is not None:
            params["top_p"] = model.top_p
        if model.reasoning_effort is not None:
            params["reasoning_effort"] = model.reasoning_effort
        if tools:
            params["tools"] = tools
        if response_schema is not None:
            params["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": response_schema.__name__,
                    "schema": strict_schema(response_schema.model_json_schema()),
                    "strict": True,
                },
            }

        response = self.openai_client.chat.completions.create(**params)
        message = response.choices[0].message
        content = message.content

        tool_calls = []
        for call in message.tool_calls or []:
            raw_args = call.function.arguments or "{}"
            try:
                arguments = json.loads(raw_args)
            except json.JSONDecodeError:
                arguments = None
            if not isinstance(arguments, dict):
                arguments = {UNPARSED_KEY: raw_args}
            tool_calls.append(ToolCall(id=call.id, name=call.function.name, arguments=arguments))

        usage = response.usage
        details = getattr(usage, "completion_tokens_details", None)
        reasoning = getattr(details, "reasoning_tokens", None)
        parsed_usage = Usage(
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            reasoning_tokens=reasoning,
        )

        parsed = None
        if response_schema is not None:
            try:
                parsed = response_schema.model_validate_json(content or "")
            except ValidationError as exc:
                raise StructuredOutputError("reply does not match schema", content or "") from exc

        return ChatResult(
            content=content,
            tool_calls=tool_calls,
            usage=parsed_usage,
            parsed=parsed,
            raw=response.model_dump(),
        )
