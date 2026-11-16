# 2026-11-15 M2 Task 15: the Scorer after the Critic and Critic agreement

## 2026-11-15 milestone: the Critic's verdict is compared with the Scorer's result in the run record

The Scorer already ran after the Critic step (the pipeline order set in Task 14), so the new work was reporting. `RunRecord.critic_agreement` is True when the Critic's verdict being `success` equals the comparison having passed, False when they differ, and None when either is missing. A comparison that could not be made carries `passed=False` with a problem and therefore counts as a Scorer that did not pass; this is an owner-visible choice and is noted in the PR. `summary.md` gained a Critic section (verdict, failed checks, reasons, an unfollowed send-back), a fix-loop table and an agreement line, and `scripts/replay_scripted_run.py` prints the same agreement line. The Critic's accuracy (design 9.2) can now be read straight from `run_record.json`. Verified: 2626 unit tests passed; the scripted pipeline integration tests (6 of 6) passed in the image `cfdagent:dev`; `code-reviewer` returned `approve` with no blocking findings in one round. Cost: $0 (no Azure, no LLM).

## 2026-11-15 observation: only the final pass can be scored

Quantities of an earlier pass cannot be scored, because a route to an earlier stage moves `results/` to `superseded/`. A route to the Analyst itself is safe: the Analyst gate demands a fresh `quantities.json` and promotion replaces `results/` wholesale, so the Scorer never sees stale quantities. The agreement therefore always refers to the final pass and the Critic's final verdict.
