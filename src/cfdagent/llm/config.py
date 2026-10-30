"""Settings for the LLM client. Secrets are never stored here, only the names of variables."""

import os
from typing import Literal, Self

from pydantic import BaseModel


class ModelConfig(BaseModel):
    """Per-agent model parameters. `deployment` is the Foundry deployment name."""

    deployment: str
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int = 4096
    reasoning_effort: Literal["low", "medium", "high"] | None = None


class LLMSettings(BaseModel):
    """Where and how to reach the models."""

    endpoint: str  # OpenAI v1 base URL, e.g. https://<account>.services.ai.azure.com/openai/v1/
    auth: Literal["entra", "api_key"] = "entra"
    api_key_env: str = "CFDAGENT_LLM_API_KEY"
    timeout: float = 120.0  # seconds per request
    # Automatic SDK retries of failed requests (connection errors, 429, 5xx). Cost caution: each
    # retry of a stalled call can be billed, up to (1 + max_retries) times with the default
    # timeout of 120 s per attempt, and the cost tracker never sees it. Cost-capped runs should
    # use max_retries=1.
    max_retries: int = 2

    @classmethod
    def from_env(cls) -> Self:
        """Read CFDAGENT_LLM_ENDPOINT (required) and CFDAGENT_LLM_AUTH (optional)."""
        endpoint = os.environ.get("CFDAGENT_LLM_ENDPOINT")
        if not endpoint:
            raise ValueError("CFDAGENT_LLM_ENDPOINT is not set")
        auth = os.environ.get("CFDAGENT_LLM_AUTH")
        return cls(endpoint=endpoint, **({"auth": auth} if auth else {}))
