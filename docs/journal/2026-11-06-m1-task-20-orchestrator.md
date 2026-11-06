# 2026-11-06 M1 execution, session 5: Orchestrator (Task 20)

## 2026-11-06 milestone: Task 20 orchestrator and run_task core API implemented

`orchestrator.run_task` runs the Planner, Geometry, Mesh, Case Setup, Solver Runner and Analyst in order from a `Pipeline` list of stage factories. It maintains a `status.json` heartbeat, an active-run marker and `run.log` events, and uploads the run in a `finally` block. The Scorer runs after the Analyst gate: `validate_quantities` runs before the score, and the score never causes a retry. Verified by unit tests with fake stages and by a scripted full pipeline in the image; both the scripted pipeline and a pipeline with one bad geometry attempt pass the Scorer path. Tests: 1565 passed on the host, 1687 unit tests in the image, 3 scripted integration tests passed. The scripted Planner conversation is handwritten because no recorded real run exists yet (R16). Azure cost: 0.

## 2026-11-06 decision: failure handling and audit trail of the orchestrator

Abnormal exits (`BaseException`) default to `failed` and are never reported as `completed`. A disk-growth watchdog (20 GiB cap, 10 s sampling) kills executors repeatedly, and the sandbox client refuses later calls once the cap is crossed. `summary.md` states, from `logs/solver_run.json`, whether `residualControl` was met, because the Analyst gate only keyword-matches "converg" (consequence of R44, ADR 0004). `knowledge_reads` is recorded in the run record as an audit of blind evaluation.

## 2026-11-06 observation: first review found three Important defects in failure paths

The first code review found three Important defects: aborts recorded as completed, a one-shot watchdog that missed calls made after the cap was crossed, and an unguarded final record write. All were fixed in one round and the re-review approved. Failure paths of the orchestrator are where a silently wrong status would corrupt results, so they are reviewed as closely as the success path.

## 2026-11-06 obstacle: runaway test loop and verification limits

An unbounded loop in a new test hung and left two pytest processes on the laptop that the agent could not kill; the owner was told. Limitations to carry into the report: the watchdog was verified at unit level only, not against a real container, and the steady-run residual-control sentence in `summary.md` is unit-tested only because the scripted plan is transient.
