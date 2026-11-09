# 2026-11-09 M1 execution, session 8: five unchanged repeat runs (Task 23)

Setup for all runs: benchmark `cavity-re100`, project VM, docker sandbox, commit 2849c86 (main with PR #58, the CLI image fix, and PR #59, the blank-reply nudge), image `sha-2849c86`, model gpt-oss-120b via Azure AI Foundry with Entra ID, chat-completions API, reasoning effort medium. Host setup exited 0 and `verify_sandbox` passed 7 of 7. There was no `docker tag` alias this time, so the CLI fix was exercised on the real VM. Code and prompts did not change between runs.

## 2026-11-09 decision: five unchanged repeats, no tuning

The owner merged #58 and #59 in chat and gave a standing go-ahead for further runs and VM use (M1 cap $10, report at $5). I ran 5 unchanged repeats (runs 3-7) as the first success-rate datapoint, not as an acceptance run. No prompt or tool tuning was done (generality rule); the triage below is recorded for the owner.

## 2026-11-09 experiment: runs 3-7, none reached the solver

None of the five runs reached the Solver Runner, so the Scorer never ran: nothing here is compared with reference data or physically checked. Attempts per stage are given as the number of attempts used; "failed" marks the stage where the run ended.

| Run | Failed stage | Attempts per stage | Tokens in / out | LLM cost | Wall time | Nudges |
|---|---|---|---|---|---|---|
| 3 (074302) | geometry 3/3 | planner 1, geometry 3 | 36,468 / 7,858 | $0.0102 | 52 s | 0 |
| 4 (074831) | case_setup 3/3 | planner 1, geometry 2, mesh 3, case_setup 3 | 418,291 / 34,862 | $0.0837 | 487 s | 15 |
| 5 (080019) | mesh 3/3 | planner 2, geometry 2, mesh 3 | 108,988 / 29,806 | $0.0342 | 230 s | 2 |
| 6 (080856) | case_setup 3/3 | planner 2, geometry 2, mesh 2, case_setup 3 | 328,439 / 23,971 | $0.0636 | 356 s | 4 |
| 7 (081539) | case_setup 3/3 | planner 2, geometry 1, mesh 2, case_setup 3 | 659,532 / 37,148 | $0.1212 | 795 s | 11 |

Errors per attempt:

- Run 3, geometry: attempt 1 `AttributeError` (`'list' object has no attribute 'size'`); attempts 2 and 3 an identical `IndentationError` at the same line, so the model repeated its mistake.
- Run 4, case_setup: no `system/controlDict`; the `simpleFoam` trial run failed with `Unable to set reference cell for field p`; missing `constant/turbulenceProperties`.
- Run 5, mesh: `mesh.py` exited with code 1 (traceback in the Gmsh helper) in attempts 1 and 2; attempt 3 failed `checkMesh` with "faces on empty patches not divisible by number of cells; mesh is not 1D or 2D".
- Run 6, case_setup: no `controlDict`; `Expected a ( while reading VectorSpace, found int32 0` (malformed vector in a field file); missing `constant/turbulenceProperties`.
- Run 7, case_setup: `fvSchemes` lacks `divSchemes`; `transportProperties` lacks `transportModel`; `turbulenceProperties` lacks `simulationType`.

Totals for runs 3-7: 0 of 5 completed; failed stages geometry 1, mesh 1, case_setup 3. With runs 1-2 of 2026-11-08 (run 1 was the infrastructure failure fixed by #58), 0 of 7 live runs have completed.

## 2026-11-09 observation: the blank-reply nudge works but is not sufficient

Blank replies (`finish_reason` `stop`, no text, no tool call) are frequent: in run 4 there were 15 nudges in 47 turns, about a third of the turns. Turns with several thousand completion tokens and no visible output also occur (for example 3,370 tokens, then nothing), which suggests that the model's reasoning, and possibly its tool call, is not delivered in the visible channel; the endpoint does not report `reasoning_tokens`. This is a hypothesis, not verified. With the nudge, case_setup now writes whole cases (run 4 attempt 2 wrote seven files), whereas in run 2, before the nudge, case_setup failed at attempt 1 with a single file.

## 2026-11-09 observation: incomplete solver cases are the new dominant failure

In runs 4, 6 and 7 the model produced incomplete solver cases: each attempt fixes the error the previous trial run reported and omits something else (reference cell for `p`, `constant/turbulenceProperties`, `simulationType`, `transportModel`, `divSchemes`, a malformed vector). The case_setup prompt already says that `simpleFoam` reads `constant/transportProperties` and `constant/turbulenceProperties`, so the model does not follow it reliably. The trial run reveals one missing item at a time and each attempt starts in a fresh directory, so three attempts are not enough to converge. The Planner chose `simpleFoam` (steady) in all runs; that is its legitimate choice and was not changed. Candidate general remedies, for the owner to decide and none done: (a) a deterministic pre-trial check in `check_case_consistency` that lists all files and mandatory entries the chosen solver reads, so the model gets the complete list in one error message; (b) a short completeness checklist in the case_setup prompt; (c) repair in place within an attempt instead of a fresh directory (a design change, needs the owner).

## 2026-11-09 observation: geometry and mesh failures look like model code errors

The geometry and mesh failures are ordinary code errors (syntax and API misuse; in run 3 the same `IndentationError` twice) and are consistent with a model limitation. The second and third attempts do not reliably change the faulty line. The mesh traceback in the Gmsh helper was not analysed further.

## 2026-11-09 experiment: cost of runs 3-7

LLM cost of runs 3-7 was $0.3329 ($0.0102 + $0.0837 + $0.0342 + $0.0636 + $0.1212). The VM was up about 65 minutes (about $0.23) and was then deallocated. Running total of M1 since Task 22: LLM about $0.37 (runs 1-7) plus VM about $0.33, roughly $0.70, far below the $5 report threshold.

## 2026-11-09 milestone: M1 acceptance not reached

Cavity Re=100 end to end without human intervention has not been achieved: 0 of 7 live runs completed. This is a valid partial result; the CLI fix (#58) is confirmed on the real VM and the nudge (#59) measurably changes case_setup behaviour, but the success rate remains 0.
