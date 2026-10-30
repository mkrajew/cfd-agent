"""Thin chat client over the OpenAI v1 route of Azure AI Foundry, with Entra ID authentication."""

import json
import os
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import httpx2
import openai
from azure.core.credentials import TokenCredential
from azure.core.exceptions import ClientAuthenticationError
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


def _is_count(value: Any) -> bool:
    return type(value) is int and value >= 0


def usage_is_known(usage: Usage | None) -> bool:
    """True if `usage` holds real token counts: ints (not bool, float or str) that are >= 0."""
    return (
        usage is not None and _is_count(usage.prompt_tokens) and _is_count(usage.completion_tokens)
    )


def _usage_from_sdk(raw: Any) -> Usage | None:
    """Build a `Usage` from the SDK's usage object, or `None` if it is missing or implausible.

    The SDK does not validate responses, so a service can send `{}`, nulls, one field only,
    negative numbers or strings. Any of that is "unknown" (never 0); the cost tracker then
    estimates from text length.
    """
    if raw is None:
        return None
    prompt = getattr(raw, "prompt_tokens", None)
    completion = getattr(raw, "completion_tokens", None)
    if not (_is_count(prompt) and _is_count(completion)):
        return None
    reasoning = getattr(getattr(raw, "completion_tokens_details", None), "reasoning_tokens", None)
    return Usage(prompt, completion, reasoning if _is_count(reasoning) else None)


@dataclass(frozen=True)
class ChatResult:
    """One model reply.

    `usage` is `None` when the service reported none or an implausible one (unknown, never 0);
    `CostTracker` then estimates from text length. `finish_reason` is the service's value
    ("stop", "tool_calls", "length", ...) or `None` if it sent none.
    """

    content: str | None
    tool_calls: list[ToolCall]
    usage: Usage | None
    parsed: BaseModel | None
    raw: dict
    finish_reason: str | None = None


class LLMError(Exception):
    """Base class of the errors raised by this package when a model call fails or its reply is
    unusable.

    Errors raised for a reply that did arrive carry its `usage` (or `None`) so the caller can
    still bill it. `SchemaError` is deliberately NOT a subclass: it is a programming error that a
    retry loop catching `LLMError` must never swallow.
    """

    def __init__(self, message: str, *, usage: Usage | None = None):
        super().__init__(message)
        self.usage = usage


def retryable_status(status_code: int | None) -> bool:
    """Whether an HTTP status is worth retrying later: timeouts, conflicts, rate limits, 5xx."""
    return status_code is not None and (status_code in (408, 409, 429) or status_code >= 500)


class LLMTransportError(LLMError):
    """The request failed before a usable reply arrived (after the SDK's own retries).

    `status_code` is the HTTP status, or `None` for connection, timeout and authentication
    failures. `retryable` says whether the same call can succeed later (rate limit, server
    error, timeout, dropped connection) or not (bad request, authentication failure, missing
    permission). The original exception is kept as `__cause__`.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        usage: Usage | None = None,
    ):
        super().__init__(message, usage=usage)
        self.status_code = status_code
        self.retryable = retryable


class EmptyChoicesError(LLMError):
    """The service answered 200 with an empty `choices` list."""


class MalformedReplyError(LLMError):
    """The service answered 200 but the first choice has no message."""


class SchemaError(Exception):
    """A pydantic model cannot be used as a strict structured-output schema.

    This is a programming error in the model definition, so it derives from `Exception` and not
    from `LLMError`: retrying cannot help and an `except LLMError` retry loop does not hide it.
    """


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
        if finish_reason == "content_filter":
            message = "reply withheld by the content filter (finish_reason='content_filter')"
        else:
            message = "reply does not match schema"
        raise StructuredOutputError(message, text, usage=usage) from exc


@runtime_checkable
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
_TYPING_KEYWORDS = ("type", "$ref", "anyOf", "allOf", "oneOf", "enum", "const")


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
    to let the model say "no value"). `default` keywords are removed from properties; defaults
    still apply when the reply is validated.
    Only schema keywords are followed, so a field that happens to be named `properties` or
    `items` is not mistaken for a keyword.

    Raises `SchemaError` for a map: an object whose `additionalProperties` is a schema
    (`dict[str, X]`) or an object without declared properties (`dict`), because closing it would
    leave the model only `{}` to say and silently lose the data. A field typed `Any` (a schema
    with no type, reference or union) is refused too: the service cannot constrain it.
    """
    if not isinstance(node, dict):
        return node
    out = dict(node)
    if _path != "$" and not any(key in out for key in _TYPING_KEYWORDS):
        raise SchemaError(
            f"schema field {_path} has no type (an `Any` field); give it a concrete type so the "
            "service can constrain it."
        )
    props = out.get("properties")
    if isinstance(out.get("additionalProperties"), dict):
        raise _map_error(_path)
    if isinstance(props, dict):
        out["properties"] = {
            name: strict_schema(
                {k: v for k, v in sub.items() if k != "default"} if isinstance(sub, dict) else sub,
                f"{_path}.{name}",
            )
            for name, sub in props.items()
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
    """Chat client; implements `ChatBackend`.

    Failures that prevent a reply are raised as `LLMTransportError` (HTTP status errors,
    connection and timeout errors, authentication failures) with the original exception as
    `__cause__`. Replies that arrive but are unusable raise the other `LLMError` subclasses.

    Cost caution: the SDK retries a failed or stalled request `settings.max_retries` times
    (default 2) with `settings.timeout` seconds (default 120) per attempt. A call that stalls
    after the service has started to generate can be billed up to 3 times without the cost
    tracker seeing any of it. Runs that enforce a cost cap should use `max_retries=1`.
    """

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

        try:
            response = self.openai_client.chat.completions.create(**params)
        except openai.APIStatusError as exc:
            raise LLMTransportError(
                f"HTTP {exc.status_code} from the model service: {exc.message}",
                status_code=exc.status_code,
                retryable=retryable_status(exc.status_code),
            ) from exc
        except openai.APIConnectionError as exc:  # includes APITimeoutError
            raise LLMTransportError(
                f"could not reach the model service ({type(exc).__name__}: {exc.message})",
                retryable=True,
            ) from exc
        except ClientAuthenticationError as exc:
            raise LLMTransportError(
                f"authentication failed ({type(exc).__name__}); is `az login` current? {exc}",
                retryable=False,
            ) from exc

        usage = _usage_from_sdk(response.usage)

        if not response.choices:
            raise EmptyChoicesError("the service returned no choices", usage=usage)
        choice = response.choices[0]
        message = choice.message
        if message is None:
            raise MalformedReplyError("the first choice has no message", usage=usage)
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
            raw=response.model_dump(warnings=False),
            finish_reason=choice.finish_reason,
        )
