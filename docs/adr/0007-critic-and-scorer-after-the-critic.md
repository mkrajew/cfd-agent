# ADR-0007: Critic, the Scorer after the Critic, and the run status

- **Status:** Accepted, 2026-11-14 (owner approval in chat, together with the M2 plan)
- **Deciders:** the owner

## Context

Design 6.2 rows 7 and 8 define the Critic (verdict `success` / `partial` / `failed`, may send the run back to one named stage, bounded) and the Scorer (deterministic, after the Critic's verdict is final, never triggers a retry). Several points were left open: what the Critic may read, how many rounds it gets, what happens when the fix-loop budget is used up, whether the Scorer scores runs the Critic did not accept, and whether design 6.5's "ends as `failed` or `partial`" means a new run status. In M1 no Critic existed and `critic_verdict` was a `None` placeholder. Plan: `docs/superpowers/plans/2026-11-14-m2-robustness-and-full-benchmark-set.md`, Tasks 14 and 15. The Critic accuracy metric of design 9.2 needs the Critic's judgement and the Scorer's result to stay independent.

## Decision

1. **The Critic** is a seventh agent (`critic` in `AGENT_NAMES`) with a fresh context and read-only tools: `read_file`, `list_dir`, `calculator`, at most 12 turns, then one structured call returning `CriticVerdict`.
2. **Reads:** `task.md`, `casespec.json`, `boundary_manifest.json`, the `mesh/` reports, `logs/solver_run.json` and the solver log tail, `analysis.md`, `results/quantities.json`. **Never reads:** `results/comparison.json`, `superseded/`, `benchmarks/`, other stages' transcripts. Blind evaluation is preserved; a transcript scan test checks that no reference value reaches the Critic.
3. **Required checks**, general and named in the prompt: requested outputs present with the requested names and normalisation; parameters (Reynolds number, fluid, dimensions, boundary conditions) match the task text and not only the CaseSpec; convergence or periodicity evidence; conservation and plausibility.
4. **Record types** (in `record.py`, no maps or tuples; text capped and redacted like record errors): `CriticCheck(name, passed, evidence)`; `CriticVerdict(verdict: "success" | "partial" | "failed", checks, reasons, send_back_to: "planner" | "geometry" | "mesh" | "case_setup" | "analyst" | None, instruction | None)`; `RunRecord.critic_verdict` and `RunRecord.critic_rounds`, with defaults so M1 records still load.
5. **Reply guard.** A `success` with a failed check, or a `send_back_to` without an `instruction`, is invalid and costs an attempt; at most 3 invalid replies.
6. **Rounds.** `max_critic_rounds = 2`. A send-back is followed (as a `RouteRequest` with `origin="critic"`, ADR-0006) only while rounds are left and the fix-loop budget is left; each followed send-back counts as one fix loop. Otherwise the send-back is recorded in the verdict and the verdict is final; the run does not end `failed` for that reason.
7. **Critic failure** (3 invalid replies, or a cost or wall-clock stop inside the Critic) leaves `critic_verdict = None` and the run continues to the Scorer.
8. **Run status.** No new `partial` run status. `status` stays the pipeline status (what the orchestrator did); the Critic verdict (`success` / `partial` / `failed`) is reported beside it in the run record and in `summary.md`. The M1 line "No Critic reviewed this run" appears only when `critic_verdict` is `None`.
9. **The Scorer** runs once, after the Critic step, for benchmark runs only, when the Analyst passed in the final pass, also when the verdict is `partial`, `failed` or `None`. It scores only the final pass (quantities of an earlier pass were moved aside, ADR-0006) and never starts a fix loop. `critic_agreement` is `True` when (`verdict == "success"`) equals (`comparison.passed`), and `None` when either is missing.

## Alternatives considered

- **A `partial` run status** (design 6.5 wording). Rejected: it would mix the Critic's judgement into the status, make M1 and M2 records less comparable, and blur what the Critic-accuracy metric measures.
- **Scorer only for runs the Critic accepts.** Rejected: rejected runs would have no reference comparison, so Critic accuracy could not be measured.
- **Unlimited Critic rounds inside the fix-loop budget.** Rejected: one round limit of 2 bounds cost and keeps a disagreeing Critic from consuming all 10 loops.
- **Critic with a view of the previous passes (`superseded/`).** Rejected: it breaks the fresh-context rule of design 6.3.

## Consequences

- Reports state two things per run: what the pipeline did (`status`) and what the Critic concluded (verdict), plus whether the Scorer agreed.
- A run can be `completed` with a `failed` Critic verdict; this is intended and visible.
- A Critic send-back at the end of the budget is recorded but not followed, so some runs end with a known unresolved concern.
- The Critic adds LLM cost per run (plan estimate: part of the $0.15-0.30 per run).
- Design 6.2 and 6.5 get sentences referring to this ADR.
