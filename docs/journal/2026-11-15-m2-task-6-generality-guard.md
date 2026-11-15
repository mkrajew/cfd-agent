# 2026-11-15 M2 Task 6: generality-set storage and run guard

## 2026-11-15 milestone: the generality test set has a place, a loader flag and a run guard

`benchmarks/generality/<id>/` is reserved for the generality test set (design 9.1); the directory holds only a README with the rules and no task. `Benchmark.task_set` is set by the loader from the directory and a `task_set` key in a `benchmark.toml` is rejected. `load_registry` skips the set unless `include_generality=True`; `cfdagent run --benchmark <generality id>` exits 2 unless `--allow-generality` is given (also with `--detach`); the orchestrator opens the set only for a run configured as `generality`. The executor's knowledge roots (notes and tutorials) contain nothing under `benchmarks/`; this is now a test. Verified: full suite 2360 passed, 143 skipped, ruff and pre-commit clean; `code-reviewer` and `cfd-reviewer` returned `approve` after one fix round. Azure cost was zero.

## 2026-11-15 decision: a development run never opens the generality set

The first version of `run` loaded the whole registry including the generality set, so a fault in a generality entry blocked a development run and its error text appeared in that session's output (found by `code-reviewer`). Now the development registry is read first; the generality set is opened only for an id that is not a development benchmark, only to refuse it (or to run it with `--allow-generality`), and its parse errors are never printed. A link from a development directory into `benchmarks/generality/` is also refused.

## 2026-11-15 decision: banned words for the generality families, agreed with `cfd-reviewer`

The lint bans, under `src/cfdagent` (contents and file names, whole text, no letter boundaries so that CamelCase is caught): backward/rearward-facing, Couette, Poiseuille, Blasius, Armaly, Gartling, flat plate, de Vahl Davis, differentially heated, natural convection and reattachment length. Words with a general use were left out on purpose (boundary layer, reattachment alone, Nusselt, Rayleigh, buoyancy, similarity, channel, backward as a time scheme). `src/` had no hit. One finding the list cannot see: `prompts/mesh.md:61` gives the laminar boundary-layer thickness estimate for a plate (5 L / sqrt(Re_L)), the closed form of one flat-plate quantity; `cfd-reviewer` judged it admissible as a mesh-sizing rule but suggested saying "wall" instead of "plate" or reducing it to the scaling. It was not changed in this task (prompts were out of scope) and goes to the owner.

## 2026-11-15 decision: guard the task-file path and deny reading `benchmarks/generality/**`

On the owner's approval, `cfdagent run <task file>` is refused (exit 2, `--allow-generality` lifts it) when the resolved path lies under `benchmarks/generality`, so the text of a generality task cannot start a run by path. `.claude/settings.json` now denies the Read tool on `benchmarks/generality/**`; this is a guard against accidents, not a barrier (a shell command can still read the files). The rule has to be lifted by the owner for the session that prepares the set (Task 7) and for the M3 campaign. The stronger option, keeping the texts out of `main` until the freeze, was left for Task 7.
