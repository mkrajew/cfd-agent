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


@dataclass(frozen=True)
class ToolCall:
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


def _strict_schema(node: Any) -> Any:
    """Add `additionalProperties: false` to every object schema, as strict mode requires."""
    if isinstance(node, dict):
        out = {key: _strict_schema(value) for key, value in node.items()}
        if out.get("type") == "object" or "properties" in out:
            out["additionalProperties"] = False
        return out
    if isinstance(node, list):
        return [_strict_schema(item) for item in node]
    return node


class LLMClient:
    def __init__(
        self,
        settings: LLMSettings,
        *,
        credential: TokenCredential | None = None,
        http_client: httpx2.Client | None = None,
    ):
        self.settings = settings
        if settings.auth == "entra":
            credential = credential or DefaultAzureCredential()
            api_key: Any = get_bearer_token_provider(credential, TOKEN_SCOPE)
        else:
            api_key = os.environ.get(settings.api_key_env)
            if not api_key:
                raise ValueError(f"{settings.api_key_env} is not set")
        self._client = openai.OpenAI(
            base_url=settings.endpoint, api_key=api_key, http_client=http_client
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
                    "schema": _strict_schema(response_schema.model_json_schema()),
                    "strict": True,
                },
            }

        response = self._client.chat.completions.create(**params)
        message = response.choices[0].message
        content = message.content

        tool_calls = []
        for call in message.tool_calls or []:
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {"_unparsed": call.function.arguments}
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
