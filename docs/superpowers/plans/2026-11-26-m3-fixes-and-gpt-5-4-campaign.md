# M3 Fixes and gpt-5.4 Campaign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. **Each task runs in its own session** (owner's rule).

**Goal:** Remove the few pipeline problems that would make a gpt-5.4 run expensive or lose it for an avoidable reason, freeze a release, run each of the five development benchmarks once and one or two generality tasks once on gpt-5.4, and deliver a table of observations for the report.

**Architecture:** The pipeline structure does not change. The cost tracker learns the cached-input price; old tool output is trimmed in batches so the cached prompt prefix survives; a few deterministic checks are added or corrected before money is spent (plan gate, mesh gate, force function objects before the trial run). A small tested script replaces `cfdagent bench`. A tag freezes code, prompts, image and configuration before the first campaign run; the generality set is added after the tag as data only.

**Tech Stack:** unchanged from M2: Python 3.12, uv, pydantic v2, typer, openai SDK + azure-identity + azure-storage-blob, Docker sandbox, OpenFOAM ESI v2512, Gmsh, CadQuery, numpy, pytest, ruff, GitHub Actions.

**Spec:** `docs/design/2026-10-28-cfd-agent-design.md` (sections 6.2, 6.5, 6.10, 9.1, 9.2, 11, 12, 14) as amended by `docs/adr/0010-m3-one-run-per-benchmark-on-gpt-5-4.md` (proposed with this plan). Inputs: `docs/design/m3-carryover-from-m2.md` (items 1-18, "carry-over N" below), the journal entries of 2026-11-25 and 2026-11-26, `docs/release-notes/v0.2.0.md`, `docs/runbooks/m2-runs.md`. Format model: `docs/superpowers/plans/2026-11-14-m2-robustness-and-full-benchmark-set.md`.

## Global Constraints

- Everything in the repository is English. Claude talks to the owner in Polish.
- Python 3.12, `uv`, `ruff` line length 100, `pytest --strict-markers`. Default tests (`tests/unit`) need no Docker, no network, no real LLM.
- OpenFOAM **ESI v2512** pinned. CadQuery creates every geometry; no blockMesh path. 2D cases one cell thick with `empty` front and back. **Max 4 MPI ranks.** The pipeline runs only `icoFoam`, `simpleFoam` and `pimpleFoam`.
- Campaign model: `gpt-5.4`, reasoning effort `medium`, `--repair-mode in_place`, Entra ID only. `gpt-oss-120b` stays the code default. No further `gpt-oss-120b` campaign, no Llama-3.3-70B comparison, no reasoning-effort arm (owner, 2026-11-26).
- Generality rule: prompts, knowledge notes and tool code contain only general CFD/OpenFOAM knowledge; `src/` never names a benchmark case. `tests/unit/test_generality_lint.py` and `cfd-reviewer` enforce it.
- Blind evaluation: no agent, the Critic included, can read `benchmarks/` or `results/comparison.json`; the Scorer runs only after the Critic step and never triggers a retry. No file an agent can read, and no run id, names a benchmark.
- Generality set: texts written by the owner; **never run by the pipeline before the frozen release**; nothing learned from it flows into `src/`, `docker/` or `scripts/`. This plan holds no reference data for it, and no task before Task 9 may add any outside `benchmarks/generality/`.
- No secrets, keys, tokens, subscription IDs, IP addresses or e-mail addresses in the repository. No simulation results or transcripts in git.
- Azure: CLAUDE.md rule 4. Announce in chat before spending. Explicit owner approval for anything outside `rg-cfdagent`, deleting the resource group, storage account or `runs` container, **any task or campaign estimated above $5** (D3 gives it for the campaign), and anything beyond the **M3 envelope of $45**. The M0-M2 remainder and the reserve are not part of M3.
- **No session edits `.claude/settings.json`.** The deny rule on `benchmarks/generality/**` is lifted and restored by the owner by hand.
- Workflow: conventional commits; one branch per task (`feat/m3-<slug>`, `fix/...`, `infra/...`, `docs/...`), each from the current `main`; every task is a PR with what, why, how verified and Azure cost impact; the main session merges after green CI and approving reviews (ADR-0009). Commit trailer and PR footer as in CLAUDE.md rule 2.
- Lean workflow (owner-approved): subagents on Sonnet (implementers `high`, reviewers `xhigh`); minor review findings deferred to a list, at most two fix rounds per review; `cfd-reviewer` only for physics, prompts, notes, tool code, agent-facing messages and benchmarks.
- New run-record fields always have defaults, so `cfdagent status` and `list` still read M1 and M2 run records.
- Import boundaries hold: `tools/function_objects.py` is container side (never imports `cfdagent.bench`, `agents`, `orchestrator`); host code never imports `cfdagent.tools`. `tests/unit/test_import_boundaries.py` covers them.

**Session start** (every task): read `CLAUDE.md`, this plan's header and the task, the files the task names; check `git status`; branch from the current `main`.

**Standard close** (last step of every task): `uv run pytest`; `uv run ruff check . && uv run ruff format --check .`; for `[integration]` tasks the integration suite in the image (CLAUDE.md); `code-reviewer` on the diff with the spec only, plus `cfd-reviewer` for `[cfd]` tasks; a `changes` or `reject` verdict blocks the PR; commit with the given message; PR description.

## Decisions for the owner

| # | What | Recommendation | Blocks |
|---|---|---|---|
| D1 | **ADR-0010** (drafted with this plan, Proposed): the narrowed M3 scope, what "statistics ready for the report" now means, a freeze tag, no `cfdagent bench`, the run rules (when a run may be repeated), the generality rules | Approve | Tasks 2-11 |
| D2 | **Per-run cost cap** for gpt-5.4 (`--max-cost-usd`; the CLI allows at most $10) | **$8**, counted at the billed price list after Task 2 (a run can pass it by at most one model call). Trade-off: with $8, if every run hit its cap only three runs would start within D3; with $6, all five development benchmarks start even then, but a run in the upper part of the guessed cost range is more likely to be cut off. If the service reports no cached-token count (Task 2, live check), use $10, treat tracked cost as an upper bound and revisit D3 | Tasks 8, 10 |
| D3 | **Campaign LLM budget** inside the $45 M3 envelope. Also the rule-4 approval for a campaign estimated above $5 | **$30** of tracked model cost. The campaign script does not start a run whose cap would take the total above it. Before Task 10, Cost Management is checked against the $45 | Tasks 8, 10 |
| D4 | **Fix list** (section "Fixes") | In: carry-over 7, 8, 12, 13, the cheap parts of 1 and 3, a campaign script (covers 18), a wall-clock option. The rest stays out | Tasks 2-6 |
| D5 | **Campaign configuration** | gpt-5.4, medium effort, `in_place`, **wall clock 240 min** per run (M2 used 120; see Task 7 Step 6), 2 Critic rounds, everything else default (3 attempts per stage, 10 fix loops, 4 ranks). Know this about the Critic: the Scorer runs only if the final pass completes, so a send-back can turn a complete solution into a failed, unscored run (as in M2 run 201843). The alternative, 0 rounds (the Critic only reports), costs less and always scores a complete solution, but tests a different system; if chosen, Task 6 adds `--max-critic-rounds` | Task 8 |
| D6 | **Generality tasks:** how many and which families; the owner writes the texts | Two, if the budget allows (families and solver limits in Task 9) | Task 9 |
| D7 | **Generality references, acceptance thresholds, and the fixed list and order** of the tasks (D5 of M2) | Approve before the first generality run; nothing of it changes afterwards | Task 10 |
| D8 | **Freeze tag `v0.3.0`** | Approve when Task 7 is green | Tasks 8, 10 |
| D9 | After M3: the idle environment keeps costing about $8.6 a month | Decide at close-out (Task 11) | nothing |

## What preparing this plan showed (read before D2-D5)

1. **Cached-token pricing (carry-over 7) does not lower the Azure bill.** It makes the tracked cost and the cost cap agree with the bill. In the one gpt-5.4 run the record tracked $6.00, while Azure billed $2.63 for all gpt-5.4 usage of the project (that run and the smoke calls).
2. **Most of that bill is output.** Output tokens $1.81, cached input $0.38, uncached input $0.45. Reducing context (carry-over 8) can lower only the input part, $0.83 of $2.63 here. The other lever is fewer wasted model turns, because every turn produces output tokens: the Analyst made 19 calls and wrote no quantities.
3. **Part of carry-over 8 already exists.** A tool result is cut at 8,000 bytes (`agents/tools.py`, `MAX_TOOL_OUTPUT_BYTES`) and old tool results are removed above 200,000 characters (`agents/loop.py`, `_elide_old_outputs`). Read from the code, not measured: once a conversation passes that limit, the trimming rewrites one more old message before almost every call. A rewritten old message ends the cached prefix there, and cached input costs a tenth of uncached input. Whether this happened in the gpt-5.4 run is not checked; the small billed uncached input (about 180,000 tokens, derived from the bill, smoke calls included) suggests it did not happen in many calls.
4. **The "Could not find rho" failure is already described in a knowledge note** (`knowledge/notes/forces-and-coefficients.md`, rule 2), which M2 added before the acceptance series, and runs still failed on force function objects. So carry-over 12 needs a deterministic check, not another note.
5. The `gpt-5.4` deployment is not in `infra/main.bicep`, so it was created outside the project's infrastructure code (Task 7 records it there).

## Are a frozen release and `cfdagent bench` still needed?

- **Frozen release: yes.** It costs nothing, and the generality tests depend on it: without a tag that predates them, it cannot be shown that nothing learned from them reached the system (design 9.1). Every run records its commit, image and prompt hashes.
- **`cfdagent bench`: no.** Six or seven runs are one sequence. A small tested script (Task 6) runs them in order with correct exit codes and a budget stop, and a second one builds the table from run records. ADR-0010 records this change to design 6.10, 11 and 12.

## What the M3 results can and cannot say

State this wherever M3 results appear (README, journal, report):

- **One run per development benchmark on gpt-5.4 (n = 1) is an observation, not a success rate.** A pass means that the scored quantities of that run were within the thresholds; it shows no grid or domain independence. A fail shows one way the system can fail.
- **The three runs per benchmark on gpt-oss-120b of the M2 acceptance series (n = 3) are observations, not rates, too.**
- **The two sets do not compare the two models.** M3 runs a different build (with the fixes below), with a different cost cap and wall clock. No statement of the form "gpt-5.4 is better by X" follows from these data.
- The development benchmarks are the tasks the pipeline was built on. One or two generality tasks cannot show how general the system is; they show what happened on those tasks. The agents can search the OpenFOAM tutorials, so each generality task is reported with its closest tutorial.
- Blind evaluation withholds the project's files only. These are classic published cases, and a model may know their values from training. No deterministic check shows that `quantities.json` comes from the solution, so `cfd-reviewer` reads the `post.py` of every run that passes the Scorer (Tasks 8 and 10).
- Critic accuracy is reported separately for M2 and M3 (different Critic models and builds).
- gpt-5.4 is a commercial, hosted model. The project's open-weight evidence is M1 and M2. Each M3 record stores the served model name, because the deployment can change on the service side.
- "Passed" means compared **with reference data** by the Scorer. The Critic judges physical checks only.

## Fixes

Ranked after the two the owner put first, by expected effect on gpt-5.4 runs per cost of building. The evidence of every item is from one or a few runs, mostly gpt-oss-120b, so "expected effect" is a judgement, not a measurement.

| Rank | Carry-over | Change | Why | Task |
|---|---|---|---|---|
| 1 (owner) | 7 | Record cached tokens, bill them at the cached price, record the served model | Without it the cap stops gpt-5.4 runs at less than half of it in billed terms | 2 |
| 2 (owner) | 8 | Measure the one gpt-5.4 transcript; trim old tool output in batches; one more change only if the measurement supports it | Protects the cached prefix; targets wasted Analyst turns. Expected input saving: at most a few tenths of a dollar per run | 3 |
| 3 | 13, plus two plan-gate rules | The plan gate rejects a solver the pipeline does not run, `simpleFoam` for a transient plan and `icoFoam` for a turbulent one | Very small. Today an unsupported solver ends a run as an internal error with no attempt made. A steady solver for a transient flow cannot be repaired later (the solver gate routes only to Case Setup or Mesh); `icoFoam` solves a turbulent plan as laminar without any message | 4 |
| 4 | 12 | Report every force function-object problem at once, before the trial run | Both cylinder benchmarks need forces. Three M2 acceptance runs ended on it, each trial run stopping on one new problem (a missing reference velocity was also seen in Task 17); on gpt-5.4 each Case Setup attempt is costly (Case Setup was about $2.17 tracked in the one run, which passed it first time) | 5 |
| 5 | 1, cheap part | After meshing, scale the plan's run time to the real cell count and reject a mesh that exceeds the run limit | One oversized mesh can consume the only transient observation | 4 |
| 6 | 3, text part, and one wrong message | Stop the gate advising to loosen tolerances; compare write times at least 100 iterations apart; stop telling the Solver Runner that the plan's estimate is for one core | Text only. A loosened tolerance let one M2 run end unconverged | 4 |
| 7 | 18 and design 12 | A tested campaign script with correct exit codes, a budget stop and a repeat guard, a results table, `--wall-clock-min` | Needed to run the campaign safely; replaces `cfdagent bench` | 6 |

**Left out** (the owner may pull any of them back in under D4):

| Carry-over | Reason | What would bring it back |
|---|---|---|
| 1 (rest), 2 runtime projection from the trial run | Medium changes against one gpt-oss run; the cheap check above and the 240-minute wall clock (D5) cover the larger risk | A campaign run that dies on the wall clock is reported as such |
| 3 drift check when `residualControl` ended the run | Medium; every M3 run is scored against reference data, so a wrong "converged" value fails the Scorer instead of passing unnoticed | Ad hoc runs without reference data (not in M3) |
| 4 number-based Analyst | Medium; its evidence is a gpt-oss run | gpt-5.4 results with unsupported qualitative claims (record them) |
| 5 route from Geometry to Planner | Design change with an ADR; wrong plan volumes were seen on gpt-oss only | A campaign run ending on a wrong plan volume |
| 6 minors of PR #110 | Costs one wasted pass at worst | - |
| 9 commercial arm | Decided; replaced by ADR-0010 | - |
| 10 Responses API | gpt-5.4 works on chat completions | A newer model |
| 11 manifest helper | gpt-5.4 passed Geometry first time; the gate message already names the unmatched surface; about $0.2 per attempt | A campaign run ending on manifest coverage |
| 14, 15 invented patch type, `none` scheme | OpenFOAM's error names both; each seen once on gpt-oss; three attempts available | - |
| 16 convergence statement | Seen in one gpt-oss run (three attempts with the same literal message; cause not examined); judged unlikely on gpt-5.4 | A campaign run ending on it |
| 17 Critic disagreement | No prompt change without gpt-5.4 evidence; agreement is already recorded (`critic_agreement`). The table shows Critic rounds and whether an earlier pass reached the Analyst (D5) | The M4 report |
| Also seen: mixed-rejection retry, volume-from-calculator rejections, records stuck "running", blob writes | A calculator rejection costs one cheap Planner rework; a run with about 90,000 files would cost about $0.4 in blob writes (an inference from M2) | Blob writes: check at close-out |

## Recommended order

1. **Task 1** (this plan and ADR-0010; D1-D5).
2. **Fixes:** Task 2 → Task 3 → Task 4 → Task 5 → Task 6. Task 2 goes first because the cap depends on it. Shared files: `agents/loop.py` (2, 3), `agents/analyst.py` and `prompts/analyst.md` (3, 4), `record.py` and `orchestrator.py` (2, 6). Two tasks that touch the same file are never in flight together.
3. **Freeze:** Task 7 (D8).
4. **Development campaign:** Task 8, canary run first.
5. **Generality:** Task 9 (preparation; may run while Task 8 runs, because it only adds data and uses local Docker) → D7 → Task 10 (only after Task 8: owner's order).
6. **Close-out:** Task 11.

The owner can write the generality task texts at any time, but **keeps them outside the repository** and gives them only to the Task 9 session.

## Review Focus

1. **Cost counted too low** (a cached count larger than the prompt, negative, or missing; mixed pricing): the tracker never bills less than the service would; an implausible count is billed at the full input price. Task 2 (`test_usage_drops_implausible_cached_tokens`, `test_tracker_ignores_a_cached_count_above_the_prompt`, `test_run_pricing_takes_the_dearest_cached_price`).
2. **Trimming that breaks the conversation** (a tool message without its call, a trim that never stops, history rewritten on every call): messages keep their order and ids, and the call after a batch leaves earlier messages unchanged. Task 3 (`test_next_call_leaves_older_messages_unchanged`, `test_trimming_that_cannot_reach_low_water_stops`).
3. **The force check rejecting a valid case:** a form OpenFOAM v2512 accepts, a `$` expansion, an `#include`, a disabled entry, a patch group (the implicit `wall` group too) or a regular expression never gives a problem. Task 5 (`test_v2512_agrees_with_the_force_check`, `test_unsure_forms_are_silent`, `test_handwritten_cases_have_no_force_problems`).
4. **The campaign script misbehaving:** it overspends, runs a benchmark a second time, names a benchmark in a run id, lets `run_args` override the cap, carries on after a broken run or loses an exit code. Task 6 (`test_budget_stop_before_a_run_that_might_not_fit`, `test_refuses_a_benchmark_already_in_the_log`, `test_campaign_run_id_never_names_the_benchmark`, `test_run_args_cannot_override_protected_flags`, `test_stops_after_a_broken_run`).
5. **Generality leaking into the frozen build, or the build changing during the campaign:** the generality files arrive only after the tag, and on the VM the checkout differs from the tag only under `benchmarks/generality/`. Task 9 Step 6, Task 10 Step 2.

## Owner touch points in M3

| When | What the owner does |
|---|---|
| Task 1 | Approves the plan, ADR-0010 and D2-D5 |
| Task 3 | Hears what the measurement of the gpt-5.4 transcript showed |
| Task 7 | Approves the tag `v0.3.0` (D8); hears the VM timing of the hand-written transient case |
| Task 8 | Told after the canary and after the series; decides whether a stopped run is invalid; sees the table and plots (milestone approval point) |
| Task 9 | Lifts and later restores the deny rule by hand; writes the task texts (D6); approves references, thresholds and the list (D7) |
| Task 10 | Sees the generality results |
| Task 11 | Approves the release notes; decides on the idle cost (D9) |

## Budget

M3 envelope: **$45** (design 14.2). M3 spend = Cost Management actual cost of `rg-cfdagent` from 2026-11-27 (M2's snapshot of $27.54 ends on 2026-11-26; later charges for earlier days count to M2). Prices: VM $0.208/h; fixed items about $8.6/month (disk about $4.8, public IP about $3.65, storage about $0.1; `docs/runbooks/m2-runs.md`); gpt-5.4 per 1M tokens $2.50 input, $0.25 cached input, $15.00 output.

**Cost per gpt-5.4 run: $2-6, central $3.** This is a guess from one observation: $2.63 billed for a run that stopped in the Analyst (smoke calls included). A complete run adds the rest of the Analyst and the Critic, and each fix loop re-runs stages.

| Item | Runs / time | Central | Upper |
|---|---|---|---|
| Fixed infrastructure, 3 weeks (upper: 4 weeks) | - | $6.0 | $8.6 |
| Task 2 live check (3 small calls, VM only if needed), Task 3 one blob download | - | under $0.1 | $0.1 |
| Task 7 VM refresh, scripted replay, one hand-written case on the VM (no LLM) | up to 2 h | $0.3 | $0.5 |
| Task 8 development campaign: LLM | 5 runs | $15 | with Task 10: $30 (D3) |
| Task 10 generality campaign: LLM | 1-2 runs | $6 (2 runs) | in the $30 above |
| VM for Tasks 8 and 10 | 5-12 h | $1.5 | $2.5 |
| Blob storage writes | - | $0.3 | $1.0 |
| Task 9 hand-written calibration (local Docker) | - | $0 | $0 |
| **Total** | | **about $29** | **about $42.7** of $45 |

- **The VM upper figure is an expectation, not a bound.** If all seven runs took the full 240 minutes, the VM would run 28 h (about $5.8) and the upper total would be about $46. That is why Cost Management is checked before Task 10: actual M3 spend, plus the fixed items to the planned end, plus the generality runs at their cap and wall clock, must be at most $45.
- The per-run cap (D2) limits one run, apart from one last model call; the campaign budget (D3) limits the sum. Before each run the script checks `spent + cap <= budget`. With $8 and $30, the fifth run starts only if the first four averaged at most $5.5, the sixth if the first five averaged at most $4.4, the seventh if the first six averaged at most $3.67. In the worst case (every run at its cap) only three runs start. When the script stops, the owner decides: a lower cap for the remaining runs, fewer generality tasks, or a higher budget if Cost Management shows room under $45.
- A run that stops on its cap is a result ("failed: cost limit"), not a reason to repeat it. With one run per task a cap stop turns the only observation into a statement about the budget, which is the argument for $8 (three times the billed cost of the one run so far) over $6.
- If the VM, disk and IP stay after M3, the fixed items continue during M4 (D9).

## File structure (new and changed)

```
src/cfdagent/
  llm/client.py             Usage.cached_tokens, ChatResult.served_model
  llm/cost.py               Pricing.cached_input_per_m, gpt-5.4 cached price, tokens_cached
  record.py                 tokens_in_cached, served_models, source_commit, max_cost_usd, wall_clock_s
  orchestrator.py           run pricing with the cached price; record sync and new fields
  rundir.py                 read_head (only if Task 3's rule is met)
  agents/transcript.py      cached tokens in the usage entry
  agents/loop.py            batch trimming (low-water mark); served model collection
  agents/analyst.py         write-time message; case inventory (only if Task 3's rule is met)
  agents/casespec.py        KNOWN_SOLVERS moves here
  agents/planner.py         solver checks
  agents/mesh.py            run-time check after meshing
  agents/solver_runner.py   estimate text
  tools/convergence.py      no advice to loosen tolerances
  tools/function_objects.py force function-object check (new)
  tools/case_check.py       calls it
  tools/case_requirements.py  monitor skeleton aligned with the force check
  prompts/planner.md        names the three solvers
  prompts/analyst.md        write times at least 100 iterations apart
  cli.py                    --wall-clock-min
scripts/
  transcript_profile.py     where the tokens of a transcript went (new)
  run_campaign.py           campaign loop with budget stop and repeat guard (new)
  campaign_table.py         results table from run records (new)
  run_calibration.py        --allow-generality
infra/main.bicep            gpt-5.4 deployment recorded
benchmarks/generality/<id>/ task.md, benchmark.toml, reference, README.md, handwritten/ (after the freeze)
docs/runbooks/m3-campaign.md
docs/journal/m3-run-notes.json  validity, repeat and post.py notes per run
```

---

# Phase 0: plan

### Task 1: M3 plan and ADR-0010 (this PR)

**Files:**
- Create: `docs/superpowers/plans/2026-11-26-m3-fixes-and-gpt-5-4-campaign.md`, `docs/adr/0010-m3-one-run-per-benchmark-on-gpt-5-4.md` (status Proposed)

- [ ] **Step 1:** The owner reviews the plan and ADR-0010 and answers D1-D5.
- [ ] **Step 2:** After approval, in the same PR before the merge: set ADR-0010 to Accepted ("owner approval in chat", with the date), apply the design edits listed in ADR-0010 under "Changes to the design document", and add "(the M3 campaign runs on gpt-5.4, ADR-0010)" to the Project paragraph of `CLAUDE.md`. Record the answers to D2-D5 below the decisions table and carry any changed value into the tasks that use it.
- [ ] **Step 3: Standard close** (docs only). Commit: `docs: M3 plan and ADR-0010`.

---

# Phase A: fixes

### Task 2: Cached-token usage, price and served model (carry-over 7)

**Files:**
- Modify: `src/cfdagent/llm/client.py`, `src/cfdagent/llm/cost.py`, `src/cfdagent/llm/scripted.py` (accept an optional `cached_tokens` in a fixture's usage), `src/cfdagent/orchestrator.py` (`_pricing`, record sync near `record.cost_usd`), `src/cfdagent/record.py` (fields and `summary.md` token line), `src/cfdagent/agents/transcript.py`, `src/cfdagent/agents/loop.py`
- Create: `tests/e2e/test_cached_tokens.py`
- Test: `tests/unit/test_llm_client.py`, `tests/unit/test_cost.py`, `tests/unit/test_orchestrator.py`, `tests/unit/test_record.py`, `tests/unit/test_agent_loop.py`

**Interfaces:**
- `Usage(prompt_tokens: int, completion_tokens: int, reasoning_tokens: int | None, cached_tokens: int | None = None)`. `_usage_from_sdk` reads `usage.prompt_tokens_details.cached_tokens` and keeps it only if it is an `int` (not `bool`) with `0 <= cached <= prompt_tokens`; otherwise `None`. The other counts are unaffected.
- `ChatResult.served_model: str | None = None`, from the response's `model` field.
- `Pricing(input_per_m: float, output_per_m: float, cached_input_per_m: float | None = None)`; property `effective_cached_per_m` (the cached price, or `input_per_m` when `None`). `ValueError` if the cached price is negative, not finite or above `input_per_m`. `PRICING["gpt-5.4"] = Pricing(2.50, 15.00, cached_input_per_m=0.25)`; `gpt-oss-120b` keeps `None` (its bill shows no cached-token meter, journal of 2026-11-25) and so does Llama (no cached price is recorded for it). Update the price comment.
- `CostTracker.add` applies the same check as `_usage_from_sdk` (a count outside `0..prompt_tokens` or not an `int` counts as 0, because a scripted backend builds `Usage` directly), then bills the cached tokens at `effective_cached_per_m`, the other prompt tokens at `input_per_m` and output as now; an estimated call has no cached tokens. New property `tokens_cached`; `tokens_in` stays the whole input, cached included.
- `orchestrator._pricing`: input and output as now (maximum over the models); cached = maximum of `effective_cached_per_m` over the models.
- `RunRecord.tokens_in_cached: int = Field(default=0, ge=0)`; `RunRecord.served_models: list[str] = []` (distinct, in order of first reply; the validator keeps the first 10 and cuts each to 100 characters, and never raises, so a record can always be saved). The `summary.md` line becomes `- Tokens: {tokens_in} in ({tokens_in_cached} cached), {tokens_out} out`.
- The transcript usage entry gets `"cached_tokens"`.

- [ ] **Step 1: Write the failing tests:**
  - `test_usage_reads_cached_tokens` (prompt 2000, completion 10, details cached 1536 → `cached_tokens == 1536`);
  - `test_usage_drops_implausible_cached_tokens` (parametrised `-1`, `2001`, `"5"`, `True`, details missing → `None`, prompt and completion kept);
  - `test_cost_bills_cached_input_at_the_cached_rate` (gpt-5.4, `Usage(1_000_000, 100_000, None, 900_000)` → `total_usd == 1.975`);
  - `test_gpt54_price_reproduces_the_billed_split` (prompt 1_700_000 with 1_520_000 cached, completion 120_000 → `pytest.approx(2.63, abs=0.005)`);
  - `test_tracker_ignores_a_cached_count_above_the_prompt` (billed as if nothing were cached);
  - `test_model_without_cached_price_bills_all_input_at_input_rate`; `test_estimated_call_has_no_cached_tokens`; `test_pricing_rejects_a_cached_price_above_input`;
  - `test_run_pricing_takes_the_dearest_cached_price` (a configuration mixing `gpt-oss-120b` and `gpt-5.4` → cached 0.25);
  - `test_record_stores_cached_tokens_and_served_models`; `test_too_many_or_too_long_served_models_are_cut_not_refused`; `test_old_record_without_cached_fields_loads`; `test_transcript_usage_has_cached_tokens`.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** as in Interfaces.
- [ ] **Step 4: Run** the unit suite. Expected: PASS.
- [ ] **Step 5: Live check.** Write `tests/e2e/test_cached_tokens.py::test_gpt54_reports_cached_tokens` (marker `e2e`): send the same prompt of at least 2,000 tokens three times (`reasoning_effort="low"`, `max_tokens=1024`); every reply has `cached_tokens is not None`, and the second or third has `cached_tokens > 0`; the served model starts with `gpt-5.4`. Announce (rule 4, under $0.05), then run it: `uv run pytest -m e2e tests/e2e/test_cached_tokens.py -s` with `CFDAGENT_LLM_ENDPOINT` set and `az login`. If the signed-in account lacks the model role, run it on the VM instead (announce the VM start; stop it afterwards). Expected: PASS. **If the field is missing, stop and tell the owner (D2 fallback).** Put the observed counts (no endpoint) in the PR.
- [ ] **Step 6: Standard close.** Commit: `feat: bill cached input tokens at the cached price and record the served model`.

### Task 3: Context growth: measure, trim in batches, then act on the measurement (carry-over 8) [cfd]

**Files:**
- Create: `scripts/transcript_profile.py`, `tests/unit/test_transcript_profile.py`
- Modify: `src/cfdagent/agents/loop.py`; only if the rule of Step 6 is met: `src/cfdagent/agents/analyst.py`, `src/cfdagent/rundir.py`, `src/cfdagent/prompts/analyst.md`
- Test: `tests/unit/test_agent_loop.py`, `tests/unit/test_analyst_stage.py`, `tests/unit/test_rundir.py`

**Interfaces:**
- `transcript_profile.profile(lines: Iterable[str]) -> list[CallRow]`, one row per transcript line: `stage`, `pass_no`, `attempt`, `prompt_tokens`, `cached_tokens` (`None` in old transcripts), `completion_tokens`, `reasoning_tokens`, `reply_tools: list[tuple[str, str]]` (tool name and its `path` argument, or `""`), `new_chars: dict[str, int]` by source (`system`, `user`, `assistant` for reply text and tool-call arguments, `tool:<name>` matched through `tool_call_id`), `history_chars` (cumulative per conversation, before any trimming). A conversation is the lines of one `(stage, pass_no)`. The CLI `python scripts/transcript_profile.py <llm_transcript.jsonl>` prints the rows and per-stage totals. The transcript clips each text at 20,000 characters, so character counts are lower bounds; the output says so.
- `loop.CONTEXT_LOW_WATER_CHARS = 120_000`. `_elide_old_outputs` keeps its trigger (above `CONTEXT_CHAR_LIMIT`) and its rules (never the last `KEEP_LAST_MESSAGES`, only tool results), but once triggered removes the oldest tool results until the size is at most `CONTEXT_LOW_WATER_CHARS`, or nothing is left to remove.

- [ ] **Step 1: Write the failing tests:** `test_profile_attributes_tool_results_by_name`; `test_profile_history_is_cumulative_per_stage_and_pass`; `test_no_trimming_at_or_below_the_limit`; `test_trimming_goes_down_to_the_low_water_mark_in_one_batch`; `test_next_call_leaves_older_messages_unchanged` (after a batch, two small messages are appended and trimming runs again: every earlier message is unchanged); `test_trimming_that_cannot_reach_low_water_stops` (large user and assistant messages stay). The existing `test_old_tool_outputs_are_elided_when_the_conversation_grows` patches only `CONTEXT_CHAR_LIMIT` (to 10,000, below the new mark): make it patch both constants.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** both.
- [ ] **Step 4: Run** the unit suite. Expected: PASS.
- [ ] **Step 5: Measure.** Announce (rule 4: one blob download, under $0.01), then download `llm_transcript.jsonl` of run `20261125-111308-*` from the `runs` container into the scratchpad (`az storage blob list --auth-mode login ... --prefix 20261125-111308`, then `az storage blob download`); never into the repository. Run the profile. Record in the journal: calls, input and output tokens per stage, the largest history, whether 200,000 characters was passed, and what the Analyst's 19 calls did.
- [ ] **Step 6: Act by this rule.** If, among the Analyst's tool calls before its first `write_file` of `post.py` (all of them if it never wrote one), more than half are `read_file`, `list_dir`, `search_knowledge` or `read_knowledge`: add `analyst.case_inventory(ctx: StageContext, max_chars: int = 4000) -> str` to the Analyst's opening message. It gives:
  - the time directories of `case/`, sorted as numbers: their count, the first and the last three; the field files of the last one;
  - for each `case/postProcessing/<object>/`: the count of its time directories and, for the **latest** one, each file's path and its header, which is the last of the leading lines starting with `#` (cut at 200 characters and marked "(clipped)"); a file without such a line shows "(no header line)" and never a data row; binary or XML content shows "(not shown)";
  - at most 4,096 bytes are read per file, through a new `RunDir.read_head(rel: str, max_bytes: int) -> str` (the same refusals of links and special files as `read_text`, but a larger file is cut, not refused with `FileTooLarge`);
  - "[N more entries not shown]" at the end when `max_chars` cuts it.

  Add to `prompts/analyst.md`: "Start from the inventory and write a first `post.py` early; then read what you need to correct it and to make the convergence, conservation and plausibility checks." (the instruction to read the matching notes first stays). Tests first: `test_inventory_sorts_times_as_numbers_and_shows_the_last_three`, `test_inventory_shows_the_latest_time_of_each_function_object`, `test_inventory_never_shows_a_data_row_or_binary_content`, `test_inventory_is_capped`, `test_inventory_skips_links`, `test_read_head_cuts_a_large_file`, `test_opening_message_includes_the_inventory`. If the rule is **not** met: change nothing more, record what the calls did, and tell the owner.
- [ ] **Step 7: Standard close.** Commit: `fix: trim old tool output in batches to keep the cached prefix` (and `feat: case inventory in the Analyst opening message` if done).

Not in this task, with reason: removing the arguments of old `write_file` calls (the model is editing those files; hiding them invites re-reads, which cost output tokens); a smaller tool-result cap (more pages mean more turns).

### Task 4: Gate and message fixes before money is spent (carry-over 13; parts of 1 and 3) [cfd]

Three independent changes, each with its own tests and commit (ADR-0009 allows one PR).

**Files:**
- Modify: `src/cfdagent/agents/casespec.py`, `agents/case_setup.py`, `agents/solver_runner.py`, `agents/planner.py` (`_solver_errors`), `agents/mesh.py`, `agents/analyst.py` (the convergence message), `tools/convergence.py` (`NOT_ENDED_RULE`), `prompts/planner.md`, `prompts/analyst.md`
- Test: `tests/unit/test_planner.py`, `tests/unit/test_mesh_stage.py`, `tests/unit/test_convergence.py`, `tests/unit/test_analyst_stage.py`, `tests/unit/test_solver_stage.py`

**Interfaces, A (solver checks):**
- `casespec.KNOWN_SOLVERS = ("icoFoam", "simpleFoam", "pimpleFoam")` moves from `case_setup`, which imports it (existing imports of `case_setup.KNOWN_SOLVERS` keep working).
- Gate error for any other `solver.suggested_solver` (wording may change in `cfd-reviewer`'s pass): "solver.suggested_solver is `<name>`, which this pipeline does not run. It runs only `icoFoam` (transient; laminar, Newtonian), `simpleFoam` (steady; laminar or with a turbulence model) and `pimpleFoam` (transient; laminar or with a turbulence model), all for a single phase of constant density with no energy equation. Never change the physics to fit a solver. If one of the three solves the equations the task needs, choose it; otherwise name the solver you would need and why in `assumptions`; failing this check is then the correct result."
- Gate error for `simpleFoam` with `time_dependence == "transient"`: it says `simpleFoam` is a steady solver, to choose `pimpleFoam` or `icoFoam`, and to set "steady" only if the flow is steady. Gate error for `icoFoam` with `flow_regime == "turbulent"`: it says `icoFoam` solves laminar Newtonian flow only, to choose `simpleFoam` or `pimpleFoam` with a turbulence model, and to set "laminar" only if the flow is laminar.
- Planner prompt, in the **Solver** bullet, the same facts: which three solvers run, what each covers, and "if none can represent the physics, name the one you would need and say so in `assumptions`".

**Interfaces, B (run time after meshing):** `mesh.runtime_problem(spec: CaseSpec, n_cells: int, limits: RunLimits) -> str | None`, part of the Mesh gate. Only when `n_cells > spec.resource_estimate.cells > 0`: `scaled = runtime_minutes * n_cells / resource_estimate.cells`; a problem when `scaled > limits.max_runtime_minutes`. The message gives the cells of the mesh and of the estimate, the estimated and scaled minutes and the limit, and asks to coarsen the mesh where the physics allows it. It trusts the plan's cost per cell and is optimistic when a finer mesh also needs a smaller time step.

**Interfaces, C (texts):**
- `convergence.NOT_ENDED_RULE` gives no advice to loosen a tolerance. When the residuals levelled off, it says to add a function object that monitors a requested quantity, so the run can be judged from it, or to find the cause.
- `prompts/analyst.md` (the sentence and the code example) and the Analyst gate message ask, for a steady run, for the change between two write times at least 100 iterations apart and to state which two; "last two write times" disappears from both.
- The Solver Runner opening message no longer says the plan's run time is "on one core"; it says the estimate may assume several ranks.

- [ ] **Step 1: Write the failing tests:** A: `test_plan_gate_rejects_an_unsupported_solver` (`pisoFoam` → one error naming `pisoFoam` and all three solvers); `test_plan_gate_accepts_every_supported_solver`; `test_plan_gate_rejects_simplefoam_for_a_transient_plan`; `test_plan_gate_rejects_icofoam_for_a_turbulent_plan`; `test_planner_prompt_names_every_supported_solver`; `test_unsupported_solver_costs_a_planner_attempt_not_the_run` (scripted: first plan `pisoFoam`, second `simpleFoam` → the Planner passes on attempt 2). B: `test_mesh_gate_rejects_a_mesh_whose_scaled_runtime_exceeds_the_limit` (estimate 53 min for 100_000 cells, mesh 551_895 cells, limit 90); `test_mesh_gate_accepts_a_finer_mesh_within_the_limit`; `test_mesh_gate_ignores_a_coarser_mesh`. C: `test_not_ended_rule_does_not_advise_loosening`; `test_analyst_texts_ask_for_write_times_100_iterations_apart`; `test_solver_opening_message_does_not_claim_one_core`.
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** as in Interfaces.
- [ ] **Step 4: Run** the unit suite. Expected: PASS.
- [ ] **Step 5: Standard close.** Commits: `fix: the plan gate checks the suggested solver`, `feat: check the scaled run time after meshing`, `fix: correct convergence and run-time texts shown to agents`.

### Task 5: Check force function objects completely before the trial run (carry-over 12) [cfd] [integration]

**Files:**
- Create: `src/cfdagent/tools/function_objects.py`, `tests/unit/test_function_objects.py`, `tests/integration/test_function_objects_openfoam.py`
- Modify: `src/cfdagent/tools/case_check.py` (`check_case_consistency` calls the check after `solver_problems`; the static checks already run before the trial run); `src/cfdagent/tools/case_requirements.py` (`MONITOR_SKELETON` shows the same force form as the new messages, with `rho rhoInf; rhoInf` and `CofR`); `src/cfdagent/knowledge/notes/forces-and-coefficients.md` where the integration test shows it wrong (rule 4 says `CofR` is required and lists `pitchAxis`)

**Interfaces:**
- `function_objects.force_problems(case_dir: Path, solver: str, patch_names: Collection[str], skipped: list[str]) -> list[str]`, where `patch_names` holds the mesh patch names and group names from `case_check._mesh_patches`, plus `wall` when any patch is of type `wall` (OpenFOAM adds that group itself).
- Judged: every entry of `functions` in `system/controlDict` whose `type` is the literal word `forces` or `forceCoeffs`. Other types are ignored. **Silent (not judged):** `#include`, `#includeEtc` or any directive except `#includeFunc` at the top of `controlDict` or of `functions` (`#includeFunc` adds its own object and silences nothing else); an entry that holds a directive or a `$` expansion, as an entry or inside a value, lists included; a macro `type`; `enabled false`; `directForceDensity` switched on. The rule on the library is silent on any directive anywhere in `controlDict`.
- How v2512 chooses the coordinate system (from reading its source, `forces.C`; the integration test confirms it): (1) a `coordinateSystem {}` sub-dictionary wins over `CofR`; `liftDir` and `dragDir` are then ignored and `origin` is optional inside it; (2) otherwise, with `CofR`: `forceCoeffs` needs `liftDir` and `dragDir`, `forces` needs nothing more; (3) otherwise: top-level `origin` plus `rotation`, or one pair of `(e1, e2)`, `(e2, e3)`, `(e3, e1)` or `(axis, direction)`; `liftDir` and `dragDir` are ignored. `pitchAxis` is not read and is never required. Message skeletons show form (2).
- Candidate defects: no `patches`; no `rho rhoInf` ("Could not find rho:rho" at the first execution); `rhoInf`, and for `forceCoeffs` `magUInf`, `lRef`, `Aref`, missing or not positive; form (2) without `liftDir` or `dragDir`; form (3) without `origin` or without an axis pair or `rotation`; an axis of zero length, or two parallel axes; a literal patch name that is no patch and no group (quoted regular expressions are accepted); the forces library loaded nowhere (no `libs` or `functionObjectLibs` in any entry, no `libs` in `functions`, no top-level `libs`).
- **A rule exists if v2512, given the defect, (a) stops, (b) gives no requested output (the object is dropped, or the output is zeros), or (c) computes with other axes or patches than written.** Some defects only warn and let the run finish, so exit status alone proves nothing. A form the check does not understand is accepted.
- All problems of all entries are returned together, one message each: `system/controlDict: function object `<name>` (<type>): <what is missing or wrong>; <generic skeleton>`. No case values.

- [ ] **Step 1: Write the integration test** `test_v2512_agrees_with_the_force_check`, parametrised over every candidate and every valid form. Each case runs one or two steps on a tiny 2D case and **asserts on the output**: `force.dat` or `coefficient.dat` exists and is not all zeros, the axes in its header equal the requested ones, and the log has no patch warning. A defect that meets (a), (b) or (c) gives a non-empty `force_problems`; a valid form runs clean and gives none. Valid forms, at least: `forceCoeffs` with `CofR`, with and without `pitchAxis`; `forces` with `CofR` only; form (3) for both types without `liftDir`/`dragDir`; form (1), with `rotation`; `#include`, `#includeEtc` of the stock incompressible configuration and `#includeFunc`; a `$` expansion and `$` values; a disabled defective entry; a named patch group, the implicit `wall` group and a quoted regular expression; each way of loading the library once. Run with `icoFoam` and with `simpleFoam` or `pimpleFoam` (they read the viscous stress differently). Run it in the image to settle which candidates are defects.
- [ ] **Step 2: Write the failing unit tests** for the confirmed rules: `test_all_problems_are_reported_at_once`; `test_missing_patches_is_reported`; `test_incompressible_forces_need_rho_rhoinf`; `test_reference_values_must_be_present_and_positive`; `test_cofr_form_needs_lift_and_drag_directions`; `test_origin_form_needs_a_complete_axis_pair`; `test_zero_or_parallel_axes_are_reported`; `test_unknown_patch_is_reported_but_groups_and_regex_are_accepted`; `test_library_loaded_nowhere_is_reported`; `test_unsure_forms_are_silent` (one case per silence rule; `#includeFunc` does not silence a sibling entry); `test_other_function_object_types_are_ignored`; `test_handwritten_cases_have_no_force_problems` (every `tests/fixtures/handwritten/*/case`); `test_case_consistency_includes_force_problems`. A rule the integration test rejects loses its unit test.
- [ ] **Step 3: Run** them. Expected: FAIL.
- [ ] **Step 4: Implement** the check and call it.
- [ ] **Step 5: Run** unit and integration suites. Expected: PASS. Then, for both cylinder fixtures, run `scripts/run_calibration.py --gate --keep DIR` and `check_case_consistency` on the kept case: the calibration passes and the check reports no problem (calibration alone does not run this check).
- [ ] **Step 6: Standard close** (`cfd-reviewer`: the rules against v2512 and the generality rule). Commit: `feat: report every force function-object problem before the trial run`.

### Task 6: Campaign tooling (replaces `cfdagent bench`; carry-over 18)

**Files:**
- Create: `scripts/run_campaign.py`, `scripts/campaign_table.py`, `tests/unit/test_run_campaign.py`, `tests/unit/test_campaign_table.py`
- Modify: `src/cfdagent/cli.py` (`--wall-clock-min`, `_child_command`; `--max-critic-rounds` only if D5 chose a value other than 2), `src/cfdagent/record.py`, `src/cfdagent/orchestrator.py`, `scripts/run_calibration.py` (`--allow-generality`), `CLAUDE.md` (key commands)
- Test: `tests/unit/test_cli.py`, `tests/unit/test_record.py`, `tests/unit/test_orchestrator.py`, `tests/unit/test_run_calibration.py`

**Interfaces:**
- CLI: `--wall-clock-min INT` (1 to 360) sets `limits.wall_clock_s = 60 * INT`; passed on by `--detach`.
- Record: `source_commit: str | None = None` (from `CFDAGENT_COMMIT` when it is 40 hexadecimal characters), `max_cost_usd: float | None = None` and `wall_clock_s: float | None = None` (from the run's limits). None of them names a benchmark.
- Campaign plan file (JSON): `llm_budget_usd`, `max_cost_usd`, `wall_clock_min`, `run_args` (list), `benchmarks` (list), and optional `repeat_of` (`{benchmark id: run id}`, default `{}`) and `spent_outside_log_usd` (default 0). Unknown keys are an error. `run_args` may not hold `--max-cost-usd`, `--wall-clock-min`, `--run-id`, `--benchmark`, `--detach` or `--no-upload`, in either spelling (`--flag x`, `--flag=x`), because a repeated flag overrides the first. For generality plans it holds `--allow-generality`.
- `run_campaign(plan, run: Callable[[list[str]], int], runs_root: Path, log: Path, now: Callable[[], datetime]) -> int`:
  - Returns 2 without running anything unless `CFDAGENT_LLM_ENDPOINT`, `CFDAGENT_STORAGE_ACCOUNT_URL`, `CFDAGENT_IMAGE` and `CFDAGENT_COMMIT` are set (without the storage address a run skips its upload and still exits 0), or if a listed benchmark already has a `start` line in the log and `repeat_of` does not map it to that run id. **This is the guard against running a task twice.**
  - One log serves the whole campaign, one JSON object per line, each with an `event`: `start`, `end` or `budget_stop`. Spend = `spent_outside_log_usd` + the sum of `cost_usd` over the `end` lines + the cap of every run with a `start` line and no `end` line.
  - For each benchmark in order: if `spend + max_cost_usd > llm_budget_usd`, log `budget_stop` (`next`, `spend_usd`) and return 3. Otherwise make the run id with `orchestrator.new_run_id(<text of the benchmark's task.md>, now())`, **never from the benchmark id** (blind evaluation: executors see the run directory); the generality registry is opened only when `run_args` holds `--allow-generality`. Append the `start` line (`benchmark`, `run_id`, `cap_usd`). Call `run([sys.executable, "-m", "cfdagent", "run", "--benchmark", id, "--run-id", rid, "--max-cost-usd", ..., "--wall-clock-min", ..., *run_args])` and keep its return value **at once** in a variable. Read `<runs_root>/<rid>/run_record.json`; the cost is its `cost_usd`, or the cap if it cannot be read. Append the `end` line: `benchmark, run_id, exit_code, status, failed_stage, failure_reason, cost_usd, tokens_in, tokens_in_cached, tokens_out, spend_usd`.
  - Return 4 at once if the exit code is not 0 or 1, the record is unreadable or still `running`, or `failure_reason` starts with `internal_error` or contains "the model service failed". Return 0 after the last benchmark.
  - CLI, from the repository root: `python scripts/run_campaign.py <plan.json> --log <file.jsonl>`.
- `campaign_table.table(records: list[dict], log_lines: list[dict], notes: dict[str, RunNote]) -> str`, with `RunNote(invalid_reason: str | None, repeat_of: str | None, post_check: str | None)`; the notes file is a JSON object `{run id: {those three keys}}`. One row for **every** run id in the records or the log (one without a readable record says so). "Earlier pass reached the Analyst" means a passed `analyst` stage record in a pass before the run's last one. Columns: run id, benchmark, task set, commit, cap USD, wall-clock limit, status, failed stage, Scorer (`pass`, `FAIL`, `-`), errors, post.py check, Critic verdict, Critic rounds, earlier pass reached the Analyst (yes/no), agrees, attempts per stage, fix loops, tokens in (cached) / out, tracked USD, wall min, served model, image, validity (`valid` or `invalid: <reason>`), repeat of. A criterion cell is `<quantity> <error>/<threshold> pass|FAIL`; without a threshold (a range criterion) `<quantity> outside range by <error> pass|FAIL`; without an error `<quantity> not scored`. A comparison that holds only `problem` gives Scorer `FAIL` and `problem: <first 200 characters>`. **`ValueError`** when one benchmark has two or more rows without `invalid_reason`, or `repeat_of` does not name an invalid row of the same benchmark. Footer, verbatim: "One row per run. One run per task is an observation, not a success rate. 'Scorer' is compared with reference data; the Critic judges physical checks only." CLI: `python scripts/campaign_table.py --log <file.jsonl> --notes <notes.json> <run_record.json>...`.
- `run_calibration.py --allow-generality` loads the registry with `include_generality=True`; without it a generality id stays unknown.

- [ ] **Step 1: Write the failing tests:** CLI and record: `test_wall_clock_flag_sets_the_limit_and_survives_detach`; `test_record_stores_commit_cap_and_wall_clock`. Script: `test_missing_environment_variable_refuses_to_start`; `test_refuses_a_benchmark_already_in_the_log`; `test_repeat_of_allows_exactly_the_named_repeat`; `test_budget_stop_before_a_run_that_might_not_fit`; `test_a_started_unfinished_run_counts_as_its_cap`; `test_campaign_run_id_never_names_the_benchmark`; `test_argv_carries_cap_wall_clock_and_run_args`; `test_run_args_cannot_override_protected_flags`; `test_exit_code_is_recorded_per_run` (a fake `run` returning 1 then 0; the script returns 0); `test_stops_after_a_broken_run` (parametrised: exit 2, exit 130, unreadable record, `running` record, `internal_error`, "the model service failed"; each returns 4); `test_unreadable_record_counts_the_full_cap`; `test_unknown_plan_key_is_rejected`. Table: `test_table_has_a_row_for_every_run_in_the_log`; `test_table_columns_and_footer`; `test_table_cells_for_range_unscored_and_problem_results`; `test_two_valid_rows_of_one_benchmark_are_refused`; `test_repeat_of_must_name_an_invalid_row`. Calibration: `test_calibration_needs_allow_generality_for_a_generality_id` (a fake generality entry in a temporary registry).
- [ ] **Step 2: Run** them. Expected: FAIL.
- [ ] **Step 3: Implement** as in Interfaces.
- [ ] **Step 4: Run** the unit suite. Expected: PASS.
- [ ] **Step 5: Standard close.** Commit: `feat: campaign script, results table and a wall-clock option`.

---

# Phase B: freeze

### Task 7: Freeze `v0.3.0` [owner gate D8] [infra]

**Frozen from the tag until the last campaign run ends:** `src/`, `docker/`, `pyproject.toml`, `uv.lock`, `scripts/`, the development benchmarks (`benchmarks/<id>/`), the image, prompts, notes and the campaign configuration (model, effort, repair mode, wall clock, run limits). **May change:** `benchmarks/generality/` (Task 9, data only), `tests/`, `docs/`. **One exception:** the repair of a run that a defect of the pipeline's own code made invalid (run rules below). The cost cap and budget (D2, D3) are money limits that only the owner may change; every change is recorded, and each row of the table shows its cap.

**Read:** `infra/CLAUDE.md`, `docs/runbooks/vm-host.md`, `docs/runbooks/m2-runs.md`, ADR-0010.

**Files:**
- Modify: `infra/main.bicep` (`gpt-5.4` entry in `modelDeployments`), `infra/README.md`, `pyproject.toml`, `src/cfdagent/__init__.py`, `CITATION.cff`, `uv.lock`, `tests/unit/test_package.py` (0.3.0)
- Create: `docs/runbooks/m3-campaign.md`

- [ ] **Step 1:** Tasks 2-6 merged; CI green on `main`.
- [ ] **Step 2:** Locally, at the candidate commit: every hand-written development calibration (`tests/fixtures/handwritten/` `cavity_re100`, `cavity_re1000`, `pipe_re500`, `cylinder_re20`, `cylinder_re100`) passes `scripts/run_calibration.py --gate` in a freshly built test image. This exercises the tool gates and the convergence gate, not the plan gate or the case check (Tasks 4 and 5 test those). A failure means a fix broke a gate: fix it test-first before the tag.
- [ ] **Step 3:** `infra-engineer` reads the existing deployment (read-only: `az cognitiveservices account deployment list -g rg-cfdagent -n <account>`), adds exactly that model, version, SKU and capacity to `modelDeployments`, and runs `what-if`. Deploy only if `what-if` shows no change to existing resources; otherwise stop and show the owner. Announce first; cost $0.
- [ ] **Step 4:** Version bump to 0.3.0 (own commit `chore: bump the package version to 0.3.0`); merge.
- [ ] **Step 5: Owner gate (D8).** Tag `v0.3.0` on that merge commit and push the tag; CI publishes `ghcr.io/mkrajew/cfd-agent:0.3.0`. Wait for the tag's CI run to finish before the VM set-up: it pushes the commit's `sha-` tag again.
- [ ] **Step 6:** Announce (VM time only, about $0.3). Start the VM; `scripts/vm-host-setup.sh <tag commit> ...`; confirm `/etc/cfdagent/env` names the tag commit's image; `verify_sandbox.py` 7 of 7 PASS; scripted replay (`completed`, Scorer passed, Critic `success`, upload complete); `cfdagent run --help` shows `--wall-clock-min`. Then time the hand-written transient case on the VM, without any model, with `/etc/cfdagent/env` loaded: `docker run --rm -v /opt/cfdagent-src:/work -w /work "$CFDAGENT_IMAGE" python scripts/run_calibration.py tests/fixtures/handwritten/cylinder_re100 --benchmark cylinder-re100 --n-procs 4 --gate`. The only timings so far are from the laptop (8-11 min). If the VM needs more than 60 minutes, tell the owner before the campaign: the 240-minute limit (D5) also has to hold the model's time and a repeated solve. Deallocate.
- [ ] **Step 7:** `docs/runbooks/m3-campaign.md`:
  - git on the VM runs as root (`sudo git -C /opt/cfdagent-src ...`), because root cloned the checkout;
  - the campaign runs as the admin user with the host interpreter and environment: `sudo -u azureuser -H bash -c 'cd /opt/cfdagent-src && set -a && . /etc/cfdagent/env && set +a && nohup /opt/cfdagent-host/bin/python scripts/run_campaign.py /home/azureuser/campaign/<plan>.json --log /home/azureuser/campaign/m3-log.jsonl > /home/azureuser/campaign/<plan>.out 2>&1 &'`;
  - paths: plan files `m3-dev-canary.json`, `m3-dev.json`, `m3-generality.json` and the one log `m3-log.jsonl` in `/home/azureuser/campaign/`, with an example of each plan;
  - the idle shutdown counts a running `cfdagent` as busy (an image pull does not). The daily shutdown at 23:00 Europe/Warsaw waits for an active run but can fire in the gap between two runs, so each batch is started early enough to end before 23:00 (worst case: runs × wall clock), or split;
  - how to read the log and how to deallocate.

  `scribe` records the freeze: commit, tag, image digest, configuration, prompt hashes, the VM timing.
- [ ] **Step 8: Standard close.** Commit: `infra: record the gpt-5.4 deployment` and `docs: M3 campaign runbook and freeze record`.

---

# Phase C: campaign

**Run rules (ADR-0010 point 5), for Tasks 8 and 10:**
- No run is repeated to get a better result. Every run, invalid ones included, appears in the table with its cost. A repeat never removes the first row.
- A run is **invalid** only if it ended through a fault outside the agents' work: (a) infrastructure (VM, disk, Docker, network, an Azure outage, or HTTP 429 or 5xx that used up the retries), **except** disk or memory exhaustion caused by the run's own output, which is a result; (b) a defect of the pipeline's own code: an `internal_error`, or a gate or tool that rejected or broke correct work, **shown by a failing test**. A cost-cap or wall-clock stop, any agent failure, a Scorer fail or physics the solvers cannot represent are **results**.
- The owner decides, on the session's evidence, whether a run is invalid. The decision goes into `docs/journal/m3-run-notes.json` (`invalid_reason`, `repeat_of`), and a repeat needs the plan's `repeat_of` entry.
- A development run invalid for (a) is repeated once on the same tag. One invalid for (b) is fixed (test first, review), tagged `v0.3.1`, and repeated once; earlier valid runs are not repeated, and the table shows each run's commit.
- A generality run is repeated only for (a), unchanged. A generality run that hits (b) is reported as it is, and no fix is made in M3: a fix informed by a generality task would end its blindness.
- A session that makes a (b) fix, its subagents and reviewers included, does not read `benchmarks/generality/` or the journal entries of Tasks 9 and 10; the deny rule stays in force for it.

### Task 8: Development campaign on gpt-5.4 [owner gate D3]

**Read:** `docs/runbooks/m3-campaign.md`, `infra/CLAUDE.md`, ADR-0010, the run rules above, the docstrings of `scripts/run_campaign.py` and `scripts/campaign_table.py`.

- [ ] **Step 1: Announce** (rule 4): five runs, LLM about $15 (range $10-30), cap $8 each, campaign budget $30; VM 4-8 h, about $1-1.7 (use the values the owner chose in D2, D3, D5).
- [ ] **Step 2:** Start the VM. Confirm the frozen checkout: `sudo git -C /opt/cfdagent-src describe --tags` is `v0.3.0` and `sudo git -C /opt/cfdagent-src status --porcelain` is empty.
- [ ] **Step 3: Canary.** The plan `m3-dev-canary.json` with `cavity-re100` only. This run **is** the `cavity-re100` observation and is not run again. Check its record: `tokens_in_cached > 0`; `served_models`, `source_commit` and `max_cost_usd` filled; no `internal_error`; upload complete. If a check fails, stop and tell the owner before spending more; a pipeline cause follows the run rules. If it stopped on its cost cap, that is its result: ask the owner about D2 before the next run.
- [ ] **Step 4:** `m3-dev.json` with `cavity-re1000`, `pipe-re500`, `cylinder-re20`, `cylinder-re100`, in this order, same log. Start it early enough to end before 23:00 Europe/Warsaw (runbook). Nobody edits anything while it runs.
- [ ] **Step 5:** On exit 3 (budget) or 4 (broken run), report to the owner and apply the run rules. A benchmark that did not start is listed in a new plan; one that started needs `repeat_of`.
- [ ] **Step 6:** For every run that passed the Scorer, `cfd-reviewer` reads its `post.py` and confirms that no reported value is hard-coded or taken from theory instead of the solution; the answer goes into the notes file (`post_check`) and changes no run. Then run `scripts/campaign_table.py` over the log, notes and records; show the owner the table, the Scorer results and the plots (milestone approval point). `scribe` records the series, including the log lines and the spend. Deallocate the VM.
- [ ] **Step 7: Standard close** (docs only). Commit: `docs: M3 development campaign`.

### Task 9: Generality tasks: texts, references, hand-written calibration (M2 Task 7) [owner gate] [cfd] [integration]

**Precondition:** the tag `v0.3.0` exists. This session reads the generality files and changes nothing under `src/`, `docker/`, `scripts/` or the development benchmarks. It writes nothing about the task contents into persistent memory. **The owner lifts the Read deny on `benchmarks/generality/**` in `.claude/settings.json` by hand at the start, and restores it by hand at the end** (no session edits that file).

**Families (design 9.1).** The pipeline can only solve single-phase, constant-density flows with `icoFoam`, `simpleFoam` or `pimpleFoam`, so the heated cavity is out. Recommended if two: a laminar flat-plate boundary layer (an external boundary layer with a similarity solution) and a two-dimensional laminar backward-facing step (separation and reattachment). A plane channel is the closest relative of the pipe benchmark, so it tests generality least. The owner chooses (D6) and may add own ideas within the limits below.

**What the owner is told before writing (limits only, no task content):** constant density, single phase; no energy equation, buoyancy, compressibility or free surface; no periodic or cyclic boundaries; no moving mesh; axisymmetric cases without swirl; the convergence thresholds were checked on laminar cases only; a periodic statistic needs a force-coefficient history on a body, and other transients get no convergence check; the run limits (cells, run time, memory, 4 ranks). A task states its geometry, fluid, inlet and outlet conditions, the definition of any dimensionless number, and the definition of every quantity it asks for.

**Files:**
- Create: `benchmarks/generality/<id>/task.md` (the owner's text, verbatim), `benchmark.toml`, `reference/*.csv` or `make_reference.py`, `README.md` (sources, reasoning of the criteria, closest stock v2512 tutorial; `benchmark.toml` allows no extra keys), `handwritten/{geometry.py, mesh.py, case/, post.py, fixture.json}`
- Test: `tests/unit/test_registry.py`

**Interfaces:**
- Consumes: the registry of M2 Task 6 (`load_registry(include_generality=True)`), `scripts/run_calibration.py --allow-generality` (Task 6).

- [ ] **Step 1: Owner gate.** Ask the owner for one or two task texts in the owner's own words. Claude states the limits above and what a task file must state (as `benchmarks/README.md` does), but writes no task text and suggests no values.
- [ ] **Step 2:** `cfd-reviewer` prepares references from primary sources (analytic scripts with `--check`, or literature tables with full citations; not the rounded figures of design 9.1) and acceptance criteria with their reasoning, under the rules of the development benchmarks. Every definition a comparison depends on is fixed. The criteria use only the Scorer's existing metrics, and no reference point needs extrapolation. If no published reference matches the owner's text, say what is missing, without proposing values; the owner revises the text. If none can match, the owner decides whether to run it by hand as an ad hoc task, labelled "physical checks only", with its cost entered as `spent_outside_log_usd`. For each task, name the closest stock tutorial.
- [ ] **Step 3:** Hand-written, LLM-free solutions under `benchmarks/generality/<id>/handwritten/` (inside the deny rule, not in `tests/fixtures/`), run in the image: `python scripts/run_calibration.py benchmarks/generality/<id>/handwritten --benchmark <id> --allow-generality --gate`. Expected: PASS. If it fails, **do not change `src/`**. First check the hand-written solution and the reference themselves; an error in the reference or a threshold is corrected before D7, with the reason recorded. If the cause is the frozen pipeline, the recommendation is to **run the task anyway** and report that a hand-written solution did not pass, with the cause. A task may be **replaced only** for a defect of the task or its reference that does not depend on the pipeline (ambiguous text, no verifiable reference, outside the stated limits). Every replaced or dropped task is listed with its reason in the table's notes, the journal and the README.
- [ ] **Step 4:** `test_generality_references_load_and_check` (every generality benchmark loads with `include_generality=True`; every `make_reference.py --check` passes). Run it. Expected: PASS.
- [ ] **Step 5: Owner gate (D7).** The owner approves references, thresholds, and the list and order of the generality tasks. Nothing of this changes after the first generality run. A task that is approved but not run (for example for lack of budget) is reported as "not run".
- [ ] **Step 6:** Check that the PR touches only `benchmarks/generality/`, `tests/` and `docs/`: `git diff --name-only v0.3.0...HEAD -- src docker scripts pyproject.toml uv.lock benchmarks ':!benchmarks/generality'` is empty (or lists only a run-rule fix of Task 8).
- [ ] **Step 7: Standard close.** Commit: `docs: generality test set tasks, references and criteria`. Then the owner restores the deny rule.

### Task 10: Generality runs on gpt-5.4 [owner gate D3, D7]

**Preconditions:** Task 8 finished (owner's order); Task 9 merged and D7 given. This session does not need the deny rule lifted: it passes ids to the VM.

**Read:** `docs/runbooks/m3-campaign.md`, `infra/CLAUDE.md`, ADR-0010, the run rules above.

- [ ] **Step 1: Check the money:** Cost Management (scoped to `rg-cfdagent`, from 2026-11-27) plus the fixed items to the planned end, plus the generality runs at cap and wall clock, is at most $45. **Announce:** one or two runs, about $3 each (range $2-6), cap $8, within the campaign budget (state the spend in the log so far); VM 1-4 h. If it does not fit, ask the owner.
- [ ] **Step 2:** Start the VM on the latest freeze tag and add only the data: `sudo git -C /opt/cfdagent-src fetch`, then `sudo git -C /opt/cfdagent-src checkout <Task 9 merge commit> -- benchmarks/generality`. Confirm that `sudo git -C /opt/cfdagent-src diff --name-only <freeze tag>` lists only paths under `benchmarks/generality/`.
- [ ] **Step 3:** `m3-generality.json` with the ids in the order of D7, `--allow-generality` in `run_args`, same log. Run it. Apply the run rules; no rerun for a better result.
- [ ] **Step 4:** `cfd-reviewer` checks the `post.py` of every pass (as Task 8 Step 6). Table, Scorer results and plots to the owner; each result with its closest tutorial. `scribe` records them separately from the development results (design 9.2). Deallocate.
- [ ] **Step 5: Standard close** (docs only). Commit: `docs: M3 generality results`.

---

# Phase D: close-out

### Task 11: M3 close-out

**Read:** the journal entries of Tasks 7-10 (they hold the campaign log), `docs/journal/m3-run-notes.json`, ADR-0010. The run records come from the `runs` container (`run_record.json` of each run id in the log; read-only download, announce).

**Files:**
- Modify: `README.md` (Validation: M3 table and the M2 n = 3 table, with the statements of "What the M3 results can and cannot say"), `CLAUDE.md`
- Create: `docs/release-notes/v0.3.0.md`, `docs/design/m4-carryover-from-m3.md`

- [ ] **Step 1:** The full table from `campaign_table.py` (every run, invalid ones too, and any replaced, dropped or not-run generality task named beside it), stored in the journal; plots and result files stay out of git.
- [ ] **Step 2:** Spend report: Cost Management scoped to `rg-cfdagent`, from 2026-11-27, against $45; tracked against billed cost per model (this also checks Task 2); fixed and variable items separately.
- [ ] **Step 3: Owner decision D9:** keep the environment (about $8.6 a month), release only the public IP (about $3.65 a month, SSH access lost), or delete the VM, its disk and the IP and keep the storage account with the runs. Any deletion needs the owner's explicit approval.
- [ ] **Step 4:** Confirm with the owner that the deny rule is restored. `scribe` milestone entry; `m4-carryover-from-m3.md`; release notes for `v0.3.0` (results with n per task, compared with reference data or physical checks only); the owner approves them before the GitHub release.
- [ ] **Step 5: Standard close.** Commit: `docs: M3 complete, results and M4 carry-over`.

**M3 is done when** every planned run has finished (completed, failed, or listed as invalid with its reason), every approved generality task is run or listed as not run with its reason, the results table is built from run records, and the statement of limits stands next to it in the README and the journal.

---

## Self-review notes

- **Spec coverage:** design 12 M3 row as amended by ADR-0010: frozen release Task 7; campaign tooling instead of `cfdagent bench` Task 6; generality first runs Tasks 9-10; "statistics ready for the report" Tasks 8, 10, 11. Design 9.1 (never run before the freeze, owner-written texts, references by `cfd-reviewer`) Task 9; 9.2 blind evaluation unchanged, development and generality reported separately Tasks 10-11; 14.2 envelope: Budget, D3 and Task 10 Step 1.
- **Carry-over coverage:** 1 (part) and 13 Task 4; 3 (part) Task 4; 7 Task 2; 8 Task 3; 12 Task 5; 18 Task 6; M2 Task 7 (D5) Task 9; all others, and the rest of 1 and 3, in "Left out" with a reason. The owner's order (fixes, then the five benchmarks, then further tests) is Tasks 2-6, 8, 10.
- **Additions the design does not have (for the owner):** run rules with a definition of an invalid run; a frozen file set with one exception; `--wall-clock-min` and a 240-minute campaign wall clock; a campaign budget stop and repeat guard; a run-time check after meshing and two new plan-gate rules; a human-readable check of `post.py` for passes; hand-written generality solutions stored inside `benchmarks/generality/`.
- **Names checked across tasks:** `Usage.cached_tokens`, `ChatResult.served_model`, `Pricing.cached_input_per_m`, `Pricing.effective_cached_per_m`, `CostTracker.tokens_cached`, `RunRecord.tokens_in_cached`, `served_models`, `source_commit`, `max_cost_usd`, `wall_clock_s`, `CONTEXT_LOW_WATER_CHARS`, `transcript_profile.profile`, `analyst.case_inventory`, `RunDir.read_head`, `casespec.KNOWN_SOLVERS`, `mesh.runtime_problem`, `function_objects.force_problems`, `--wall-clock-min`, `run_campaign` and its plan keys (`repeat_of`, `spent_outside_log_usd`), `campaign_table.table`, `RunNote`, `m3-run-notes.json`, `--allow-generality` (CLI and `run_calibration.py`).
- **Not verified while writing the plan:** that the service reports cached tokens (Task 2 Step 5); what filled the gpt-5.4 conversations (Task 3 Step 5); the v2512 force-entry behaviour, taken by `cfd-reviewer` from reading the source and not run (Task 5 Step 1); the capacity of the gpt-5.4 deployment (Task 7 Step 3); the VM speed on the transient case (Task 7 Step 6).
