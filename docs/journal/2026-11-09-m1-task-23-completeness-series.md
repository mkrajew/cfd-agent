# 2026-11-09 M1 execution, session 9: five runs with the completeness check (Task 23)

Setup for all runs: benchmark `cavity-re100`, project VM, docker sandbox, commit c7b6f11 (main with PR #60, the journal of runs 3-7, and PR #61, the case_setup completeness check), image `sha-c7b6f11`, model gpt-oss-120b via Azure AI Foundry with Entra ID, chat-completions API, reasoning effort medium. Host setup exited 0 and `verify_sandbox` passed 7 of 7. Code and prompts did not change between runs.

## 2026-11-09 decision: five unchanged runs to measure the completeness check

The owner merged #60 and #61 in chat and had given a standing go-ahead for runs and VM use (M1 cap $10, report at $5). I ran 5 unchanged runs (runs 8-12) to measure the effect of PR #61, in which `check_case_consistency` lists every file and entry the chosen solver reads before the trial run (validated against real OpenFOAM in 69 integration cases). No prompt or tool tuning was done (generality rule); the triage below is recorded for the owner.

## 2026-11-09 experiment: runs 8-12, none reached the solver runner

None of the five runs reached the Solver Runner, so the Scorer never ran: nothing here is compared with reference data or physically checked. Attempts per stage are the number of attempts used; "failed" marks the stage where the run ended.

| Run | Failed stage | Attempts per stage | Tokens in / out | LLM cost | Wall time | Nudges |
|---|---|---|---|---|---|---|
| 8 (124433) | case_setup | planner 2, geometry 2, mesh 3, case_setup 2 recorded plus a third that hit `max_turns_per_attempt` | 1,005,089 / 44,660 | $0.1776 | 1199 s | 18 |
| 9 (131019) | case_setup | planner 2, geometry 2, mesh 1, case_setup attempt 1 ended after 30 turns | 366,836 / 29,263 | $0.0726 | 429 s | 6 |
| 10 (132511) | case_setup 3/3 | planner 2, geometry 2, mesh 2, case_setup 3 | 453,839 / 38,556 | $0.0912 | 532 s | 12 |
| 11 (134155) | case_setup 3/3 | planner 1, geometry 2, mesh 1, case_setup 3 | 474,960 / 28,967 | $0.0886 | 555 s | 8 |
| 12 (135519) | mesh 3/3 | planner 2, geometry 2, mesh 3 | 89,936 / 19,150 | $0.0250 | 123 s | 1 |

Totals: 0 of 5 completed; failed stages case_setup 4, mesh 1. Geometry and mesh were passed in 4 of 5 runs, a change from runs 3-7. With the 7 earlier live runs, 0 of 12 live runs have completed.

Errors per run:

- Run 8: the completeness check passed and the real solver was reached, so the earlier missing-file failures are gone. All three attempts failed at the solver: the model wrote an unneeded `0/phi` file (class `surfaceScalarField`) with volume-field boundary types (`movingWall` in attempt 1, `zeroGradient` in attempt 2), which surface fields do not accept. The solver error "Unknown patchField type X for patch type wall" does not name the file, so the model kept rewriting `0/phi`; attempt 3 hit the 30-turn limit still doing so, after three knowledge-base searches for phi.
- Run 9: in attempt 1 the model repaired its files in place with `validate_dictionaries` (5 calls, guided by the completeness messages) but used all 30 turns; 5 were blank replies and 5 were knowledge searches or reads, and it never reached its final reply. The turn budget, not knowledge, stopped it.
- Run 10: attempt 1 had no `controlDict`; attempt 2 was rejected by the completeness check for missing `pRefCell`/`pRefValue` (the check worked as designed); attempt 3 passed the checks but the 3-step `simpleFoam` trial run diverged: the model chose central differencing (`Gauss linear`) for the steady convection term and put `relaxationFactors` inside the SIMPLE dictionary, where it has no effect.
- Run 11: every attempt was rejected by the completeness check, each time for a different item: missing `transportModel`, a missing `div((nuEff*dev2(T(grad(U)))))` entry, and in attempt 3 no `fvSolution` at all. Fresh attempt directories make the model forget items it wrote before.
- Run 12: `mesh.py` failed in all three attempts with Gmsh API misuse: attempt 1 called a non-existent `gmsh.model.getAdjacents`; attempts 2 and 3 called `gmsh.model.getBoundary` with wrong arguments and raised in the Gmsh wrapper. This is a model code/API error.

## 2026-11-09 observation: the completeness check changed the failure mode

These are observations from 5 runs, not proofs, and no failure rates can be claimed. (1) The check removed the main failure of runs 3-7, incomplete solver cases reaching the trial run one missing item at a time: in four of five runs case_setup got further, and in runs 10 and 11 the check itself rejected incomplete cases with complete lists. (2) The new dominant failures are different: a wrong or unnecessary `0/phi`, turn-budget exhaustion during in-place repair, an unstable scheme choice with misplaced relaxation factors, and Gmsh API errors. (3) The attempt model (fresh directory per attempt, then rewrite everything) makes the model forget items; in-place repair within an attempt appeared to work in run 9 and was limited by the 30-turn budget (`RunLimits.max_turns_per_attempt`, default 30; blank replies count as turns).

## 2026-11-09 decision: candidate remedies, none implemented

For the owner to decide; each is general, not case-specific. (A) Raise `max_turns_per_attempt` and/or do not count blank (nudged) turns. (B) Name the offending file in the trial-run error, and add a general case_setup note that solver-computed fields such as `phi` must not be written (a deterministic check could flag a `0/` surface-field file). (C) A general check or prompt rule against unstable steady convection schemes and for `relaxationFactors` being a top-level `fvSolution` dictionary. (D) In-place repair after a failed trial run (a design change, needs an owner decision). (E) A short Gmsh API note in the knowledge base for the mesh stage. Still open from earlier entries: the reasoning round-trip hypothesis and the effort-high experiment.

## 2026-11-09 experiment: cost of runs 8-12

LLM cost of runs 8-12 was $0.4550. The VM was up about 2.5 hours (about $0.5) and was then deallocated. Running total of M1 since Task 22: roughly $1.7 (LLM about $0.83, VM about $0.85), below the $5 report threshold.

## 2026-11-09 milestone: M1 acceptance not reached

Cavity Re=100 end to end without human intervention has not been achieved: 0 of 12 live runs completed. This is a valid partial result; the completeness check (#61) measurably moves failures from missing solver files to later, different causes, but the success rate remains 0.
