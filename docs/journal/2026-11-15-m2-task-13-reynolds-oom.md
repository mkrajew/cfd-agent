# 2026-11-15 M2 Task 13: stated Reynolds number check and executor out-of-memory

## 2026-11-15 decision: Task 13 closes two M1 carry-overs, a Reynolds number check in the plan gate and an out-of-memory response

(a) The Planner's `CaseSpec` must carry the Reynolds number the task states. `planner.stated_reynolds` reads values from the task text, and `plan_gate` fails when exactly one value is stated and `reference.reynolds_number` differs from it by more than 0.1 % (also when it is null). With several stated values or none there is no check; the Critic (Task 14) covers the rest. The extraction is deliberately conservative, because a false value would fail a correct `CaseSpec` and burn the Planner's attempts. It skips approximate values ("about", "≈"), "10 000", numbers with units, definitions of scales ("based on U = 1 and L = 1") and lists cut off by an unreadable number. The five benchmark task files each yield exactly one value. (b) An executor container killed by the out-of-memory killer (exit 137 and `OOMKilled` in `docker inspect`) now returns a failed, non-internal response, "the executor ran out of memory (limit <memory>); reduce the problem size". The tools raise it as `AgentInputError`, so it costs the agent an attempt instead of aborting the run as an internal error.

## 2026-11-15 obstacle: `docker run --rm` hides the out-of-memory flag

`docker run --rm` removes the container as soon as it exits, so `docker inspect` could not report `OOMKilled`. Solution: `--rm` was dropped from the argv, and the client now inspects the container after exit 137 and always removes it itself (`docker rm -f`). Trade-off: after a hard crash of the orchestrator, exited containers remain until they are pruned. A real-container probe by the reviewer confirmed exit 137 and `OOMKilled=true` with `--init`.

## 2026-11-15 observation: both reviewers sent the first regex back twice, all blocking findings were false-positive risks

Both reviewers (Sonnet) returned `changes` on the stated-Reynolds extraction twice. The blocking findings were readings of the wrong number: "Re = 10 000" read as 10, "Reynolds number at 300 K is 5000" read as 300, "based on U = 1 and L = 1 is 100" read as 1, a later "is 2000" in the same sentence read as the value, and "from 1000 to 10 000" read as 1000. Each became a unit test. An out-of-memory kill that hits only a child process of the executor (the usual case for a solver) still gives "exited with code -9" without the out-of-memory hint; both outcomes are the agent's failed attempt.

## 2026-11-15 decision: findings deferred out of Task 13

Not done in this task: a key-dimension check, rulings R25, R28, R31, R32 and R33, solver warning thresholds, `createPatch` and a `run_utility` timeout parameter. The reviewers' minors go to Task 17 or 20: compact lists such as "100,400", "100 or lower", wrapped lines, routing an out-of-memory failure in `run_solver` back to Mesh or Case Setup, and the convergence report, where an out-of-memory kill is still an internal error. Azure and LLM cost of Task 13: $0.
