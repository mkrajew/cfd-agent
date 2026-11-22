# 2026-11-22 M2 Task 17, Step 1: repair-mode comparison on cavity-re100

## 2026-11-22 experiment: six runs, fresh against in_place repair mode

Project VM, host set up at commit 8164468 (main tip, PR #94 merged) with image `sha-052833f`, because the CI image build for 8164468 was still running after more than an hour; 052833f..8164468 changes only docs (runbook, journal), so the code in the image is the code of the commit. `verify_sandbox` passed 7 of 7. Model gpt-oss-120b, reasoning effort medium, default limits (3 attempts per stage and pass, 10 fix loops, 2 h wall clock, $1 per run). The six runs ran as one background loop, modes alternating fresh / in_place, nothing edited between runs, under the owner's approved Task 17 cap of $7 (decision D7). Attempts are gate runs per stage; `#n` is pass n after fix loop n; X means failed.

| # | Run id | Mode | Status | Failed stage | Attempts per stage | Fix loops | Critic | Scorer | Critic agrees | Tokens in / out | LLM cost | Wall time |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 20261122-142349 | fresh | completed | - | planner 1, geometry 2, mesh 3, case_setup 1, solver_runner 1, analyst 1, critic 1 | 0 | success | passed | yes | 436,842 / 43,964 | $0.0919 | 499 s |
| 2 | 20261122-143219 | in_place | completed | - | planner 1, geometry 3, mesh 1; case_setup 1 + 1 + 3 + 1 and solver_runner 1 per pass over passes 0-3; analyst 1, critic 1 | 3 (all: steady state not reached) | success | passed | yes | 2,352,405 / 107,403 | $0.4173 | 3273 s |
| 3 | 20261122-153438 | fresh | failed | case_setup, pass 1, 3 of 3 | planner 1, geometry 2, mesh 2, case_setup 3, solver_runner 1 X, case_setup#1 3 X | 1 (steady state not reached) | - | - | - | 1,466,152 / 62,113 | $0.2572 | 1768 s |
| 4 | 20261122-160422 | in_place | failed | geometry, 3 of 3 | planner 2, geometry 3 X | 0 | - | - | - | 76,338 / 11,734 | $0.0185 | 96 s |
| 5 | 20261122-160603 | fresh | failed | case_setup, pass 1, 3 of 3 | planner 1, geometry 2, mesh 1, case_setup 1, solver_runner 1 X, case_setup#1 3 X | 1 (steady state not reached) | - | - | - | 934,780 / 57,538 | $0.1747 | 1278 s |
| 6 | 20261122-162727 | in_place | failed | case_setup (wall-clock limit of 7200 s reached during the stage) | planner 1, geometry 3, mesh 1; case_setup 1 and solver_runner 1 X in each of passes 0-6, case_setup#7 unfinished | 7 (5 steady state not reached, 2 solver run without final 'End' line, killed at its time limit) | - | - | - | 1,617,869 / 78,632 | $0.2899 | 7204 s |

Fresh completed 1 of 3 and in_place completed 1 of 3; this is a first observation from n = 3 per mode, not a rate. Both completed runs passed the Scorer (compared with reference data) and the Critic said success in both, so the Critic agreed with the Scorer in both. LLM cost $1.2495; VM about 4.4 h (14:19 to 18:42 UTC, start to deallocation), about $0.92 at $0.208/h; series total about $2.16 of the $7 cap. All six runs uploaded to Blob Storage (`summary.md` present under each run prefix). The VM was deallocated and the state verified.

## 2026-11-22 observation: no evidence on the Courant limit (D9)

No run hit the trial-run Courant limit of 10. All six runs chose simpleFoam (steady), for which the Courant check does not apply, so this series gives no evidence for decision D9.

## 2026-11-22 observation: convergence gate and steady SIMPLE

Four of six runs were routed back at least once with "steady state not reached". The first Case Setup typically set `residualControl` (p, U at 1e-5 or 1e-6) with endTime 1000 and no monitored quantity, and the run reached endTime without meeting `residualControl`. Run 1 passed first time with endTime 5000 and `residualControl` 1e-5 (converged at iteration 1772). Run 2 reached the gate three times and passed at the fourth pass with endTime 50000 (converged at iteration 9788). In run 6 the model changed only the `residualControl` tolerances for three passes with endTime still 1000, then added a centre probe (the gate reported relative peak-to-peak changes of 0.054-0.15 in the probed velocity over the last 0.1 of the run), and finally set endTime 20000 and then 200000 with tolerances 1e-8 / 1e-9. The initial residual of Ux stalled at about 1.2e-5 at iteration 17,773, the solver hit its time limit twice, and the run ended at the 2 h wall-clock limit.

## 2026-11-22 observation: fresh mode loses files in fix-loop re-runs

Both failed fresh runs failed in a Case Setup re-run after a route back, with files missing in the new attempt directory (run 3: `system/controlDict` missing in the third attempt; run 5: the trial run could not find `0/p`). This is the M1 pattern (each fresh attempt starts from an empty directory) seen again, now inside a fix-loop pass.

## 2026-11-22 observation: in_place mode keeps files but can spend the whole wall clock

No in_place run lost files. In_place runs used more fix loops (3 and 7) because the model kept repairing the same steady-state problem; run 6 shows that up to 10 fix loops can use the whole 2 h wall clock.

## 2026-11-22 observation: geometry failures in run 4 (wrong casespec path, number parsing)

Attempt 1's `geometry.py` opened `/job/attempts/casespec.json` (`../casespec.json`, one `..` short of the run directory); the prompts say `../../casespec.json` from the attempt directory. Attempts 2 and 3 parsed numbers out of the description text with a regular expression and failed on '0,'. This is the same class as the wrong casespec path of M1 run 24 (mesh). Planner attempt 1 of the same run gave `solver.end_time_s = 0` and was rejected by the planner check.

## 2026-11-22 decision: repair mode for the acceptance series is pending

Which repair mode the acceptance series uses is the owner's decision; it was reported on 2026-11-22 with a recommendation and has not been made yet. The comparison above is a first observation from n = 3 per mode, not a rate.
