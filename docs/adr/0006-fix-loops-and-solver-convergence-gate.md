# ADR-0006: Cross-stage fix loops and the Solver Runner convergence gate

- **Status:** Accepted, 2026-11-14 (owner approval in chat, together with the M2 plan). The gate thresholds are provisional, see Decision 6.
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
8. **Case Setup completeness.** A steady case must set `residualControl` (SIMPLE or PIMPLE dictionary) or, when solved with a transient solver, a function object that monitors at least one requested quantity.
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
