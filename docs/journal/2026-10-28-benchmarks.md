# Journal: benchmarks (2026-10-28)

## 2026-10-28 decision: owner approved the benchmark thresholds

The owner approved all acceptance thresholds exactly as proposed by `cfd-reviewer` (recorded in `benchmarks/*/benchmark.toml`, with the reasoning in the comments there). Cavity (Re 100 and 1000): both centreline profiles, `max_abs_error` normalised by the lid speed, at most 0.05. Pipe (Re 500): profile `max_abs_error` normalised by the centreline velocity at most 0.03, pressure drop `rel_error` at most 0.05. Cylinder: Re 20 drag coefficient `rel_error` at most 0.05 and recirculation length at most 0.10; Re 100 Strouhal number within 0.160 to 0.170 and mean drag coefficient `rel_error` at most 0.05. A threshold is not loosened to make a run pass without a new owner decision recorded here.

## 2026-10-28 decision: cylinder domain size is left to the agent

`task.md` for the cylinder cases asks for an isolated cylinder in an unbounded stream and gives no domain size. The owner decided that choosing a domain large enough is part of the task: a domain so narrow that blockage pushes the drag coefficient past its threshold is a valid finding about the agent, not a defect of the benchmark.

## 2026-10-28 decision: result identifiers stay in the task text

The owner decided that the backticked result identifiers in each `task.md` (for example `u_vertical_centreline`) stay. They specify the output format the Scorer reads, not a hint about solver, mesh or expected values. `tests/unit/test_registry.py` checks that every task names every quantity of its benchmark.
