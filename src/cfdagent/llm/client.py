"""Thin chat client over the OpenAI v1 route of Azure AI Foundry, with Entra ID authentication."""

import json
import os
from dataclasses import dataclass
from typing import Any, Protocol

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
    """One model reply.

    `usage` is `None` when the service reported none (unknown, never 0); `CostTracker` then
    estimates from text length. `finish_reason` is the service's value ("stop", "tool_calls",
    "length", ...) or `None` if it sent none.
    """

    content: str | None
    tool_calls: list[ToolCall]
    usage: Usage | None
    parsed: BaseModel | None
    raw: dict
    finish_reason: str | None = None


class LLMError(Exception):
    """Base class of the errors raised by this package for a bad or unusable model reply.

    Transport and HTTP status failures (after the SDK's own retries) propagate as the `openai`
    exceptions (`openai.APIStatusError`, `openai.APIConnectionError`, ...). Errors raised for a
    reply that did arrive carry its `usage` (or `None`) so the caller can still bill it.
    """

    def __init__(self, message: str, *, usage: Usage | None = None):
        super().__init__(message)
        self.usage = usage


class EmptyChoicesError(LLMError):
    """The service answered 200 with an empty `choices` list."""


class SchemaError(LLMError):
    """A pydantic model cannot be used as a strict structured-output schema."""


class StructuredOutputError(LLMError):
    """The reply did not validate against the requested schema. `raw_text` holds the reply."""

    def __init__(self, message: str, raw_text: str, *, usage: Usage | None = None):
        super().__init__(f"{message}; raw reply: {raw_text!r}", usage=usage)
        self.raw_text = raw_text


class TruncatedOutputError(LLMError):
    """A structured reply was cut off (`finish_reason == "length"`) before it was complete.

    Reasoning models spend `max_completion_tokens` on reasoning first, so raise `max_tokens` or
    lower the reasoning effort; retrying unchanged will usually fail the same way.
    """

    def __init__(self, raw_text: str, finish_reason: str = "length", *, usage: Usage | None = None):
        super().__init__(
            f"structured reply cut off (finish_reason={finish_reason!r}); "
            f"raise max_tokens or lower the reasoning effort; partial reply: {raw_text!r}",
            usage=usage,
        )
        self.raw_text = raw_text
        self.finish_reason = finish_reason


def parse_structured(
    schema: type[BaseModel],
    content: str | None,
    finish_reason: str | None,
    usage: Usage | None,
) -> BaseModel:
    """Validate a reply against `schema`; raise the typed error that explains a failure."""
    text = content or ""
    try:
        return schema.model_validate_json(text)
    except ValidationError as exc:
        if finish_reason == "length":
            raise TruncatedOutputError(text, finish_reason, usage=usage) from exc
        raise StructuredOutputError("reply does not match schema", text, usage=usage) from exc


class ChatBackend(Protocol):
    """What an agent needs from a model: `LLMClient` and `ScriptedBackend` both provide it."""

    def chat(
        self,
        messages: list[dict],
        model: ModelConfig,
        *,
        tools: list[dict] | None = None,
        response_schema: type[BaseModel] | None = None,
    ) -> ChatResult: ...


def assistant_message(result: ChatResult) -> dict:
    """Build the assistant message that echoes a reply (and its tool calls) back into the history.

    Tool-call arguments are sent back as JSON text; arguments that could not be parsed are echoed
    verbatim, exactly as the model produced them.
    """
    message: dict[str, Any] = {"role": "assistant", "content": result.content or ""}
    if result.tool_calls:
        message["content"] = result.content
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": call.arguments[UNPARSED_KEY]
                    if set(call.arguments) == {UNPARSED_KEY}
                    else json.dumps(call.arguments),
                },
            }
            for call in result.tool_calls
        ]
    return message


_SCHEMA_MAP_KEYWORDS = ("$defs", "definitions")  # keyword -> {name: schema}
_SCHEMA_LIST_KEYWORDS = ("anyOf", "allOf", "oneOf", "prefixItems")  # keyword -> [schema, ...]


def _map_error(path: str) -> SchemaError:
    return SchemaError(
        f"schema field {path} is a map or free-form object (dict); strict structured output "
        "can only emit {} for it. Use a list of {key, value} models instead."
    )


def strict_schema(node: Any, _path: str = "$") -> Any:
    """Return a copy of a JSON schema made acceptable to strict structured-output mode.

    Strict mode needs every object to be closed (`additionalProperties: false`) and to list all
    of its properties in `required`. Pydantic omits defaulted and `Optional` fields from
    `required`; here they are required too, so a model must always emit them (use a nullable type
    to let the model say "no value"). Defaults still apply when the reply is validated.
    Only schema keywords are followed, so a field that happens to be named `properties` or
    `items` is not mistaken for a keyword.

    Raises `SchemaError` for a map: an object whose `additionalProperties` is a schema
    (`dict[str, X]`) or an object without declared properties (`dict`). Closing such an object
    would leave the model only `{}` to say, silently losing the data, so it is refused.
    """
    if not isinstance(node, dict):
        return node
    out = dict(node)
    props = out.get("properties")
    if isinstance(out.get("additionalProperties"), dict):
        raise _map_error(_path)
    if isinstance(props, dict):
        out["properties"] = {
            name: strict_schema(sub, f"{_path}.{name}") for name, sub in props.items()
        }
        out["required"] = list(props)
        out["additionalProperties"] = False
    elif out.get("type") == "object":
        raise _map_error(_path)
    for key in _SCHEMA_MAP_KEYWORDS:
        if isinstance(out.get(key), dict):
            out[key] = {
                name: strict_schema(sub, f"{_path}#{name}") for name, sub in out[key].items()
            }
    for key in _SCHEMA_LIST_KEYWORDS:
        if isinstance(out.get(key), list):
            out[key] = [strict_schema(sub, f"{_path}|{i}") for i, sub in enumerate(out[key])]
    if isinstance(out.get("items"), dict):
        out["items"] = strict_schema(out["items"], f"{_path}[]")
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
        response_format = None
        if response_schema is not None:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": response_schema.__name__,
                    "schema": strict_schema(response_schema.model_json_schema()),
                    "strict": True,
                },
            }
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
        if response_format is not None:
            params["response_format"] = response_format

        response = self.openai_client.chat.completions.create(**params)

        usage = None
        if response.usage is not None:
            details = getattr(response.usage, "completion_tokens_details", None)
            usage = Usage(
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
                reasoning_tokens=getattr(details, "reasoning_tokens", None),
            )

        if not response.choices:
            raise EmptyChoicesError("the service returned no choices", usage=usage)
        choice = response.choices[0]
        message = choice.message
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

        parsed = None
        if response_schema is not None:
            parsed = parse_structured(response_schema, content, choice.finish_reason, usage)

        return ChatResult(
            content=content,
            tool_calls=tool_calls,
            usage=usage,
            parsed=parsed,
            raw=response.model_dump(),
            finish_reason=choice.finish_reason,
        )
