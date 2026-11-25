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
