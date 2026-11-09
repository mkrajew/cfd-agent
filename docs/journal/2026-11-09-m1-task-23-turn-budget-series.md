# 2026-11-09 M1 execution, session 10: five runs with the turn-budget and flux-file fixes (Task 23)

Setup for all runs: benchmark `cavity-re100`, project VM, docker sandbox, commit 5b704d8 (main with PR #62, the journal of runs 8-12, and PR #63), image `sha-5b704d8`, model gpt-oss-120b via Azure AI Foundry with Entra ID, chat-completions API, reasoning effort medium. Host setup exited 0 and `verify_sandbox` passed 7 of 7. Code and prompts did not change between runs. PR #63 made three changes: blank nudged turns no longer count against `max_turns_per_attempt`, flux files such as `0/phi` are caught by the case check, and solver errors name the offending file.

## 2026-11-09 decision: five unchanged runs to measure the PR #63 fixes

Under the owner's standing go-ahead for runs and VM use (M1 cap $10, report at $5), I ran 5 unchanged runs (runs 13-17) as one background loop on the VM, to measure the effect of PR #63. No prompt or tool tuning was done (generality rule); the triage below is recorded for the owner.

## 2026-11-09 experiment: runs 13-17, the first runs past case_setup

Nothing here is compared with reference data: the Scorer never ran in any of the five runs (the run that got furthest ended before it). The physical statements come only from the solver log of one run. Attempts per stage are the number of attempts used; "failed" marks the stage where the run ended.

| Run | Failed stage | Attempts per stage | Tokens in / out | LLM cost | Wall time | Nudges |
|---|---|---|---|---|---|---|
| 13 (162818) | solver_runner | planner 1, geometry 1, mesh 1, case_setup 3 (passed), solver_runner 1 (not retried) | 479,510 / 25,988 | $0.0875 | 568 s | 11 |
| 14 (163748) | case_setup 3/3 | planner 1, geometry 2, mesh 2, case_setup 3 | 268,080 / 22,561 | $0.0537 | 382 s | 10 |
| 15 (164411) | analyst 3/3 | planner 1, geometry 2, mesh 1, case_setup 1, solver_runner 1 (passed), analyst 3 | 243,829 / 22,051 | $0.0498 | 316 s | 11 |
| 16 (165020) | geometry 3/3 | planner 2, geometry 3 | 93,115 / 14,933 | $0.0229 | 103 s | 1 |
| 17 (165205) | case_setup 3/3 | planner 1, geometry 2, mesh 1, case_setup 3 | 250,701 / 22,816 | $0.0513 | 275 s | 10 |

Totals: 0 of 5 completed; failed stages case_setup 2, solver_runner 1, analyst 1, geometry 1. Geometry and mesh were passed in 4 of 5 runs and case_setup in 2 of 5. With the 12 earlier live runs, 0 of 17 live runs have completed.

Errors per run:

- Run 13: case_setup passed after 3 attempts, but the `simpleFoam` run diverged (continuity errors of order 1e60). The model had put `relaxationFactors` inside the SIMPLE dictionary, where it has no effect (the same mistake as run 10), with bounded `linearUpwind` for convection.
- Run 14: attempts 1 and 2 consisted only of blank replies (finish_reason stop, empty text, 35-704 completion tokens, no tool call), including after both nudges, so the gate found no `controlDict`. Attempt 3 wrote all files, and the check rejected a missing `div((nuEff*dev2(T(grad(U)))))` entry.
- Run 15: the first run in which the solver ran to completion. `simpleFoam` ran 1000 iterations in about 12 s of solver time, with final initial residuals Ux 1.06e-4, Uy 1.28e-4, p 8.7e-5 and small continuity errors. No `residualControl` was set, so the run is not shown to be converged by the solver's own criterion. The Analyst then failed in all 3 attempts, because every attempt was blank replies (no `post.py`, no `analysis.md`); the run summary says "Benchmark run, but not scored".
- Run 16: the CadQuery script exited with code 1 in all three attempts (a different traceback in attempt 1; attempts 2 and 3 printed the bounding box and then failed).
- Run 17: attempts 1 and 2 were blank replies again (no `controlDict`). Attempt 3 wrote all files and the check rejected `0/p`: the `front` patch is of type `empty` in the mesh but the field has `zeroGradient` (patch type mismatch, caught by the case check).

## 2026-11-09 observation: blank replies are now the dominant failure

These are observations from 5 runs, not proofs, and no failure rates can be claimed. (1) For the first time runs passed case_setup and reached later stages: run 13 reached the solver runner and run 15 the analyst, so the farthest stage moved from case_setup to analyst. (2) In runs 14, 15 and 17 whole attempts consisted of blank replies, at the first turn and after both nudges (`MAX_BLANK_NUDGES` is 2), so the attempt ended as "done" with nothing written and was spent. The blank turns are short (35-110 completion tokens, plus one 704-756 token turn after the "checks were run" message). This is consistent with the earlier unverified hypothesis that the endpoint or the chat-completions history loses the model's reasoning or tool call; it was not tested. (3) `relaxationFactors` inside SIMPLE diverged runs 10 and 13, two of the ten most recent runs, a second data point for the earlier candidate remedy C. (4) PR #63 effects: no run hit `max_turns_per_attempt`, and the flux-file mistake of run 8 did not recur, which is not proof that it is fixed. (5) Since run 15 reached the Analyst, its prompt and gate behaviour are still untested with real output.

## 2026-11-09 decision: candidate remedies, none implemented

For the owner to decide; each is general, not case-specific. (F) Make blank-reply recovery stronger: more nudges before an attempt is declared done, and either do not count an attempt that wrote nothing against the three attempts or retry the first turn. (C) A general rule or check that `relaxationFactors` is a top-level `fvSolution` dictionary, with a warning on central differencing for steady convection. (G) A general case_setup or analysis guard that a solved case needs a convergence criterion. (E) Gmsh/CadQuery API notes only if geometry and mesh failures continue. (D) In-place repair after a failed trial run (a design change, needs an owner decision). Still open from earlier entries: the reasoning round-trip hypothesis and the effort-high experiment.

## 2026-11-09 experiment: cost of runs 13-17

LLM cost of runs 13-17 was $0.2652. The VM was up about 1.3 hours (about $0.27); it deallocated itself on the host idle policy once the series was done, was started once more to read the results, and was then deallocated. Running total of M1 since Task 22: about $2.2, below the $5 report threshold.

## 2026-11-09 milestone: M1 acceptance not reached

Cavity Re=100 end to end without human intervention has not been achieved: 0 of 17 live runs completed. This is a valid partial result; the farthest stage reached moved from case_setup to analyst, but the success rate remains 0.
