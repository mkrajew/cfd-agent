# ADR-0010: M3 is one run per benchmark on gpt-5.4, plus one or two generality tests

- **Status:** Proposed, 2026-11-27. The narrowing of the scope (points 1 and 7) was decided by the owner in chat on 2026-11-26; points 2-6 are proposals that await approval with the M3 plan (`docs/superpowers/plans/2026-11-26-m3-fixes-and-gpt-5-4-campaign.md`, D1).
- **Deciders:** the owner

## Context

Design 14.1 (accepted on 2026-10-28) planned the M3 campaign as a matrix in priority order: `gpt-oss-120b` with medium effort on all development benchmarks and the generality set with 5 repeats each; a Llama-3.3-70B comparison on a subset; and a reasoning-effort arm, about 65-75 runs. Design 12 says M3 is done when there are "statistics ready for the report".

M2 closed as a partial result on 2026-11-26. In its acceptance series (one frozen build, n = 3 per benchmark) `gpt-oss-120b` passed the Scorer in 3 of 15 runs, all on the lid-driven cavity at Re 100. In one comparison run, `gpt-5.4` produced a pipe solution that passed the Scorer when evaluated outside the pipeline, while the run itself stopped on its cost cap (one run, a different image). Hand-written, LLM-free solutions of all five development benchmarks pass the Scorer. The M0-M2 spend was $27.54 of the $35 envelope.

On 2026-11-25 the owner decided that M3 includes a commercial-model arm. On 2026-11-26 the owner decided the order of M3 (fixes first, then each of the five development benchmarks once on `gpt-5.4`, then further tests) and, later that day, narrowed M3: no further `gpt-oss-120b` campaign, no Llama-3.3-70B comparison and no reasoning-effort arm; the further tests are one or two generality tests on `gpt-5.4`. That is the whole campaign, and the owner said that it is enough (given in Polish; the repository records the English translation).

## Decision

1. **Scope.** M3 has three parts: (a) the fixes chosen in the M3 plan; (b) one run of each of the five development benchmarks on `gpt-5.4`; (c) one or two generality tests on `gpt-5.4`, one run each. The generality task texts are written by the owner, and their reference data and a hand-written calibration are prepared first, under blind evaluation. This replaces the matrix of design 14.1. The M3 envelope of $45 (design 14.2) is unchanged.
2. **"Statistics ready for the report" means a table of observations.** One row per run, invalid runs included, built from the run records (status, failed stage, Scorer result and errors, Critic verdict and agreement, attempts, fix loops, tokens with the cached part, tracked cost, wall time, served model, image), shown beside the M2 acceptance-series table. No success rates, no confidence intervals and no claim that compares the two models. The "benchmark success rate" headline of design 9.2 is not reported for M3.
3. **A frozen release is kept.** The tag `v0.3.0` before the first campaign run fixes `src/`, `docker/`, `pyproject.toml`, `uv.lock`, `scripts/`, the image, prompts, notes and the campaign configuration until the last campaign run ends. Only generality data, tests and documents may change. Every run records its image and prompt hashes. At this size the freeze matters for the generality tests: without it, it could not be shown that nothing learned from them reached the system (design 9.1). It costs nothing.
4. **`cfdagent bench` is not built in M3.** Six or seven runs are one sequence. A small tested script that runs them in order, records exit codes, stops before the budget could be exceeded and stops after a broken run, together with a script that builds the table from run records, does the same job.
5. **Run rules.** No run is repeated to get a better result. A run is invalid only if it ended through a fault outside the agents' work: infrastructure (VM, disk, Docker, network, an Azure outage, HTTP 429 or 5xx that used up the retries), or a defect of the pipeline's own code (an internal error, or a gate or tool that rejected or broke correct work) shown by a failing test. A cost-cap or wall-clock stop, an agent failure, a failed score or physics the solvers cannot represent are results. The owner decides whether a run is invalid. An invalid development run is repeated once; if the cause is a code defect, after a fix under a new tag, without repeating earlier valid runs. A generality run is repeated only after an infrastructure fault, unchanged; no fix is made in M3 from what a generality run shows. Every run, invalid ones included, is reported with its cost.
6. **Generality preparation happens after the freeze,** in a session that changes nothing in the frozen set. The owner lifts and restores the deny rule on `benchmarks/generality/**` by hand. If a hand-written solution cannot pass the frozen pipeline, the owner chooses to run the task with the limitation stated, to replace the task, or to fix and re-freeze and report that task as not a clean generality test.
7. **The campaign model is `gpt-5.4`** (medium reasoning effort), a commercial hosted model. The open-weight evidence of the project is M1 and M2. `gpt-oss-120b` stays the default in the code.

## What follows from the narrower scope

- **One run per benchmark on `gpt-5.4` (n = 1) and three runs per benchmark on `gpt-oss-120b` from the M2 acceptance series (n = 3) are observations, not rates.** A pass shows that the system solved that task once; a failure shows one way it can fail.
- The two sets do not compare the models: the builds, cost caps and wall-clock limits differ.
- One or two generality tests, one run each, cannot measure how general the system is. They remain the strongest evidence M3 has about unseen tasks, and the report must say how small it is. The agents can search the OpenFOAM tutorials, so each task is reported with its closest tutorial.
- Critic accuracy is based on at most seven M3 runs and the four completed runs of the M2 acceptance series.
- The effect of reasoning effort and a comparison with another open-weight model are not studied.
- A hosted commercial model can change on the service side; the served model name is recorded, and exact reproduction is not guaranteed.

## Alternatives considered

- **Keep the design 14.1 matrix on `gpt-oss-120b`.** Rejected by the owner.
- **Several repeats on `gpt-5.4`.** Not chosen. At about $3 per run (a guess from one run), five repeats of five benchmarks would cost about $75, above the envelope.
- **No freeze at this size.** Rejected: see point 3.
- **Build `cfdagent bench`.** Rejected: no scientific gain at this size; it would cost implementation and review.
- **Repeat each failed run once.** Rejected: it would turn n = 1 into the better of two tries.

## Changes to the design document (applied when this ADR is accepted)

- Section 12, row M3: scope "Fixes; frozen release; one run of each development benchmark and one or two generality tests on gpt-5.4 (ADR-0010)"; done when "A table of observations, one row per run, ready for the report".
- Section 14.1: a first line "Replaced by ADR-0010: M3 is one run per development benchmark and one or two generality tests on gpt-5.4. The matrix below is kept as the original plan."
- Section 9.2, after the headline metric: "For M3, ADR-0010 replaces the success rate with a table of single observations."
- Section 6.10, after the CLI block: "`cfdagent bench` was not built in M3 (ADR-0010)."
