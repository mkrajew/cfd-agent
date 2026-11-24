# ADR-0008: One resource-reduction retry for the Planner

- **Status:** Accepted, 2026-11-24 (owner approval in chat, during Task 17 of the M2 live development series)
- **Deciders:** the owner

## Context

Design 6.8 says a run ends `failed` at stage 1 if the Planner's estimate "still exceeds the run limits (6.5) after legitimate reductions". The M1 and M2 code ended the run on the first resource-only plan-gate rejection (`exceeds resources:`), with no retry. This happened although the gate message itself tells the model to look for a legitimate reduction first: symmetry, a 2D or axisymmetric model, or a justified coarser mesh. Three live runs of two benchmarks stopped after a single plan in about 30 s each (Task 17 runs 20261123-081620, 20261123-234250 and 20261124-135320). Their estimates were 250, 354 and 132 minutes against a limit of 90. The behaviour of the agents on those cases was therefore never observed, and the wording of 6.8 was not followed.

## Decision

1. After a resource-only plan-gate rejection, the Planner gets exactly one further attempt per run, with the gate message as feedback. The attempt reuses the normal rework mechanism and stays inside `max_attempts_per_stage`.
2. If the new plan is again resource-only, or no attempt is left, the run ends `failed` with the `exceeds resources:` reason, as before.
3. If the new plan has other errors, the normal retry rules apply. No second resource retry is ever given, including in later Planner passes after a fix loop (ADR-0006).
4. The retry is recorded in `StageRecord.resource_retries`. `summary.md` says "the plan was reworked once after a resource rejection". The rejected plan stays in the record as a failed attempt.
5. `plan_gate`'s classification and messages are unchanged. The limits are unchanged.

## Alternatives considered

- **End the run on the first resource-only rejection (the previous behaviour).** Rejected: it contradicts the "after legitimate reductions" wording of design 6.8 and hides how agents behave on a case whose first plan is too large. Three live runs showed this.
- **Unlimited resource retries.** Rejected: the retries cost model calls and invite repeated shrinking until the plan passes. One retry matches what the gate message asks for and bounds the cost.

## Consequences

- The wording of design 6.8, "after legitimate reductions", is now actually followed.
- A few extra model calls are made, and only for runs that would otherwise have failed.
- A reduction must be legitimate and recorded in the plan's `assumptions`. The Critic and the Scorer still judge the result, so an illegitimate reduction (for example an unjustifiably coarse mesh) shows up there.
- The report can count the runs that needed a reduction.
- Design 6.8 gets one sentence referring to this ADR, as ADR-0005, ADR-0006 and ADR-0007 did for their clarifications.
