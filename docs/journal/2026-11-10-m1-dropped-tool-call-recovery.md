# 2026-11-10 M1 implementation: recovering tool calls the service drops

## 2026-11-10 decision: read dropped tool calls from the raw tokens

Following the probe of PR #68 (the model writes a complete tool call with the marker ` code` or on the analysis channel and the Foundry parser drops it), `LLMClient.chat` now asks for `logprobs` whenever tools are sent. A reply with no text and no `tool_calls` is searched for complete calls to offered tools with JSON-object arguments; they become ordinary `ToolCall`s with generated ids, `finish_reason` becomes `tool_calls`, `ChatResult.recovered` is set and the transcript line carries `"recovered": true`. Only a reply that ended with `stop` is recovered (a cut-off or filtered one is not), replies that already hold text or tool calls are never touched, so the change does nothing if the service parser is fixed. Alternatives rejected: retrying or nudging (the exploring turns are blank about 97 % of the time per sample) and parsing `reasoning_content` (it holds the arguments only sometimes and never the tool name).

## 2026-11-10 experiment: live check of the patched client

The patched client was run on the project VM against the endpoint (gpt-oss-120b, effort medium) on four recorded turns, five calls each: 0 of 20 replies were blank, and 13 of 20 carried recovered calls (analyst first turn 5 of 5, a case_setup turn 4 of 5, a mesh turn 3 of 5, a geometry write turn 1 of 5). The endpoint accepts `logprobs` together with tools. Unit tests: 1934 passed. Cost about $0.01; the VM was deallocated afterwards. Not measured: the effect on whole runs, and the extra response size of `logprobs`.
