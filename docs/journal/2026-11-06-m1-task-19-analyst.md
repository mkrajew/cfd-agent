# 2026-11-06 M1 execution, session 4: Analyst stage (Task 19)

## 2026-11-06 decision: owner rulings on R44, R38 and warning thresholds

On 2026-11-06 (chat) the owner approved keeping R44 for M1: the Solver Runner has no convergence gate, which deviates from design 6.2 row 5 and is recorded in ADR 0004 (`docs/adr/0004-no-solver-convergence-gate-in-m1.md`). The gate returns in M2 together with routing back to Case Setup. The owner also approved R38 (trial-run Courant limit of 10, to be revisited after the Task 23 live runs) and left the solver warning thresholds as warnings. Cost if wrong: an unconverged steady case reaches the Analyst; the Courant limit may need retuning.

## 2026-11-06 milestone: Task 19 Analyst stage implemented and reviewed

The Analyst (`src/cfdagent/agents/analyst.py`, `src/cfdagent/prompts/analyst.md`; commits 5fc8ff0 and d08de3d) reads `case/`, `logs/` (including `solver_run.json`) and `casespec.json`, and writes `post.py`, `analysis.md` and plots. It has `run_utility` restricted to an allowlist of OpenFOAM post-processing utilities and no knowledge tools (blind evaluation). The gate validates on the host that every `OutputSpec` is present with the right kind, finite and sorted, and that `analysis.md` exists and mentions convergence. In the integration test a scripted cavity fixture passes the Scorer (the test calls the Scorer). Tests: 1496 passed on the host, 1618 unit tests in the image, 3 passed in `test_analyst_stage` (integration). Azure cost: 0.

## 2026-11-06 obstacle: reviews found a gate ordering hole and prompt errors (Task 19)

The code reviewer (Important) found that `analysis.md` was checked before `post.py` ran but promoted after it, so the script could rewrite or delete the file. The check now runs after the script, plus a digest check that `case/` and `logs/` are unchanged. The CFD reviewer (Important) found three prompt errors: a function-object name that does not exist (`patchFlowRate`; the correct name in the pinned OpenFOAM is `flowRatePatch`), a false plausibility rule that speeds must lie within the boundary-condition range (incompressible velocity has no maximum principle), and a missing rule that integrated quantities must be scaled for the mesh thickness or wedge sector (factor 360/angle). All were fixed in one round and both re-reviews approved.

## 2026-11-06 observation: limits of the Analyst gate (for the report)

The convergence check is a keyword match, so the honest verdict is enforced by the prompt and by the later Critic and summary, not by the gate; this is a consequence of R44. The fixture's `analysis.md` claims convergence without computing it and must not be used as a model example. `post.py` can still write outside `case/` and `logs/` in the run directory. Hashing the whole solved case twice costs time on large transient cases.

## 2026-11-06 decision: next step

Task 20 (orchestrator, `code-reviewer` only) starts in a fresh session; open carries are in the hand-over note.
