# M3 carry-over from M2

Source: the M2 Task 17 live development series (runs 7-33, 2026-11-22 to 2026-11-25), recorded in `docs/journal/2026-11-25-m2-task-17-step2-live-development-series.md`, and the triage of its final series (runs `20261124-191508`, `-191832`, `-194334`, `-214411`, image `sha-27a8123`). This file lists what Task 17 left open as input for the M3 plan. Nothing here is decided except where an item says so: items are design decisions for the owner, and an ADR is needed where the design document changes, before anything is built. Sizes are rough (S small, M medium).

## Design decisions for the owner

### 1. Re-estimate the runtime after meshing (M)

Evidence: in run 194334 the plan estimated 53.3 min for 1e5 cells, the Mesh stage built 551,895 cells, and the solver needed about 31 us per cell-step against the 5 us of the planner prompt, so the plan was about 85 times too optimistic (full run roughly 75 h), and the only mesh gate is a maximum of 2e6 cells. Proposal: after meshing, recompute the estimate from the real cell count and fail early when it exceeds `max_runtime_minutes`; correct the constant (observed about 31 us); make `runtime_minutes` mean one thing (rank count stated); make the Solver Runner compute cells per rank (the run used 1 rank for 551,895 cells without computing it).

### 2. Project the solver runtime from the trial run and the step rate (M)

Evidence: the same run lost the run-wide wall clock (7200 s) after 5253 s and 309 steps without any message about the projected total. Proposal: from the trial run and the live step rate, project the total and stop early with "N of M steps in T s, projected H h", naming the levers (fewer steps, larger time step within the Courant limit, a GAMG solver, a coarser mesh); state in the solver message that the run was killed by the time budget.

### 3. Convergence drift check also when `residualControl` ended the run (M)

Evidence: in run 214411 `residualControl` stopped the run at iteration 2901 after Case Setup loosened the U tolerance to 1e-4, the gate skipped the monitored-change check, and the pressure drop (2.73 Pa and still falling) was 71 % above the reference when the Scorer ran. Proposal: use sampled sets at successive write times as monitors (for example under 1 % change over the last 10 % of the iterations) even when `residualControl` ended the run; drop the gate advice to loosen tolerances; have the Analyst compare write times at least 100 iterations apart (the prompt says "last two write times", which compared 2900 with 2901).

### 4. A number-based Analyst (M)

Evidence: in run 214411 the Analyst wrote qualitative claims ("classic parabolic shape", "same order ... confirming plausibility") before `post.py` ran, and used the ideal radius, so the last profile point was dropped (0.994975), although the prompt says to normalise by the mesh wall. Proposal: no qualitative claim before the numbers are printed and compared; reject a profile that does not reach the mesh wall.

### 5. A route from Geometry back to the Planner (design decision, ADR)

Evidence: a wrong plan `volume_m3` (run 20261124-134918: 0.001 against about 0.001996; runs 10 and 18 had the bounding-box volume) cannot be corrected by Geometry, which then altered the solid to match the wrong volume. This was deferred during Task 17; the plan-volume gate of PR #107 reduces the case but does not remove it. It changes the routing table of design 6.2.

### 6. Deferred minors of PR #110 (S)

Evidence: the review of PR #110 (opened at the time of writing) left two known limits: the no-change check for fix-loop passes is off in `fresh` repair mode (no baseline), and a `.md` note written into the case defeats it. Some test assertions could also be tighter.

## Also seen in the same series (not decided)

- The Planner's resource retry (ADR-0008) did not fire in run 194334, because the rejection mixed a volume error with the resource error; consider whether mixed rejections should also get the retry.
- Each item above rests on one run (n = 1); they show a failure class, not a rate.

## From the stronger-model comparison run (2026-11-25)

### 7. Record cached tokens and price them at the cached rate (S)

Evidence: in run 20261125-111308 (gpt-5.4) the tracker counted all input at the full price and the client records no cached tokens, so the tracked $6.0034 is an upper bound, and the cost cap may have fired before the real bill reached the limit. Proposal: read the cached-token count from the response usage, store it in the run record, and price it at the cached rate.

### 8. Reduce context growth in Case Setup and Analyst (M)

Evidence: in the same run Case Setup used 656k input tokens (about $2.17) and the Analyst 841k in 19 calls (about $2.79) without writing its quantities, because each turn re-sends the whole conversation. Proposal: summarise or truncate old tool results, and limit what a tool result returns.

### 9. A commercial-model arm in the M3 campaign (decided by the owner: yes)

Evidence: in one run (image sha-901dcf8) gpt-5.4 reached the Solver Runner on pipe-re500 with no retries, and its solution passed the Scorer when evaluated outside the pipeline (pressure drop error 0.58 %, profile error 0.12 %); its Analyst still stopped on the cost cap. gpt-oss-120b completed two pipe runs on older images: one passed the pressure-drop check and failed the profile for a post-processing reason, the other failed at 71 %. One run, a different image: not a controlled comparison. Proposal: decide whether a commercial-model arm belongs in the campaign; one run cost about $6 tracked, so the arm needs its own budget line.

Decision (owner, in chat, 2026-11-25): the M3 campaign includes variants with commercial models beside gpt-oss-120b. Which models, how many runs per arm and the budget line are left to the M3 plan and the owner's approval under CLAUDE.md rule 4. Items 7 and 8 would lower the cost of such an arm.

### 10. Responses API support (S)

Evidence: gpt-6.2-sol accepts function tools only through the Responses API or without reasoning, so it could not be used. Proposal: add Responses API support to `llm/` if newer models are needed.

## M3 order of work (decided by the owner, 2026-11-26)

Decision (owner, in chat, 2026-11-26; given in Polish, recorded in English in `docs/journal/2026-11-26-m2-close-out.md`): M2 is closed now as a partial result and the project moves to M3, in this order:

1. M3 starts with the fixes there are to implement.
2. Then each of the five development benchmarks is run **once** on the stronger model, gpt-5.4.
3. Only after that do the further tests follow.

Which items of this file make up step 1, the per-run cost cap and the budget line of step 2 are not decided; they belong to the M3 plan, and spending is subject to CLAUDE.md rule 4. One run per benchmark is a first observation, not a success rate.

**Task 7 of the M2 plan (generality test set content, D5) was not done in M2 and moves to M3.** The M2 plan places it at the start of M3, before the freeze. The rules stay: the owner writes the task texts, and the set is never run by the pipeline before the frozen M3 release (design section 9.1).

## From the Task 19 acceptance series

Source: 15 runs on one frozen build (image `sha-901dcf8`, gpt-oss-120b, medium effort, `in_place`), `docs/journal/2026-11-25-m2-task-19-acceptance-series.md`. Run ids start with `20261125-`. The M2 done rule was not met. The items are failure classes to fix with general rules; counts are from one series and are not rates. Nothing here is decided.

### 11. The boundary manifest does not cover all surfaces of a solid (S)

Evidence: runs 201441 and 212511 ended in Geometry after three attempts. In one a surface was matched by no patch (a box locator slightly smaller than the surface it should contain); in the other, surfaces were matched by more than one patch (a box locator that also contained neighbouring faces). The same class was seen in Task 17. Proposal: make a manifest that covers every surface exactly once easier to write than hand-typed box coordinates, for example by letting the script pick faces and deriving the locators from them.

### 12. Force function object set-up fails in the trial run, including "Could not find rho" in an incompressible case (M)

Evidence: runs 201843 (in a pass after a fix loop), 212736 and 235035 ended in Case Setup. The three trial runs of the failing pass each stopped on a different problem, one at a time; eight of these nine trial runs stopped in or right after the start of a force function object, and for the ninth (a zero-length coordinate axis) the recorded error does not say where. The problems were missing entries (patch list, reference velocity, origin), inconsistent coordinate axes and, as the last error of 201843 and 212736, `Could not find rho:rho in database`. A missing reference velocity was also seen in Task 17. Hypothesis for the density message, not checked: the function object looks for a density field that an incompressible solver does not have. Proposal: confirm that cause; state in a general note what a force function object needs, with an incompressible solver in particular; and let the completeness check report all missing or inconsistent function-object entries at once, before the trial run, with a directive message.

### 13. The Planner suggests a solver that the pipeline does not run (S)

Evidence: in run 204837 the plan gate accepted a solver that the pipeline does not run, and the run ended at the start of Case Setup as an `internal_error`, with no attempt made. Proposal: the plan gate rejects such a solver and names the supported ones, so that it costs the Planner an attempt instead of ending the run.

### 14. An invented boundary-condition (patchField) type (S)

Evidence: run 230217 ended in Case Setup; the third trial run stopped on a patchField type that OpenFOAM v2512 does not have. Proposal: put the valid types that the solver message lists into the retry message, or check type names before the trial run; add a general note that a patch name or a role in the plan is not a patchField type.

### 15. `none` used as a discretisation scheme (S)

Evidence: run 231427 ended in Case Setup on `Unknown discretisation type none`, in a `snGradSchemes` entry. Proposal: a completeness check that rejects `none` where a scheme is required and names a valid generic entry.

### 16. The Analyst summary does not state convergence (S)

Evidence: run 210425 failed in the Analyst; all three attempts were rejected with the same message, that `analysis.md` does not mention convergence. Proposal: find out why the message was not acted on; give `analysis.md` a required convergence section with a fixed heading, or let the pipeline supply the convergence numbers to it. See also item 4.

### 17. The Critic disagrees with the Scorer (M)

Evidence: in 2 of 4 completed runs. In run 194518 the Critic reported success and the Scorer failed the results. In run 220416 the Critic ended on "partial" over a boundary type listed in the boundary manifest, and the Scorer passed the results; one of its earlier send-backs relied on a symmetry expectation that the reference data do not support (its other physical objections concern the profiles of earlier passes, which were not checked). Proposal: measure the Critic as a result of its own (agreement in both directions); require each failed check to cite a number or a file entry; look at what the Critic can know about OpenFOAM patch and boundary-condition types; consider independent checks it can compute instead of recalled expectations (under the generality rule). The Critic stays blind to reference data.

### 18. The series loop script reports a meaningless exit code (S)

Evidence: the `exit=` field of the loop script that drove the series is meaningless, because `$?` was read after `$(date)`. The script is not in the repository. Proposal: store `$?` in a variable directly after the run command; keep the series loop as a tested script in the repository.

### Also seen in the acceptance series (not decided)

- In 12 of 15 runs at least one Planner attempt was rejected because `geometry.volume_m3` was not a value returned by a calculator call; in run 234827 all three attempts failed and the last error was this one.
- The calculator rule does not make the volume right: in run 220035 the plan held the volume of the outer box without the body removed from it, and Geometry then failed on the mismatch although the solids of its last two attempts had the right volume (item 5).
- After the disk of the VM filled up in the lost first attempt, two run records stayed in the state `running`.

## From the M2 spend report

Source: `docs/journal/2026-11-26-m2-close-out.md`.

- For item 7: Cost Management billed $2.63 for all gpt-5.4 usage (the comparison run and its smoke calls), while the record of the run alone tracked $6.00. The run was therefore stopped by its $6 cap at less than half of it in billed terms. Without cached-token pricing, a cap on a model with cached-input discounts stops runs early.
- Blob write operations cost $1.71 of the $27.54. Four of 84 runs hold about 87 % of the 325,815 blobs; that they caused most of the write cost is an inference. Not examined: which files these are and whether they all need to be uploaded one by one.
