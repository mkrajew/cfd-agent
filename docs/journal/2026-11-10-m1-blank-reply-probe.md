# 2026-11-10 M1 investigation: why gpt-oss-120b returns blank replies

Setup: no pipeline run. A standalone probe (`scripts/probe_blank_replies.py`) replays recorded model turns of runs 20261110-082841 (analyst blank in every turn) and 20261110-084225 against the project's model endpoint (gpt-oss-120b, Foundry OpenAI v1 route, chat-completions, Entra ID, from the project VM). It rebuilds the exact request from `llm_transcript.jsonl` (same messages, same tool schemas from the stage code, `max_completion_tokens` 8192, reasoning effort medium) and changes one thing at a time. The rebuilt prompt had exactly the recorded token count (4613 for the analyst's first turn), so the replay is faithful. Nothing under `src/` was changed.

## 2026-11-10 experiment: blank is the norm for exploring tool calls, and not a random glitch

Replays of the analyst's first turn (system prompt and task only, no earlier assistant message), n per variant, "blank" = no text and no tool call, `finish_reason` `stop`:

| Variant (analyst, attempt 1, turn 0) | Blank / n |
|---|---|
| default (effort medium), 5 batches | 53 / 53 |
| `tool_choice` auto (explicit) | 8 / 8 |
| reasoning effort low / high / none | 9 / 10, 10 / 10, 9 / 10 |
| temperature 0 / temperature 1 | 10 / 10, 10 / 10 |
| `tool_choice` required | HTTP 400, not supported for this model |
| streaming | 8 / 8 |
| Responses API (the endpoint offers it) | 8 / 8 |
| `developer` role instead of `system` | 8 / 8 |
| prompt line "no text before a tool call" | 8 / 8 |
| `parallel_tool_calls` false | 8 / 8 |
| only `list_dir` offered | 12 / 26 |
| two to five tools (list_dir with read_file, write_file or check_stage; or calculator, check_stage or run_utility removed) | 5-6 of 6 each |

The rate depends on what the model wants to do, not on the settings. Default settings, other turns of the same run:

| Turn | Model wants to | Blank / n |
|---|---|---|
| analyst a1 t0 | list a directory | 53 / 53 |
| case_setup a1 t0 | list a directory | 6 / 6 |
| case_setup a1 t1 | list a directory | 29 / 44 |
| mesh a1 t0 | list or write | 10 / 36 |
| mesh a1 t2 | list a directory | 16 / 26 |
| geometry a1 t0 | write the geometry script | 1 / 16 |

## 2026-11-10 observation: the model does emit the tool call, the service drops it

Every reply carries a non-standard `reasoning_content` field and the usage has no reasoning-token count. Blank replies are not empty: they bill 28-50 completion tokens, more than their one-line reasoning needs. Asking for `logprobs` returns the raw tokens. A blank reply is, for example:

`<|channel|>analysis<|message|>... Let's list directory.<|end|><|start|>assistant<|channel|>commentary to=functions.list_dir code<|message|>{"path": "case"}<|call|>`

A reply that works ends with `... functions.list_dir <|constrain|>json<|message|>{...}<|call|>`. So the model writes a complete, valid call, but with the constraint marker ` code` instead of `<|constrain|>json`, or on the `analysis` channel instead of `commentary` (also with ` code`). The service parser does not recognise either form, so it returns `finish_reason` `stop`, no `tool_calls`, and sometimes the argument JSON appended to `reasoning_content` (for example `Let's list files.{"path": ""}`).

Recoverability: in the final sample (4 turns, 20 replays each, logprobs on) 47 of 47 blank replies contained a complete call that parses (37 on the commentary channel, 10 on the analysis channel, all with ` code`; the analyst's first turn split 10 / 10 between the two channels). A first version of the pattern, which looked only at the commentary channel, found 22 of 31. Injecting a recovered call as an ordinary `tool_calls` message with a stub tool result gave a normal next turn: 6 of 6 times the model's next call (`list_dir` of a sensible path) was again dropped and again recoverable, so recovery has to apply on every turn.

## 2026-11-10 decision: findings on the four hypotheses, and a recommended fix (not implemented)

(1) Reasoning not round-tripped: not the cause. The analyst's first turn has no earlier assistant message and is blank 53 of 53. Whether sending `reasoning_content` back changes later turns was not tested. (2) Tool call lost in the reasoning channel: confirmed, in the precise form above (a malformed constraint marker or channel that the service parser rejects). (3) Reasoning effort: not the cause (low, medium, high and none all blank). (4) Tool list and system prompt: no trigger found. The only effect is that offering one tool halves the rate; the dominant factor is the kind of call. Also not helpful: streaming, the Responses API, the `developer` role, a prompt line, temperature, `parallel_tool_calls`. Nudging and plain retrying cannot fix the exploring turns (about 97 % blank per sample) and also bill every try.

Recommended change, for the owner to approve: in `LLMClient.chat` request `logprobs=True` when tools are sent. If the reply has no text and no `tool_calls`, search the concatenated tokens for `<|channel|>(commentary|analysis) to=functions.NAME ... <|message|>JSON<|call|>`, parse the JSON object, and return it as a `ToolCall` (generated id, `finish_reason` `tool_calls`), recording in `raw` that it was recovered. Only blank replies are touched, so the change does nothing if the service fixes its parser. Open checks before relying on it: that `logprobs` is accepted together with every request the agents send (structured-output turns send no tools and need none), the size and latency of the extra payload, and a unit test with recorded token lists. The blank-reply nudges and free restarts stay as a safety net for replies that are blank for another reason. The service-side defect (parser vs. model habit) is also worth reporting to the platform vendor.

## 2026-11-10 experiment: cost and limits of the probe

About 400 probe calls, estimated LLM cost about $0.32 (list prices; the cap was $0.50). The VM was started twice (it had deallocated itself on the idle policy in between) and is deallocated. Limits: one benchmark, one model deployment, and mainly the analyst, case_setup and mesh turns of two runs; the rates are for these replays and not for whole runs; turns after a restart or rollback could not be rebuilt exactly and were left out.
