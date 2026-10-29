# ADR-0002: LLM capability spike and client behaviour

- **Status:** Accepted, 2026-10-29
- **Deciders:** the owner

## Context

The design (section 14.1) names three models for the M3 campaign: `gpt-oss-120b` (main), `Llama-3.3-70B-Instruct` and `qwen3-32b` (comparison). The M0 spike must show which of them can be used through one interface (tool calling, structured output, reasoning effort, Entra ID) and fix the fallbacks the client will use. The spike is `scripts/llm_spike.py`; raw results are in `docs/adr/data/0002-llm-spike.json`. Models are served by one Azure AI Services account in `italynorth` (resource group `rg-cfdagent`) through the OpenAI v1 route `https://<account>.services.ai.azure.com/openai/v1/`, read from `CFDAGENT_LLM_ENDPOINT`.

Method: 3 repeats of four checks per deployment, from the laptop with the owner's `az login` (`AzureCliCredential` through `DefaultAzureCredential`, scope `https://cognitiveservices.azure.com/.default`, role `Cognitive Services OpenAI User`). Each call is capped at 1024 completion tokens. The checks are: (1) auth: plain completion; (2) tools: a `calculator(expression)` tool call is returned and well formed; (3) structured: strict `json_schema` reply validates into a pydantic model (a prompt-only JSON fallback is tried only if that fails); (4) reasoning: `reasoning_effort` `low` and `high` are both accepted.

## Results

Pass counts out of 3 repeats:

| Model | auth (Entra) | tool calling | structured output (strict json_schema) | reasoning_effort accepted |
|---|---|---|---|---|
| `gpt-oss-120b` | 3/3 | 3/3 | 3/3 | 3/3 |
| `Llama-3.3-70B-Instruct` | 3/3 | 3/3 | 3/3 | 3/3 (see note) |
| `qwen3-32b` | not tested: could not be deployed (ARM rejected both SKUs tried) | | | |

Mean latency and tokens per call (3 repeats):

| Model | plain | tool call | structured | effort low | effort high |
|---|---|---|---|---|---|
| `gpt-oss-120b` | 0.9 s | 0.8 s | 1.3 s | 0.8 s, 305 completion tokens | 1.3 s, 589 completion tokens |
| `Llama-3.3-70B-Instruct` | 0.8 s | 0.4 s | 0.8 s | 1.7 s, 268 completion tokens | 2.0 s, 260 completion tokens |

Observations:

- Entra ID works for both models on the OpenAI v1 route with the owner's CLI credential and the `Cognitive Services OpenAI User` role. No account key was used or read, and no alternative route (Azure AI Inference `/models`) was needed.
- `max_completion_tokens`, `tools`, `response_format` of type `json_schema` with `strict: true` and `reasoning_effort` are all accepted by both deployments.
- `gpt-oss-120b`: the effort setting has a visible effect (high produces about 1.9 times the completion tokens of low on the same prompt). The service does not report reasoning tokens separately (`completion_tokens_details` is empty), so `Usage.reasoning_tokens` is `None` on this route; reasoning tokens are included in `completion_tokens`. The reply message carries an extra `reasoning_content` field, kept in `ChatResult.raw`.
- `Llama-3.3-70B-Instruct`: the API accepts `reasoning_effort` without error but the setting has no effect (268 vs 260 tokens). The check therefore only proves acceptance. For this model the effort parameter must be treated as unsupported in the experiment design and not as an experimental factor.
- Tool calls from Llama came with arguments such as `1 / 100` and `1/(100*1*1)`; they are well formed but vary between repeats, so tool-argument validation must stay in the orchestrator.
- The checks are small (one tool, one flat schema). A pass here does not show reliability in long multi-turn agent loops; M1 and M3 will measure that.
- Spike cost: about 3 100 prompt and 5 600 completion tokens in total, a fraction of one cent at serverless prices.

## Decision

1. **One interface for all models.** The client talks to the OpenAI v1 route with Entra ID bearer tokens (`LLMClient`, `src/cfdagent/llm/`). Both deployed models pass all checks on it, so no model needs a different interface.
2. **Client behaviour.**
   - `reasoning_effort` is sent only when the model config sets it. It is set only for `gpt-oss-120b`; for other models the experiment config leaves it `None`.
   - Structured output uses strict `json_schema`; the reply is validated with pydantic and `StructuredOutputError` carries the raw text. If a model later rejects `json_schema` or fails it, the fallback is: ask for a single JSON object in the system prompt, validate with pydantic, and retry with the validation error appended (the spike script contains the prompt-only variant). Not needed so far.
   - If a model were to reject reasoning effort, reasoning depth is steered through the system prompt instead ("think step by step" versus "answer briefly").
   - If a model were to reject Entra ID, the fallback is an API key kept in Key Vault and passed through the environment variable named by `LLMSettings.api_key_env` (`auth="api_key"`). Not needed so far.
3. **`qwen3-32b` is excluded.** It is not deployable on the AI Services account: ARM rejects both `GlobalStandard` and `DeveloperTier`, so the spike could not run for it. Under the comparison-model fallback rule (design section 14.1) a replacement (`Llama-4-Scout`, `mistral-small`, `Phi-4`, in that order) would have been spike-tested next. The owner declined deploying a replacement on 2026-10-29 because two models are enough. Both facts are findings of the M0 spike.
4. **Final M3 model list.**
   - Primary: `gpt-oss-120b`, with reasoning effort low, medium (default) and high. Medium was not exercised in the spike; only low and high were.
   - Comparison: `Llama-3.3-70B-Instruct`, effort not varied.
   - `reasoning_effort` is an experimental factor for `gpt-oss-120b` only; Llama ignores it.
   - Any interface difference between models must still be stated in the results, as the design requires. At present there is none.

## Consequences

- M1 can use `gpt-oss-120b` immediately; its cost and latency are low enough for the planned run counts.
- The reasoning-effort comparison in M3 (design section 14.1, row 3) applies to `gpt-oss-120b` only.
- Reasoning token counts per run cannot be reported separately; reports use completion tokens and say so.
- The client depends on `httpx2`, the HTTP library of the installed `openai` package (version 3.x); it is declared in `pyproject.toml`.
- Rate limits and quota ceilings were not stress-tested. The design's quota concern (20 for Llama-3.3-70B-Instruct) is still open until the first campaign runs.
