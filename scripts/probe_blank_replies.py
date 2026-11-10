"""Replay a recorded model turn against the model endpoint, to study blank replies.

A run's `llm_transcript.jsonl` stores, per model turn, the messages added since the last turn and
the reply. This script rebuilds the exact request of one turn (same messages, same tool schemas,
same settings as `LLMClient.chat`), sends it N times with one setting varied, and prints a table:
how often the reply is blank (no text and no tool call), finish reasons, completion tokens, and
which fields the message carried (`reasoning_content`, `reasoning`, ...).

It is an investigation tool: it makes real LLM calls (costing money), so every call is counted and
the script stops at `--max-cost-usd`. It reads the endpoint from CFDAGENT_LLM_ENDPOINT and
authenticates with Entra ID (DefaultAzureCredential); no secret is stored here.

Example (on the project VM, host venv):
    python scripts/probe_blank_replies.py RUN_DIR/llm_transcript.jsonl --stage analyst \
        --attempt 1 --turn 0 --variants base,low,high --n 10
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import openai
from azure.identity import DefaultAzureCredential, get_bearer_token_provider

from cfdagent.agents.analyst import AnalystStage
from cfdagent.agents.case_setup import CaseSetupStage
from cfdagent.agents.geometry import GeometryStage
from cfdagent.agents.loop import CHECK_TOOL, _elide_old_outputs
from cfdagent.agents.mesh import MeshStage
from cfdagent.agents.solver_runner import SolverRunnerStage
from cfdagent.agents.tools import NoArgs, Tool, ToolRegistry, builtin_tools
from cfdagent.llm.client import UNPARSED_KEY
from cfdagent.rundir import RunDir

STAGES = {
    "geometry": GeometryStage,
    "mesh": MeshStage,
    "case_setup": CaseSetupStage,
    "solver_runner": SolverRunnerStage,
    "analyst": AnalystStage,
}
TOKEN_SCOPE = "https://cognitiveservices.azure.com/.default"
# gpt-oss-120b list prices per 1M tokens (input, output); an estimate used only for the cost cap.
PRICE_IN, PRICE_OUT = 0.15, 0.60


def tool_schemas(stage_name: str) -> list[dict]:
    """The tool definitions the loop sends for a stage (the same code path as `_start_attempt`)."""
    stage = STAGES[stage_name]()
    with tempfile.TemporaryDirectory() as tmp:
        run = RunDir(tmp)
        registry = ToolRegistry()
        for tool in builtin_tools(run, stage.scope):
            registry.add(tool)
        registry.add(
            Tool(
                CHECK_TOOL,
                "Run the automatic checks on the current attempt directory. "
                "Call it only when the work is complete: every call uses one of a few "
                "attempts. Returns the list of problems to fix, or confirms that the "
                "stage passed.",
                NoArgs,
                lambda _a: "",
            )
        )
        # Building the tool objects needs no real run; stub what the stages read from it.
        with (
            patch.object(type(stage), "_solver", create=True, return_value="simpleFoam"),
            patch.object(type(stage), "_spec", create=True, return_value=MagicMock()),
        ):
            extra = stage.extra_tools(MagicMock(), Path(tmp) / "attempt")
        for tool in extra:
            registry.add(tool)
        return registry.schemas()


def echo(reply: dict) -> dict:
    """The assistant message `assistant_message()` builds from a recorded reply."""
    message: dict[str, Any] = {"role": "assistant", "content": reply.get("content") or ""}
    calls = reply.get("tool_calls") or []
    if calls:
        message["content"] = reply.get("content")
        message["tool_calls"] = [
            {
                "id": c["id"],
                "type": "function",
                "function": {
                    "name": c["name"],
                    "arguments": c["arguments"][UNPARSED_KEY]
                    if set(c["arguments"]) == {UNPARSED_KEY}
                    else json.dumps(c["arguments"]),
                },
            }
            for c in calls
        ]
    return message


def history_before(records: list[dict], index: int) -> list[dict]:
    """The messages sent at record `index` of one attempt, rebuilt as the loop built them."""
    messages: list[dict] = []
    for j, rec in enumerate(records[: index + 1]):
        messages.extend(copy.deepcopy(rec["new_messages"]))
        _elide_old_outputs(messages)
        if j < index and rec.get("reply") is not None:
            messages.append(echo(rec["reply"]))
    return messages


CALL_RE = re.compile(
    r"<\|channel\|>(commentary|analysis) to=functions\.(\w+)([^<]*)<\|message\|>(.*?)<\|call\|>",
    re.S,
)


def lost_call(tokens: list[str]) -> tuple[str, str, dict] | None:
    """The harmony tool call hidden in a reply's raw tokens, as (name, constrain marker, args)."""
    for channel, name, marker, body in CALL_RE.findall("".join(tokens)):
        try:
            args = json.loads(body)
        except json.JSONDecodeError:
            continue
        if isinstance(args, dict):
            return name, f"{channel}:{marker.strip() or '-'}", args
    return None


class Probe:
    def __init__(self, max_cost: float) -> None:
        import os

        provider = get_bearer_token_provider(DefaultAzureCredential(), TOKEN_SCOPE)
        self.client = openai.OpenAI(
            base_url=os.environ["CFDAGENT_LLM_ENDPOINT"],
            api_key=provider,
            max_retries=1,
            timeout=180,
        )
        self.cost = 0.0
        self.max_cost = max_cost
        self.calls = 0

    def _bill(self, usage: Any) -> None:
        self.calls += 1
        self.cost += (usage.prompt_tokens * PRICE_IN + usage.completion_tokens * PRICE_OUT) / 1e6

    def call_stream(self, params: dict) -> dict:
        """Same request with stream=True; the deltas are merged into one message."""
        if self.cost >= self.max_cost:
            raise SystemExit(f"cost cap reached (${self.cost:.4f}); stopping")
        stream = self.client.chat.completions.create(
            **params, stream=True, stream_options={"include_usage": True}
        )
        content, reasoning, calls, finish, usage = "", "", {}, None, None
        for chunk in stream:
            if chunk.usage:
                usage = chunk.usage
            for ch in chunk.choices:
                d = ch.delta
                content += d.content or ""
                extra = d.model_extra or {}
                reasoning += extra.get("reasoning_content") or extra.get("reasoning") or ""
                for tc in d.tool_calls or []:
                    slot = calls.setdefault(tc.index, {"function": {"name": "", "arguments": ""}})
                    slot["function"]["name"] += tc.function.name or ""
                    slot["function"]["arguments"] += tc.function.arguments or ""
                finish = ch.finish_reason or finish
        self._bill(usage)
        msg = {
            "content": content,
            "tool_calls": list(calls.values()) or None,
            "reasoning_content": reasoning,
        }
        return self._row(msg, finish, usage, sorted(["reasoning_content"] if reasoning else []))

    def call_responses(self, params: dict) -> dict:
        """The same conversation through the Responses API (if the endpoint offers it)."""
        if self.cost >= self.max_cost:
            raise SystemExit(f"cost cap reached (${self.cost:.4f}); stopping")
        items = []
        for m in params["messages"]:
            if m["role"] == "tool":
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": m["tool_call_id"],
                        "output": m["content"],
                    }
                )
            elif m["role"] == "assistant":
                if m.get("content"):
                    items.append({"role": "assistant", "content": m["content"]})
                for c in m.get("tool_calls") or []:
                    items.append(
                        {
                            "type": "function_call",
                            "call_id": c["id"],
                            "name": c["function"]["name"],
                            "arguments": c["function"]["arguments"],
                        }
                    )
            else:
                items.append(
                    {
                        "role": "developer" if m["role"] == "system" else m["role"],
                        "content": m["content"],
                    }
                )
        tools = [{"type": "function", **t["function"]} for t in params.get("tools", [])]
        resp = self.client.responses.create(
            model=params["model"],
            input=items,
            tools=tools,
            max_output_tokens=params["max_completion_tokens"],
            reasoning={"effort": params["reasoning_effort"]},
        )
        u = resp.usage
        self.calls += 1
        self.cost += (u.input_tokens * PRICE_IN + u.output_tokens * PRICE_OUT) / 1e6
        kinds = [o.type for o in resp.output]
        text = "".join(c.text for o in resp.output if o.type == "message" for c in o.content)
        calls = [
            {"function": {"name": o.name, "arguments": o.arguments}}
            for o in resp.output
            if o.type == "function_call"
        ]
        reasoning = " ".join(
            getattr(x, "text", "")
            for o in resp.output
            if o.type == "reasoning"
            for x in (o.content or o.summary or [])
        )
        msg = {"content": text, "tool_calls": calls or None, "reasoning_content": reasoning}
        usage = type(
            "U",
            (),
            {
                "prompt_tokens": u.input_tokens,
                "completion_tokens": u.output_tokens,
                "completion_tokens_details": u.output_tokens_details,
            },
        )()
        return self._row(msg, resp.status + ":" + "+".join(kinds), usage, kinds)

    def call(self, params: dict) -> dict:
        if self.cost >= self.max_cost:
            raise SystemExit(f"cost cap reached (${self.cost:.4f}); stopping")
        resp = self.client.chat.completions.create(**params)
        self._bill(resp.usage)
        u = resp.usage
        choice = resp.choices[0]
        msg = choice.message.model_dump(warnings=False)
        if choice.logprobs and choice.logprobs.content:
            msg["tokens"] = [t.token for t in choice.logprobs.content]
            msg["lost_call"] = lost_call(msg["tokens"])
        return self._row(msg, choice.finish_reason, u, sorted(choice.message.model_extra or {}))

    def _row(self, m: dict, finish: Any, u: Any, extra_keys: list) -> dict:
        reasoning = m.get("reasoning_content") or m.get("reasoning")
        details = getattr(u, "completion_tokens_details", None)
        msg = type("M", (), {"tool_calls": m.get("tool_calls"), "content": m.get("content")})()
        return {
            "blank": not msg.tool_calls and not (msg.content or "").strip(),
            "finish": finish,
            "ctoks": u.completion_tokens,
            "ptoks": u.prompt_tokens,
            "reasoning_toks": getattr(details, "reasoning_tokens", None),
            "has_content": bool((msg.content or "").strip()),
            "has_tools": bool(msg.tool_calls),
            "has_reasoning": bool(reasoning),
            "extra_keys": extra_keys,
            "reasoning_chars": len(reasoning) if isinstance(reasoning, str) else 0,
            "message": m,
        }


def variant_params(name: str, base: dict) -> dict:
    """`base` changed in one respect; the variant names are documented in the journal."""
    p = copy.deepcopy(base)
    if name == "base":
        pass
    elif name in ("low", "medium", "high"):
        p["reasoning_effort"] = name
    elif name == "noeffort":
        p.pop("reasoning_effort", None)
    elif name == "required":
        p["tool_choice"] = "required"
    elif name == "auto":
        p["tool_choice"] = "auto"
    elif name.startswith("temp"):
        p["temperature"] = float(name[4:])
    elif name == "notools":
        p.pop("tools", None)
    elif name == "developer":
        for m in p["messages"]:
            if m["role"] == "system":
                m["role"] = "developer"
    elif name == "onetool":
        p["tools"] = [t for t in p["tools"] if t["function"]["name"] == "list_dir"]
    elif name.startswith("only="):  # only=a+b : keep just these tools
        keep = set(name[5:].split("+"))
        p["tools"] = [t for t in p["tools"] if t["function"]["name"] in keep]
    elif name.startswith("drop="):  # drop=a+b : remove these tools
        gone = set(name[5:].split("+"))
        p["tools"] = [t for t in p["tools"] if t["function"]["name"] not in gone]
    elif name == "nopreamble":
        p["messages"][0]["content"] += (
            "\n\nWhen you need a tool, call it directly. Do not write any text before a tool call."
        )
    elif name == "logprobs":  # asks for the generated tokens; see `tokens` in the printed rows
        p["logprobs"] = True
        p["top_logprobs"] = 1
    elif name == "noparallel":
        p["parallel_tool_calls"] = False
    elif name in ("stream", "responses"):
        pass  # handled by the transport, see main()
    elif name == "maxtok16k":
        p["max_completion_tokens"] = 16384
    else:
        raise SystemExit(f"unknown variant {name}")
    return p


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("transcript")
    ap.add_argument("--stage", required=True)
    ap.add_argument("--attempt", type=int, default=1)
    ap.add_argument("--turn", type=int, default=0, help="0-based turn index within the attempt")
    ap.add_argument("--variants", default="base")
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--effort", default="medium")
    ap.add_argument("--max-tokens", type=int, default=8192)
    ap.add_argument("--max-cost-usd", type=float, default=0.10)
    ap.add_argument("--deployment", default="gpt-oss-120b")
    ap.add_argument("--show", action="store_true", help="print the replies of each variant")
    ap.add_argument("--show-rows", type=int, default=3)
    ap.add_argument("--run-label", default="")
    ap.add_argument("--show-tail", action="store_true")
    ap.add_argument("--logprobs", action="store_true", help="request raw tokens for every call")
    args = ap.parse_args()

    records = [json.loads(line) for line in Path(args.transcript).read_text().splitlines()]
    records = [r for r in records if r["stage"] == args.stage and r["attempt"] == args.attempt]
    rec = records[args.turn]
    messages = history_before(records, args.turn)
    base = {
        "model": args.deployment,
        "messages": messages,
        "max_completion_tokens": args.max_tokens,
        "reasoning_effort": args.effort,
        "tools": tool_schemas(args.stage),
    }
    print(
        f"turn recorded: usage={rec['usage']} reply_blank="
        f"{not rec['reply']['tool_calls'] and not (rec['reply']['content'] or '').strip()}"
    )
    print("tools:", {t["function"]["name"]: len(json.dumps(t)) for t in base["tools"]})
    probe = Probe(args.max_cost_usd)
    for name in args.variants.split(","):
        params = variant_params(name, base)
        if args.logprobs:
            params["logprobs"] = True
        rows = []
        for _ in range(args.n):
            try:
                send = {"stream": probe.call_stream, "responses": probe.call_responses}.get(
                    name, probe.call
                )
                rows.append(send(params))
            except openai.APIStatusError as exc:
                print(f"{name}: HTTP {exc.status_code}: {str(exc.message)[:200]}")
                break
        if not rows:
            continue
        blanks = sum(r["blank"] for r in rows)
        fin = Counter(r["finish"] for r in rows)
        toks = sorted(r["ctoks"] for r in rows)
        print(
            f"RESULT {args.run_label}{args.stage} a{args.attempt} t{args.turn} {name:9s} "
            f"n={len(rows)} blank={blanks} "
            f"finish={dict(fin)} ctoks_median={toks[len(toks) // 2]} min={toks[0]} max={toks[-1]} "
            f"ptoks={rows[0]['ptoks']} reasoning_toks={rows[0]['reasoning_toks']} "
            f"has_reasoning={sum(r['has_reasoning'] for r in rows)} "
            f"tools={sum(r['has_tools'] for r in rows)} text={sum(r['has_content'] for r in rows)} "
            f"extra_keys={rows[0]['extra_keys']}"
        )
        if args.logprobs:
            blank_rows = [r for r in rows if r["blank"]]
            found = [r["message"].get("lost_call") for r in blank_rows]
            markers = Counter(f[1] for f in found if f)
            print(
                f"   LOST-CALL blank={len(blank_rows)} recoverable={sum(1 for f in found if f)} "
                f"constrain_markers={dict(markers)} names={dict(Counter(f[0] for f in found if f))}"
            )
            ok_markers = Counter()
            for r in rows:
                if not r["blank"] and r["message"].get("tokens"):
                    ok_markers.update(
                        re.findall(
                            r"commentary to=functions\.\w+(.{0,14}?)<\|message",
                            "".join(r["message"]["tokens"]),
                        )
                    )
            print(f"   OK-CALL constrain_markers={dict(ok_markers)}")
        if args.show_tail:  # last tokens of the first unrecoverable blank and the first ok reply
            for label, pick in (
                ("UNRECOVERED-BLANK", lambda r: r["blank"] and not r["message"].get("lost_call")),
                ("OK", lambda r: not r["blank"]),
            ):
                hit = next((r for r in rows if pick(r) and r["message"].get("tokens")), None)
                if hit:
                    print(f"   {label} tail:", hit["message"]["tokens"][-40:])
        if args.show:
            for row in rows[: args.show_rows]:
                m = row["message"]
                calls = [
                    (c["function"]["name"], c["function"]["arguments"][:60])
                    for c in (m.get("tool_calls") or [])
                ]
                print(
                    f"   [{'BLANK' if row['blank'] else 'ok'}] "
                    f"text={(m.get('content') or '')[:80]!r}"
                    f" tool_calls={calls} reasoning={(m.get('reasoning_content') or '')[:260]!r}"
                )
                if m.get("tokens"):
                    print("   tokens:", m["tokens"])
    print(f"calls={probe.calls} est_cost=${probe.cost:.4f}", file=sys.stderr)


if __name__ == "__main__":
    main()
