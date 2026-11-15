# Generality test set

This directory is reserved for the **generality test set** of `cfd-agent` (design document,
section 9.1): tasks of flow types the system never saw during development. They test whether the
system works in general and not only on the development benchmarks in `benchmarks/`.

## Rules (design 9.1 and 9.2)

- **The owner writes the tasks, in the owner's own words.** Reference data and acceptance criteria
  are prepared by `cfd-reviewer` in M2 (plan Task 7). No development session writes or edits a task
  text.
- **No generality task is run before the frozen release.** They run only in the M3 campaign, after
  prompts, knowledge notes, image and configuration are frozen as a tagged release. Nothing learned
  from them can flow back into prompts or code. The set may also hold new variants of the
  development families (for example another Reynolds number), written by the owner.
- **Never visible during development.** Development sessions and agents do not read, summarise or
  run these tasks. The reference data and acceptance criteria are never mounted in the executor
  sandbox and never part of the knowledge base (blind evaluation).
- **Results are reported separately** from the development tasks; the generality result is the main
  evidence for the hypothesis.

## How the code enforces this

- The loader (`src/cfdagent/bench/registry.py`) reads `benchmarks/generality/<id>/` only on
  request (`load_registry(include_generality=True)`) and sets `Benchmark.task_set` from the
  directory. A `task_set` key in a `benchmark.toml` is rejected.
- `cfdagent run --benchmark <generality id>` exits with code 2 unless `--allow-generality` is given
  (also for `--detach`, which passes the flag on to the background process). The orchestrator opens
  the generality set only for a run configured as `generality`.
- `cfdagent run <path to a task file under benchmarks/generality>` is refused unless
  `--allow-generality` is given (links and `..` are resolved; a copy of the file elsewhere is not
  detected).
- `.claude/settings.json` denies the Read tool on `benchmarks/generality/**` for development
  sessions. This is a guard against accidents, not a barrier (a shell command can still read the
  files). Only the owner changes it: it has to be lifted for the session that prepares the set
  (plan Task 7) and put back afterwards, and the frozen M3 campaign needs it lifted as well.
- The knowledge tools reach only the curated notes and the OpenFOAM tutorials of the image.
- The generality lint (`tests/unit/test_generality_lint.py`) fails when a word that names a family
  of this set appears under `src/cfdagent`.

## Layout

Same as the development benchmarks (see `benchmarks/README.md`):

```
benchmarks/generality/<id>/
  benchmark.toml    id, family, task_file, citation, [[criteria]]
  task.md           the owner's text, verbatim
  reference/*.csv   reference profiles, or make_reference.py with a --check mode
```

The set is empty until plan Task 7.
