"""Capability spike for one Foundry deployment: auth, tool calling, structured output, reasoning.

Usage (needs `az login` and CFDAGENT_LLM_ENDPOINT):
    uv run python scripts/llm_spike.py --deployment gpt-oss-120b --repeats 3

Results are merged into the JSON file given by --out, keyed by deployment name. The file holds no
tokens or hostnames. Each call is capped at --max-tokens; nothing loops.
"""

import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import BaseModel, ValidationError

from cfdagent.llm.client import ChatResult, LLMClient
from cfdagent.llm.config import LLMSettings, ModelConfig

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "docs" / "adr" / "data" / "0002-llm-spike.json"

CALCULATOR_TOOL = {
    "type": "function",
    "function": {
        "name": "calculator",
        "description": "Evaluate an arithmetic expression and return the number.",
        "parameters": {
            "type": "object",
            "properties": {"expression": {"type": "string", "description": "e.g. 3*4/5"}},
            "required": ["expression"],
        },
    },
}
TOOL_PROMPT = (
    "Re = 100, lid speed 1 m/s, cavity side 1 m: compute the kinematic viscosity "
    "using the calculator tool."
)
STRUCTURED_PROMPT = (
    "Describe the setup for a laminar, steady, incompressible lid-driven flow at Re = 100 "
    "as JSON with the fields solver (OpenFOAM solver name), steady (boolean) and "
    "reynolds (number)."
)
FALLBACK_SYSTEM = (
    "Answer with a single JSON object only, no prose and no code fences, with keys "
    '"solver" (string), "steady" (boolean) and "reynolds" (number).'
)
REASONING_PROMPT = "A pipe of diameter 0.1 m carries water at 2 m/s (nu = 1e-6 m2/s). Give Re."


class Setup(BaseModel):
    solver: str
    steady: bool
    reynolds: float


def _scrub(text: str, endpoint: str) -> str:
    """Remove hostnames and cap length so error text is safe to store."""
    host = re.sub(r"^https?://([^/]+).*$", r"\1", endpoint)
    text = text.replace(host, "<host>")
    text = re.sub(r"https?://\S+", "<url>", text)
    return text[:300]


def _timed(fn, endpoint: str) -> dict[str, Any]:
    start = time.perf_counter()
    try:
        result: ChatResult = fn()
    except Exception as exc:  # the spike records every failure instead of aborting
        return {
            "ok": False,
            "seconds": round(time.perf_counter() - start, 2),
            "status_code": getattr(exc, "status_code", None),
            "error": f"{type(exc).__name__}: {_scrub(str(exc), endpoint)}",
        }
    return {"ok": True, "seconds": round(time.perf_counter() - start, 2), "result": result}


def _usage(result: ChatResult) -> dict[str, Any]:
    u = result.usage
    return {
        "prompt_tokens": u.prompt_tokens,
        "completion_tokens": u.completion_tokens,
        "reasoning_tokens": u.reasoning_tokens,
    }


def check_auth(client: LLMClient, model: ModelConfig, endpoint: str) -> dict[str, Any]:
    messages = [{"role": "user", "content": "Reply with: ok"}]
    call = _timed(lambda: client.chat(messages, model), endpoint)
    if not call["ok"]:
        return {**call, "passed": False}
    result = call.pop("result")
    return {**call, "passed": bool(result.content), "usage": _usage(result)}


def check_tools(client: LLMClient, model: ModelConfig, endpoint: str) -> dict[str, Any]:
    messages = [{"role": "user", "content": TOOL_PROMPT}]
    call = _timed(lambda: client.chat(messages, model, tools=[CALCULATOR_TOOL]), endpoint)
    if not call["ok"]:
        return {**call, "passed": False}
    result = call.pop("result")
    well_formed = any(
        tc.name == "calculator"
        and isinstance(tc.arguments.get("expression"), str)
        and tc.arguments["expression"].strip() != ""
        for tc in result.tool_calls
    )
    return {
        **call,
        "passed": well_formed,
        "tool_calls": [{"name": t.name, "arguments": t.arguments} for t in result.tool_calls],
        "content_excerpt": (result.content or "")[:120],
        "usage": _usage(result),
    }


def check_structured(client: LLMClient, model: ModelConfig, endpoint: str) -> dict[str, Any]:
    """Strict json_schema first; if it fails, try the fallback (JSON requested in the prompt)."""
    messages = [{"role": "user", "content": STRUCTURED_PROMPT}]
    call = _timed(lambda: client.chat(messages, model, response_schema=Setup), endpoint)
    if call["ok"]:
        result = call.pop("result")
        out = {**call, "passed": isinstance(result.parsed, Setup), "usage": _usage(result)}
    else:
        out = {**call, "passed": False}
    if out["passed"]:
        return out

    fb_messages = [{"role": "system", "content": FALLBACK_SYSTEM}, *messages]
    fb = _timed(lambda: client.chat(fb_messages, model), endpoint)
    if not fb["ok"]:
        out["fallback"] = {**fb, "passed": False}
        return out
    result = fb.pop("result")
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (result.content or "").strip())
    try:
        Setup.model_validate_json(text)
        ok = True
    except ValidationError:
        ok = False
    out["fallback"] = {**fb, "passed": ok, "usage": _usage(result)}
    return out


def check_reasoning(client: LLMClient, model: ModelConfig, endpoint: str) -> dict[str, Any]:
    messages = [{"role": "user", "content": REASONING_PROMPT}]
    efforts: dict[str, Any] = {}
    for effort in ("low", "high"):
        cfg = model.model_copy(update={"reasoning_effort": effort})
        call = _timed(lambda cfg=cfg: client.chat(messages, cfg), endpoint)
        if call["ok"]:
            result = call.pop("result")
            efforts[effort] = {**call, "accepted": True, "usage": _usage(result)}
        else:
            efforts[effort] = {**call, "accepted": False}
    accepted = all(e["accepted"] for e in efforts.values())
    rejected = [e for e in efforts.values() if not e["accepted"]]
    status = (
        "pass"
        if accepted
        else ("unsupported" if all(e["status_code"] == 400 for e in rejected) else "fail")
    )
    return {"status": status, "passed": accepted, "efforts": efforts}


def main(
    deployment: Annotated[str, typer.Option(help="Foundry deployment name")],
    repeats: Annotated[int, typer.Option(help="Repeats of the four checks")] = 3,
    max_tokens: Annotated[int, typer.Option(help="Completion cap per call")] = 1024,
    out: Annotated[
        Path, typer.Option(help="JSON results file, merged by deployment")
    ] = DEFAULT_OUT,
) -> None:
    settings = LLMSettings.from_env()
    client = LLMClient(settings)
    model = ModelConfig(deployment=deployment, max_tokens=max_tokens)
    runs = []
    for i in range(repeats):
        runs.append(
            {
                "repeat": i + 1,
                "auth": check_auth(client, model, settings.endpoint),
                "tools": check_tools(client, model, settings.endpoint),
                "structured": check_structured(client, model, settings.endpoint),
                "reasoning": check_reasoning(client, model, settings.endpoint),
            }
        )
    names = ("auth", "tools", "structured", "reasoning")
    summary = {name: sum(1 for r in runs if r[name]["passed"]) for name in names}
    entry = {
        "date": datetime.now(UTC).strftime("%Y-%m-%d"),
        "route": "openai-v1",
        "auth_mode": settings.auth,
        "repeats": repeats,
        "max_tokens": max_tokens,
        "pass_counts": summary,
        "runs": runs,
    }
    data = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    data[deployment] = entry
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")
    typer.echo(f"{deployment}: {summary} (of {repeats}) -> {out}")


if __name__ == "__main__":
    typer.run(main)
