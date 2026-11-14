# ADR-0005: In-place repair of a failed attempt, behind a configuration flag

- **Status:** Proposed, 2026-11-14 (awaiting the owner's approval; nothing is built before it)
- **Deciders:** the owner

## Context

Design section 6.2 says: "On failure, the error is fed back to the responsible agent for another attempt, within limits (6.5)." In M1 every attempt of a stage starts in a fresh, empty attempt directory (`RunDir.attempt_dir`). The previous attempt's files stay readable, and the model is told so (`Stage.retry_text`), but it has to write the whole stage output again. The live runs of M1 Task 23 show the cost of this: Case Setup repeatedly fixed the error of the previous attempt and dropped something else that the previous attempt had right (runs 4, 6, 7, 11, 21, 26; in run 26 three attempts lost `pRefCell`, then `turbulenceProperties`, then `fvSchemes` and `fvSolution`). In run 9 the agent repaired a case in place by itself within one attempt, which shows the need but not that it works (it used all 30 turns). See `docs/design/m2-carryover-from-m1.md`, item 1, and `docs/journal/2026-11-12-m1-task-23-first-completed-runs.md`.

## Decision

1. A new configuration field `RunConfig.repair_mode: Literal["fresh", "in_place"]`, default `"fresh"` (today's behaviour). The CLI gets `--repair-mode fresh|in_place`. The mode is recorded in `run_record.json`.
2. In `in_place` mode, after a failed gate the next attempt still gets its own directory `attempts/<stage>-<n>/` (so every attempt stays inspectable and the record keeps one entry per gate run), but the directory is **seeded with a copy of the files the agent wrote in the failed attempt** (only files matching the stage's writable scope; inputs such as the mesh are prepared as before). The retry message says that the files are already there and that only the reported problems need fixing.
3. When a later stage sends work back to an earlier one (fix loops, ADR-0006 to follow), the stage that receives it starts from its own accepted (promoted) files in `in_place` mode and from an empty directory in `fresh` mode (stage inputs such as the mesh are prepared in both modes). The mapping from promoted files to the attempt directory is per stage: Geometry its `geometry.py`, Mesh its `mesh.py`, Case Setup the dictionaries of `0/`, `constant/` (without `polyMesh`) and `system/`, the Analyst its `post.py`.
4. Limits are unchanged: at most 3 gate runs per stage, the turn limit per attempt, the cost and wall-clock caps.
5. `fresh` stays the default until the M2 experiment (Task 17, Step 1 of the M2 plan) shows on the same benchmarks and commit which mode completes more runs. Switching the default is a separate owner decision. The M3 matrix can then compare both modes.

## Alternatives considered

- **Make `in_place` the only mode.** Simpler code, but the M1 evidence for the change is six failed runs observed without a controlled comparison, not a measurement, and the comparison itself is a finding for the report.
- **Keep fresh attempts and only improve the check messages.** Planned as well (M2 Task 9), but it does not stop the model from forgetting what an earlier attempt had right.
- **One attempt directory edited in place across attempts.** Loses the record of what each attempt produced, which the failure taxonomy needs.

## Consequences

- Design section 6.2 gets one sentence: the retry may start from the failed attempt's files (`repair_mode`). Approved together with this ADR.
- A wrong file that the model never touches is carried from attempt to attempt; the gate still reports it each time.
- Disk use per run grows slightly (copied scripts and dictionaries, not meshes or results).
- Every run record states the mode, so runs of both modes are never mixed in a statistic without saying so.
