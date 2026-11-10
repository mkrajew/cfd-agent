# 2026-11-10 M1 execution, session 11: five runs with the blank-reply recovery and relaxation-check fixes (Task 23)

Setup for all runs: benchmark `cavity-re100`, project VM, docker sandbox, commit f7ef4aa (main with PR #64, the journal of runs 13-17, and PR #65), image `sha-f7ef4aa`, model gpt-oss-120b via Azure AI Foundry with Entra ID, chat-completions API, reasoning effort medium. Host setup exited 0 and `verify_sandbox` passed 7 of 7. Code and prompts did not change between runs. PR #65 made two changes: stronger blank-reply recovery (more nudges; an attempt in which the model produced nothing at all does not consume one of the stage's attempts; a bounded number of free restarts per stage, recorded as `free_restarts` in the run record), and a case check that `relaxationFactors` sits at the top level of `fvSolution`, with one sentence added to the prompt.

## 2026-11-10 obstacle: CI infrastructure failures before the runs

Two CI problems occurred that day, both unrelated to the code. The first CI run of PR #65 lost its lint, unit and infra-lint jobs; a rerun was green. On main the publish job failed three times with a registry "unauthenticated" error when pushing the image, and succeeded on the fourth attempt (the GitHub status page showed no incident, and no workflow or Docker change was made). The image was then published and the runs started.

## 2026-11-10 decision: five unchanged runs to measure the PR #65 fixes

Under the owner's standing go-ahead for runs and VM use (M1 cap $10, report at $5), I ran 5 unchanged runs (runs 18-22) as one background loop on the VM, to measure the effect of PR #65. No prompt or tool tuning was done (generality rule).

## 2026-11-10 experiment: runs 18-22, the Analyst is reached twice

Nothing here is compared with reference data: the Scorer never ran in any of the five runs, because every run ended before it. The physical statements come only from solver logs. Attempts per stage are the number of attempts used; "failed" marks the stage where the run ended.

| Run | Failed stage | Attempts per stage | Tokens in / out | LLM cost | Wall time | Nudges |
|---|---|---|---|---|---|---|
| 18 (082841) | analyst 3/3 | planner 1, geometry 1, mesh 2, case_setup 2, solver_runner 1 (passed), analyst 3 | 612,009 / 29,425 | $0.1095 | 721 s | 38 |
| 19 (084046) | mesh 3/3 | planner 1, geometry 1, mesh 3 | 84,325 / 12,243 | $0.0200 | 98 s | 3 |
| 20 (084225) | analyst 3/3 | planner 1, geometry 2, mesh 3, case_setup 2, solver_runner 1 (passed), analyst 3 with 1 free restart | 981,835 / 63,117 | $0.1851 | 1272 s | 48 |
| 21 (090347) | case_setup 3/3 | planner 1, geometry 2, mesh 1, case_setup 3 | 737,540 / 34,149 | $0.1311 | 906 s | 14 |
| 22 (091855) | case_setup 3/3 | planner 2, geometry 2, mesh 3, case_setup 3 | 843,549 / 48,375 | $0.1556 | 1078 s | 15 |

Totals: 0 of 5 completed; failed stages analyst 2, case_setup 2, mesh 1. The solver runner passed in 2 of 5 runs (both then failed in the Analyst). With the 17 earlier live runs, 0 of 22 live runs have completed. LLM cost of runs 18-22 was $0.6013.

Errors per run:

- Run 18: the solver converged by its own criterion (`SIMPLE solution converged in 1913 iterations`, with `residualControl` set), the first solver run with a convergence verdict from the solver itself. The Analyst produced nothing in any attempt: all analyst model turns were blank replies (15, 5 and 5 turns), so there is no `post.py` and no `analysis.md`.
- Run 19: `mesh.py` exited with code 1 in all three attempts (attempt 1 a Gmsh error of the "Unknown model entity" type, attempts 2 and 3 Python tracebacks).
- Run 20: the solver ran to the end (final initial residuals about 1e-4, small continuity errors; `residualControl` was set but the run reached `End`, so convergence is not claimed beyond what the log shows). Analyst attempt 1 wrote nothing (25 of 41 turns blank). Attempts 2 and 3 wrote `post.py`, but it crashed: attempt 2 on a pyvista file-reader error on the case, attempt 3 on an error in its own field parser at line 60 of `extract_profiles`.
- Run 21: attempts 1 and 2 were rejected by the completeness check for the same missing entry, `div((nuEff*dev2(T(grad(U)))))`, with the same message both times; attempt 3 had no `controlDict`.
- Run 22: in attempt 1 `transportProperties` could not be parsed (ill-defined primitiveEntry); in attempts 2 and 3 the trial run failed with "Discretisation scheme not specified" at `laplacianSchemes/default`.

In the two runs that reached the solver, `relaxationFactors` was at the top level, so the divergence mistake of runs 10 and 13 did not occur there.

## 2026-11-10 observation: the Analyst is the new bottleneck, blank replies persist

These are observations from 5 runs, not proofs, and no failure rates can be claimed. (1) The farthest stage is the analyst again (as in run 15), now in two of five runs, and in one of them the solver converged by its own criterion. The Analyst prompt and tools were exercised with real input for the first time: either the model returned blanks (run 18: 25 of 25 turns blank across 3 attempts; run 20 attempt 1: 25 of 41) or its post-processing script crashed on reading the OpenFOAM case, with pyvista or with a hand-written field parser. The blank share looks higher in the Analyst than in other stages. (2) The PR #65 recovery works as designed (`free_restarts` recorded for run 20, analyst, 1), but cannot help when the model returns blanks in every turn of every attempt; nudge counts rose to 38 and 48. (3) The model did not act on a repeated check message: in run 21 the same missing-entry message was returned in two attempts in a row. (4) Blank replies remain unexplained; the reasoning round-trip hypothesis from earlier entries is still untested.

## 2026-11-10 decision: candidate remedies, none implemented

For the owner to decide; each is general, not case-specific. (H) Investigate the blank replies directly with a small standalone probe against the endpoint (same messages, tools and settings; compare reasoning effort and whether the reasoning content must be passed back), because all stages are affected and the Analyst almost completely; it needs no full run and should cost cents. (I) Give the Analyst a ready general helper for reading OpenFOAM results (for example a function that loads the fields of the latest time as arrays), so the model does not have to write a parser. (J) One more attempt for the Analyst, or in-place repair (D, a design change). (G) The convergence criterion stays the model's job; run 18 shows it can set one. Still open from earlier entries: the reasoning round-trip, the effort-high experiment and Gmsh/CadQuery API notes (E).

## 2026-11-10 experiment: cost of runs 18-22

LLM cost of runs 18-22 was $0.6013. The VM was up about 1.8 hours on 2026-11-10 (about $0.37) before it was deallocated. Running total of M1 since Task 22: about $3.2 (LLM about $1.70, VM about $1.49; the VM figure is an estimate from hours up), below the $5 report threshold but approaching it; the cap is $10.

## 2026-11-10 milestone: M1 acceptance not reached

Cavity Re=100 end to end without human intervention has not been achieved: 0 of 22 live runs completed. The solver converged by its own criterion once (run 18). This is a valid partial result; the farthest stage reached is the analyst, but the success rate remains 0.
