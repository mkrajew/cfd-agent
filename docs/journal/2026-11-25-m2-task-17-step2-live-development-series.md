# 2026-11-25 M2 Task 17, Step 2: live development series on the harder benchmarks (runs 7-33)

Continuation of `2026-11-22-m2-task-17-repair-mode-series.md` (Step 1, runs 1-6, cavity-re100). Step 2 ran the same pipeline on `cavity-re1000`, `pipe-re500`, `cylinder-re20` and `cylinder-re100` in repeated short series: run a series, triage the failures, merge general fixes (PRs #97-#108, then #110), refresh the image, run again. Model gpt-oss-120b, reasoning effort medium, default limits, repair mode `in_place`, VM host refreshed to the then-current commit for every series. Courant limit hits (D9): 0 in all 27 runs of Step 2 and in the final series (runs 30-33); the Step 2 runs that reached the solver before run 32 were steady simpleFoam, where the check does not apply. "Scorer" below means compared with reference data; everything marked "physical check" was not compared with reference data.

## 2026-11-22 decision: in_place repair mode for the acceptance series

After the Step 1 comparison (fresh 1 of 3 completed, in_place 1 of 3 completed, n = 3 per mode, not a rate) the owner chose `in_place` in chat on 2026-11-22. The reason on record is that both fresh failures lost files in fix-loop re-runs (the M1 pattern), while no in_place run lost files; ADR-0005 defined the flag. The cost seen in Step 1 was that in_place runs can spend the whole 2 h wall clock on repeated repairs (run 6).

## 2026-11-25 experiment: run tables, Step 2a to 2g (runs 7-33)

LLM cost is from the run records. Failed stage is the stage that ended the run; "3/3" means all three attempts of that stage failed.

**Step 2a** (commit 8164468, `cavity-re1000`)

| Run | Run id | Result | LLM $ | Cause |
|---|---|---|---|---|
| 7 | 20261122-185931 | failed mesh 3/3 | 0.0343 | Gmsh API misuse (`importShapes` unpack), then `assign_physical_groups`: degenerate surfaces matched by 2 patches |
| 8 | 20261122-190223 | failed mesh 3/3 | 0.0455 | `getBoundary` keyword args, `extrude` tuple unpack, then the same helper error (the model extruded a 3D geometry, z range -0.99..0.01) |

**Step 2b** (commit 9d524c3, PRs #96, #97 merged)

| Run | Run id | Benchmark | Result | LLM $ | Cause |
|---|---|---|---|---|---|
| 9 | 20261123-075411 | cavity-re1000 | completed; Critic success; Scorer FAIL (u error 0.0538, v error 0.0553 against threshold 0.05); Critic did not agree | 0.1013 | 200 x 200 = 40k cells; `residualControl` ended the run at iteration 3266 |
| 10 | 20261123-081414 | cylinder-re20 | failed geometry 3/3 | 0.0275 | plan volume 0.004 = outer box, body not subtracted; the geometry was correct (0.003992) |
| 11 | 20261123-081620 | cylinder-re20 | failed planner (resource refusal, no retry by design 6.8) | 0.0036 | plan asked for 1,000,000 cells, 250 min against 90; volume again the box |
| 12 | 20261123-083224 | pipe-re500 | failed geometry 3/3 | 0.0559 | plan volume = full solid of revolution (no angle fraction); wedge patches unmatched |
| 13 | 20261123-083712 | pipe-re500 | failed geometry 3/3 | 0.0392 | plan bounding box ymin -R (wedge needs y >= 0); then CadQuery revolve errors |

**Step 2c** (commit 55cb97a, PR #98 merged)

| Run | Run id | Benchmark | Result | LLM $ | Cause |
|---|---|---|---|---|---|
| 14 | 20261123-121802 | cylinder-re20 | failed planner 3/3 | 0.0059 | degenerate stub CaseSpec (title "calc", empty boundaries and outputs) repeated identically on both gate retries; the retries had no calculator |
| 15 | 20261123-121838 | pipe-re500 | failed geometry 3/3 | 0.1357 | planner chose a 3D full pipe; `.Solids().size()` (list API misuse); wall box locator did not contain the curved wall face |
| 16 | 20261123-122950 | cylinder-re20 | failed case_setup 3/3 | 0.0916 | `.Faces().size()`; transportProperties syntax; forceCoeffs `magUInf` missing; `bounded` scheme misplaced |
| 17 | 20261123-124128 | pipe-re500 | failed geometry 3/3 | 0.0461 | wedge plan consistent (PR #98 worked); `.Solids().size()`; `empty` instead of `wedge` patches; box locators missed wall and wedge faces |

**Step 2d** (commit f125b49, PRs #99, #100 merged)

| Run | Run id | Benchmark | Result | LLM $ | Cause |
|---|---|---|---|---|---|
| 18 | 20261123-152110 | cylinder-re20 | failed geometry 3/3 | 0.0383 | plan volume 0.06 = full bounding box again; geometry shifted its bounding box to force the volume |
| 19 | 20261123-152552 | pipe-re500 | failed geometry 3/3 | 0.0907 | wedge plan consistent; a face matched twice, the wall face unmatched; syntax error |
| 20 | 20261123-153404 | cylinder-re20 | failed mesh 3/3 | 0.0730 | plan and geometry correct; own search for the body surface failed; `assign_physical_groups` error; `BoundaryPatch` treated as a dict |
| 21 | 20261123-153953 | pipe-re500 | failed mesh 3/3 | 0.0601 | first pipe past geometry; the mesh stage looked for a -z face with zero z extent (2D habit) instead of the wedge-face rule in the prompt |

**Step 2e** (commit 70d9ece, PR #101 merged)

| Run | Run id | Benchmark | Result | LLM $ | Cause |
|---|---|---|---|---|---|
| 22 | 20261123-225148 | pipe-re500 | failed analyst (40 turns, no check passed) | 0.2933 | first pipe run past the solver: one steady-gate fix loop; the Analyst listed the same directories about 15 times and never wrote `post.py` |
| 23 | 20261123-232227 | cylinder-re20 | failed mesh 3/3 | 0.0417 | plan labelled 3D with a 0.002 slab thickness (a 2D slab called 3D), 374k cells; mesh timeout at 900 s, then tet volume mesh failure |
| 24 | 20261123-234250 | cylinder-re100 | failed planner (resource-only, 354 min, no retry) | 0.0035 | design 6.8, same as run 11 |
| 25 | 20261123-234311 | pipe-re500 | failed geometry 3/3 | 0.0914 | plan correct; wedge built at z 0..+2h because the rotation was applied to the sketch before the revolve |

**Step 2f** (commit 70d6687, PRs #102-#104 merged)

| Run | Run id | Benchmark | Result | LLM $ | Cause |
|---|---|---|---|---|---|
| 26 | 20261124-132425 | pipe-re500 | completed (first completed run of the harder benchmarks after run 9); Critic success; Scorer not passed; agreement no | 0.2604 | Scorer: pressure drop error 0.46 % against threshold 5 % (pass); velocity profile refused, because the profile was normalised by the ideal radius while the mesh wall is at R cos(2.5 deg), so it did not reach the wall and could not be extrapolated |
| 27 | 20261124-134918 | cylinder-re20 | failed geometry | 0.0479 | 2D plan with slab thickness (no 3D mislabel), but plan volume 0.001 against about 0.001996; geometry then changed the solid to meet the wrong volume and produced 2 solids |
| 28 | 20261124-135320 | cylinder-re100 | failed planner (resource-only, 132 min > 90) | 0.0067 | design 6.8, no retry |
| 29 | 20261124-135355 | pipe-re500 | failed geometry 3/3 | 0.0947 | wedge now rotated correctly (z about +-0.000218); in all 3 attempts the manifest boxes were shrunk inward by 1e-6 and the wall and -z wedge face went unmatched |

**Step 2g** (final series, commit 27a8123, PRs #105-#108 merged; image `sha-27a8123`; VM 3.65 h)

| Run | Run id | Benchmark | Result | LLM $ | Cause |
|---|---|---|---|---|---|
| 30 | 20261124-191508 | pipe-re500 | failed geometry 3/3 | 0.0313 | attempt 1: wrong casespec path; attempt 2: wall box shrunk by 1e-6; attempt 3: took "grow the box" literally and returned to a box around the whole solid, so every face matched "more than one patch" |
| 31 | 20261124-191832 | cylinder-re20 | failed case_setup 3/3 | 0.2267 | plan 2 attempts, geometry 1, mesh 1; the model wrapped all fvSchemes entries in a `schemes { }` dictionary in every attempt and the message "missing gradSchemes/divSchemes/laplacianSchemes" did not say why |
| 32 | 20261124-194334 | cylinder-re100 | failed (wall clock 7200 s) | 0.2148 | plan 3 attempts, geometry 2, case 3; estimated 53.3 min, but the mesh had 551,895 cells and the solver ran 5253 s for 309 steps (about 31 us per cell-step against the 5 us in the planner prompt); on 1 rank; 2 fix loops were no-ops (see below) |
| 33 | 20261124-214411 | pipe-re500 | completed after 1 fix loop (steady state); Critic gave 3 invalid replies; Scorer not passed | 0.2704 | see the result entry below |

**Cost.** LLM costs are from run records; VM time is at $0.208/h from VM start to verified deallocation (VM hours before 2g are approximate, "~").

| Step | Runs | LLM $ | VM h | VM $ | Running total |
|---|---|---|---|---|---|
| 1 (cavity-re100) | 1-6 | 1.2495 | ~4.4 | ~0.92 | ~2.16 |
| 2a | 7-8 | 0.0798 | ~0.37 | ~0.08 | ~2.33 |
| 2b | 9-13 | 0.2275 | ~1.0 | ~0.21 | ~2.77 |
| 2c | 14-17 | 0.2793 | ~1.07 | ~0.22 | ~3.27 |
| 2d | 18-21 | 0.2621 | ~0.6 | ~0.12 | ~3.66 |
| 2e | 22-25 | 0.4299 | ~1.15 | ~0.24 | ~4.33 |
| 2f | 26-29 | 0.4097 | ~0.75 | ~0.16 | ~4.90 |
| 2g | 30-33 | 0.7432 | 3.65 | ~0.76 | ~6.40 |

The total of about $6.40 is within the $7 cap of decision D7. At the $3.5 checkpoint (after Step 2d) the owner was told. On 2026-11-24 the owner said that, if the cap would be exceeded, the last series should still finish (a modest overspend is allowed); the final series ended below the cap. The VM was deallocated and the state verified after each series (in one series the host idle timer had done it first).

## 2026-11-25 observation: general fixes and what they did in later runs

All fixes are general (no case content); each went through `code-reviewer` (and `cfd-reviewer` where rule 3 applies). Letters are the labels of the series notes.

| PR | Fix | Found in | Observed effect in later runs |
|---|---|---|---|
| #97 | steady-state gate guidance reaches Case Setup (and no loosening) | Step 1 runs 2, 6 | not shown to help: in run 33 Case Setup loosened U tolerance anyway (below) |
| #98 | planner fluid volume and wedge bounding-box consistency checks in the plan gate | runs 10-13 | pipe plans consistent from run 17 onwards; cylinder volume still wrong in runs 18 and 27 (a prompt rule alone was ignored) |
| #99 | planner gate retries rework the plan with the calculator | run 14 | no stub plan recorded in later runs |
| #100 | geometry prompt and note: `Solids()`/`Faces()` return lists | runs 15-17 | no `.size()` misuse in runs 18-21 |
| #101 | mesh `base_face` helper for one-cell 2D and wedge meshes | run 21 | worked at the first mesh attempt in run 22 |
| #102 (A) | loop notes repeated identical tool calls, warns before the turn limit | run 22 | run 26: the Analyst wrote `post.py`; in run 31 the note appeared and the model only altered the text of the same wrong file |
| #103 (B) | plan gate rejects 3D plans with slab thickness or wedge angle | run 23 | run 27: no 3D mislabel |
| #104 (C) | rotate the wedge after the revolve; one-sided wedge hint | run 25 | run 29: wedge rotated correctly |
| #105 (D) | unmatched-surface error names the nearest box locator and shortfall | runs 26, 29 | the hint appeared in run 30 attempt 2; the model then over-grew the box in attempt 3 and every face matched several patches |
| #106 (E) | analyst divides profile positions by the mesh boundary position | run 26 | not followed in run 33: the Analyst used the ideal radius, the last profile point was dropped (0.994975 = 198/199) |
| #107 (F) | plan `volume_m3` must come from a calculator call of the planning run | runs 18, 27 | fired in run 31 (191832): the typed value 0.0499215 was refused, the second plan used the calculator; the typed value was in fact correct, so one cheap attempt was spent |
| #108 (G, ADR-0008) | one resource-reduction retry for the Planner | runs 11, 24, 28 (each ended after about 30 s with 250, 354 and 132 min against 90) | did not fire in run 32 (194334): the first rejection mixed a volume error with the resource error, so it was not resource-only; `resource_retries` was 0 in every run of the series |
| #110 (open) | five small fixes: Solver Runner diagnosis survives an invalid reply (plus a no-change check for fix passes), manifest overlap message names the locators, nested fvSchemes message, Critic retry names empty lists, geometry script gets the casespec path | triage of runs 30-33 | not yet run live; 2789 tests passed, `code-reviewer` approved after one `changes` round (a host-side FIFO hang) |

PR #109 holds ADR-0009 (below), not a code fix. PR #96 is the Step 1 journal.

## 2026-11-24 decision: ADR-0008 and ADR-0009

ADR-0008: after a resource-only plan-gate rejection the Planner gets exactly one further attempt, because design 6.8 says "after legitimate reductions" and three runs had ended after one plan. ADR-0009: Claude merges pull requests once CI is green and the reviewers approved, related fixes from one series may share one PR, and one or two live runs may use a stronger model to separate model errors from pipeline errors (not a change of the default). Reason: the series spent most of its elapsed time waiting for merges. No stronger-model run is recorded in this step.

## 2026-11-25 observation: failures that the general fixes did not reach

Run 32 (cylinder Re 100): the plan estimated 53.3 min (1.6e9 cell-steps at 5 us, divided by 2.5 for 4 ranks) but the Solver Runner ran 1 rank and never computed cells per rank. The only mesh gate is a maximum of 2e6 cells, so 551,895 cells passed. The run-wide wall clock (7200 s) killed the solver after 5253 s, 309 steps; the full run would need roughly 75 h. The Solver Runner's correct diagnosis (lower endTime or raise deltaT) failed schema validation twice (markdown instead of JSON), so the fix request fell back to the bare gate text; Case Setup made no file change in passes 1 and 2 and the passes were accepted unchanged. PR #110 addresses the lost diagnosis and the no-change pass (in in_place mode only), not the missing runtime projection. Run 31 shows a loop in which the checker was right and the model wrong: in attempt 3 it flipped 17 times between flat and wrapped schemes files and ended on the wrapped one, telling itself the wrapper was "required by the validator".

## 2026-11-25 milestone: honest result of Task 17 Step 2

The final series gave one completed run (run 33, pipe-re500) that the Scorer did not pass, so none of the harder benchmarks (cavity-re1000, pipe-re500, cylinder-re20, cylinder-re100) passed the Scorer with Critic agreement in Step 2; cavity-re100 had passed in Step 1 (2 of 6 runs, Scorer pass and Critic agreement, see the Step 1 entry). In Step 2, three runs completed (9, 26, 33) and 24 failed, most of them in geometry, mesh or the planner.

Run 33, compared with reference data (Scorer): pressure drop 2.73 Pa against about 1.60 Pa, 71 % off; the velocity profile was refused because it ends at 0.994975 and cannot be extrapolated to the wall. Physical checks only (not compared with reference data): the centreline u/U at the sampled times was 1.04, 1.16 and 1.295 (the analytic Poiseuille value is 2.0), the pressure drop was 11.5, 4.1 and 2.73 Pa at the same three times and still falling, the bulk velocity of the final profile was 0.04999 (mass conserved), and nu, the unit conversion, the sample location and the wedge angle were correct. So the solution had not converged; the unit conversion, the sample location and the wedge angle were not the cause. The run ended at iteration 2901 because `residualControl` stopped it after Case Setup loosened the U tolerance to 1e-4, although the gate text allowed that only when the monitored quantities had stopped changing; when `residualControl` ends a run the gate skips the monitored-change check, and sampled sets are not counted as monitors. The mesh (500 radial x 200 axial cells, aspect ratio about 250) differed from the plan (200 radial x 500 axial); the triage suspects that this aspect ratio makes SIMPLE converge slowly (a hypothesis, not checked). The Analyst wrote prose before `post.py` ran ("centreline value close to 1.0", "classic parabolic shape", "same order ... confirming plausibility") and its "change 5.2e-5" compared times 2900 and 2901, because the prompt says "last two write times". The Critic returned an empty summary and then three non-success verdicts with empty `checks` and `reasons`; it read neither the solver log nor the quantities file. Run 26 (pipe, earlier series) passed the pressure-drop check (0.46 % against 5 %, Scorer) and failed only on the velocity profile, which the later Analyst fix (#106) was meant to cure; in run 33 the Analyst still did not follow it.

The M3 carry-over list was written from this triage: `docs/design/m3-carryover-from-m2.md`.
