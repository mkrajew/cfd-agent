# 2026-11-25 M2 Task 19: acceptance series

Owner-approved done rule (D8): M2 is done when each of `pipe-re500`, `cylinder-re20` and `cylinder-re100` has at least 1 of 3 unchanged runs passing the Scorer; `cavity-re1000` and `cavity-re100` are run and reported in the same series. Series cap $6 (D8). Candidate and configuration: `2026-11-25-m2-task-18-freeze.md`. "Scorer" means compared with reference data; nothing else here is.

## 2026-11-25 obstacle: first attempt lost to a full OS disk

The first attempt ran on 2026-11-25 from about 13:27Z to 14:13Z. 16 old sandbox images filled the VM's 64 GB OS disk to 100 %, runs failed with `ENOSPC` and `az vm run-command` stopped answering until space was freed over SSH (as recorded in PR #116). Four runs are infrastructure failures, excluded from all results: 20261125-132714 (`cavity-re100`; internal error, the executor for `run_solver` failed with exit code 1), 20261125-140745 (`cylinder-re20`; "No space left on device"), and 20261125-140838 and 20261125-141318 (run records never finalised, still "running"); their tracked LLM cost was $0.162. The record of 132714 shows only that the `run_solver` executor failed with exit code 1, after one fix loop; counting it with the disk failure is the series operator's attribution, not something the record shows. The series was restarted from the beginning at 19:35Z. PR #116, which makes the host set-up script prune old images, is the later prevention: it was opened at 19:49Z and merged at 20:07Z, while the restarted series ran.

## 2026-11-25 experiment: 3 rounds of 5 benchmarks (15 runs)

Run from 2026-11-25 19:35Z to about 00:00Z on 2026-11-26; every run id is `20261125-` plus the time part. Scorer errors are the maximum errors of the u and v centreline profiles (threshold 0.05). "-" means no score or verdict.

| Run id | Benchmark | Result | Scorer | Critic verdict | Fix loops | LLM $ | Wall s (truncated) |
|---|---|---|---|---|---|---|---|
| 193559 | cavity-re100 | completed | pass (u 0.0035, v 0.0075) | success | 0 | 0.1006 | 553 |
| 194518 | cavity-re1000 | completed | FAIL (u 0.0719, v 0.0687) | success | 0 | 0.1689 | 1290 |
| 201441 | pipe-re500 | failed geometry: boundary manifest does not cover `geometry.step` (a surface matched by no patch; a box locator short by about 1e-4) | - | - | 0 | 0.0388 | 241 |
| 201843 | cylinder-re20 | failed case_setup in pass 1: third simpleFoam trial run, "Could not find rho:rho in database" | not scored | no final verdict (note below) | 1 | 0.2897 | 1787 |
| 204837 | cylinder-re100 | failed at case_setup with 0 attempts (`internal_error`): the plan suggested pisoFoam, which the pipeline does not run (it runs icoFoam, simpleFoam, pimpleFoam), and the planner gate had not rejected it | - | - | 0 | 0.0260 | 184 |
| 205143 | cavity-re100 | completed | pass (u 0.0303, v 0.0193) | success | 1 | 0.1270 | 755 |
| 210425 | cavity-re1000 | failed analyst: 3 attempts, each rejected because `analysis.md` did not mention convergence | - | - | 1 | 0.1335 | 1234 |
| 212511 | pipe-re500 | failed geometry: manifest surfaces matched by more than one patch | - | - | 0 | 0.0213 | 144 |
| 212736 | cylinder-re20 | failed case_setup: simpleFoam trial, "Could not find rho:rho in database" | - | - | 0 | 0.0903 | 1970 |
| 220035 | cylinder-re100 | failed geometry: volume mismatch, expected 0.004, got 0.00398429 (0.4 %) | - | - | 0 | 0.0397 | 220 |
| 220416 | cavity-re100 | completed | pass (u 0.0041, v 0.0086) | partial after 2 Critic rounds | 4 (loops 1-2 solver gate, steady state not reached; loops 3-4 Critic to Analyst) | 0.3698 | 2879 |
| 230217 | cavity-re1000 | failed case_setup: trial run, "Unknown patchField type movingWall for patch type wall" | - | - | 0 | 0.1211 | 722 |
| 231427 | pipe-re500 | failed case_setup: trial run, "Unknown discretisation type none" at `snGradSchemes` `snGrad(p)` | - | - | 0 | 0.2871 | 2037 |
| 234827 | cylinder-re20 | failed planner, 3 attempts; last error: `geometry.volume_m3` was not a value returned by a calculator call | - | - | 0 | 0.0214 | 126 |
| 235035 | cylinder-re100 | failed case_setup: pimpleFoam trial, "No entries of the type (e1, e2) or (e2, e3) or (e3, e1) found", in a `controlDict` function object | - | - | 0 | 0.0954 | 564 |

Run 201843: pass 0 had passed every stage up to the Analyst, then the Critic sent the run back to Geometry (reason: recirculation length output missing or incorrect, domain boundaries introduce walls contrary to the requested unbounded flow, zero recirculation length implausible at Re 20); the record keeps no final Critic verdict (`critic_rounds` 1). Run 220035: according to its `casespec.json`, the expected 0.004 m3 is the planned volume and equals the outer box (2 x 1 x 0.002 m) with the 0.1 m cylinder not subtracted; box minus cylinder is 0.003984 m3, which the CAD solid of the last two attempts had, so the plan was wrong and the volume of those solids was right (the class of item 5 in `docs/design/m3-carryover-from-m2.md`).

## 2026-11-25 milestone: honest result, the D8 done rule was not met

4 of 15 runs completed and 3 of 15 passed the Scorer, all three `cavity-re100`. Passes per benchmark: `cavity-re100` 3 of 3; `cavity-re1000` 0 of 3 (1 completed, not passed); `pipe-re500`, `cylinder-re20` and `cylinder-re100` 0 of 3 each, with no completed run and therefore none scored, so the D8 done rule was not met. Failures before the Scorer say nothing about solution accuracy, and n = 3 per benchmark is an observation, not a success rate.

## 2026-11-25 observation: the Critic disagreed with the Scorer in 2 of 4 completed runs

The disagreement went once in each direction: in 194518 the Critic returned success with all checks passed while the Scorer failed the run (a false positive against reference data), and in 220416, which the Scorer passed, the final verdict was partial with the failed check `task_parameters`, because the top lid was listed as type "wall" instead of "movingWall" (in run 230217 of the same series OpenFOAM v2512 rejected `movingWall` as a patchField type). In 220416 there were two earlier send-backs. Loop 3 said that the u profile "contains negative values where the bottom wall should enforce u=0". Loop 4 said that the u centreline is essentially linear and that the v centreline shows transverse velocities of about 0.15 U, "whereas symmetry dictates values near zero". The symmetry expectation is not supported: the benchmark reference data have v between about -0.25 and +0.18. The loop 3 statement and the linearity statement concern the profiles of those passes, which were not checked.

## 2026-11-25 observation: other patterns in the run records

In 12 of the 15 runs at least one Planner attempt was rejected because `geometry.volume_m3` was not a value from a calculator call; in 234827 all three Planner attempts failed, the last one with this error. The `exit=` field printed by the loop script that drove the series is meaningless, because `$?` was read after `$(date)`.

## 2026-11-25 observation: force function object set-up failed one entry at a time

The table above gives only the last error. In three runs all three Case Setup trial runs of the failing pass stopped in, or right after the start of, a force function object, on a different problem each time: 212736 a missing `patches` entry, then a missing `magUInf`, then `Could not find rho:rho`; 201843 (pass 1) a missing `origin`, then a zero-length coordinate axis (the recorded error does not say where), then `Could not find rho:rho`; 235035 a missing `magUInf`, then a missing `origin`, then incomplete coordinate axes.

## 2026-11-25 experiment: cost of Task 19

Tracked LLM cost of the 15 runs (run records, list prices) was $1.9306 (about $1.93); the lost first attempt added $0.16. The gpt-oss-120b bill has no cached-token meter, so this figure is not shown to be an upper bound; `2026-11-26-m2-close-out.md` compares the bill with the run records. The VM ran about 4.5 h for the series and about 2 h during the incident (both approximate), about $0.94 and $0.42 at $0.208/h. The total of about $3.45 is within the $6 cap of D8.
