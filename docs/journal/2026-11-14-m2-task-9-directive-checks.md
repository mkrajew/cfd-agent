# 2026-11-14 M2 Task 9: directive check messages, explicit input paths, turn budget

## 2026-11-14 milestone: Task 9 implemented, both reviewers approve after one fix round

Task 9 of `docs/superpowers/plans/2026-11-14-m2-robustness-and-full-benchmark-set.md` makes the gate feedback to agents more directive. (1) Completeness errors from `tools/case_requirements.py` name the file and show a minimal generic skeleton. (2) Static-check failures list the files that passed ("already in order"), but never a file that any error names; trial-run and Courant failures carry no such list. (3) When the gate errors repeat exactly, the loop prefixes "This is the same problem as in the previous attempt; it was not fixed:" (composes with the Task 8 `retry_text`). (4) The Geometry, Mesh and Analyst opening messages state exact paths (the script cwd is the attempt directory; the plan is `../../casespec.json`). (5) `RunLimits.max_turns_per_attempt_by_stage` is new and `RunConfig.default()` sets `case_setup: 60`; the general limit stays 40 and the $1 cost cap is unchanged. (6) `--reasoning-effort high` sets the Planner structured-output token limit to 8192. Verified with 2127 unit tests passed, 140 skipped; Azure cost: zero.

## 2026-11-14 observation: first review found real contradictions in the feedback

`code-reviewer` and `cfd-reviewer` both returned `changes`. The blocking points were: the "passed" list contradicted trial-run, Courant and parse errors (a file reported as in order while an error concerned it); prompt wording "leave unchanged"; `nNonOrthogonalCorrectors` shown as a case value in the skeleton; the steady `fvSolution` skeleton lacking under-relaxation; a turbulence note; and a `_final_hint` contradiction. After one fix round (the passed list was restricted to static-check failures) both re-reviews returned `approve`. The repeat prefix will mostly fire for static errors, because trial-run messages contain varying solver output and rarely repeat exactly.

## 2026-11-14 observation: input path is reasoned, not verified in a sandbox

That `../../casespec.json` is readable from the script cwd was derived from reading `ops.run_script`, not tested in a real sandbox. A scripted replay on the VM (`scripts/replay_scripted_run.py`) would confirm it.

## 2026-11-14 observation: Task 8 prompt leftover and deferred minors

Task 8 said the prompts would be made mode-neutral in Task 9. A leftover remains: `prompts/case_setup.md` (around line 12) still tells the agent to copy dictionaries with `write_file`, which is wrong in `in_place` mode. Deferred minors under the lean review workflow: key validation for the per-stage turn override, `controlDict` skeletons in `case_check`, `pRefCell` in the missing-`fvSolution` skeleton, an explicit time-scheme field, and a viscous-term note for RAS.
