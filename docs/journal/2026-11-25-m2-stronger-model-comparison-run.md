# 2026-11-25 M2 stronger-model comparison run (pipe-re500, gpt-5.4)

ADR-0009 point 3 allows one or two comparison runs on a stronger model than gpt-oss-120b, as a measurement; the default model does not change. The owner approved one run of about $5 (cap $6) on 2026-11-25. "Scorer" means compared with reference data.

## 2026-11-25 decision: gpt-5.4 as the comparison model

No stronger open-weight model is deployable on the subscription: DeepSeek, Kimi and GLM have quota 0, and Llama 3.3 70B and Mistral are weaker; OpenAI models have quota. Smoke calls (each under $0.01) decided between them. gpt-6.2-sol rejects function tools together with any reasoning effort in `/v1/chat/completions` (it accepts tools only through the Responses API or with no reasoning), and the agents need tools in every step, so it was dropped. gpt-5.4 accepts tools with `reasoning_effort=medium` and strict JSON schemas but rejects `logprobs`, as does gpt-5.2. PR #112 (merged as 901dcf8) added the gpt-5.4 price (2.50 / 15.00 USD per 1M input / output tokens), `cfdagent run --max-cost-usd` (at most 10), and made `ModelConfig.request_logprobs` send `logprobs` only for the default model, because `logprobs` exists only for the gpt-oss dropped-call recovery.

## 2026-11-25 experiment: run 20261125-111308, pipe-re500 on gpt-5.4

Image sha-901dcf8; `--model gpt-5.4 --reasoning-effort medium --repair-mode in_place --max-cost-usd 6`; 11:13 to 11:59Z (wall 2772 s). Planner, Geometry, Mesh, Case Setup and Solver Runner each passed on the first attempt, with no fix loops and 0 Courant hits. Mesh: 32,000 cells, maximum aspect ratio 40, checkMesh OK. Solver: simpleFoam on 1 rank; `residualControl` (p 1e-10) stopped it at iteration 745, with final initial residuals Ux 2.0e-11, Uy 1.5e-10, p 9.5e-11 and Uz 3.3e-3 (Uz is the swirl component of the x-axis wedge, which the gate leaves out); one warning, a linear solve that hit maxIter 1000. The run ended **failed** in the Analyst stage on the cost cap: tracked LLM cost 6.0034 USD against the 6.00 limit, with the Analyst having made 19 calls (841k input and 46k output tokens) without writing its quantities. The Critic and the Scorer did not run inside the run.

| Stage | Input tokens | Output tokens | Cost (tracked) |
|---|---|---|---|
| Planner | 28k | 12k | ~$0.26 |
| Geometry | 42k | 6k | ~$0.19 |
| Mesh | 88k | 20k | ~$0.52 |
| Case Setup | 656k | 36k | ~$2.17 |
| Solver Runner | 23k | 0.8k | ~$0.07 |
| Analyst | 841k | 46k | ~$2.79 |

Total 1.68M input and 120k output tokens. The tracker prices cached input at the full price and the client does not record cached tokens, so the real bill is at most this amount and probably lower; it was not yet checked in Cost Management.

## 2026-11-25 observation: evaluation outside the pipeline, a solution that passes the Scorer

The main session evaluated the final case after the run; the agents never saw this. The agent's plan uses the same conventions as the hand-written fixture (axis x, 5-degree wedge, R = 0.005 m), so the hand-written `tests/fixtures/handwritten/pipe_re500/post.py` ran on the run's final case and the deterministic Scorer was applied. Scorer **passed**: pressure drop between x = 0.35 m and 0.45 m 1.609 Pa, error 0.58 % (threshold 5 %); axial velocity profile at x = 0.45 m, maximum error 0.12 % (threshold 3 %). For comparison, gpt-oss-120b completed two pipe runs in Task 17 (see the Task 17 step 2 entry of the same date): run 26 (20261124-132425) passed the pressure-drop check (0.46 %) and failed only on the velocity profile, which was normalised by the ideal radius instead of the mesh wall position (a post-processing cause, later addressed by PR #106); run 33 (20261124-214411, image sha-27a8123) failed with a pressure drop of 2.73 Pa (71 % error) after the solver was stopped before convergence. Other gpt-oss pipe runs ended earlier, at the plan gate, in Geometry, in Mesh or in the Analyst. The gpt-oss runs used older images (sha-27a8123 or earlier); this run used sha-901dcf8, which includes the later general fixes of PR #110.

In one run on a newer image, gpt-5.4 produced a solution that passes the Scorer, and every stage up to the Solver Runner passed on its first attempt. This is n = 1 on one benchmark, and because the image also changed it does not separate model capability from pipeline changes. The run itself is recorded as failed: its Analyst made 19 calls without writing its quantities before the cost cap stopped it, so the Scorer pass comes from the main session's evaluation with the hand-written post-processing, not from the pipeline. Cost is the limit: the Case Setup and Analyst conversations grow large because their context is re-sent on every turn.

## 2026-11-25 experiment: cost of the comparison

LLM up to $6.00 tracked (probably less, see above), plus smoke calls under $0.01. VM about 1.4 h at $0.208/h, about $0.30; the idle shutdown deallocated the VM once during set-up (the image pull is not counted as busy) and it was restarted. Total about $6.3. The M3 follow-ups are in items 7 to 10 of `docs/design/m3-carryover-from-m2.md`.
