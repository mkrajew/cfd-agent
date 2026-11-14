# M2 Robustness and Full Benchmark Set Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the M1 pipeline robust enough to solve all five development benchmarks: repair of failed attempts, cross-stage fix loops, a convergence gate, the Critic, the Scorer after the Critic's final verdict, curated knowledge notes, the four remaining benchmarks calibrated with hand-written solutions, and the generality test set prepared but not run. M2 is done when `pipe-re500`, `cylinder-re20` and `cylinder-re100` pass the Scorer in unchanged live runs on one frozen candidate commit (design section 12; the exact rule is decision D8); `cavity-re1000` is run and reported in the same series.

**Architecture:** The orchestrator stays a plain Python state machine on the VM host. Its pipeline becomes a loop over a fixed stage order: a failed gate or a Critic verdict may name an earlier stage, and the orchestrator re-runs from that stage, and every stage after it, within a run-wide budget of fix loops (design 6.5: at most 10). Each pass through a stage is numbered and has its own attempt directories, so nothing earlier is overwritten. A configuration flag chooses whether a retry starts from an empty directory or from the failed attempt's files (ADR-0005). The Critic is a seventh agent with a fresh context and read-only access to the run's artefacts; the Scorer stays deterministic and blind and runs after the Critic's verdict is final. All new checks are general (generality rule); everything case-specific lives in `benchmarks/` and `tests/fixtures/`.

**Tech Stack:** unchanged from M1: Python 3.12, uv, pydantic v2, typer, openai SDK + azure-identity + azure-storage-blob, Docker sandbox, OpenFOAM ESI v2512, Gmsh, CadQuery, numpy, pytest, ruff, GitHub Actions.

**Spec:** `docs/design/2026-10-28-cfd-agent-design.md` (sections 6.2, 6.3, 6.5, 6.6, 9, 12, 14). Inputs: `docs/design/m2-carryover-from-m1.md` (items 1-17, "carry-over 1..17" below), the status table of `docs/design/m1-carryover-from-m0-review.md`, `docs/adr/0004-no-solver-convergence-gate-in-m1.md`, `docs/adr/0005-in-place-repair-behind-a-flag.md` (proposed with this plan), the journal entries of 2026-11-09 to 2026-11-13. Format model: `docs/superpowers/plans/2026-10-29-m1-first-autonomous-run.md`.

## Global Constraints

- Everything in the repository is English. Claude talks to the owner in Polish.
- Python 3.12, `uv`, `ruff` line length 100, `pytest --strict-markers`. Default tests (`tests/unit`) need no Docker, no network, no real LLM.
- OpenFOAM **ESI v2512** pinned. CadQuery creates every geometry; no blockMesh path. 2D cases one cell thick with `empty` front and back. **Max 4 MPI ranks.**
- Default run limits (design 6.5, configurable): max 3 attempts per stage and pass; **max 10 fix loops overall**; wall-clock 2 h; max $1.00 LLM cost; max 4 MPI ranks.
- Models: `gpt-oss-120b`, reasoning effort `medium` by default, Entra ID only. The Critic is a seventh entry of `RunConfig.models` (`AGENT_NAMES` grows by `critic`).
- Generality rule: prompts, knowledge notes and tool code contain only general CFD/OpenFOAM knowledge; `src/` never names a benchmark case. `tests/unit/test_generality_lint.py` and `cfd-reviewer` enforce it.
- Blind evaluation: no agent, the Critic included, can read `benchmarks/` or `results/comparison.json`; the Scorer runs only after the Critic's final verdict and never triggers a retry or a fix loop.
- Generality test set: written by the owner, references and criteria by `cfd-reviewer`, **never run by the pipeline before the frozen release of M3** (design 9.1). The main session does not use what it reads there in prompts, notes or code.
- No secrets, keys, tokens, subscription IDs, IP addresses or e-mail addresses in the repository. No simulation results in git (calibration numbers go to PR descriptions and the journal; result files stay out of git).
- Azure: CLAUDE.md rule 4 (standing approval inside `rg-cfdagent` and the envelope: announce in chat first, then work). Explicit owner approval for anything outside `rg-cfdagent` or at subscription/Entra level (Cost Management queries are scoped to `rg-cfdagent`), deleting the resource group, storage account or `runs` container, **any task or series estimated above $5**, and anything beyond the envelope.
- Workflow: conventional commits; one branch per task (`feat/m2-<slug>`, `fix/...`, `test/...`, `docs/...`), each from the current `main`; every task is a PR with what, why, how verified and Azure cost impact, green CI; **the owner merges**; no push without being asked. Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`; PR descriptions end with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- Lean workflow (owner-approved): subagents on Sonnet (implementers effort `high`, reviewers `xhigh`); minor review findings deferred to a list, at most two fix rounds per review; `cfd-reviewer` only for physics, prompts, notes, tool code, agent-facing messages and benchmarks.
- New run-record fields always have defaults, so `cfdagent status` and `list` still read M1 run records.
- Import boundaries of M1 hold for the new modules: `agents/routing.py` and `agents/critic.py` are host side (never import `cfdagent.tools`, CadQuery, Gmsh, PyVista); `tools/convergence.py` is container side (never imports `cfdagent.bench`, `agents`, `orchestrator`). `tests/unit/test_import_boundaries.py` covers them.

**Standard close** (last step of every task): `uv run pytest`; `uv run ruff check . && uv run ruff format --check .`; for `[integration]` tasks the integration suite in the image (CLAUDE.md); `code-reviewer` on the diff with the spec only, plus `cfd-reviewer` for `[cfd]` tasks; a `changes` or `reject` verdict blocks the PR; commit with the given message; PR description.

## Decisions that need an ADR or the owner's approval before work starts

| # | What | Why it needs approval | Blocks |
|---|---|---|---|
| D1 | **ADR-0005: in-place repair behind `repair_mode`** (drafted with this plan, status Proposed) | Changes the retry rule of design 6.2 | Task 8 |
| D2 | **ADR-0006: fix loops and the Solver Runner convergence gate** (supersedes ADR-0004): stage order and valid route targets, rounds, what is cleared on a route back, the gate rules and thresholds | Restores design 6.2 row 5 and implements 6.5 | Tasks 11, 12 |
| D3 | **ADR-0007: Critic and the Scorer after the Critic**: Critic inputs and checks, round limit, what happens when the fix-loop budget is used up, run status versus Critic verdict (and whether a `partial` status is introduced, design 6.5), the Scorer scoring runs the Critic did not accept | Fixes open points of design 6.2 rows 7-8 | Tasks 14, 15 |
| D4 | Any change of a benchmark reference value or threshold found during calibration | `benchmarks/README.md`: never changed without a new owner decision | Tasks 3-5 |
| D5 | Generality set: the owner's task texts; their calibration deferred to M3 (recommended, Task 7); optionally a `settings.json` deny rule for development-session reads of `benchmarks/generality/**` | Owner's decision in design 9.1 | Task 7 |
| D6 | **Envelope:** the M2 estimate below reaches or exceeds the $20.7 left in the $30 envelope. Either raise the M0-M2 envelope from the reserve (up to about $5) or accept the cuts in the order listed under Budget | CLAUDE.md rule 4, design 14.2 | Tasks 17, 19 |
| D7 | **Task 17 live series** (estimated $5-8, above $5) | CLAUDE.md rule 4 | Task 17 |
| D8 | **Done rule and acceptance series** (Task 19, estimated $4-6): proposed "each of pipe, cylinder Re 20 and cylinder Re 100 has at least 1 of 3 unchanged runs passing the Scorer" | CLAUDE.md rule 4; the done rule is a research choice | Task 19 |
| D9 | The Case Setup trial-run Courant limit of 10 (ruling R38, ADR-0004), which the owner asked to revisit after live runs: keep or change, on the evidence of Task 17 | Owner asked for it | Task 18 |
| D10 | Tag `v0.2.0` | Release | Task 20 |

ADR-0006 and ADR-0007 are written by `scribe` from this plan's interfaces (with `cfd-reviewer` fixing the gate thresholds in ADR-0006), each in a small `docs:` PR that the owner approves before the first task it blocks starts.

## Recommended order

Two tracks start together once the plan and ADR-0005 are approved:

- **Track B (benchmarks, no LLM cost):** Task 2 (time-series helpers) → Tasks 3, 4, 5 (calibration of `cavity-re1000`, `pipe-re500`, `cylinder-re20`, `cylinder-re100`, hand-written solutions run locally in Docker). Tasks 6 and 7 (generality set) any time; Task 7 waits for the owner's texts.
- **Track A (pipeline):** Task 8 (in-place repair) → Task 9 (messages, paths, turn budget) → Task 10 (knowledge notes) → D2 → Task 11 (routing and fix loops) → Task 12 (convergence gate) → Task 13 (CaseSpec check, OOM) → D3 → Tasks 14, 15 (Critic, Scorer after the Critic).
- **Then:** D6/D7 → Task 16 (VM refresh, scripted replay) → Task 17 (live development series) → Task 18 (freeze, D9) → D8 → Task 19 (acceptance series) → Task 20 (close-out).

Tasks are numbered in this order of work. The tracks share some files: `executor/ops.py` (Tasks 10, 12, and 4-5 if an agent-facing utility is added), `agents/analyst.py` (Tasks 8, 9, 10, 11, and 4-5 as before), `cli.py` (Tasks 6, 8, 9), `orchestrator.py` (Tasks 6, 11, 13, 14, 15), `tools/foam.py` and `tools/process.py` (Tasks 4, 5). Rule: two tasks that touch the same file are never in flight at the same time; the later one branches from `main` after the earlier one is merged. Task 12 Step 4 re-runs every calibration merged by then through the new gate; calibrations merged later are run through the gate in their own task.

If the calibration track runs in parallel sessions, each session works on its own benchmark's fixture directory and calibration test only.

## Review Focus

1. **A fix loop that never ends, skips a stage, or leaves stale results** (Critic sends back to Mesh, Case Setup is skipped because it passed before, the Scorer reads pass-0 quantities after a failed pass 1): every stage after the target runs again, downstream canonical outputs are moved aside on a route back, the counter stops at `max_fix_loops`, and the Scorer only reads quantities from the final pass's passing Analyst. Task 11 (`test_route_back_reruns_every_later_stage`, `test_route_back_moves_downstream_outputs_aside`, `test_fix_loop_budget_ends_run_failed`), Task 15 (`test_scorer_ignores_quantities_of_an_earlier_pass`).
2. **The Critic seeing what it must not see** (benchmark reference values, the Scorer output): the Critic's readable scope excludes `results/comparison.json` and everything outside the run directory, and a transcript scan finds no reference value. Task 14 (`test_critic_cannot_read_comparison_or_benchmarks`, `test_transcript_has_no_reference_values_with_critic`).
3. **In-place repair or a route back carrying a stale file** (a `0/U` from before a mesh change, an old time directory resuming the trial run): only files in the stage's writable scope are carried; a routed Case Setup copies only `constant/polyMesh` from the canonical case, never time directories or logs, and its carried dictionaries are re-checked against the new patch list. Task 8 (`test_in_place_seeds_only_writable_files`), Task 11 (`test_routed_case_setup_gets_mesh_only`, `test_case_setup_seed_after_mesh_change_is_rechecked`).
4. **A steady case judged converged on residuals that never fell, or a periodic case judged on a too-short record** (one shedding period, a growing signal): the gate demands a stated criterion or a measured steadiness for steady cases, and a minimum number of periods with stable mean and amplitude when the CaseSpec says the outputs come from a periodic regime. Task 12 (`test_unconverged_steady_run_routes_back`, `test_too_few_periods_is_not_periodic`, `test_growing_amplitude_is_not_periodic`, `test_aperiodic_transient_is_not_checked_for_periodicity`).
5. **A generality task started by accident during development** (`cfdagent run --benchmark <generality id>`, also with `--detach`): refused unless explicitly allowed for the frozen campaign; generality files are never mounted or indexed. Task 6 (`test_generality_benchmark_refused_by_default`, `test_detach_passes_allow_generality_through`, `test_knowledge_index_excludes_benchmarks`).

## Owner touch points in M2

| When | What the owner does |
|---|---|
| Task 1 | Approves this plan and ADR-0005 (Polish summary) |
| Before Tasks 11 and 14 | Approves ADR-0006 and ADR-0007 |
| Tasks 3-5 | Sees each calibration result; decides on any reference or threshold change (D4) |
| Task 7 | Writes the generality task texts in their own words; approves references and criteria (D5) |
| Every task | Merges the PR after a plain-language summary and CI status |
| Before Task 16 | Decides on the envelope (D6) and approves the Task 17 cap (D7) |
| Task 17 | Told at half the approved cap; can stop the series |
| Task 18 | Decides on the Courant limit (D9) |
| Task 19 | Approves the done rule and the acceptance series (D8), sees the results (milestone approval point) |
| Task 20 | Approves the tag `v0.2.0` (D10) |

## Budget

Cost Management on 2026-11-14: about $9.3 of the $30 M0-M2 envelope spent, about $20.7 left. That remainder also carries the **fixed infrastructure cost of about $8.70 per month** (about $0.29 a day), so the length of M2 matters as much as the runs. M1 measured $0.02-0.19 LLM per run (the completed runs $0.11 and $0.18) and 1-21 minutes per run; M2 runs add the Critic and fix loops and the cylinder at Re 100 is the longest case, so the estimates use $0.15-0.30 LLM and 30-40 minutes of VM time per run including overhead, at $0.208/h.

| Item | Runs | LLM | VM | Total |
|---|---|---|---|---|
| Fixed infrastructure, 5 weeks | - | - | - | about $10 |
| Calibration (Tasks 2-5), local Docker | - | - | - | $0 |
| Task 16 scripted replay | - | - | under 1 h | under $0.2 |
| Task 17 live development series | about 20 | $3-6 | 10-13 h, $2-2.7 | **$5-8.7** |
| Task 19 acceptance series | 15 | $2.3-4.5 | 8-10 h, $1.7-2.1 | **$4-6.6** |
| Total | | | | **about $19-25.5** against $20.7 left |

The upper end exceeds the envelope (D6). Proposed caps: Task 17 **$7** (report at $3.5, stop at $7, D7); Task 19 **$6** (D8). Cuts if the owner does not raise the envelope, in this order: (1) acceptance repeats from 3 to 2 per benchmark (saves about $1.5-2); (2) drop `cavity-re100` from the acceptance series (about $0.5-1); (3) Task 17 repair-mode comparison from 6 to 4 runs (about $0.5); (4) Task 17 from about 20 to about 14 runs (about $2); (5) deallocate the VM between sessions strictly and keep M2 short, since each extra week costs about $2 fixed. Task 16 reports what the fixed $8.70/month consists of (read-only, scoped to `rg-cfdagent`) so the owner can decide on savings.

## File structure (new and changed)

```
src/cfdagent/
  config.py                 repair_mode, max_fix_loops, max_critic_rounds, max_turns_per_attempt_by_stage, critic model
  record.py                 repair_mode, StageRecord.pass_no, FixLoopRecord, CriticVerdict, critic_agreement
  rundir.py                 attempt directories per pass
  orchestrator.py           pipeline loop with routing, Critic step, Scorer after the Critic
  agents/routing.py         STAGE_ORDER, RouteRequest, clearing downstream outputs (new)
  agents/loop.py            in-place seeding, repeated-error detection, routed opening message, structured diagnosis hook
  agents/stage.py           Stage.carry_over, Stage.seed_from_canonical, Stage.route_after_failure, StageContext.fix_request
  agents/transcript.py      pass number in every transcript line
  agents/critic.py          Critic agent (new); prompts/critic.md (new)
  agents/planner.py         stated-Reynolds check, fix request in the opening message
  agents/casespec.py        periodic_outputs flag
  agents/solver_runner.py   convergence gate, structured fix request
  agents/case_setup.py      mesh-only preparation, convergence criterion for steady cases, directive messages
  tools/convergence.py      steadiness and periodicity reports (new, executor side)
  tools/foam_results.py     function-object tables, dominant frequency, period averages
  knowledge/search.py       notes + tutorials, notes first
  knowledge/notes/*.md      curated general notes (new)
  bench/registry.py         task_set per benchmark, generality guard
benchmarks/generality/<id>/ owner-written tasks, references, criteria (not run in M2)
tests/fixtures/handwritten/<case>/   hand-written solutions of the four remaining benchmarks
tests/calibration/          long calibration tests (new marker `calibration`, not in CI)
scripts/run_calibration.py  runs one hand-written case and prints the Scorer table
```

---

# Phase 0: plan

### Task 1: M2 plan and ADR-0005 (this PR)

**Files:**
- Create: `docs/superpowers/plans/2026-11-14-m2-robustness-and-full-benchmark-set.md`, `docs/adr/0005-in-place-repair-behind-a-flag.md` (status Proposed)

- [ ] **Step 1:** Owner reviews the plan and ADR-0005 in the PR.
- [ ] **Step 2:** After approval, set ADR-0005 to Accepted and add the sentence on `repair_mode` to design 6.2 (same PR or a `docs:` commit before Task 8).
- [ ] **Step 3: Standard close** (docs only). Commit: `docs: M2 plan`.

---

# Phase B: benchmarks and the generality set (Track B)

### Task 2: Time-series helpers for force histories [cfd] [integration]

**Files:**
- Modify: `src/cfdagent/tools/foam_results.py`, `src/cfdagent/prompts/analyst.md` (helper names, one sentence each)
- Test: `tests/unit/test_foam_results_series.py`, `tests/integration/test_foam_results_openfoam.py`

**Interfaces:**
- Produces: `foam_results.FunctionObjectTable(columns: list[str], data: numpy.ndarray)` with `column(name: str) -> numpy.ndarray`; `foam_results.read_function_object(case_dir, name: str, file: str | None = None) -> FunctionObjectTable` (reads `postProcessing/<name>/<start time>/<file>.dat`, concatenates restarts in time order, drops duplicated times; raises `FoamResultsError` with the list of available files when `file` is ambiguous or missing); `foam_results.dominant_frequency(t, y, skip_fraction: float = 0.5) -> float` (uniform resampling, mean removed, FFT peak refined by parabolic interpolation; `FoamResultsError` when fewer than 3 periods remain after skipping); `foam_results.period_average(t, y, frequency: float, n_periods: int | None = None) -> float` (average over the last whole number of periods).

- [ ] **Step 1: Write the failing tests:** `test_dominant_frequency_of_sine_with_transient` (`exp(-t) + sin(2*pi*1.7*t)` at irregular steps gives 1.7 within 0.5 %); `test_dominant_frequency_needs_three_periods`; `test_period_average_of_offset_sine` (mean 1.33 within 1e-3); `test_read_function_object_concatenates_restarts` (two start-time directories, one overlapping time); `test_read_function_object_lists_files_when_ambiguous`; integration `test_forces_table_from_real_pimplefoam_run` (a few steps of a tiny 2D case with a `forces` function object; columns found by name).
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_foam_results_series.py -v`. Expected: FAIL (functions missing).
- [ ] **Step 3: Implement** as in Interfaces.
- [ ] **Step 4: Run** unit and integration suites. Expected: PASS.
- [ ] **Step 5: Standard close.** Commit: `feat: function-object tables, dominant frequency and period averages`.

### Task 3: Calibration of `cavity-re1000` [cfd] [integration]

**Files:**
- Create: `tests/fixtures/handwritten/cavity_re1000/` (`geometry.py`, `mesh.py`, `case/`, `post.py`), `tests/calibration/__init__.py`, `tests/calibration/conftest.py`, `tests/calibration/test_cavity_re1000.py`, `scripts/run_calibration.py`
- Modify: `pyproject.toml` (marker `calibration: hand-written benchmark solutions, long, run manually in the image`), `CLAUDE.md` (key command)

**Interfaces:**
- Produces: `scripts/run_calibration.py <fixture-dir> --benchmark <id> [--n-procs N] [--gate]`: runs the hand-written geometry, mesh, case and `post.py` through `cfdagent.tools` in a temporary directory, then `score(benchmark, quantities)`; prints criterion, value, reference, error, threshold, pass, and the wall time; exits 0 only if every criterion passes. `--gate` is added in Task 12. Used by Tasks 4, 5, 12.
- `tests/calibration/conftest.py`: fixture `calibrate(fixture_name: str, benchmark_id: str) -> CalibrationResult(passed: bool, criteria: list[dict], wall_seconds: float)`; skipped outside the image (as `tests/integration`).

- [ ] **Step 1: Write the failing test** `test_cavity_re1000_handwritten_passes_scorer` (marker `calibration`; asserts `result.passed`).
- [ ] **Step 2: Run** in the image: `MSYS_NO_PATHCONV=1 docker run --rm -v "$PWD:/work" -w /work cfdagent:dev pytest -m calibration tests/calibration/test_cavity_re1000.py`. Expected: FAIL (fixture missing).
- [ ] **Step 3: Write the hand-written solution:** steady solver with `residualControl` set and met, mesh chosen with `cfd-reviewer` so both profiles pass 0.05 with margin.
- [ ] **Step 4: Run** the test and the script. Expected: PASS. Record cells, wall time (laptop; a lower bound for the VM), maximum errors and whether `residualControl` ended the run in the PR. If no reasonable mesh passes, stop and bring D4 to the owner.
- [ ] **Step 5: Standard close.** Commit: `test: hand-written calibration of cavity-re1000`.

### Task 4: Calibration of `pipe-re500` [cfd] [integration]

**Files:**
- Create: `tests/fixtures/handwritten/pipe_re500/`, `tests/calibration/test_pipe_re500.py`
- Modify (only if the solution needs a utility): `src/cfdagent/tools/process.py` (`ALLOWED_EXECUTABLES`), `src/cfdagent/tools/foam.py` (`NON_SOLVERS`), with `tests/unit/test_foam_allowlist.py`

**Interfaces:**
- Consumes: `scripts/run_calibration.py`, `calibrate(...)` (Task 3).
- The solution uses the dimensionality an agent would most likely choose and the gates support: axisymmetric wedge (`CaseSpec.wedge_angle_deg`, two `wedge` patches) unless `cfd-reviewer` prefers 3D; the other is noted as untested. The pressure drop is reported in Pa (`p * rho`, the kinematic-pressure note of `benchmarks/README.md`).
- Any utility added to the allowlist (carry-over 15; for example `topoSet`) gets a test that it is in `NON_SOLVERS` and runs in the sandbox. A utility the agents should call is also added to `UTILITIES` in `executor/ops.py` and `agents/analyst.py`.
- If Task 12 is already merged, Step 4 also runs `scripts/run_calibration.py --gate` (same in Tasks 3 and 5).

- [ ] **Step 1: Write the failing tests** `test_pipe_re500_handwritten_passes_scorer` and `test_pipe_profile_reaches_axis_and_wall` (sampled `r/R` spans 0 to 1).
- [ ] **Step 2: Run them** in the image. Expected: FAIL.
- [ ] **Step 3: Write the hand-written solution** (CadQuery, Gmsh, steady solver, `post.py` using `foam_results`). A wedge geometry rejected by the geometry or mesh gate is a gate bug: fix it here, test first (`test_wedge_case_passes_geometry_and_mesh_gates`).
- [ ] **Step 4: Run** the calibration. Expected: PASS. Record cells, wall time, errors and the entrance-length check (fully developed before x = 0.35 m) in the PR.
- [ ] **Step 5: Standard close.** Commit: `test: hand-written calibration of pipe-re500`.

### Task 5: Calibration of `cylinder-re20` and `cylinder-re100`, with a reference check [cfd] [integration]

**Files:**
- Create: `tests/fixtures/handwritten/cylinder_re20/`, `tests/fixtures/handwritten/cylinder_re100/`, `tests/calibration/test_cylinder.py`
- Modify (only if needed): allowlist files as in Task 4 (for example `potentialFoam`)
- Modify (only after owner approval, D4): `benchmarks/cylinder-re20/benchmark.toml`, `benchmarks/cylinder-re100/benchmark.toml`, `benchmarks/README.md`

**Interfaces:**
- Consumes: Task 2 helpers, Task 3 calibration runner. `tests/fixtures/handwritten/cylinder_channel/` (a channel, not unbounded) is a starting point for the scripts only.
- Domain size: `cfd-reviewer` chooses a domain for an unbounded stream (the README notes that a 40D x 20D domain overshoots Cd at Re = 20 by more than 5 %) and records a two-size domain check in the PR.
- Reference check (carry-over 6): `cfd-reviewer` tries to confirm the values from the original papers (Dennis and Chang 1970, Fornberg 1980, Coutanceau and Bouard 1977, Williamson 1988/1989, Park et al. 1998). A value or threshold that must change goes to the owner (D4) before any `benchmark.toml` edit.

- [ ] **Step 1: Write the failing tests** `test_cylinder_re20_handwritten_passes_scorer` (steady; Cd and `recirculation_length_over_d`) and `test_cylinder_re100_handwritten_passes_scorer` (transient `pimpleFoam`; St and `cd_mean` from at least 10 shedding periods after the transient).
- [ ] **Step 2: Run them** in the image. Expected: FAIL.
- [ ] **Step 3: Write both hand-written solutions** (2D, one cell thick, `empty` front and back; `forces`/`forceCoeffs`; at most 4 ranks).
- [ ] **Step 4: Run** the calibrations. Expected: PASS. Record cells, wall time per case (laptop; a lower bound for design 14's 20-40 min), errors and the reference check in the PR. A Re = 100 wall time above about 60 minutes on 4 ranks is a finding for the owner, since the run limit is 2 h including LLM time.
- [ ] **Step 5: Standard close.** Commit: `test: hand-written calibration of both cylinder benchmarks`.

### Task 6: Generality-set storage and run guard

**Files:**
- Modify: `src/cfdagent/bench/registry.py`, `src/cfdagent/cli.py` (`run`, `_build_config`, `_child_command`), `src/cfdagent/orchestrator.py` (registry lookup), `benchmarks/README.md`, `tests/unit/test_generality_lint.py`
- Create: `benchmarks/generality/README.md`
- Test: `tests/unit/test_registry.py`, `tests/unit/test_cli.py`, `tests/unit/test_executor_knowledge_root.py`

**Interfaces:**
- Produces: `Benchmark.task_set: Literal["development", "generality"]`, set by the loader from the directory (`benchmarks/<id>/` development, `benchmarks/generality/<id>/` generality); a TOML key `task_set` is rejected. `load_registry(root: Path = Path("benchmarks"), include_generality: bool = False) -> dict[str, Benchmark]`.
- CLI: `cfdagent run --benchmark <generality id>` exits 2 with "generality benchmarks run only in the frozen M3 campaign (--allow-generality)" unless `--allow-generality` is given; `_build_config` takes `task_set` from the benchmark instead of the hard-coded `"development"`; `_child_command` passes the flag through for `--detach`.
- The generality lint gains a banned-word list for the generality families, fixed with `cfd-reviewer` (for example `backward-facing`, `couette`, `blasius`, `armaly`).

- [ ] **Step 1: Write the failing tests:** `test_generality_benchmark_refused_by_default`; `test_allow_generality_sets_task_set`; `test_detach_passes_allow_generality_through`; `test_registry_task_set_from_directory`; `test_toml_cannot_set_task_set`; `test_default_registry_excludes_generality`; `test_knowledge_index_excludes_benchmarks` (the executor's knowledge roots contain no path under `benchmarks`); `test_src_has_no_generality_family_words`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** as in Interfaces; `benchmarks/generality/README.md` states the rules of design 9.1.
- [ ] **Step 4: Run** the unit suite. Expected: PASS.
- [ ] **Step 5: Standard close.** Commit: `feat: generality benchmark set with a run guard`.

### Task 7: Generality test set content [owner gate] [cfd]

**Files:**
- Create: `benchmarks/generality/<id>/task.md` (owner's text, verbatim), `benchmark.toml`, `reference/*.csv` or `make_reference.py`
- Test: `tests/unit/test_registry.py`

**Interfaces:**
- Consumes: Task 6 layout and loader.
- Candidates (design 9.1): backward-facing step (2D laminar), plane Couette or Poiseuille channel, Blasius flat plate; optionally the heated cavity (needs a buoyant solver, first to drop).

- [ ] **Step 1: Owner gate.** Ask the owner for the task texts in their own words. Claude may describe what a task file must state (as in `benchmarks/README.md`) but writes no task text.
- [ ] **Step 2:** `cfd-reviewer` prepares references (analytic scripts with `--check`, or literature tables with full citations) and acceptance criteria with their reasoning, under the rules of the development benchmarks. Carry-over 8: `cfd-reviewer` also lists, per task, the closest stock OpenFOAM v2512 tutorial (for example the backward-facing step has relatives among the tutorials), so the report can say how much a pass measures retrieval.
- [ ] **Step 3:** `test_generality_references_load_and_check` (every generality benchmark loads with `include_generality=True`; every `make_reference.py --check` passes). No pipeline run and no hand-written solution in M2.
- [ ] **Step 4: Owner approves** references and criteria (D5). Recommendation: calibrate the generality tasks with hand-written solutions only at the start of M3, after the freeze, by a session with no part in prompt or note changes. If the owner's texts are not ready at M2 close-out, Task 7 moves to the start of M3 (before the freeze) and Task 20 says so.
- [ ] **Step 5: Standard close.** Commit: `docs: generality test set tasks, references and criteria`.

---

# Phase A: pipeline robustness (Track A)

### Task 8: In-place repair behind `repair_mode` (ADR-0005) [cfd]

**Files:**
- Modify: `src/cfdagent/config.py`, `src/cfdagent/record.py`, `src/cfdagent/agents/stage.py`, `src/cfdagent/agents/loop.py`, `src/cfdagent/agents/case_setup.py`, `src/cfdagent/agents/solver_runner.py` and `src/cfdagent/agents/analyst.py` (`retry_text` override signatures), `src/cfdagent/cli.py`, `docs/design/2026-10-28-cfd-agent-design.md` (6.2 sentence, if not done in Task 1)
- Test: `tests/unit/test_agent_loop.py`, `tests/unit/test_case_setup_stage.py`, `tests/unit/test_cli.py`, `tests/unit/test_config.py`, `tests/unit/test_record.py`

**Interfaces:**
- Produces: `RunConfig.repair_mode: Literal["fresh", "in_place"] = "fresh"`; `RunRecord.repair_mode: Literal["fresh", "in_place"] = "fresh"`; CLI `--repair-mode fresh|in_place`.
- `Stage.carry_over(ctx: StageContext, previous_dir: Path, attempt_dir: Path) -> list[str]`: copies into the new attempt directory every regular file of `previous_dir` that `self.scope.can_write` allows (writable and not excluded) and returns the copied run-relative paths; symlinks refused through `RunDir`. Called by the loop after `prepare_attempt`, only in `in_place` mode and only for attempts 2 and later of a pass. Case Setup's inputs (the mesh) still come from `prepare_attempt`; only the agent's dictionaries are carried.
- `Stage.retry_text(previous: str, carried: list[str] | None = None) -> str`: with `carried`, it says those files are already in the new directory and only the reported problems need fixing. Every override (`solver_runner.py`, `analyst.py`) takes the new parameter.

- [ ] **Step 1: Write the failing tests** (`ScriptedBackend`, toy stage): `test_fresh_mode_is_default_and_unchanged`; `test_in_place_seeds_only_writable_files` (attempt 1 wrote `a.py` in scope and `junk.bin` out of scope; attempt 2 has `a.py` only); `test_in_place_respects_excluded_patterns`; `test_in_place_retry_text_lists_carried_files`; `test_case_setup_in_place_keeps_dictionaries_and_fresh_mesh`; `test_repair_mode_in_record_and_cli`; `test_old_run_record_without_repair_mode_loads`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** as in Interfaces.
- [ ] **Step 4: Run** the unit suite. Expected: PASS.
- [ ] **Step 5: Standard close.** Commit: `feat: in-place repair of failed attempts behind repair_mode`.

### Task 9: Directive check messages, explicit input paths, turn budget [cfd]

**Files:**
- Modify: `src/cfdagent/agents/loop.py`, `src/cfdagent/agents/case_setup.py`, `src/cfdagent/tools/case_requirements.py`, `src/cfdagent/agents/geometry.py`, `src/cfdagent/agents/mesh.py`, `src/cfdagent/agents/analyst.py`, `src/cfdagent/config.py`, `src/cfdagent/cli.py` (`_build_config`), `src/cfdagent/prompts/{geometry,mesh,case_setup}.md`
- Test: `tests/unit/test_agent_loop.py`, `tests/unit/test_case_setup_stage.py`, `tests/unit/test_mesh_stage.py`, `tests/unit/test_geometry_stage.py`, `tests/unit/test_analyst_stage.py`, `tests/unit/test_config.py`, `tests/unit/test_cli.py`

**Interfaces:**
- Carry-over 2: every completeness error from `case_requirements` names the file to create or edit and shows the minimal generic skeleton of the missing entry (for example `system/fvSchemes: missing laplacianSchemes; add "laplacianSchemes { default Gauss linear corrected; }"`; the skeleton is the solver's generic requirement, never a case value). Each check result also lists what already passed (files present and parsed).
- Carry-over 2 (run 21): the loop detects a gate error text identical to the previous attempt's and prefixes "This is the same problem as in the previous attempt; it was not fixed:".
- Carry-over 3: the opening messages of Geometry, Mesh and Analyst state the exact input and output paths relative to the script's working directory (`casespec.json`, `geometry.step`, `boundary_manifest.json`, the case directory).
- `RunLimits.max_turns_per_attempt_by_stage: dict[str, int]` overrides `max_turns_per_attempt` per stage name; field default `{}`, and `RunConfig.default()` sets `{"case_setup": 60}` (the cost cap is unchanged; the value is shown to the owner in the PR). Carry-over 7 (part): `cli._build_config` sets the Planner structured-call limit to 8192 tokens when the effort is `high`.

- [ ] **Step 1: Write the failing tests:** `test_completeness_error_names_file_and_skeleton`; `test_passed_items_listed_per_attempt`; `test_identical_error_is_flagged_as_repeated`; `test_opening_messages_state_input_paths` (geometry, mesh, analyst); `test_turn_limit_override_per_stage`; `test_high_effort_raises_planner_structured_tokens`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** as in Interfaces; prompt changes are one sentence each, general wording.
- [ ] **Step 4: Run** the unit suite. Expected: PASS.
- [ ] **Step 5: Standard close.** Commit: `feat: directive check messages, explicit input paths and per-stage turn limits`.

### Task 10: Curated knowledge notes and note search [cfd] [integration]

**Files:**
- Create: `src/cfdagent/knowledge/notes/` with `README.md` (rules), `gmsh-api.md`, `cadquery-api.md`, `openfoam-function-objects.md`, `steady-solver-setup.md`, `transient-time-step.md`, `forces-and-coefficients.md`, `axisymmetric-wedge.md`
- Modify: `src/cfdagent/knowledge/search.py`, `src/cfdagent/executor/ops.py` (`search_knowledge`, `read_knowledge`), `src/cfdagent/agents/{case_setup,mesh,geometry,analyst}.py` (offer both tools and record `knowledge_reads` in every stage that gets them), `pyproject.toml` (package data), `docker/Dockerfile` only if package data is not copied, `tests/unit/test_generality_lint.py`
- Test: `tests/unit/test_knowledge_search.py`, `tests/integration/test_executor_ops.py`

**Interfaces:**
- Produces: `knowledge.search.NOTES_DIR: Path` (inside the installed package); `KnowledgeRoot(name: Literal["note", "tutorial"], path: Path)`; `search_knowledge(query: str, roots: list[KnowledgeRoot], max_hits: int = 10) -> list[SourcedHit]` with `SourcedHit(source: str, path: str, line: int, text: str)` (a new type, so code that unpacks the existing `Hit` NamedTuple keeps working); note hits rank before tutorial hits; `read_knowledge(path: str, roots, max_bytes=20000)` resolves `note:<file>` and tutorial paths and refuses anything else. `search_tutorials` stays for the existing callers.
- Note content (carry-over 8): Gmsh Python API calls that the M1 runs misused (OCC import, physical groups, extrusion with `recombine`, MSH 2.2 output); CadQuery primitives, boolean cuts, STEP export in metres; v2512 function-object names (`solverInfo`, `forces`, `forceCoeffs`, `sets`/`surfaces`) and where their output lands; steady SIMPLE set-up (relaxation placement, `residualControl`, bounded convection schemes, pressure reference cell); transient time step from a Courant target and write control; force coefficients with reference values taken from the task; wedge geometry rules. Each note under 150 lines, no case names, values only as general rules.

- [ ] **Step 1: Write the failing tests:** `test_note_hits_rank_before_tutorial_hits`; `test_read_knowledge_note_prefix_and_refusals`; `test_notes_are_packaged` (`importlib.resources` finds every note); `test_generality_lint_rejects_planted_note` (a temporary note with a banned word in a copy of the notes directory fails the lint function); `test_knowledge_reads_recorded_for_every_stage_with_the_tools`; integration `test_search_knowledge_op_finds_notes_in_image`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** the search and write the notes; `cfd-reviewer` reviews each note for the generality rule and for correctness on v2512.
- [ ] **Step 4: Run** unit and integration suites. Expected: PASS.
- [ ] **Step 5: Standard close.** Commit: `feat: curated knowledge notes searched before the tutorials`.

---

# Phase C: fix loops and verification gates (ADR-0006)

### Task 11: Routing, passes and cross-stage fix loops [cfd] [integration]

**Files:**
- Create: `src/cfdagent/agents/routing.py`, `tests/unit/test_routing.py`
- Modify: `src/cfdagent/rundir.py` (`attempt_dir` with a pass number), `src/cfdagent/agents/transcript.py` (pass number in each line), `src/cfdagent/agents/loop.py` (`StageOutcome.route`, routed opening message, in-place seed from canonical files), `src/cfdagent/agents/stage.py` (`StageContext.fix_request`, `Stage.seed_from_canonical`, `Stage.route_after_failure`), `src/cfdagent/agents/{geometry,mesh,case_setup,analyst}.py` (`seed_from_canonical`), `src/cfdagent/agents/case_setup.py` (`prepare_attempt` copies only `constant/polyMesh`), `src/cfdagent/agents/planner.py` (fix request in phase 1), `src/cfdagent/orchestrator.py` (`run_pipeline`), `src/cfdagent/config.py`, `src/cfdagent/record.py`
- Test: `tests/unit/test_rundir.py`, `tests/unit/test_orchestrator.py`, `tests/unit/test_agent_loop.py`, `tests/unit/test_case_setup_stage.py`, `tests/unit/test_planner.py`, `tests/integration/test_pipeline_scripted.py`

**Interfaces:**
- Produces: `routing.STAGE_ORDER = ("planner", "geometry", "mesh", "case_setup", "solver_runner", "analyst", "critic")`; `routing.ROUTE_TARGETS = frozenset({"planner", "geometry", "mesh", "case_setup", "analyst"})` (the Solver Runner edits nothing, so a fix aimed at it goes to `case_setup`); `routing.RouteRequest(source: str, target: str, origin: Literal["gate", "critic"], reason: str, instruction: str | None)` (frozen; `ValueError` unless `target` is in `ROUTE_TARGETS` and precedes `source` in `STAGE_ORDER`; `reason` and `instruction` capped at 1000 characters and redacted like record errors); `routing.move_downstream_aside(run: RunDir, target: str, pass_no: int) -> list[str]` moves the canonical outputs of every stage after `target` (`geometry/`, `mesh/`, `case/`, `case_setup/`, `logs/`, `results/`, `analysis.md`, as each stage's `promote` defines them) to `superseded/pass-<n>/` and returns what moved; for a target of `case_setup` it keeps `case/constant/polyMesh` in place.
- `RunDir.attempt_dir(stage: str, attempt: int, pass_no: int = 0) -> Path`: `attempts/<stage>-<attempt>/` for pass 0 (unchanged M1 layout), `attempts/<stage>-p<pass_no>-<attempt>/` afterwards; never deletes another pass's directory. Transcript lines carry `pass_no`.
- `StageOutcome.route: RouteRequest | None = None`; `StageContext.fix_request: RouteRequest | None = None`, `StageContext.pass_no: int = 0`. The loop appends "Fix requested by <source>: <reason>. <instruction>" to the opening message when `fix_request` is set; `run_planner` adds the same text to its phase-1 message.
- `Stage.seed_from_canonical(ctx, attempt_dir) -> list[str]`: in `in_place` mode, attempt 1 of a routed pass starts from the stage's own accepted files: Geometry `geometry/geometry.py`; Mesh `mesh/mesh.py`; Case Setup the dictionaries of `case/0`, `case/constant` (not `polyMesh`) and `case/system`; Analyst its `post.py`, which `AnalystStage.promote` now also copies to `analyst/post.py`. The orchestrator seeds the target **before** `move_downstream_aside` runs, so the target reads its own canonical files while they are still in place. In `fresh` mode nothing is seeded. `CaseSetupStage.prepare_attempt` copies only `case/constant/polyMesh` from the canonical case (never time directories, `postProcessing/` or logs), in every pass.
- `Stage.route_after_failure(ctx, errors) -> RouteRequest | None` (default `None`); a stage's failed outcome carries it.
- `RunLimits.max_fix_loops: int = 10` (design 6.5); `record.FixLoopRecord(n: int, source: str, target: str, origin: str, reason: str, moved: list[str])`; `RunRecord.fix_loops: list[FixLoopRecord] = []`; `StageRecord.pass_no: int = 0`.
- Orchestrator: the pipeline is an index over `STAGE_ORDER` (the Critic step from Task 14 is the last entry; until then the list ends with the Analyst). A failed outcome with a route: if the fix-loop budget is left, record the loop, `move_downstream_aside`, set `fix_request` and `pass_no` for the target, move the index back, clear `fix_request` after the target ran, and re-run every later stage. A failed outcome without a route ends the run as in M1. A route with no budget left ends the run `failed` with "fix-loop limit of 10 reached" (a Critic send-back at that point is handled by Task 14). The free-restart log line names the pass.

- [ ] **Step 1: Write the failing tests:** `test_attempt_dirs_of_two_passes_coexist`; `test_transcript_lines_carry_pass_no`; `test_route_back_reruns_every_later_stage` (fake stages: solver routes to mesh; mesh, case_setup, solver_runner run again in order); `test_route_back_moves_downstream_outputs_aside`; `test_fix_loop_budget_ends_run_failed`; `test_route_to_solver_runner_or_forward_is_rejected`; `test_fix_request_in_opening_message_then_cleared`; `test_planner_receives_fix_request`; `test_in_place_target_seeds_from_canonical_files` (each stage's mapping); `test_routed_case_setup_gets_mesh_only` (canonical `case/` with a time directory and logs; the attempt has `constant/polyMesh` only); `test_case_setup_seed_after_mesh_change_is_rechecked`; `test_old_run_record_without_fix_loops_loads`; integration (scripted): `test_scripted_pipeline_recovers_through_route_to_case_setup` (a fake gate routes once; the scripted Case Setup fix passes; the run completes and the Scorer passes).
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** as in Interfaces.
- [ ] **Step 4: Run** unit and integration suites. Expected: PASS.
- [ ] **Step 5: Standard close.** Commit: `feat: cross-stage fix loops with numbered passes and a run-wide budget`.

### Task 12: Solver Runner convergence gate with steadiness and periodicity checks [cfd] [integration]

**Files:**
- Create: `src/cfdagent/tools/convergence.py`, `tests/unit/test_convergence.py`
- Modify: `src/cfdagent/agents/solver_runner.py`, `src/cfdagent/agents/loop.py` and `src/cfdagent/agents/stage.py` (diagnosis schema hook), `src/cfdagent/agents/casespec.py` (`periodic_outputs`), `src/cfdagent/prompts/planner.md` (one sentence), `src/cfdagent/agents/case_setup.py` and `src/cfdagent/tools/case_requirements.py` (criterion for steady cases), `src/cfdagent/executor/ops.py` and `src/cfdagent/executor/protocol.py` (op `convergence_report`; `time_dependence` passed to the completeness op), `src/cfdagent/prompts/{solver_runner,case_setup}.md`, `scripts/run_calibration.py` (`--gate`), `docs/adr/0004-no-solver-convergence-gate-in-m1.md` (Superseded by ADR-0006)
- Test: `tests/unit/test_solver_stage.py`, `tests/unit/test_case_setup_stage.py`, `tests/unit/test_casespec.py`, `tests/unit/test_planner.py`, `tests/fixtures/scripted/*.json` (CaseSpec replies gain `periodic_outputs`), `tests/integration/test_solver_stage.py`

**Interfaces:**
- Consumes: Task 2 (`read_function_object`, `dominant_frequency`), Task 3 (`scripts/run_calibration.py`), Task 11 (`RouteRequest`, `Stage.route_after_failure`).
- `CaseSpec.periodic_outputs: bool` (description: true when the requested outputs are statistics of a periodic unsteady regime, such as a shedding frequency or a period-averaged force; the Planner sets it; strict schema, no default keyword issues: covered by `test_casespec_schema_has_no_maps_or_tuples`).
- Executor side, pure numpy: `convergence.ConvergenceThresholds` (frozen dataclass; proposed `final_residual_max=1e-4`, `steady_window=0.1`, `steady_rel_change=1e-3`, `min_periods=10`, `periodic_rel_change=0.02`; `cfd-reviewer` fixes them in ADR-0006); `SteadinessReport(ok: bool, criterion: Literal["residual_control", "monitored_change", "none"], details: str)`; `steadiness(summary: SolverLogSummary, monitors: dict[str, FunctionObjectTable], thresholds) -> SteadinessReport` (ok when the solver stopped by `residualControl`, or when every monitored quantity changes by less than `steady_rel_change` over the last `steady_window` fraction of the run and the final initial residuals are below `final_residual_max`); `PeriodicityReport(ok: bool, n_periods: float, frequency: float | None, mean_change: float, amplitude_change: float, details: str)`; `periodicity(t, y, thresholds) -> PeriodicityReport` (ok when at least `min_periods` periods remain after the transient and the mean and amplitude of the last two halves differ by less than `periodic_rel_change`).
- Gate (design 6.2 row 5): `SolverRun.ok`; plus `steadiness(...).ok` when `casespec.time_dependence == "steady"` (also when solved with a transient solver; carry-over 10); plus `periodicity(...).ok` on a force-coefficient history when `casespec.periodic_outputs` is true. A transient case with `periodic_outputs` false gets no periodicity check. Every gate failure of a solver run that happened, divergence included, sets `retry_after_failure` to `False` and returns a route; the existing "the solver was not run" failure is still retried in the stage.
- Structured diagnosis: a new hook `Stage.diagnosis_schema() -> type[BaseModel] | None` (default `None`, today's free-text turn); when it returns a model, the loop makes the diagnosis turn one structured call with that schema and hands the parsed reply to the stage, which keeps it for `route_after_failure`. For the Solver Runner the diagnosis turn is one structured call with `response_schema=SolverDiagnosis(diagnosis: str, target: Literal["case_setup", "mesh"], instruction: str)` (no tools needed in that turn); an invalid or missing reply gives `target="case_setup"` with the gate errors as instruction. `route_after_failure` builds the `RouteRequest(origin="gate")` from it; the diagnosis text is still stored in `AttemptRecord.diagnosis`.
- Case Setup completeness (carry-over 4): a steady case must set `residualControl` (SIMPLE or PIMPLE dictionary) or, when solved with a transient solver, a function object that monitors at least one requested quantity.

- [ ] **Step 1: Write the failing tests:** `test_residual_control_met_is_steady`; `test_monitored_change_makes_time_marched_case_steady`; `test_unconverged_steady_run_routes_back` (gate fails, no retry, outcome route to `case_setup` with the diagnosis instruction); `test_divergence_routes_back`; `test_too_few_periods_is_not_periodic`; `test_growing_amplitude_is_not_periodic`; `test_clean_periodic_signal_is_periodic`; `test_aperiodic_transient_is_not_checked_for_periodicity`; `test_invalid_diagnosis_reply_defaults_to_case_setup`; `test_steady_case_without_criterion_fails_completeness`; `test_casespec_periodic_outputs_in_strict_schema`; integration `test_solver_stage_cavity_with_residual_control_passes`, `test_solver_stage_routes_unconverged_steady_case` (end time too short).
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** as in Interfaces; mark ADR-0004 superseded.
- [ ] **Step 4: Run** unit and integration suites. Expected: PASS. Run every calibration merged so far with `scripts/run_calibration.py --gate`: all pass. A calibration that fails the gate means a threshold is wrong: fix it with `cfd-reviewer` and record it in ADR-0006, never by changing the hand-written case to fit.
- [ ] **Step 5: Standard close.** Commit: `feat: Solver Runner convergence gate with steadiness and periodicity checks`.

### Task 13: CaseSpec check against the task, OOM as a gate failure [cfd]

**Files:**
- Modify: `src/cfdagent/agents/planner.py`, `src/cfdagent/sandbox/client.py`, `src/cfdagent/agents/loop.py` (error class), `src/cfdagent/orchestrator.py` (if the internal-error path needs it)
- Test: `tests/unit/test_planner.py`, `tests/unit/test_sandbox_client.py`, `tests/unit/test_agent_loop.py`

**Interfaces:**
- Carry-over 12: `planner.stated_reynolds(task_text: str) -> list[float]` finds numbers stated as the Reynolds number (patterns such as "Reynolds number ... is 100", "Re = 100", "Re 100"); `plan_gate` fails when the task states exactly one value and `reference.reynolds_number` differs from it by more than 0.1 %. Several or no stated values: no check (the Critic covers the rest, Task 14).
- Carry-over 13: a container killed by the out-of-memory killer (exit 137 with `OOMKilled` in `docker inspect`) becomes `AgentInputError("the executor ran out of memory (limit <memory>); reduce the problem size")`, so it costs the agent an attempt instead of aborting the run as an internal error.
- Deferred with reason (recorded in Task 20): the key-dimension check (`KeyDimension` has only a free-text name, so the gate cannot tell what to measure; the Critic checks dimensions against the task), rulings R25, R28, R31, R32 and R33 (residual geometry and mesh gate weaknesses: left to the Critic and to Task 17 observations), solver warning thresholds, `createPatch`, and a `run_utility` timeout parameter (added in Tasks 4-5 only if a calibration needs them).

- [ ] **Step 1: Write the failing tests:** `test_stated_reynolds_patterns`; `test_plan_gate_rejects_spec_reynolds_different_from_task`; `test_plan_gate_skips_when_task_states_several_values`; `test_oom_kill_is_an_agent_input_error`; `test_oom_costs_one_attempt_not_the_run`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** as in Interfaces.
- [ ] **Step 4: Run** the unit suite. Expected: PASS.
- [ ] **Step 5: Standard close.** Commit: `feat: check the stated Reynolds number and treat OOM as a gate failure`.

---

# Phase D: Critic and Scorer (ADR-0007)

### Task 14: Critic agent [cfd] [integration]

**Files:**
- Create: `src/cfdagent/agents/critic.py`, `src/cfdagent/prompts/critic.md`, `tests/unit/test_critic.py`, `tests/fixtures/scripted/critic_ok.json`
- Modify: `src/cfdagent/config.py` (`AGENT_NAMES` + `critic`, `RunLimits.max_critic_rounds: int = 2`), `src/cfdagent/record.py` (`CriticCheck`, `CriticVerdict`, fields), `src/cfdagent/orchestrator.py` (`CriticStep` as the last entry of the pipeline), `tests/fixtures/scripted/cavity_pipeline.json`, `scripts/replay_scripted_run.py`, `tests/unit/test_record.py`
- Test: `tests/unit/test_orchestrator.py`, `tests/integration/test_pipeline_scripted.py`

**Interfaces:**
- Produces (in `record.py`, a leaf module, to avoid an import cycle): `CriticCheck(name: str, passed: bool, evidence: str)`; `CriticVerdict(verdict: Literal["success", "partial", "failed"], checks: list[CriticCheck], reasons: list[str], send_back_to: Literal["planner", "geometry", "mesh", "case_setup", "analyst"] | None, instruction: str | None)` (no maps, no tuples; text fields capped and redacted like record errors); `RunRecord.critic_verdict: CriticVerdict | None = None` (replaces the M1 placeholder `None` type), `RunRecord.critic_rounds: int = 0`. `critic.run_critic(ctx: StageContext, task_text: str) -> StageOutcome` (route set when a send-back is followed, `origin="critic"`).
- Like the Planner: a read-only tool loop (`read_file`, `list_dir`, `calculator`; max 12 turns) over `task.md`, `casespec.json`, `boundary_manifest.json`, `mesh/` reports, `logs/solver_run.json` and the solver log tail, `analysis.md`, `results/quantities.json`; then one structured call returning `CriticVerdict`. It never reads `results/comparison.json`, `superseded/`, `benchmarks/` or other stages' transcripts (fresh context, design 6.3). Required checks, named in the prompt and general: requested outputs present with the requested names and normalisation; parameters (Reynolds number, fluid, dimensions, boundary conditions) match the task text, not only the CaseSpec (carry-over 12); convergence or periodicity evidence; conservation and plausibility.
- Guard on the reply: `success` with a failed check, or `send_back_to` without `instruction`, is invalid and costs an attempt (max 3); after 3 invalid replies, or a cost or wall-clock stop inside the Critic, `critic_verdict` stays `None` and the run continues to the Scorer. A send-back is followed only while `critic_rounds < max_critic_rounds` and the fix-loop budget is left; otherwise it is recorded in the verdict and the verdict is final. Each followed send-back is one fix loop.

- [ ] **Step 1: Write the failing tests:** `test_critic_schema_has_no_maps_or_tuples`; `test_success_with_failed_check_is_rejected`; `test_send_back_requires_instruction`; `test_critic_cannot_read_comparison_or_benchmarks`; `test_critic_send_back_becomes_route`; `test_critic_rounds_capped_then_final`; `test_send_back_without_fix_loop_budget_is_final_not_failed`; `test_critic_failure_leaves_verdict_none_and_run_continues`; `test_transcript_has_no_reference_values_with_critic`; `test_old_run_record_without_critic_fields_loads`; integration (scripted): `test_scripted_cavity_pipeline_with_critic_success`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** the Critic and write the prompt (`cfd-reviewer` checks it against the generality rule and design 6.3).
- [ ] **Step 4: Run** unit and integration suites. Expected: PASS.
- [ ] **Step 5: Standard close.** Commit: `feat: Critic agent with bounded send-backs`.

### Task 15: Scorer after the Critic's final verdict, Critic agreement

**Files:**
- Modify: `src/cfdagent/orchestrator.py` (`run_scorer`), `src/cfdagent/record.py` (`write_summary`, `critic_agreement`)
- Test: `tests/unit/test_orchestrator.py`, `tests/unit/test_record.py`

**Interfaces:**
- The Scorer runs once, after the Critic step, for benchmark runs only, when the Analyst passed in the **final** pass (quantities of an earlier pass were moved aside by Task 11 and are never scored), also when the verdict is `partial`, `failed` or `None` (design 9.2, Critic accuracy). It never starts a fix loop.
- `RunRecord.critic_agreement: bool | None = None`: `True` when (`verdict == "success"`) equals (`comparison["passed"]`), `None` when either is missing. `status` stays the pipeline status; the Critic verdict is reported beside it (ADR-0007 decides whether a `partial` status is added).
- `summary.md`: the Critic verdict with reasons and failed checks; the fix loops; for benchmark runs the comparison; the sentence of design 6.8 for runs without reference data; the M1 line "No Critic reviewed this run" only when `critic_verdict` is `None`.

- [ ] **Step 1: Write the failing tests:** `test_scorer_runs_after_critic_failed_verdict_when_final_analyst_passed`; `test_scorer_ignores_quantities_of_an_earlier_pass`; `test_scorer_never_starts_a_fix_loop`; `test_critic_agreement_values`; `test_summary_lists_critic_verdict_and_fix_loops`; `test_summary_no_reference_sentence_unchanged`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** as in Interfaces.
- [ ] **Step 4: Run** the unit suite. Expected: PASS.
- [ ] **Step 5: Standard close.** Commit: `feat: Scorer after the Critic's final verdict and Critic agreement`.

---

# Phase E: live development and acceptance

### Task 16: VM refresh and scripted replay of the M2 pipeline

**Files:**
- Create: `docs/runbooks/m2-runs.md`

- [ ] **Step 1: Owner decisions D6 and D7** are recorded (envelope, Task 17 cap). Then announce in chat (rule 4): VM start, host setup at the merged commit, image pull; cost VM time only (under $0.20).
- [ ] **Step 2:** `scripts/vm-host-setup.sh <commit> ...`, `verify_sandbox.py` (7 of 7 PASS), scripted replay (`completed`, Scorer passed, Critic `success`, upload present).
- [ ] **Step 3:** Read-only cost breakdown of the fixed $8.70/month, scoped to `rg-cfdagent` (Cost Management by resource); report to the owner with any saving that needs their decision.
- [ ] **Step 4:** Runbook with commands and timings; PR `docs: M2 runbook and scripted replay`. Deallocate the VM unless Task 17 follows immediately.

### Task 17: Live development series and general fixes

**Files:**
- Modify: prompts, notes and tool code only through general-rule fixes, each its own small PR with a test and `cfd-reviewer`; `docs/journal/` through `scribe`

- [ ] **Step 1: Repair-mode comparison on `cavity-re100`** (known reachable): 3 runs `--repair-mode fresh`, 3 runs `--repair-mode in_place`, same commit. Report completed runs, attempts per stage, fix loops, cost. A first observation, not a rate; the owner decides the mode for the acceptance series.
- [ ] **Step 2: One benchmark at a time, cheapest first:** `cavity-re1000`, `cylinder-re20`, `pipe-re500`, `cylinder-re100`, 2-3 runs each. Triage every failure by stage (prompt gap / tool or gate bug / model limitation, as in M1 Task 23); general fixes only; refresh the VM checkout after each merge. Note every trial run that hit the Courant limit of 10 (input to D9).
- [ ] **Step 3: Budget checkpoints:** report to the owner at half the approved cap, stop at the cap. `scribe` records each series with the run table (status, failed stage, attempts, fix loops, Critic verdict, Scorer result, tokens, cost, wall time).

### Task 18: Freeze the M2 candidate

- [ ] **Step 1:** All Track A and B PRs merged; CI green on `main`; the image of that commit published.
- [ ] **Step 2:** Owner decides D9 (Courant limit) on the Task 17 evidence; a change lands before the freeze as a normal PR.
- [ ] **Step 3:** Record the candidate commit, image tag and configuration (repair mode, limits, model, effort) in the journal; no code or prompt changes from here to the end of Task 19.

### Task 19: Acceptance series [owner gate]

- [ ] **Step 1: Owner gate (D8).** Present the done rule and the series: 3 unchanged runs each of `pipe-re500`, `cylinder-re20`, `cylinder-re100`, `cavity-re1000` and `cavity-re100` (regression), about 15 runs, about $4-6.6 (fewer if D6 cut it). Start only after the owner's yes.
- [ ] **Step 2: Run** the series on the frozen candidate as one background loop on the VM; nobody edits anything during the series.
- [ ] **Step 3: Result.** Under the proposed rule, M2 is done when each of `pipe-re500`, `cylinder-re20` and `cylinder-re100` has at least one completed run that passes the Scorer. Report per benchmark: passes out of n, errors against reference, Critic verdicts and agreement, fix loops, cost, wall time; `cavity-re1000` and `cavity-re100` in the same table. A benchmark with no pass is reported with its failure taxonomy (a valid research result); whether to iterate further is the owner's decision.
- [ ] **Step 4:** Show the owner the Scorer tables and plots (milestone approval point).

### Task 20: M2 close-out

**Files:**
- Modify: `README.md` (Validation: all five development benchmarks, n per benchmark, Critic present), `CLAUDE.md` (`--repair-mode`, calibration commands), `pyproject.toml`, `src/cfdagent/__init__.py`, `CITATION.cff`, `uv.lock`, `tests/unit/test_package.py` (version 0.2.0), `docs/design/m2-carryover-from-m1.md` (status table)
- Create: `docs/design/m3-carryover-from-m2.md`

- [ ] **Step 1:** Status table for carry-over 1-17: done (task), M3 (streaming convergence check, item 11; effort-high experiment, item 7, inside M3 matrix row 3; generality calibration and Task 7 if late), deferred with reason (the items listed in Task 13), or dropped with reason (item 9 unless blank replies return; item 16, the vendor report, is the owner's call).
- [ ] **Step 2:** Spend report: variable (run records, VM hours) and fixed (Cost Management, `rg-cfdagent` scope) against the $30 envelope.
- [ ] **Step 3:** `scribe` milestone entry; owner approves tag `v0.2.0` (D10); release notes state results honestly (compared with reference data, n per benchmark).
- [ ] **Step 4: Standard close.** Commit: `docs: M2 complete, validation update and M3 carry-over`.

---

## Self-review notes

- **Spec coverage (design 12, M2 row):** fix loops Tasks 11-12; Critic Task 14; Scorer after the Critic Task 15; knowledge base Task 10; generality set prepared, not run, Tasks 6-7; done criterion Task 19. Design 6.2 row 5 Task 12; 6.5 fix-loop limit Task 11; 6.6 notes Task 10; 9.2 Critic accuracy Task 15; 9.1 never run before the freeze Task 6 guard.
- **Carry-over coverage:** 1 Task 8 (ADR-0005); 2 Task 9; 3 Tasks 9, 10; 4 Task 12 (ADR-0006); 5 Tasks 11, 14 (ADR-0006, ADR-0007); 6 Tasks 3-5; 7 Task 9 (token limit) and M3; 8 Tasks 7, 10; 9 dropped unless blanks return; 10 Task 12; 11 M3; 12 Tasks 13, 14; 13 Task 13 (OOM; the rest deferred with reasons there; R38 is D9); 14 Tasks 17, 19; 15 Tasks 4, 5 (only what calibration needs); 16 owner's call, the probe script stays the early warning; 17 done in M1, repeated in Task 20.
- **Deliberate additions to the spec (for the owner):** `repair_mode` flag (ADR-0005); numbered passes with superseded outputs moved aside; a `calibration` pytest marker outside CI; `CaseSpec.periodic_outputs`; a structured Solver Runner diagnosis that names the stage to fix; the Scorer also scores runs the Critic did not accept; the Critic's own round limit of 2 inside the 10 fix loops; the `--allow-generality` guard.
- **Names checked across tasks:** `RunConfig.repair_mode`, `Stage.carry_over`, `Stage.seed_from_canonical`, `Stage.route_after_failure`, `STAGE_ORDER`, `ROUTE_TARGETS`, `RouteRequest`, `move_downstream_aside`, `StageOutcome.route`, `StageContext.fix_request`, `StageContext.pass_no`, `StageRecord.pass_no`, `RunLimits.max_fix_loops`, `RunLimits.max_critic_rounds`, `RunLimits.max_turns_per_attempt_by_stage`, `FixLoopRecord`, `CriticVerdict`, `RunRecord.critic_agreement`, `SolverDiagnosis`, `ConvergenceThresholds`, `CaseSpec.periodic_outputs`, `read_function_object`, `dominant_frequency`, `period_average`, `FoamResultsError`, `KnowledgeRoot`, `SourcedHit`, `Benchmark.task_set`, `scripts/run_calibration.py` are each defined once and used with the same names.
