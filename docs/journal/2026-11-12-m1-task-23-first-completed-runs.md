# 2026-11-12 M1 execution, session 12: five runs after the foam_results helper and the dropped-tool-call recovery (Task 23)

Setup for all runs: benchmark `cavity-re100`, project VM, docker sandbox, commit eae8031 (clean main with PR #66, the journal of runs 18-22; PR #67, the `foam_results` helper for the Analyst; PR #68, the blank-reply probe; PR #69, recovery of dropped tool calls), image `sha-eae8031` published by CI, model gpt-oss-120b via Azure AI Foundry with Entra ID, chat-completions API, reasoning effort medium. Host setup exited 0 and `verify_sandbox` passed 7 of 7. Code and prompts did not change between runs.

## 2026-11-12 obstacle: none before the runs

CI was green on the commit and the image was published, so there was no infrastructure problem to record this session (unlike 2026-11-10).

## 2026-11-12 decision: five unchanged runs on main after PRs #66-#69

Under the owner's standing go-ahead for runs and VM use (M1 cap $10, report at $5), I ran 5 unchanged runs (runs 23-27) as one background loop on the VM. No human touched the runs and nothing was edited between the start of the first and the end of the last. No prompt or tool tuning was done (generality rule). Run ids: 23 = 20261112-102229, 24 = 103933, 25 = 104104, 26 = 105204, 27 = 110711.

## 2026-11-12 experiment: runs 23-27, the first two completed runs

Attempts per stage are the number of attempts used; "failed" marks the stage where the run ended.

| Run | Status | Attempts per stage | Tokens in / out | LLM cost | Wall time | Nudges |
|---|---|---|---|---|---|---|
| 23 (102229) | completed | planner 2, geometry 1, mesh 3, case_setup 1, solver_runner 1, analyst 1 | 985,828 / 46,394 | $0.1757 | 972 s | 0 |
| 24 (103933) | failed, mesh 3/3 | planner 1, geometry 2, mesh 3 | 101,257 / 15,016 | $0.0242 | 90 s | 0 |
| 25 (104104) | failed, case_setup | planner 1, geometry 2, mesh 1, case_setup attempt 1 unfinished | 522,004 / 35,535 | $0.0996 | 651 s | 0 |
| 26 (105204) | failed, case_setup 3/3 | planner 2, geometry 1, mesh 2, case_setup 3 | 740,261 / 36,829 | $0.1331 | 905 s | 2 |
| 27 (110711) | completed | planner 1, geometry 1, mesh 1, case_setup 1, solver_runner 1, analyst 1 | 578,717 / 39,256 | $0.1104 | 732 s | 3 |

Totals: 2 of 5 completed; failed stages mesh 1, case_setup 2. These are the first completed live runs after 22 failed ones (2 of 27 live runs overall). LLM cost of the series was $0.5430.

Errors in the failed runs:

- Run 24: `mesh.py` exited with code 1 in all three attempts; attempt 3 failed with `FileNotFoundError` when opening the casespec path (the script opened a wrong path).
- Run 25: 40 model turns passed in case_setup attempt 1 without a passing check, so `max_turns_per_attempt` was reached and the run ended.
- Run 26: the check messages differed per attempt: attempt 1 missing `pRefCell`/`pRefValue`, attempt 2 `constant/turbulenceProperties` missing, attempt 3 `system/fvSchemes` missing; the last errors were `fvSchemes` and `fvSolution` missing.

The two completed runs were compared by the Scorer with the benchmark's reference data and acceptance criteria. Overall result: passed, both criteria pass with threshold 0.05.

| Run | u_vertical_centreline error | v_horizontal_centreline error | Final time | Final initial residuals (Ux, Uy, p) |
|---|---|---|---|---|
| 23 | 0.00424 | 0.00810 | 2000 | 3.62e-06, 4.41e-06, 4.16e-06 |
| 27 | 0.01513 | 0.01189 | 1000 | 5.78e-05, 7.09e-05, 5.7e-06 |

Both runs wrote `analysis.md` and the two centreline profile plots (`u_vertical_centreline.png`, `v_horizontal_centreline.png`) in `results/plots`; the plots are in the run folders on the VM and in Blob storage, not in git. In both runs no `residualControl` criterion ended the run, so convergence by the solver's own criterion is not shown; the result stands on the comparison with reference data, together with small final residuals. There is no Critic stage (it is not part of M1).

## 2026-11-12 observation: fewer blank replies, the Analyst finally produces output, failures moved

These are observations from 5 runs, not proofs. (1) 141 tool calls across the five runs were recovered from the raw tokens (transcript lines with `"recovered": true`), and nudges per run fell to 0-3, against 14-48 in runs 18-22. (2) `foam_results` appears in the Analyst transcripts of both completed runs; in the earlier series the Analyst produced nothing or crashed. (3) The three failures are not blank-reply failures: a wrong path in `mesh.py` (run 24), turn-budget exhaustion in case_setup (run 25), and files missing in case_setup (`fvSchemes`, `fvSolution`, `turbulenceProperties`; run 26) although the completeness check reported them. The last one fits the fresh-attempt-directory model: each attempt starts from an empty directory and the model forgets what an earlier attempt already had to write (candidate D, in-place repair, still open). PRs #67 and #69 changed two things at once and n = 5, so I do not claim that either fix caused the successes.

## 2026-11-12 decision: candidate next steps, none taken

For the owner to decide. (a) Accept the series as the acceptance run, or run one more acceptance run on a fresh commit. (b) Reduce case_setup omissions in a general way (for example the in-place repair D, or a clearer completeness-check message). (c) Look at the mesh script path error from run 24 and whether a general prompt or tool fix is warranted. (d) More repeats on the same commit, to get a success-rate datapoint beyond 2 of 5. Still open from earlier entries: the effort-high experiment and Gmsh/CadQuery API notes (E).

## 2026-11-12 experiment: cost of the session

LLM cost of runs 23-27 was $0.5430. The VM was up about 2.2 hours (about $0.46, an estimate from hours up) and was deallocated at the end. Running total of M1 since Task 22: about $4.2 (LLM about $2.25, VM about $1.95; estimates), below the $5 report threshold; the cap is $10.

## 2026-11-12 milestone: Task 23 acceptance criteria met by this series on paper, M1 not yet accepted

In the letter of the plan, Task 23 Step 3 (an acceptance run on clean main with the CI image, no edits, status completed, all criteria passed, the owner sees the profile plots and the Scorer table) and Step 4 (four repeats on the same commit) are satisfied by this series on commit eae8031: run 23 is the first run, runs 24-27 are the repeats, and 2 of 5 pass. Both passing runs were checked against reference data, not only by physical checks. Whether this counts as the acceptance run, and the approval of milestone M1, are the owner's decisions; M1 is not declared accepted here.
