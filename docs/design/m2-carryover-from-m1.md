# M2 carry-over from M1

Source: the M1 close-out (2026-11-12, main `eae8031` plus the journal PR #73) and the journal entries it rests on: `docs/journal/2026-11-09-*`, `2026-11-10-*` and `2026-11-12-m1-task-23-first-completed-runs.md`. M1 was accepted by the owner on 2026-11-12 on this evidence (a sixth default-setting run on the same commit, started afterwards to check the README quick start, failed in Case Setup, so 2 of 6; two runs stopped on purpose and two runs with reasoning effort high are not counted): on `eae8031`, 2 of 5 unchanged live runs of `cavity-re100` completed and passed the Scorer against the benchmark reference data (maximum errors 0.004 to 0.015 against the threshold 0.05); 2 of 27 live runs completed over all of M1. `residualControl` was met in neither completed run. There is no Critic. The design's M2 scope is: fix loops, Critic, Scorer, knowledge base, the generality test set prepared but not run; done when the pipe and both cylinder cases pass (design section 12). This file lists what M1 deliberately left open and the new failure classes found in Task 23, as input for the M2 plan. Nothing here is decided: items marked "design change" need an ADR and the owner's approval first.

The earlier list, `docs/design/m1-carryover-from-m0-review.md`, has a status table; its open items (calibration, residual gate weaknesses, streaming parse) are repeated below where they matter for M2.

## What the evidence says about where runs fail

From the 27 live runs (the pipeline, prompts and tools changed between series, so these are observations, not rates; the run lists are examples, not exhaustive):

| Class | Seen in | State at the end of M1 |
|---|---|---|
| Blank replies: the model writes a tool call in a token form the service does not parse | all stages, about one turn in three; Analyst almost always | Cause found (PR #68), tool calls recovered from the raw tokens (PR #69); 141 calls recovered in runs 23-27, nudges per run 0-3. The service-side defect is not reported to the vendor |
| Case Setup writes an incomplete case, one omission per attempt | runs 4, 6, 7, 11, 21, 22, 26 | A completeness check lists every missing file and entry before the trial run (PR #61) and removed the plain missing-file failures; omissions still recur (run 26 lost `fvSchemes`, `fvSolution` and `turbulenceProperties` in three attempts) |
| Case Setup runs out of turns | runs 8, 9, 25 and the quick-start check run of 2026-11-12 (40 turns, `20261112-140135`) | Blank turns no longer count (PR #63); run 25 still used 40 turns without a passing check |
| Unstable set-up (`relaxationFactors` inside the SIMPLE dictionary, central differencing) | runs 10, 13 | Placement check and a prompt sentence (PR #65); did not recur in the later runs that reached the solver |
| Analyst failures: crashing result parsers (run 20); all Analyst turns blank (runs 15, 18) | runs 15, 18, 20 | `foam_results` helper (PR #67) for the first, recovery of dropped tool calls (PR #69) for the second; the helper appears in both completed Analyst transcripts |
| Geometry and mesh script errors (CadQuery and Gmsh API misuse, a wrong input path) | runs 3, 5, 12, 16, 19, 24 | Not addressed. The second and third attempt often repeat the first mistake |
| A check message that the model does not act on | run 21 (the same missing entry reported in two attempts in a row) | Not addressed |
| Steady runs that did not end by a convergence criterion | runs 15, 20, 23, 27 (run 15 set none; in run 20 `residualControl` was set but the run reached its end time; in runs 23 and 27 no criterion ended the run) | Not addressed; run 18 converged by its own criterion, so the model can set one |

## Priority items

### 1. In-place repair instead of fresh attempts (candidate D): design change, ADR needed

Today each attempt of a stage starts in an empty directory and the model sees only the error text of the previous gate, so it forgets what an earlier attempt already got right and omits something else (runs 4, 6, 7, 11, 21, 26). In-place repair keeps the attempt's files and lets the agent edit the failing ones after a failed gate or trial run. The agent already tries it by itself: in run 9 it repaired a case in place with `validate_dictionaries` and used all 30 turns of one attempt doing so, which shows the need but not that it succeeds. This changes design section 6.2 (the retry rule) and the stage loop, so it needs an ADR and the owner's approval before it is built. Recommended as the first M2 item, **behind a configuration flag** (fresh attempts stay the default until the experiment says otherwise), so that the M3 matrix can compare the two modes on the same benchmarks. It also removes most of the motivation for the next two items.

### 2. Case Setup omissions and the turn budget

The turn budget was exhausted in two of the six runs on the final M1 commit (run 25 and the check run of 2026-11-12, each ending at the limit of 40 model turns without a passing check). Independent of item 1, the check message could be made more directive (name the file to create, show the required skeleton of the entry), and `max_turns_per_attempt` (now 40) should be reviewed together with the cost cap: two runs used all of it without a passing check. Candidate measures are all general: a clearer completeness message, a per-attempt list of what already passed, a higher turn limit for case_setup only.

### 3. Mesh and geometry script errors

Run 24 failed three times because `mesh.py` opened a wrong path for the case specification; runs 5, 12 and 19 failed on Gmsh API misuse; runs 3 and 16 on CadQuery. Candidates: tell the script its input paths explicitly in the stage's opening message (a general rule, not case content); curated Gmsh and CadQuery API notes in the knowledge base (item 8); checking whether a repeated identical traceback should change the next prompt.

### 4. The convergence criterion `residualControl` (candidate G) and the Solver Runner gate

Neither completed run ended by `residualControl`, so convergence by the solver's own criterion is not shown for either; the answers pass on the comparison with reference data and small final residuals. ADR 0004 removed the Solver Runner convergence gate for M1 because the agent could not edit the case. In M2 the gate returns with routing back to Case Setup (design section 6.2, row 5), and Case Setup (or the completeness check) should require a stated convergence criterion for steady cases. The ADR is then superseded or the design amended.

### 5. Critic and cross-stage fix loops

The Critic (fresh context, design section 6.3) and the routing of a failed gate or a Critic verdict to the stage that can fix it (maximum ten fix loops overall, design section 6.5). Needs the in-place decision of item 1 and the Solver Runner gate of item 4. The Scorer is already wired (M1) and stays blind: it runs after the Critic's verdict is final.

### 6. Calibration of the four remaining benchmarks (carry-over F)

Only `cavity-re100` has a hand-written solution that passes the Scorer. Before the first agent run on `cavity-re1000`, `pipe-re500`, `cylinder-re20` and `cylinder-re100`, each needs a hand-written solution (LLM-free, through the same tools) that passes, so that an agent failure there is interpretable. The cylinder references come from secondary tables and the original papers were not available; check them as part of the calibration. The pipe also needs the kinematic-pressure trap handled in practice (the Analyst must multiply `p` by the density). The generality test set (owner-written tasks, references, criteria) is prepared in M2 and not run.

### 7. Effort-high experiment

Reasoning effort was medium in all 27 runs. A first, uncontrolled observation from the README check on 2026-11-12: of two `cavity-re100` runs started with `--reasoning-effort high`, one ended in the Planner (three attempts; the last error says the reply was cut off at the output limit, so reasoning probably consumed the token budget) and the other was stopped by the test harness after 120 s in the geometry stage, after the Planner had passed; neither was scored. This is n = 2 and not a result. The experiment needs a fixed commit, five or more runs per setting, and probably a larger `max_tokens` for structured replies at high effort.

## Knowledge and prompts

### 8. Curated knowledge notes

`src/cfdagent/knowledge/` holds only the search code; the planned `notes/*.md` do not exist yet (design section 6.6). Candidates: Gmsh and CadQuery API notes for the mesh and geometry stages (item E of the Task 23 triage), the OpenFOAM v2512 function-object names (`solverInfo`, not `residuals`), steady-solver set-up rules (relaxation placement, convective scheme choice, reference cell for pressure). All notes must obey the generality rule and pass `cfd-reviewer`. The OpenFOAM tutorials remain searchable by the agents, and a stock cavity set-up among them is a close relative of the first benchmark (Task 17 review noted similar stock set-ups for the other families, not re-checked here), so a benchmark pass partly measures retrieval and adaptation. The generality test set therefore needs families without a close stock tutorial.

### 9. The reasoning round-trip hypothesis (settled as a cause of blank replies)

The idea that a lost reasoning channel causes the blank replies was ruled out by the probe of PR #68: the Analyst's first turn has no earlier assistant message and was blank 53 times out of 53, and the model's tool call was found complete in the raw tokens. The narrower question whether sending the reasoning content back improves later turns was not tested. Low priority; record as a possible small experiment only if blank or poor turns reappear.

## Verification and gates

### 10. Steadiness gate for time-marched steady cases

A steady flow solved with a transient solver (for example `icoFoam` run to a late end time) has no `residualControl`; the Analyst only checks steadiness from the profiles. A deterministic gate (change of the monitored quantities over the last part of the run) is needed before such cases are scored. `foam_results` already contains a steadiness skeleton.

### 11. Streaming convergence checks (M3)

Today the solver log is parsed after the run. The M3 campaign needs a streaming parse that can stop a diverging or stalled run early (carry-over C, status M3).

### 12. The known gap: a self-consistent but wrong CaseSpec

The geometry and mesh gates compare the work with the `CaseSpec`, not with the task. If the Planner writes a wrong but internally consistent specification (wrong viscosity, wrong Reynolds basis, wrong domain), every later gate passes and only the blind Scorer, or a Critic, can notice. M2 options: a Critic check of the specification against the task text, a deterministic Reynolds-number recomputation from the task's stated quantities, or both.

### 13. Residual gate weaknesses recorded during M1

Geometry gate: a few-percent error of a small obstacle and unchecked `key_dimensions` are invisible, and a dropped or lumped boundary is caught only at the mesh stage (rulings R25, R28, R32). Mesh gate: resolution adequacy for the requested outputs is not verified (R33), and the structural-boundary exemptions are heuristic (R31). The trial-run Courant limit of 10 (R38) and the solver warning thresholds are judgment values the owner asked to revisit after the live runs; Task 23 did not examine them. A container killed for running out of memory was noted in Tasks 7 and 15 as classed an internal error, which would abort the run instead of failing the agent's stage; the final handling was not re-checked.

## Process and infrastructure

### 14. More repeats and a stable success-rate datapoint

n = 5 on one commit is a first datapoint. M2 changes will move the system, so repeats belong at the end of M2 on a frozen candidate, and the M3 campaign (design section 14.1) provides the real statistics. In M1 a run cost between 1 and 19 cents of model tokens (the two completed runs 11 and 18 cents) and took between 1 and 21 minutes (the completed runs 12 and 16 minutes).

### 15. Utility allowlist and executor growth

`foamToVTK`, `setFields`, `topoSet`, `createPatch`, `potentialFoam`, buoyant solvers and a `run_utility` timeout parameter were left out of M1. The pipe and cylinder cases may need some of them; add each with a test and `cfd-reviewer`.

### 16. Service-side defect and platform risk

The Foundry service drops tool calls that the model writes in a recognisable form. The recovery from logprobs works around it but depends on the service returning raw tokens. If the platform changes, blank replies may return; the unit tests with recorded token lists and the blank-reply probe script (`scripts/probe_blank_replies.py`) are the early warning. Reporting the defect to the vendor was recommended in the PR #68 entry and not done.

### 17. Release hygiene

The package version is `0.0.1` in `pyproject.toml`, `__init__` and `CITATION.cff` (a test keeps them equal, and `tests/unit/test_package.py` hard-codes the number). A `v0.1.0` tag needs all of them bumped first. Dependency, image and branch-protection settings were not re-examined in M1 close-out.
