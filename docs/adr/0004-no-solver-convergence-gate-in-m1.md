# ADR-0004: No Solver Runner convergence gate in M1

- **Status:** Accepted, 2026-11-06
- **Deciders:** the owner

## Context

Design section 6.2, table row 5 (Solver Runner), defines the gate as: "Convergence (residual targets for steady; statistically periodic forces for unsteady); no divergence. On error: agent reads the log, diagnoses, proposes a concrete fix; orchestrator routes to stage 4 or 3".

In M1 the Solver Runner agent cannot edit the case: it writes only logs and `solver_run` artefacts. A failed convergence gate could therefore only trigger a retry of the same run, which cannot change the outcome. The meaningful remedy is routing back to Case Setup (adjust schemes, relaxation, mesh), which belongs to the routing and rework loops of M2.

## Decision

Ruling R44 (Task 18): in M1 the Solver Runner gate is only that the last `run_solver` call finished OK (`SolverRun.ok`). Steady non-convergence (`residualControl` not met within `endTime`) is not a gate failure. After a failed solver run the stage does not retry either (R45).

Mitigations in M1:

- The tool summary reports steady non-convergence and warnings.
- `logs/solver_run.json` records first and last initial residuals, `stopped_by_residual_control`, `steady_expected`, `time_dependence` and warnings.
- The Analyst (Task 19) can read this record, and its gate requires `analysis.md` to discuss convergence honestly.
- The orchestrator `summary.md` (Task 20) must state whether `residualControl` was met.
- The Scorer judges numerical results blind, so a non-converged result can score as fail but is not hidden.

## Consequences

- An unconverged steady run reaches the Analyst and the Scorer and is reported as such.
- Wasted LLM and compute cost is bounded by the wall-clock limit and the per-stage turn caps.
- The risk of a misleading "pass" is limited because the Scorer compares with reference data.
- This is a deviation from design 6.2 row 5, approved explicitly by the owner in chat on 2026-11-06.

## Related decisions (same approval)

- R38: the Case Setup trial-run Courant limit is 10, to be revisited after the Task 23 live runs.
- Solver warning thresholds (Co > 10, maxIter 1000, more than 10 bounding messages) stay warnings only, not gate failures.

## Revisit

M2 adds a convergence gate with routing back to Case Setup, which restores design 6.2 row 5. The design document is then amended, or this ADR is superseded.
