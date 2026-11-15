# ADR-0006: Cross-stage fix loops and the Solver Runner convergence gate

- **Status:** Accepted, 2026-11-14 (owner approval in chat, together with the M2 plan). The gate thresholds were provisional; `cfd-reviewer` confirmed them on 2026-11-15, see Decision 6 and the Amendments.
- **Deciders:** the owner
- **Supersedes:** ADR-0004

## Context

Design section 6.2, row 5, requires a convergence gate on the Solver Runner and routing of a failed run to an earlier stage; section 6.5 sets at most 10 fix loops per run. ADR-0004 removed the gate in M1 because the Solver Runner cannot edit the case, so a retry of the same run could not change the outcome. The M1 live runs show the price: runs 15, 20, 23 and 27 were not ended by a convergence criterion, so a steady case could reach the Analyst without having converged. M2 adds the missing remedy, a route back to the stage that can change the outcome. Plan: `docs/superpowers/plans/2026-11-14-m2-robustness-and-full-benchmark-set.md`, Tasks 11 and 12; evidence: `docs/design/m2-carryover-from-m1.md`, items 4, 5 and 10.

## Decision

1. **Stage order and targets.** `STAGE_ORDER = (planner, geometry, mesh, case_setup, solver_runner, analyst, critic)`. `ROUTE_TARGETS = {planner, geometry, mesh, case_setup, analyst}`. The Solver Runner edits nothing, so a fix aimed at it goes to `case_setup`.
2. **Route request.** `RouteRequest(source, target, origin: "gate" | "critic", reason, instruction)` is frozen and valid only if `target` is in `ROUTE_TARGETS` and precedes `source` in `STAGE_ORDER`. `reason` and `instruction` are capped at 1000 characters and redacted like record errors.
3. **Numbered passes.** A route back starts a new pass for the target stage and for every later stage. Attempt directories are `attempts/<stage>-p<n>-<attempt>/` (pass 0 keeps the M1 name `attempts/<stage>-<attempt>/`); no earlier directory is deleted or overwritten. Transcript lines carry the pass number.
4. **What happens on a route back.** In `in_place` mode (ADR-0005) the target is first seeded from its own canonical files; only then `move_downstream_aside` moves the canonical outputs of every stage after the target to `superseded/pass-<n>/`. For a `case_setup` target `case/constant/polyMesh` stays in place. A routed Case Setup copies only `constant/polyMesh` from the canonical case, never time directories, `postProcessing/` or logs, and its carried dictionaries are re-checked against the new patch list. Every stage after the target runs again.
5. **Budget.** `max_fix_loops = 10` run-wide (design 6.5), in addition to 3 attempts per stage and pass. When a route is requested with no budget left the run ends `failed` ("fix-loop limit of 10 reached"). Each loop is recorded as `FixLoopRecord(n, source, target, origin, reason, moved)`.
6. **Convergence gate (design 6.2 row 5).** The gate passes only if the solver run was OK and:
   - for a steady case (`time_dependence == "steady"`, also when solved with a transient solver): steadiness holds, meaning `residualControl` stopped the run, or every monitored quantity changes by less than `steady_rel_change` over the last `steady_window` fraction of the run and the final initial residuals are below `final_residual_max`;
   - when `CaseSpec.periodic_outputs` is true (set by the Planner; the requested outputs are statistics of a periodic regime): periodicity holds on a force-coefficient history, meaning at least `min_periods` periods after the transient and the mean and amplitude of the last two halves differ by less than `periodic_rel_change`;
   - for an aperiodic transient case: no periodicity check.

   **Provisional thresholds** (`ConvergenceThresholds`): `final_residual_max = 1e-4`, `steady_window = 0.1`, `steady_rel_change = 1e-3`, `min_periods = 10`, `periodic_rel_change = 0.02`. `cfd-reviewer` confirms or changes them in Task 12, and every merged calibration of a hand-written solution must pass the gate. Any change is recorded in this ADR with its reason; a threshold is never fitted by changing a hand-written case.
7. **Diagnosis and routing.** After a gate failure of a solver run that happened (divergence included) the stage does not retry in-stage. The diagnosis turn is one structured call with `SolverDiagnosis(diagnosis, target: "case_setup" | "mesh", instruction)` and no tools; an invalid or missing reply gives `target = "case_setup"` with the gate errors as the instruction. `Stage.route_after_failure` turns it into a `RouteRequest(origin="gate")`. The failure "the solver was not run" is still retried in-stage.
8. **Case Setup completeness** (amended, see Amendments: `residualControl` counts only in the `SIMPLE` dictionary). A steady case must set `residualControl` (SIMPLE or PIMPLE dictionary) or, when solved with a transient solver, a function object that monitors at least one requested quantity.
9. ADR-0004 is superseded; design 6.2 and 6.5 refer to this ADR.

## Alternatives considered

- **No gate; leave convergence to the Critic.** Cheapest, but an unconverged run would pass through the Analyst and cost a Critic round, and the Critic reads evidence rather than the solver log's criteria. ADR-0004's M1 evidence shows this lets unconverged steady runs through.
- **Always route to `case_setup`, no diagnosis.** Simpler, but a mesh-related failure then costs several loops on a stage that cannot fix it.
- **Per-stage fix budgets instead of one run-wide budget.** More tunable, but design 6.5 specifies a run-wide limit and one number is easier to report.
- **Delete the target's old attempt directories on a route back.** Loses the record the failure taxonomy needs; numbered passes keep it.

## Consequences

- The Solver Runner gate fails more runs than in M1; those runs now cost fix loops instead of being scored as unconverged.
- A route back can carry stale files if the seeding is wrong; Task 11 tests cover this (`test_routed_case_setup_gets_mesh_only`, `test_case_setup_seed_after_mesh_change_is_rechecked`).
- A periodicity check on a short record is not a pass: a run that cannot produce `min_periods` periods inside the wall-clock limit fails the gate, which is a finding if it happens on the shedding benchmark.
- The thresholds are a research choice that the report must state, with the evidence from the calibrations.
- Design 6.2 gets a sentence referring to this ADR.

## Amendments

### 2026-11-15, Task 12: thresholds confirmed, three rules made explicit (proposed; the owner confirms with the pull request)

**Thresholds.** `cfd-reviewer` confirmed all five provisional values on the calibration evidence of Task 12 and changed none: `final_residual_max = 1e-4`, `steady_window = 0.1`, `steady_rel_change = 1e-3`, `min_periods = 10`, `periodic_rel_change = 0.02`. The evidence (hand-written solutions run through the gate in the image, OpenFOAM v2512): the steady-monitor branch separates a developing record from a settled one (the hand-written cavity with one added `probes` function object, a scratch variant that is not in git and not a hand-written fixture: largest relative change 1.8e-3 at t = 15 s, 1.9e-4 at 20 s, 8.6e-8 at 40 s; the limit is 1e-3); a steady cylinder history changes by at most 2.6e-5 over the last tenth, a margin of 38; the shedding cylinder has 24.6 periods in the developed half with mean and amplitude changes of 3e-6 and 4e-6 (limit 2e-2), and records cut at 8, 10 and 12 s fail, from 13 to 14 s on they pass. The remaining error of a force coefficient at the pass boundary is about the measured change (ratio 0.5 to 2). **All five calibrations are laminar**; no turbulent steady case, no aperiodic transient and no noisy periodic regime has been calibrated, so the values are validated for laminar flow only.

**Rules made explicit** (decision 6; none changes a value):

1. The window of the steadiness check is the last `steady_window` fraction of the run's span (first and last time of the solver log), so a monitor that stopped writing early cannot look steady. A monitored quantity is a column of a function-object table with a `# Time` header, other than the residual histories (`solverInfo`, `residuals`); the change of a column is its peak-to-peak range over the window divided by its own largest magnitude in the window, but by not less than a thousandth of the largest magnitude in its table.
2. The final initial residual of the component normal to the wedge planes (`Uz`) of an axisymmetric case is not judged: it is round-off noise normalised by itself and stays near 1e-2 at convergence (measured in the hand-written pipe case, where the other components are below 1e-6).
3. The periodic regime is judged on the second half of the record (the first half is the transient), on the force-coefficient column with the largest swing, and only when that history is written at least 20 times per period of its dominant oscillation: a coarser history aliases or the peak is noise. The guard does not close the hole: white noise written at every step still passes in about 1 % of random trials (12 to 16 % without the guard) and lightly coloured noise in up to about 8 %; a sine with 5 % white noise fails in a third of the trials, so the 2 % limit is for laminar, clean periodic regimes.

**Decision 8 amended.** `residualControl` counts as a stopping criterion only in the `SIMPLE` dictionary. In `PIMPLE` it ends only the outer loop of a time step, so the run goes on to `endTime` and the gate would find no evidence of steadiness (the log parser knows only the SIMPLE line; checked on v2512 by `cfd-reviewer`: a `PIMPLE` `residualControl` with residuals at 1e-14 leaves `pimpleFoam` running to `endTime`, and the scalar form `p 1e-3;` is a FOAM FATAL ERROR, the entry must be a dictionary). A steady case solved with a transient solver needs a function object that monitors a requested quantity.

**Open for the owner, not changed here:** a run ended by `residualControl` passes whatever the tolerances are (the option is to require the monitored quantities to be steady as well; all five calibrations would still pass); an optional spectral-concentration guard for the periodicity check (the share of the variance in the dominant peak, about 0.2 would reject noise) was not added; the periodicity check judges one force-coefficient column and compares no other mean (the drag-coefficient mean is the benchmark statistic); a periodic flow without a body (no `Cd` or `Cl`) can never pass; the diagnosis may route only to Case Setup or Mesh, so a steady plan on a physically unsteady flow spends fix loops where the cause cannot be fixed.
