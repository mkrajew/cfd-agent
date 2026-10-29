# M1 First Autonomous Run Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the M1 runtime: sandboxed executors, an orchestrator that drives agents 1-6 through the gated pipeline, the CLI, Blob upload and the run record. M1 is done when the cavity Re = 100 task goes from its text to results on the Azure VM with no human intervention and the deterministic Scorer passes.

**Architecture:** The orchestrator is plain Python on the VM host with no CadQuery, Gmsh or OpenFOAM. It parses agent-written files only with size-limited pure-Python code. Every execution of agent-authored content (scripts, dictionaries, STEP, solver) happens in a short-lived executor container from the same image (`--network none`, no capabilities, read-only root, only the run directory mounted), reached through one operation protocol. Each agent works in a tool loop; after each attempt a deterministic gate decides, and gate errors go back to the agent (max 3 attempts per stage). Tool-layer contracts and security gaps from the M0 review land first, before any LLM output is executed.

**Tech Stack:** Python 3.12, uv, pydantic v2, typer, openai SDK + azure-identity + azure-storage-blob, zstandard, Docker (sandbox), OpenFOAM ESI v2512, Gmsh, CadQuery, pytest, ruff, GitHub Actions, Bicep.

**Spec:** `docs/design/2026-10-28-cfd-agent-design.md` (sections 6, 9.2, 12, 14). Inputs: `docs/design/m1-carryover-from-m0-review.md` (items A-F, S1-S9, referred to below as "carry-over A..F / S1..S9"), `docs/adr/0002-llm-capability-spike.md`, `docs/adr/0003-infrastructure-deviations.md`. Format model: `docs/superpowers/plans/2026-10-28-m0-foundations.md`.

## Global Constraints

- Everything in the repository is English. Claude talks to the owner in Polish.
- Python 3.12, `uv`, `ruff` line length 100, `pytest` with `--strict-markers`. Default tests are `tests/unit` and must need no Docker, no network and no real LLM.
- OpenFOAM **ESI v2512** pinned in the Dockerfile. CadQuery creates every geometry; no blockMesh path. 2D cases are one cell thick with `empty` front and back patches. **Max 4 MPI ranks.**
- Default run limits (spec 6.5, configurable): max 3 attempts per stage; wall-clock 2 h; max $1.00 LLM cost per run; max 4 MPI ranks. (The 10 cross-stage fix loops and the Critic belong to M2.)
- Models: `gpt-oss-120b` primary (reasoning effort `medium` default), `Llama-3.3-70B-Instruct` comparison without effort; Entra ID only; one OpenAI-compatible interface for both (ADR-0002). Prices: gpt-oss-120b $0.15 / 1M input and $0.60 / 1M output tokens.
- Run ID `YYYYMMDD-HHMMSS-<slug>`; Blob container `runs`, folder per run (spec 6.9); local copy under `~/runs/<run-id>/`.
- Generality rule: prompts, knowledge notes and tool code contain only general CFD/OpenFOAM knowledge; `src/` never names a benchmark case (cavity, pipe, cylinder, ghia). Case-specific content lives in `benchmarks/` and `tests/fixtures/` only.
- Blind evaluation: agents never see `benchmarks/` reference data or criteria; the Scorer runs in the orchestrator after the last stage and can never trigger a retry.
- No secrets, keys, tokens, subscription IDs, IP addresses or e-mail addresses in the repository. No simulation results in git.
- Azure: **nothing is created, changed or deleted without the owner's explicit chat approval.** Inside the M0-M2 envelope (at most $30, about $0.6 spent) starting and stopping the project VM and running tests or benchmarks need no extra approval. M1 budget cap: **$10** (VM + LLM); report spend to the owner at $5 and stop at $10.
- Workflow: conventional commits; one branch per task (`feat/m1-<slug>`, `fix/...`, `infra/...`, `docs/...`), stacked on the previous task's branch when it depends on it; every task is a PR with a description (what, why, how verified, Azure cost impact) and green CI. **The owner merges** (merge commits only). Do not push without being asked. Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`; PR descriptions end with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- Subagents run on Sonnet: implementers effort `high`, reviewers (`code-reviewer`, `cfd-reviewer`) effort `xhigh`. Azure CLI is not on PATH: `C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd`; run `az` from PowerShell; inspect the VM with `az vm run-command invoke --scripts @file`.

**Standard close** (the last step of every task; tasks only name the commit message): run `uv run pytest` and `uv run ruff check . && uv run ruff format --check .`; for tasks marked `[integration]` also run the integration suite in the image (see CLAUDE.md); run `code-reviewer` on the diff with the spec only (plus `cfd-reviewer` for tasks marked `[cfd]`); a `changes` or `reject` verdict blocks the PR; commit with the message given; write the PR description. Prompt text, knowledge notes and tool code are always `[cfd]`.

## File Structure

```
src/cfdagent/
  gates.py quantities.py errors.py rundir.py config.py record.py orchestrator.py cli.py __main__.py
  sandbox/   config.py docker.py client.py            host side: builds `docker run`, calls executor ops
  executor/  protocol.py ops.py __main__.py           container side: runs ops with cfdagent.tools
  tools/     (existing) process geometry mesh foam post bbox gmsh_helpers  + polymesh.py
  llm/       client.py config.py cost.py scripted.py
  agents/    loop.py tools.py stage.py casespec.py planner.py geometry.py mesh.py case_setup.py solver_runner.py analyst.py
  prompts/   <agent>.md + __init__.py (loader)
  knowledge/ search.py                                thin tutorial search (curated notes come in M2)
  storage/   blob.py
tests/unit/ tests/integration/ tests/sandbox/ (needs Docker + image) tests/fixtures/scripted/ (scripted LLM runs)
```

Import boundaries (machine-checked in Task 1): host-side packages (`agents`, `orchestrator`, `sandbox`, `storage`, `cli`, `rundir`, `llm`) never import `cfdagent.tools`, `cadquery`, `gmsh` or `pyvista`; container-side packages (`tools`, `executor`) never import `cfdagent.bench`, `agents`, `orchestrator`.

## Review Focus

The inputs and conditions the spec implies that a person is most likely to hit and that no obvious unit test covers; each has a test in the named task.

1. **Hostile or odd files in the run directory** (symlinks, FIFOs, huge files, paths that climb out): refused with a clear error, never followed, never uploaded. Task 7 (`test_symlink_refused_on_read_and_upload`, `test_fifo_skipped`), Task 13 (`test_write_file_rejects_escape_and_oversize`).
2. **Truncated, empty or malformed model replies and missing token counts** (reasoning eats the token budget, `choices` empty, no `usage`): a typed error that costs one attempt, never a crash, and the cost cap still holds. Task 11 (`test_finish_reason_length_is_typed`, `test_missing_usage_is_unknown_not_zero`, `test_cost_cap_with_unknown_usage`).
3. **Stale or skeleton files that make a bad run look good** (old `geometry.step`, an earlier attempt's time directory, skeleton `controlDict` running one step): fresh directory per attempt, no skeletons left in the case, `end_time` compared with `endTime`. Tasks 2, 4, 5.
4. **A run cut short** (wall-clock or cost limit, SSH drop, daily shutdown, executor killed): the record says why, partial results are still uploaded, `status` shows `interrupted` rather than a stuck `running`, no orphan containers. Tasks 7, 10, 12, 20, 21.
5. **A Planner output that omits something the task asked for** (an output identifier named in backticks, a wrong Reynolds number): deterministic gate on identifiers and on `Re = U L / nu` consistency, and the expected bounding box and volume come from the CaseSpec for both geometry and mesh gates. Task 14 (`test_planner_gate_requires_every_backticked_identifier`, `test_planner_gate_checks_reynolds_consistency`).

Known gap carried to M2: a consistently wrong but self-consistent CaseSpec is only caught by the Critic, which does not exist yet; the Scorer reveals it on benchmark runs.

## Owner touch points in M1

| When | What the owner does |
|---|---|
| Plan | Approves this plan (Polish summary) |
| Tasks 1-21, 24 | Merge each PR after a plain-language summary and CI status |
| Task 10 | Approves the Azure redeploy (S7) after seeing `what-if` and the cost estimate (expected: no new cost) |
| Task 22 | Nothing; the VM is started inside the envelope. Told what ran |
| Task 23 | Sees the first autonomous cavity results (milestone approval point); told at $5 spend |
| Task 24 | Approves the `v0.1.0` tag and release |

Expected M1 Azure spend: about $6 (VM about 15 h = $3; LLM about 20 live runs at $0.15 = $3), hard cap $10.

---

# Phase A: tool-layer contracts and gates (carry-over A, B, C)

### Task 1: Process runner hardening and neutral modules

**Files:**
- Modify: `src/cfdagent/tools/process.py`, `tools/geometry.py` (import), `tools/mesh.py` (import), `tools/post.py` (import), `bench/scoring.py` (import)
- Create: `src/cfdagent/gates.py`, `src/cfdagent/quantities.py`, `tests/unit/test_import_boundaries.py`
- Test: `tests/unit/test_process.py`

**Interfaces:**
- Produces: `cfdagent.gates.GateResult(ok: bool, errors: list[str], data: Any = None)` (frozen dataclass; moved from `tools/geometry.py`, no re-export); `cfdagent.quantities.Quantities` (moved type alias) and `REQUIRED_PROFILE_KEYS = ("x", "value")`; `process.TAIL_MAX_BYTES = 8000`; `process.SAFE_ENV_PREFIXES` / `SAFE_ENV_KEYS`.
- `run_tool` never raises for environment problems: missing `cwd` gives `ok=False` with the reason in the log and tail. It still raises `ToolNotAllowed` for a bad argv (a programmer error).

- [ ] **Step 1: Write the failing tests** in `tests/unit/test_process.py`: `test_missing_cwd_returns_not_ok` (ok False, returncode None, "cwd" in tail); `test_tail_is_capped_by_bytes` (a child printing one 3 MB line gives `len(tail.encode()) <= TAIL_MAX_BYTES`, tail ends with the end of the line); `test_timeout_kills_child_process` (child spawns `sleep`, writes its pid to a file; after the timeout `os.kill(pid, 0)` raises `ProcessLookupError` within 2 s); `test_keyboard_interrupt_kills_child_and_reraises` (monkeypatch `Popen.wait`); `test_environment_is_scrubbed` (with `AZURE_CLIENT_SECRET`, `CFDAGENT_LLM_API_KEY`, `IDENTITY_ENDPOINT`, `MSI_ENDPOINT` set in the parent, the child prints none of them; it does see `PATH`, and `PWD` equals `cwd`). In `tests/unit/test_import_boundaries.py`: `test_host_packages_do_not_import_tools_or_toolchain` and `test_container_packages_do_not_import_bench_or_orchestrator` (AST scan of `src/cfdagent`, passes vacuously for packages that do not exist yet).
- [ ] **Step 2: Run** `uv run pytest tests/unit/test_process.py tests/unit/test_import_boundaries.py`. Expected: the new tests FAIL.
- [ ] **Step 3: Implement.** Pass an explicit environment to the child: keys in `SAFE_ENV_KEYS` (`PATH`, `HOME`, `LANG`, `LC_ALL`, `TMPDIR`, `USER`, `LD_LIBRARY_PATH`) plus keys starting with `SAFE_ENV_PREFIXES` (`FOAM_`, `WM_`, `MPI_`, `OMPI_`, `PYTHON`, `VTK_`, `PYVISTA_`, `MPL`), then `PWD`. Cap the tail by bytes (keep the last bytes, drop a broken first line). Wrap the wait in `try/finally` so any exception, `KeyboardInterrupt` included, kills the process group before propagating. Add the comment from S6 to the module docstring: the allowlist is not a security boundary (`python` and `mpirun` can run anything); confinement is the sandbox of Task 7.
- [ ] **Step 4: Move `GateResult` and `Quantities`** to the neutral modules and fix every import; `tools/post.py` and `tools/*` must no longer import `cfdagent.bench`. Run the unit suite: PASS.
- [ ] **Step 5: Standard close.** Commit: `feat: harden the process runner and add neutral gate and quantity modules`.

### Task 2: Error channel, gate alignment and stale outputs [integration]

**Files:**
- Create: `src/cfdagent/errors.py`
- Modify: `tools/mesh.py`, `tools/geometry.py`, `tools/post.py`, `manifest.py`
- Test: `tests/unit/test_errors.py`, `tests/unit/test_geometry_runner.py`, `tests/integration/test_mesh.py`

**Interfaces:**
- Produces: `errors.AgentInputError(Exception)`: the base for failures caused by agent-authored content; `MeshError`, `ManifestError` and the new `PostProcessingError` derive from it. `errors.as_gate(fn: Callable[[], GateResult]) -> GateResult` turns an `AgentInputError` into `GateResult(False, [str(exc)])`; every other exception propagates and is a programmer error (the orchestrator will record it as `internal_error`). Rule stated in the module docstring.
- `check_mesh(...) -> GateResult` with `data=CheckMeshReport | None` (was a tuple). `check_geometry(...) -> GateResult`.
- `run_geometry_script` mirrors `run_mesh_script`: unlinks stale `geometry.step` and `boundary_manifest.json` first and returns `ok=False` (reason in log and tail) if the script exits 0 without writing both.
- `convert_to_foam` raises `MeshError` if `constant/polyMesh` already exists (a fresh case directory per attempt is the caller's job, Task 12).
- `tools.post.latest_time_dir` raises `PostProcessingError` instead of `FileNotFoundError`; update its callers and tests.

- [ ] **Step 1: Write the failing tests:** `test_as_gate_converts_agent_input_error`; `test_as_gate_propagates_programmer_error` (`KeyError` passes through); `test_geometry_script_exiting_zero_without_outputs_is_not_ok`; `test_geometry_runner_removes_stale_outputs`; `test_convert_to_foam_refuses_existing_polymesh`; update the mesh integration tests to the `GateResult.data` shape.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** as in Interfaces; update all callers and tests of the tuple return.
- [ ] **Step 4: Run** the unit suite and the integration suite in the image. Expected: PASS.
- [ ] **Step 5: Standard close.** Commit: `fix: one error channel, aligned gate results and no stale outputs`.

### Task 3: Geometry gate: manifest coverage, volume, per-axis tolerance [cfd] [integration]

**Files:**
- Modify: `tools/geometry.py`, `tools/bbox.py`, `tools/gmsh_helpers.py`
- Test: `tests/unit/test_bbox.py`, `tests/integration/test_geometry.py`

**Interfaces:**
- `check_geometry(workdir: Path, expected_bbox: tuple[float, ...], expected_volume: float | None = None, rel_tol: float = 1e-3) -> GateResult`.
- `bbox.compare_bbox` applies the tolerance **per axis** as `rel_tol * extent_of_that_axis` (extent 0 falls back to `rel_tol * largest extent`). `cfd-reviewer` confirms or replaces this rule; the decision and reason go in the PR description and the journal (carry-over A, bounding-box item).
- Volume check: the sum of `Volume()` of all solids equals `expected_volume` within `rel_tol` (skipped when `None`).
- Coverage check: import the STEP into a scratch Gmsh model, run `match_surfaces(manifest)`; a `ManifestError` (unmatched surface, doubly matched surface, patch matching nothing) is a gate error that names the geometry stage. `patches: []` is rejected.

- [ ] **Step 1: Write the failing tests** (unit): `test_per_axis_tolerance_rejects_small_radius_error` (box 0.01 x 0.01 x 1 with 10 % error in x passes today's rule, fails the new one); `test_zero_extent_axis_uses_largest_extent`. (Integration): `test_gate_rejects_empty_patch_list`, `test_gate_reports_unmatched_surface_at_geometry_stage`, `test_gate_rejects_wrong_cylinder_diameter_via_volume` (cylinder with the right bbox from a wrong diameter in a box-shaped bbox test: a cylinder of diameter 0.9 d inside the same bbox fails on volume), `test_gate_passes_handwritten_cavity_and_cylinder`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** as in Interfaces. Keep NaN/inf handling of M0.
- [ ] **Step 4: Run** unit and integration suites. Expected: PASS.
- [ ] **Step 5: Standard close.** Commit: `feat: geometry gate checks manifest coverage, volume and per-axis size`.

### Task 4: Mesh gate: placement, thickness, quality, skeletons [cfd] [integration]

**Files:**
- Create: `src/cfdagent/tools/polymesh.py`, `tests/fixtures/polymesh/box_one_cell/{points,faces,boundary,owner,neighbour}`, `tests/fixtures/logs/checkMesh_long_names.log`
- Modify: `tools/mesh.py`
- Test: `tests/unit/test_polymesh.py`, `tests/unit/test_checkmesh_parser.py`, `tests/integration/test_mesh.py`

**Interfaces:**
- `polymesh.read_patch_face_centres(case_dir: Path) -> dict[str, numpy.ndarray]` (pure Python parse of ASCII `points`, `faces`, `boundary`; shape `(n_faces, 3)`; raises `MeshError` for binary or malformed files). `polymesh.locator_contains(locator: Locator, point, tol: float) -> bool`.
- `MeshThresholds(max_non_orthogonality: float = 70.0, max_skewness: float = 4.0, max_aspect_ratio: float = 1000.0)` frozen dataclass; `check_mesh(case_dir, manifest, expected_bbox, rel_tol=1e-3, thresholds=MeshThresholds()) -> GateResult`. Values above a threshold, and NaN, fail. `cfd-reviewer` confirms the defaults.
- Placement: every face centre of a patch lies on at least one of that patch's locators (tolerance `1e-6` times the largest bbox extent).
- 2D thickness: `sum(faces of empty patches) == 2 * n_cells` in addition to `solution_directions == 2`.
- `max_aspect_ratio` printed with a colon is parsed (no NaN).
- `_parse_patch_table` counts table rows, not dict keys.
- `convert_to_foam` and `check_mesh` run the OpenFOAM utilities that need `system/` in a scratch directory holding a copy of `constant/polyMesh` plus skeleton `controlDict`, `fvSchemes`, `fvSolution`; the real case directory never receives skeleton files.

- [ ] **Step 1: Write the failing tests:** `test_inlet_outlet_swap_is_rejected` (unit, on the box fixture with swapped patch names), `test_face_centres_of_box_fixture`, `test_check_mesh_rejects_two_layer_2d_mesh` (integration, cylinder fixture with two layers), `test_check_mesh_passes_one_layer_cylinder`, `test_quality_threshold_exceeded_fails`, `test_nan_aspect_ratio_fails`, `test_aspect_ratio_with_colon_is_parsed`, `test_patch_names_with_shared_long_prefix_are_not_collapsed`, `test_case_dir_has_no_skeleton_files_after_check`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** as in Interfaces.
- [ ] **Step 4: Run** unit and integration suites. Expected: PASS, including the M0 hand-written cavity and cylinder cases.
- [ ] **Step 5: Standard close.** Commit: `feat: mesh gate checks patch placement, thickness and quality`.

### Task 5: Solver tool: explicit solvers, parallel path, SolverRun, trial run [cfd] [integration]

**Files:**
- Modify: `tools/foam.py`, `tools/process.py` (allowlist), `tests/integration/test_handwritten_cavity.py`
- Test: `tests/unit/test_solver_log.py`, `tests/unit/test_foam_allowlist.py`, `tests/integration/test_solver.py`

**Interfaces:**
- `foam.SOLVERS = frozenset({"icoFoam", "simpleFoam", "pimpleFoam"})`, `foam.NON_SOLVERS` (the other allowlisted names); a test asserts `ALLOWED_EXECUTABLES == SOLVERS | NON_SOLVERS`.
- `SolverRun(tool: ToolResult, solver_log: Path, summary: SolverLogSummary, case_end_time: float | None)` frozen dataclass; property `ok` is true only when the step succeeded, `summary.completed` and `summary.end_time` equals `case_end_time` (read with `foamDictionary -entry endTime -value`) within `1e-9` relative.
- `run_solver(case_dir, solver, n_procs=1, timeout_s=7200.0) -> SolverRun`; the failure reason is appended to the log and shown in `tool.tail`. Parallel: `mpirun --use-hwthread-cpus -np N ...`.
- `trial_run(case_dir: Path, solver: str, steps: int = 3, timeout_s: float = 300.0) -> SolverRun`: copies the case (without `postProcessing` and time directories except `0`) to a scratch directory, sets `endTime = startTime + steps * deltaT` with `foamDictionary`, runs serially.
- `parse_solver_log(text)` stays; add `parse_solver_log_file(path) -> SolverLogSummary` that streams lines. A residual or Courant number above `1e6` counts as `diverged` even if the log ends with `End`.

- [ ] **Step 1: Write the failing tests:** `test_allowlist_is_solvers_plus_non_solvers`; `test_unknown_executable_is_not_a_solver`; `test_residual_blowup_then_end_is_diverged` (log fixture); `test_parse_log_file_streams` (a 50 MB generated log parses with bounded memory is not asserted, only equality with `parse_solver_log`); `test_mpirun_exit_zero_with_bad_log_is_not_ok` (fake `run_tool`); `test_missing_controldict_does_not_complete` (skeleton `deltaT 1` regression, integration); `test_end_time_mismatch_is_not_ok`; `test_mpirun_np4_hostname` and `test_parallel_cavity_matches_serial` for 2 and 4 ranks (integration); `test_trial_run_does_not_touch_case_dir` and `test_trial_run_reports_fatal_error`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** as in Interfaces. In `tests/integration/test_handwritten_cavity.py` add a solver timeout, print `solved.tool.tail` on failure and tie the sampled profile to `end_time` (carry-over F, test hygiene).
- [ ] **Step 4: Run** unit and integration suites. Expected: PASS. Record in the PR whether `-np 4` worked on the 2-core CI runner.
- [ ] **Step 5: Standard close.** Commit: `feat: explicit solver set, hardened parallel path and trial run`.

### Task 6: Quantities and Scorer contracts [cfd]

**Files:**
- Modify: `src/cfdagent/quantities.py`, `tools/post.py`, `bench/scoring.py`, `bench/registry.py`, `benchmarks/README.md`
- Test: `tests/unit/test_post.py`, `tests/unit/test_scoring.py`, `tests/unit/test_registry.py`, `tests/unit/test_pipe_sync.py`

**Interfaces:**
- `quantities.validate_quantities(data: object) -> Quantities` (pure Python; raises `ValueError`): a dict of finite floats or profiles with exactly the keys `x` and `value`, equal lengths, at least 2 points. `post.write_quantities` calls it before writing.
- `score()` also catches `OverflowError` and `csv.Error`; `Benchmark.root` is resolved to an absolute path in `_load_one`.
- `benchmarks/README.md` gains the kinematic-pressure note: OpenFOAM incompressible solvers report p/rho, so a pressure drop must be multiplied by rho.

- [ ] **Step 1: Write the failing tests:** `test_write_quantities_rejects_profile_with_wrong_keys` (`{"y","u"}`); `test_validate_quantities_rejects_short_profile_and_bool`; `test_score_survives_400_digit_integer`; `test_score_survives_malformed_reference_csv`; `test_benchmark_root_is_absolute_after_chdir`; `test_pipe_task_matches_make_reference_parameters` (parses numbers from `task.md` and `make_reference.py`).
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** as in Interfaces.
- [ ] **Step 4: Run** the unit suite. Expected: PASS.
- [ ] **Step 5: Standard close.** Commit: `fix: enforce quantity keys at write time and harden the Scorer`.

---

# Phase B: sandbox, image and VM (carry-over S1-S9, E)

### Task 7: Sandbox library and executor protocol [integration] [cfd]

**Files:**
- Create: `src/cfdagent/rundir.py`, `src/cfdagent/sandbox/{__init__,config,docker,client}.py`, `src/cfdagent/executor/{__init__,protocol,ops,__main__}.py`, `tests/unit/test_rundir.py`, `tests/unit/test_sandbox_argv.py`, `tests/unit/test_executor_protocol.py`, `tests/sandbox/test_docker_mode.py`
- Modify: `pyproject.toml` (marker `sandbox`: needs Docker and the image; run with `uv run pytest -m sandbox tests/sandbox`)

**Interfaces:**
- `rundir.RunDir(root: Path)`: `path(rel: str) -> Path` (resolves inside `root`, raises `UnsafePath` for escapes and for any symlink component); `read_text(rel, max_bytes=1_000_000) -> str`; `read_json(rel, max_bytes=1_000_000) -> Any`; `write_text(rel, text, max_bytes=2_000_000)`; `iter_files() -> Iterator[Path]` (regular files only; symlinks, FIFOs and devices are skipped and counted in `skipped`); `attempt_dir(stage: str, attempt: int) -> Path` (fresh, created, under `attempts/<stage>-<n>/`); `promote(attempt_dir, canonical: str)` (symlink-free copy). `UnsafePath(AgentInputError)`.
- `sandbox.config.SandboxConfig(mode: Literal["docker", "local"] = "docker", image: str, uid: int, gid: int, memory: str = "8g", cpus: float = 4, pids: int = 512, tmpfs_size: str = "512m", shm_size: str = "512m")`; `uid`/`gid` default to the host user's (the container runs as them, so files in the run directory belong to the host user).
- `sandbox.docker.build_argv(config, name: str, run_dir: Path, op: str, request_rel: str) -> list[str]`: `docker run --rm --name <name> --network none --cap-drop ALL --security-opt no-new-privileges --read-only --tmpfs /tmp:rw,size=<tmpfs_size> --shm-size ... --pids-limit ... --memory ... --cpus ... --user uid:gid -v <run_dir>:/job:rw -w /job <image> python -m cfdagent.executor <op> <request_rel>`. Nothing else is mounted (no repo, no `benchmarks/`, no docker socket).
- `executor.protocol`: `OpRequest(op: str, args: dict)`, `OpResponse(ok: bool, data: dict | None, error: str | None)` (pydantic). Request and response travel as JSON files under `<run>/.sandbox/<call-id>.{request,response}.json`; the response is read by the host with `RunDir.read_json` and a size limit.
- `sandbox.client.SandboxClient(config: SandboxConfig, run_dir: RunDir, run_id: str)`: `call(op: str, args: BaseModel | dict, timeout_s: float) -> OpResponse`. A timeout runs `docker kill <name>` (never only kills the client); every call has a unique container name `cfd-<run_id>-<n>`; cleanup also runs on `KeyboardInterrupt`. In `local` mode it runs `python -m cfdagent.executor` as a subprocess without Docker (used in tests inside the image; no confinement).
- Executor ops in `executor.ops` (each takes pydantic args, returns plain data; all paths relative to `/job`): `run_script(script, cwd, timeout_s)`, `check_geometry(workdir, expected_bbox, expected_volume, rel_tol)`, `convert_mesh(msh, case_dir)` (gmshToFoam + `apply_patch_types`), `check_mesh(case_dir, expected_bbox, rel_tol)`, `check_dictionaries(case_dir)` (`foamDictionary` parse of every file in `0/`, `constant/`, `system/`), `trial_run(case_dir, solver, steps)`, `run_solver(case_dir, solver, n_procs, timeout_s)`, `run_utility(argv, cwd)` (allowlisted OpenFOAM utilities only), `search_knowledge`/`read_knowledge` (Task 17). An unknown op or invalid args gives `OpResponse(ok=False, error=...)`; `AgentInputError` becomes a gate-style failure; other exceptions give `error` with `internal=True`.

- [ ] **Step 1: Write the failing unit tests:** `test_path_escape_and_symlink_component_refused`; `test_read_text_enforces_max_bytes`; `test_fifo_skipped_and_symlink_skipped_by_iter_files`; `test_attempt_dir_is_fresh_each_time`; `test_promote_does_not_copy_symlinks`; `test_build_argv_has_every_hardening_flag` (asserts each flag from the list above, exactly one `-v`, and that the image is the last element before the command); `test_argv_mounts_nothing_but_run_dir`; `test_protocol_roundtrip_and_unknown_op`; `test_client_kills_named_container_on_timeout` (fake runner records `docker kill cfd-...`).
- [ ] **Step 2: Run them.** Expected: FAIL (modules missing).
- [ ] **Step 3: Implement** as in Interfaces. The executor `__main__` reads the request, calls the op, writes the response; it never reads outside `/job`.
- [ ] **Step 4: Write the sandbox tests** in `tests/sandbox/test_docker_mode.py` (marker `sandbox`; they use the real `docker run` against `cfdagent:dev`): `test_no_network` (a script opening a socket to a public IP and to 169.254.169.254 fails), `test_environment_has_no_credentials` (parent exports `AZURE_CLIENT_SECRET` and `CFDAGENT_LLM_API_KEY`; script prints its environment), `test_root_filesystem_is_read_only_and_cannot_gain_privileges` (write to `/etc` fails; `su`/`sudo` unusable), `test_cannot_see_repo_or_benchmarks`, `test_timeout_leaves_no_container` (`docker ps -q --filter name=cfd-` empty after the call), `test_codestream_dictionary_is_refused` (a `#codeStream` in `system/controlDict` makes `check_dictionaries` fail without executing it; this needs Task 8's image change, so mark `xfail(strict=True)` until then and remove the mark in Task 8), `test_parallel_run_in_sandbox` (`mpirun -np 4` on the cavity fixture). Run them: PASS except the xfail.
- [ ] **Step 5: Standard close.** Commit: `feat: sandbox executor containers and safe run-directory access`.

### Task 8: Image runtime stage and sandbox CI [integration] [cfd]

**Files:**
- Modify: `docker/Dockerfile`, `.dockerignore`, `.github/workflows/ci.yml`, `tests/sandbox/test_docker_mode.py` (remove the xfail), `tests/integration/test_image_toolchain.py`
- Create: `docker/etc-controlDict-override` (site-level `controlDict` setting `allowSystemOperations 0`)

**Interfaces:**
- Dockerfile gets two targets: `test` (today's content, with the `dev` group) and `runtime` (default `docker build` target stays `runtime`): no uv, pytest, pre-commit; no gcc where the OpenFOAM utilities do not need it (verify with the full integration suite; if `wmake`-free runs need none). `/etc/cfd/` holds the site `controlDict` override, installed where OpenFOAM reads site-level files (`$WM_PROJECT_SITE`/`$FOAM_SITE_ETC` equivalent; the implementer finds the exact path and records it). setuid helper binaries (`su`, `mount`, `sudo`) lose their setuid bit or are removed.
- CI: job `image` builds both targets (cache shared); `integration` runs the `test` target under `--network none --cap-drop ALL --security-opt no-new-privileges --read-only --tmpfs /tmp --pids-limit 512 --memory 8g`; new job `sandbox` (needs `image`) loads the `test` image on the runner host and runs `uv run pytest -m sandbox tests/sandbox`; `publish` pushes the `runtime` target and needs `[lint, infra-lint, unit, image, integration, sandbox]`. `.dockerignore` patterns are anchored (`/benchmarks`, `/tests`, `/docs`, `/infra`, `/.git`).

- [ ] **Step 1: Write the failing tests:** `test_codestream_refused_site_wide` (integration: `foamDictionary` on a dictionary with `#codeStream` fails with the "system operations" message and runs nothing); `test_runtime_image_has_no_uv_pytest_or_setuid_helpers` (checked by `docker run` against the `runtime` target in the `sandbox` job); remove `xfail` on `test_codestream_dictionary_is_refused`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** the Dockerfile and CI changes as in Interfaces.
- [ ] **Step 4: Build both targets and run** the integration suite under the hardened flags and the sandbox tests. Expected: PASS. Record both image sizes in the PR.
- [ ] **Step 5: Standard close.** Commit: `feat: runtime image target, codeStream disabled, hardened integration and sandbox CI`.

### Task 9: CI and repository hygiene (carry-over E, F) 

**Files:**
- Modify: `.github/workflows/ci.yml` (pre-commit in `lint`, action SHA pins), `.github/dependabot.yml`, `.pre-commit-config.yaml`, `.claude/settings.json`, `tests/integration/conftest.py`, `tests/integration/test_mesh.py`, `tests/integration/test_handwritten_cavity.py`
- Create: `tests/unit/test_version_drift.py`, `tests/unit/test_generality_lint.py`

**Interfaces:**
- `lint` runs `uv run pre-commit run --all-files`; `end-of-file-fixer` excludes `tests/fixtures/logs/`; the `shellcheck` hook downloads nothing (uses the runner's shellcheck, or is skipped in CI with the dedicated `infra-lint` step as before). Branch protection lists `infra-lint` (the owner applies it with `scripts/github-setup.sh`; Claude prepares the script change and asks).
- Every `uses:` in workflows is pinned to a commit SHA with the tag in a comment. Dependabot ignores tag bumps of `opencfd/openfoam-default`.
- `.claude/settings.json` denies `docker *--pid=host*` instead of `docker *--pid*`.
- `test_version_is_consistent` compares `pyproject.toml`, `cfdagent.__version__` and `CITATION.cff`.
- Generality lint (carry-over F): `test_src_has_no_case_names` fails if any file under `src/cfdagent` (code, prompts, knowledge) contains `cavity`, `pipe`, `cylinder`, `ghia` (case-insensitive, word-ish match), with an explicit allowlist of exact strings for legitimate general words (empty at the start, additions need `cfd-reviewer`); `test_knowledge_and_prompts_dirs_do_not_leak_benchmarks` replaces the vacuous knowledge-dir test.
- The duplicated PYTHONPATH fixture moves to `tests/integration/conftest.py`.

- [ ] **Step 1: Write the failing tests** (`test_version_is_consistent`, the two generality tests, `test_settings_deny_pid_host_only`).
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** the changes in Interfaces.
- [ ] **Step 4: Run** `uv run pre-commit run --all-files`, the unit suite and the integration suite. Expected: PASS.
- [ ] **Step 5: Standard close.** Commit: `chore: run pre-commit in CI, pin actions, lint generality, drop duplicated fixtures`.

### Task 10: VM host, firewall, SSH and deferral; Entra-only redeploy (S2, S7, S8, E) [infra] [owner gate]

**Files:**
- Create: `scripts/vm-host-setup.sh` (idempotent; run on the VM through `az vm run-command` or ssh), `infra/vm/cfdagent-iptables.service`, `infra/vm/10-cfdagent-ssh-extra.conf`, `tests/unit/test_daily_shutdown_deferral.py`, `docs/runbooks/vm-host.md`
- Modify: `infra/vm/idle-shutdown.sh` (new `--daily` mode), `infra/vm/daily-shutdown.service`, `infra/modules/roles.bicep` (Blob Data Contributor scoped to the `runs` container), `infra/README.md`

**Interfaces:**
- `scripts/vm-host-setup.sh <repo-ref>`: installs `python3-venv` and `git`, clones the public repo at `<repo-ref>`, creates `/opt/cfdagent-host` (`python3 -m venv`, `pip install .` without the `toolchain` extra), writes `/etc/cfdagent/env` with `CFDAGENT_LLM_ENDPOINT`, `CFDAGENT_STORAGE_ACCOUNT_URL`, `CFDAGENT_RUNS_DIR`, `CFDAGENT_IMAGE` (values passed as arguments, never committed), installs a `/usr/local/bin/cfdagent` wrapper that calls `/opt/cfdagent-host/bin/cfdagent` (it works once Task 21 is merged; Task 22 re-runs the script at the final commit), pulls the pinned image.
- Firewall (S2): systemd unit adds `iptables -I DOCKER-USER -d 169.254.169.254 -j DROP` and `-d 168.63.129.16 -j DROP` (except UDP/TCP 53), persistent across reboots; the runbook records the one-time check from a throwaway bridge container.
- SSH (S8): drop-in with `MaxAuthTries 3` and `AllowUsers azureuser`, installed with `scp`/run-command (customData is immutable); `sshd -t` before reload.
- Deferral (E): `/var/lib/cfdagent` is made writable by `azureuser`; `idle-shutdown.sh --daily` exits without deallocating while `/var/lib/cfdagent/active-run` exists and is younger than 4 h, logging "deferred"; the timer re-fires every 10 minutes in the 23:00-03:00 window (unit change in the same drop-in directory, applied by `scp` + `daemon-reload`). `cfdagent run` creates and removes that file (Task 20).
- Bicep: role assignment of the VM identity at container scope; `disableLocalAuth` already defaults to true.

- [ ] **Step 1: Write the failing test** `tests/unit/test_daily_shutdown_deferral.py` (uses the script's test hooks): `test_daily_defers_while_run_active`, `test_daily_forces_after_four_hours`, `test_daily_without_active_run_deallocates` (dry-run output).
- [ ] **Step 2: Run it.** Expected: FAIL.
- [ ] **Step 3: Implement** the script and Bicep changes. Run `shellcheck` and `az bicep build` (read-only).
- [ ] **Step 4: Owner gate: request approval for the redeploy.** Show the owner the `what-if` output (read-only `az deployment sub what-if`) and the cost estimate (expected change: none). If `what-if` shows the VM or its disk being replaced, stop and ask. After the owner approves, apply it, then verify read-only: the AI account has `disableLocalAuth=true`, keys listing fails, a `gpt-oss-120b` call through the VM identity still works, Blob upload to `runs` still works, a write to another container is denied. Pass `budgetStartDate=2026-10-01T00:00:00Z` if the date is after 1 November.
- [ ] **Step 5: Apply the VM host changes** (start the VM inside the envelope; run the setup script with the repo ref of the merged Task 9; scp the drop-ins), run the IMDS check from a bridge container (must fail to connect), then deallocate. Record every command and result in the PR and in `docs/runbooks/vm-host.md`.
- [ ] **Step 6: Standard close.** Commit: `infra: VM host setup, metadata firewall, SSH limits, shutdown deferral, container-scoped blob role`.

---

# Phase C: runtime

### Task 11: LLM client fixes, cost tracking and scripted backend

**Files:**
- Create: `src/cfdagent/llm/cost.py`, `src/cfdagent/llm/scripted.py`, `tests/fixtures/llm/` (two real recorded replies), `tests/unit/test_cost.py`, `tests/unit/test_scripted_backend.py`
- Modify: `llm/client.py`, `llm/config.py`, `tests/unit/test_llm_client.py`

**Interfaces:**
- `ChatResult` gains `finish_reason: str | None`; `usage: Usage | None` (missing usage is `None`, never 0). New typed errors (all subclass `LLMError`): `EmptyChoicesError`, `TruncatedOutputError(raw_text, finish_reason)` (structured reply cut by `finish_reason == "length"`), `StructuredOutputError` kept.
- `strict_schema` raises `SchemaError` for a `dict[str, X]` (any object schema with `additionalProperties` set to a schema) and keeps `Optional` nullable; test with a nested model.
- `ChatBackend` protocol: `chat(messages, model, *, tools=None, response_schema=None) -> ChatResult`; `LLMClient` implements it. `assistant_message(result: ChatResult) -> dict` builds the message that echoes tool calls back.
- `llm.cost.Pricing(input_per_m: float, output_per_m: float)`; `PRICING: dict[str, Pricing]` with `gpt-oss-120b = Pricing(0.15, 0.60)` (Llama's price is read from the Azure retail API at implementation time, read-only, and added; if it cannot be found the entry is absent and a run with that model refuses to start unless `--price-in/--price-out` are given). `CostTracker(limit_usd: float, pricing: Pricing)`: `add(usage: Usage | None, prompt_chars: int, completion_chars: int) -> None` (unknown usage is estimated as `chars / 3` tokens and flagged `estimated=True`), properties `total_usd`, `estimated: bool`, `tokens_in`, `tokens_out`; raises `CostLimitExceeded` when `total_usd > limit_usd`.
- `llm.scripted.ScriptedBackend(steps: list[ChatResult | Exception])` implements `ChatBackend` and records the calls; raises `AssertionError` when the script is exhausted. Used by every later unit test and by the VM dry run.

- [ ] **Step 1: Write the failing tests:** `test_finish_reason_length_is_typed`, `test_empty_choices_is_typed`, `test_missing_usage_is_unknown_not_zero`, `test_map_in_llm_schema_is_rejected`, `test_optional_stays_nullable`, `test_status_error_429_retries_then_raises` (fake `httpx2` transport), `test_api_key_mode_reads_env`, `test_token_provider_refreshes` (fake credential), `test_assistant_message_echoes_tool_calls`, `test_cost_tracker_adds_and_caps`, `test_cost_cap_with_unknown_usage`, `test_scripted_backend_replays_in_order`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** as in Interfaces. Record two real replies (one plain, one tool call) from `gpt-oss-120b` with the owner's `az login` as fixtures (cost: cents), scrubbed of ids.
- [ ] **Step 4: Run** the unit suite. Expected: PASS.
- [ ] **Step 5: Standard close.** Commit: `feat: typed LLM errors, unknown usage, cost tracker and scripted backend`.

### Task 12: Run configuration, run record and Blob upload

**Files:**
- Create: `src/cfdagent/config.py`, `src/cfdagent/record.py`, `src/cfdagent/storage/{__init__,blob}.py`, `tests/unit/test_config.py`, `tests/unit/test_record.py`, `tests/unit/test_blob.py`
- Modify: `pyproject.toml` (`zstandard`), `uv.lock`

**Interfaces:**
- `config.RunLimits(max_attempts_per_stage: int = 3, wall_clock_s: float = 7200, max_cost_usd: float = 1.0, max_ranks: int = 4, max_turns_per_attempt: int = 30, max_cells: int = 2_000_000, max_runtime_minutes: float = 90, max_memory_gb: float = 12)`; `config.RunConfig(limits: RunLimits, models: dict[str, ModelConfig], sandbox: SandboxConfig, benchmark_id: str | None, task_set: Literal["development", "generality", "ad hoc"] = "ad hoc")`; `RunConfig.default() -> RunConfig` (all six agents on `gpt-oss-120b`, `reasoning_effort="medium"`, `max_tokens=8192`; the Planner structured call 4096); `make_run_id(slug: str, now: datetime) -> str` giving `YYYYMMDD-HHMMSS-<slug>` (slug lowercased, `[a-z0-9-]` only, max 40 characters).
- `record.RunRecord` (pydantic; written atomically to `run_record.json` after every stage): `run_id, task_set, task_sha256, benchmark_id, started_at, finished_at, status ("running" | "completed" | "failed" | "interrupted"), failed_stage, failure_reason, models, prompt_versions (agent -> 12-hex hash), image_id, cfdagent_version, stages: list[StageRecord(name, attempts: list[AttemptRecord(n, ok, errors, seconds)], ok)], tokens_in, tokens_out, cost_usd, cost_estimated, wall_seconds, critic_verdict: None (M2), comparison: dict | None`. `record.write_summary(run: RunDir, record: RunRecord, comparison: dict | None)` writes `summary.md` containing either the Scorer comparison or the exact sentence "No reference data for this task; result assessed by physical checks only (convergence, mass balance, plausibility).", and a line that no Critic reviewed the run (M1).
- `storage.blob.upload_run(run: RunDir, account_url: str, credential: TokenCredential | None = None, container: str = "runs") -> UploadReport(uploaded: int, skipped: list[str], bytes: int)`: uploads every regular file under `runs/<run-id>/`, archives `case/` as `case.tar.zst`, refuses symlinks, caps single files at 100 MB (larger ones are skipped and listed), and never raises on a partial failure: it retries each blob 3 times and reports the failures. Uses `DefaultAzureCredential` by default.

- [ ] **Step 1: Write the failing tests:** `test_run_id_format_and_slug_sanitised`; `test_default_config_models_and_limits`; `test_record_roundtrip_and_atomic_write` (the file is never half-written: write to temp then replace); `test_summary_contains_no_reference_sentence_for_ad_hoc_run`; `test_summary_contains_comparison_for_benchmark_run`; `test_upload_skips_symlink_and_oversize` and `test_upload_archives_case_as_zstd` (fake container client); `test_upload_reports_failures_without_raising`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** as in Interfaces.
- [ ] **Step 4: Run** the unit suite. Expected: PASS.
- [ ] **Step 5: Standard close.** Commit: `feat: run configuration, run record, summary and Blob upload`.

### Task 13: Agent framework: tool registry, file tools, calculator, stage loop [cfd]

**Files:**
- Create: `src/cfdagent/agents/{__init__,tools,loop,stage}.py`, `src/cfdagent/prompts/__init__.py`, `tests/unit/test_agent_tools.py`, `tests/unit/test_agent_loop.py`
- Modify: `tests/unit/test_generality_lint.py` (now scans real prompts)

**Interfaces:**
- `prompts.load_prompt(agent: str) -> Prompt(text: str, version: str)` where `version` is the first 12 hex digits of the SHA-256 of the file.
- `agents.tools.Tool(name: str, description: str, args: type[BaseModel], handler: Callable[[BaseModel], str])`; `ToolRegistry.add(tool)`, `.schemas() -> list[dict]` (OpenAI function format), `.dispatch(call: ToolCall) -> str` (validation errors and `UNPARSED_KEY` arguments come back as text for the model; handler exceptions of type `AgentInputError` too; output capped at `MAX_TOOL_OUTPUT_BYTES = 8000`; other exceptions propagate).
- Built-in tools: `calculator(expression: str)` (AST-based evaluator: numbers, `+ - * / ** %`, parentheses, `sqrt sin cos tan log exp pi e`; no names, no calls beyond the whitelist, exponent and length limits); `read_file(path)`, `list_dir(path)`, `write_file(path, content)` restricted by a `FileScope(readable: list[str], writable: list[str])` of run-directory-relative glob patterns, through `RunDir` (no escape, no symlink, max 200 KB per write).
- `agents.stage.Stage` (ABC): `name`, `agent`, `scope: FileScope`, `system_prompt() -> str`, `opening_message(ctx) -> str`, `extra_tools(ctx, attempt_dir) -> list[Tool]`, `execute_and_gate(ctx, attempt_dir) -> GateResult`, `promote(ctx, attempt_dir) -> None`. `StageContext(run: RunDir, config: RunConfig, backend: ChatBackend, sandbox: SandboxClient, cost: CostTracker, record: RunRecord, transcript: TranscriptWriter, casespec: CaseSpec | None, clock: Callable[[], float])`.
- `agents.loop.run_stage(stage: Stage, ctx: StageContext) -> StageOutcome(ok: bool, attempts: int, errors: list[str], reason: str | None)`. Behaviour: attempt directory from `RunDir.attempt_dir`; messages start with the system prompt and the opening message; the agent loops with tools including a registered `check_stage()` tool that runs `execute_and_gate` and counts as one attempt (its result text is the gate's errors capped at 4 KB); the loop ends when a gate passes (the orchestrator then calls `promote`), when `max_attempts_per_stage` gate runs have failed, after `max_turns_per_attempt` model turns without a check, or when wall-clock or cost limits trip (reason recorded). If the agent replies without tool calls and without a passing check, the orchestrator runs the gate itself (one attempt) and sends the errors back. Every request and response is appended to `llm_transcript.jsonl` through `TranscriptWriter.append(stage, agent, attempt, model, new_messages, result)`. `TruncatedOutputError`, `EmptyChoicesError` and `StructuredOutputError` cost one attempt and are reported to the model.

- [ ] **Step 1: Write the failing tests** with `ScriptedBackend` and a toy stage: `test_calculator_evaluates_and_rejects_names_and_calls`; `test_calculator_rejects_huge_exponent`; `test_write_file_rejects_escape_and_oversize`; `test_write_file_respects_scope`; `test_dispatch_returns_validation_error_text`; `test_loop_passes_on_first_check`; `test_loop_feeds_gate_errors_back_then_passes`; `test_loop_stops_after_three_failed_attempts`; `test_loop_runs_gate_itself_when_agent_stops_calling_tools`; `test_loop_stops_at_turn_limit`; `test_truncated_reply_costs_one_attempt`; `test_cost_limit_stops_loop_and_is_recorded`; `test_wall_clock_limit_stops_loop`; `test_transcript_has_one_line_per_call`; `test_generality_lint_scans_prompts`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** as in Interfaces.
- [ ] **Step 4: Run** the unit suite. Expected: PASS.
- [ ] **Step 5: Standard close.** Commit: `feat: agent tool registry, calculator, scoped file tools and gated stage loop`.

### Task 14: CaseSpec and the Planner [cfd]

**Files:**
- Create: `src/cfdagent/agents/casespec.py`, `src/cfdagent/agents/planner.py`, `src/cfdagent/prompts/planner.md`, `tests/unit/test_casespec.py`, `tests/unit/test_planner.py`, `tests/e2e/test_casespec_schema_live.py`

**Interfaces:**
- `casespec.CaseSpec` (pydantic, SI units in metres, kilograms and seconds, **no maps and no tuples** so strict structured output works): `title, summary, dimensionality ("2D" | "axisymmetric" | "3D"), flow_regime ("laminar" | "turbulent"), time_dependence ("steady" | "transient"), fluid(density_kg_m3, kinematic_viscosity_m2_s), reference(length_m, velocity_m_s, reynolds_number: float | None), geometry(description, bbox(xmin, ymin, zmin, xmax, ymax, zmax), volume_m3: float | None, key_dimensions: list[KeyDimension(name, value_m)], thickness_m: float | None), boundaries: list[BoundarySpec(name, role, description)], mesh(target_cells: int, rationale), solver(suggested_solver, end_time_s: float, time_step_s: float | None), requested_outputs: list[OutputSpec(name, kind ("profile" | "scalar"), description)], assumptions: list[str], resource_estimate(cells: int, runtime_minutes: float, memory_gb: float)`. Methods `expected_bbox() -> tuple[float, ...]` and `expected_volume() -> float | None` are what the geometry and mesh gates receive.
- `planner.plan_gate(spec: CaseSpec, task_text: str, limits: RunLimits) -> GateResult`: errors when a backticked identifier of the task text is not an `OutputSpec.name`; when `reference.reynolds_number` differs from `velocity * length / nu` by more than 0.1 %; when the bbox has `min >= max` on any axis; when `end_time_s <= 0`; when the resource estimate exceeds the limits (message begins `exceeds resources:`; the orchestrator ends the run as `failed` at stage 1 without retry); when a 2D case lacks `thickness_m`.
- `planner.run_planner(ctx: StageContext, task_text: str) -> StageOutcome` (a function, not an `agents.stage.Stage`, because the Planner has no files or sandbox) does two phases: (1) a tool loop with `calculator` until the model answers (max 8 turns), (2) one call with `response_schema=CaseSpec` over the transcript of phase 1; on `StructuredOutputError`, `TruncatedOutputError` or a failed `plan_gate` the error is appended and the call repeats (each repeat is an attempt, max 3). On success `casespec.json` is written to the run directory and `ctx.casespec` is set.
- `prompts/planner.md` contains: role; the instruction to restate the task, fill missing parameters with sensible engineering defaults and record every one in `assumptions`; never do arithmetic in the head (use `calculator`); SI units; check the flow regime; estimate cells, runtime and memory and look for legitimate reductions first (symmetry, 2D, justified mesh); list outputs with the exact identifiers the user wrote; general rules only.

- [ ] **Step 1: Write the failing tests:** `test_casespec_schema_has_no_maps_or_tuples` (walks `model_json_schema()`); `test_expected_bbox_and_volume`; `test_planner_gate_requires_every_backticked_identifier`; `test_planner_gate_checks_reynolds_consistency`; `test_planner_gate_flags_resource_excess_with_prefix`; `test_planner_rejects_2d_without_thickness`; `test_planner_retries_after_invalid_reply_then_succeeds` and `test_planner_gives_up_after_three_attempts` (`ScriptedBackend`); `test_planner_writes_casespec_json`. The live schema test `tests/e2e/test_casespec_schema_live.py` (marker `e2e`, manual) sends the real `CaseSpec` strict schema to `gpt-oss-120b` and to Llama once and checks the reply validates (resolves the `default`-keyword question of carry-over D).
- [ ] **Step 2: Run the unit tests.** Expected: FAIL.
- [ ] **Step 3: Implement** as in Interfaces and write the prompt.
- [ ] **Step 4: Run** the unit suite: PASS. Run the live schema test once (cents); record the result in the PR.
- [ ] **Step 5: Standard close** (`cfd-reviewer` reviews the prompt against the generality rule). Commit: `feat: CaseSpec and Planner stage`.

### Task 15: Geometry agent and stage [cfd] [integration]

**Files:**
- Create: `src/cfdagent/agents/geometry.py`, `src/cfdagent/prompts/geometry.md`, `tests/unit/test_geometry_stage.py`, `tests/integration/test_geometry_stage.py`, `tests/fixtures/scripted/geometry_ok.json`

**Interfaces:**
- `geometry.GeometryStage(Stage)`: agent `geometry`; writable scope `geometry.py` and `*.md` notes inside its attempt directory; `extra_tools`: none beyond the built-ins. `execute_and_gate`: sandbox ops `run_script` (the agent's `geometry.py`, cwd = attempt dir, which must write `geometry.step` and `boundary_manifest.json`), then host-side size-limited parse of the manifest, then op `check_geometry` with `ctx.casespec.expected_bbox()` and `expected_volume()`; errors are the first 4 KB of the script tail plus gate errors. `promote` copies to `geometry/`.
- `prompts/geometry.md`: role; the exact output contract (file names, manifest schema with an example of the JSON shape, locator kinds, patch types); coordinates are numeric metres (1 unit = 1 m) because CadQuery writes millimetre-labelled STEP and Gmsh keeps the numbers; 2D cases are one cell thick, modelled as a thin extrusion of `thickness_m` with an `empty` patch for both faces; name every boundary face through locators; use only CadQuery and the standard library; print what was built.

- [ ] **Step 1: Write the failing tests:** unit with a fake `SandboxClient`: `test_gate_errors_include_script_tail`, `test_manifest_parse_is_size_limited`, `test_promote_copies_step_and_manifest`. Integration (local mode in the image, `ScriptedBackend` writing the hand-written cavity geometry script): `test_geometry_stage_passes_scripted_cavity`; `test_geometry_stage_recovers_after_wrong_size` (first script has the wrong side length, the second is right: 2 attempts).
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** the stage and prompt.
- [ ] **Step 4: Run** unit and integration suites. Expected: PASS.
- [ ] **Step 5: Standard close.** Commit: `feat: geometry agent stage`.

### Task 16: Mesh agent and stage [cfd] [integration]

**Files:**
- Create: `src/cfdagent/agents/mesh.py`, `src/cfdagent/prompts/mesh.md`, `tests/unit/test_mesh_stage.py`, `tests/integration/test_mesh_stage.py`, `tests/fixtures/scripted/mesh_ok.json`

**Interfaces:**
- `mesh.MeshStage(Stage)`: agent `mesh`; writable scope `mesh.py` in its attempt directory; inputs copied in: `geometry.step`, `boundary_manifest.json`. `execute_and_gate`: op `run_script` (must write `mesh.msh`, MSH 2.2 ASCII), then op `convert_mesh` into a fresh `case/` inside the attempt directory, then op `check_mesh` with the CaseSpec's expected bbox. `promote` copies the attempt's `case/constant/polyMesh` and the script to `mesh/` and `case/` (the case directory starts with the mesh only).
- `prompts/mesh.md`: role; contract (reads `geometry.step` and the manifest from the working directory; writes `mesh.msh` with `Mesh.MshFileVersion` 2.2 ASCII); how to use `assign_physical_groups(manifest)` from `cfdagent.tools.gmsh_helpers`; 2D: one recombined layer through the thickness; justify resolution from physics (cell Reynolds number, refinement near walls, boundary-layer resolution, aspect ratio and non-orthogonality limits from the gate); a coarse mesh must still resolve the quantities the task asks for; print cell count.

- [ ] **Step 1: Write the failing tests:** unit: `test_mesh_stage_gate_errors_are_fed_back`; integration with the hand-written cavity mesh script as scripted output: `test_mesh_stage_passes_scripted_cavity`; `test_mesh_stage_rejects_script_without_physical_groups` (error mentions `defaultFaces`).
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** the stage and prompt.
- [ ] **Step 4: Run** unit and integration suites. Expected: PASS.
- [ ] **Step 5: Standard close.** Commit: `feat: mesh agent stage`.

### Task 17: Case Setup agent, tutorial search and trial run [cfd] [integration]

**Files:**
- Create: `src/cfdagent/agents/case_setup.py`, `src/cfdagent/tools/case_check.py`, `src/cfdagent/knowledge/{__init__,search}.py`, `src/cfdagent/prompts/case_setup.md`, `tests/unit/test_knowledge_search.py`, `tests/unit/test_case_setup_stage.py`, `tests/integration/test_case_setup_stage.py`, `tests/fixtures/scripted/case_setup_ok.json`

**Interfaces:**
- `knowledge.search.search_tutorials(query: str, root: Path, max_hits: int = 10) -> list[Hit(path: str, line: int, text: str)]` (lexical: case-insensitive all-terms match over file names and contents, skipping files over 200 KB and binary files; deterministic ordering) and `read_tutorial(path: str, root: Path, max_bytes: int = 20000) -> str` (read-only, refuses anything outside `root`, symlinks included). The tutorial root is `$FOAM_TUTORIALS` inside the container; the ops `search_knowledge` and `read_knowledge` of Task 7 call these. Curated notes (`knowledge/notes/*.md`) and any benchmark-aware content are M2; nothing from `benchmarks/` is ever indexed.
- `case_setup.CaseSetupStage(Stage)`: agent `case_setup`; readable: the whole `case/` copy of the attempt; writable: `0/**`, `constant/**` except `polyMesh/**`, `system/**`; `extra_tools`: `search_knowledge(query)`, `read_knowledge(path)`, `validate_dictionaries()`. A fresh copy of `case/` (mesh only) is made per attempt. `execute_and_gate`: op `check_dictionaries`, a boundary-condition consistency check (every patch of `constant/polyMesh/boundary` has an entry in every field file of `0/`, `empty` patches have `type empty`; implemented in `tools/case_check.py` and run in the executor), then op `trial_run` (3 steps); the gate also requires `controlDict` to exist in the attempt (no skeleton can satisfy it, Task 4) and `endTime` to be above `startTime`. `promote` copies the case to `case/`.
- `prompts/case_setup.md`: role; search the tutorials for the closest case first and adapt; consistent units (kinematic viscosity, p/rho); time step from a Courant-number target computed with the calculator tool; write-interval choices; add function objects (`sets`, `forces`, probes) for every quantity in `requested_outputs`, sampled over the full line including the end points on boundaries; steady versus transient; `writeFormat ascii`.

- [ ] **Step 1: Write the failing tests:** `test_search_ranks_filename_and_content_hits`, `test_read_tutorial_refuses_escape_and_symlink`, `test_search_skips_large_and_binary_files`; `test_bc_consistency_flags_missing_patch_entry` (unit, fixture case); integration: `test_case_setup_stage_passes_scripted_cavity` (scripted writes of the hand-written dictionaries), `test_case_setup_rejects_missing_controldict`, `test_trial_run_failure_reaches_the_agent`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** the search, stage and prompt.
- [ ] **Step 4: Run** unit and integration suites. Expected: PASS.
- [ ] **Step 5: Standard close.** Commit: `feat: Case Setup agent stage with tutorial search and trial-run gate`.

### Task 18: Solver Runner stage [cfd] [integration]

**Files:**
- Create: `src/cfdagent/agents/solver_runner.py`, `src/cfdagent/prompts/solver_runner.md`, `tests/unit/test_solver_stage.py`, `tests/integration/test_solver_stage.py`, `tests/fixtures/scripted/solver_ok.json`

**Interfaces:**
- `solver_runner.SolverRunnerStage(Stage)`: agent `solver_runner`; no writable files; `extra_tools`: `run_solver(solver: str, n_procs: int)` (validated: `solver` in `SOLVERS`, `1 <= n_procs <= min(4, limits.max_ranks)`; calls op `run_solver` with the remaining wall-clock as timeout; returns the summary text and the log tail), `read_solver_log(offset_lines: int, n_lines: int)` (max 200 lines per call). The stage works on a fresh copy of `case/`. `execute_and_gate`: the gate is the result of the last `run_solver` call of the attempt (`SolverRun.ok`: completed, finite, `end_time == endTime`); no call means a gate error "the solver was not run". On failure the agent is asked for a short diagnosis; the diagnosis is stored in the run record (`AttemptRecord.diagnosis`) and the stage fails when attempts are used up. Routing back to Case Setup or Mesh is M2. `promote` copies the solved case to `case/` and logs to `logs/`.
- `prompts/solver_runner.md`: role; choose `n_procs` from the cell count (parallel only when it helps, at most 4); run once; read residuals and the Courant number from the log; on failure read the `FOAM FATAL ERROR` or divergence signs and write a concrete diagnosis and a specific fix; never edit the case (this agent has no write tools).

- [ ] **Step 1: Write the failing tests:** unit: `test_run_solver_tool_rejects_bad_arguments`, `test_stage_fails_when_solver_never_run`, `test_diagnosis_stored_in_record`; integration with the scripted cavity: `test_solver_stage_passes_scripted_cavity`, `test_solver_stage_fails_on_divergent_case` (a case with a huge `deltaT`).
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** the stage and prompt.
- [ ] **Step 4: Run** unit and integration suites. Expected: PASS.
- [ ] **Step 5: Standard close.** Commit: `feat: Solver Runner stage`.

### Task 19: Analyst stage [cfd] [integration]

**Files:**
- Create: `src/cfdagent/agents/analyst.py`, `src/cfdagent/prompts/analyst.md`, `tests/unit/test_analyst_stage.py`, `tests/integration/test_analyst_stage.py`, `tests/fixtures/scripted/analyst_ok.json`

**Interfaces:**
- `analyst.AnalystStage(Stage)`: agent `analyst`; readable: `case/` (solved), `logs/`, `casespec.json`; writable: `post.py`, `analysis.md` and `plots/*.png` in the attempt directory; `extra_tools`: `run_utility(argv: list[str])` (allowlisted OpenFOAM post-processing utilities such as `postProcess`, `foamListTimes`, executed in the sandbox with cwd in the case). `execute_and_gate`: op `run_script` (the agent's `post.py`, which uses `cfdagent.tools.post.write_quantities` and writes `quantities.json`); host-side `quantities.validate_quantities` on the file (size-limited read); every `OutputSpec.name` of the CaseSpec must be present with the right kind (profile or scalar); `analysis.md` must exist and mention convergence. `promote` copies results to `results/` (`quantities.json`, `plots/`) and `analysis.md`.
- `prompts/analyst.md`: role; compute every quantity with scripts, never by hand; use exactly the output names from the CaseSpec; profiles are `{"x": [...], "value": [...]}` in the units the task defines (normalisation stated in the task); read sampled function-object output if present, otherwise sample with utilities; sort by position; include end points on boundaries; report how steady the final state is (change of the profiles between the last two write times); check conservation and plausibility from physics only; produce matplotlib plots for humans (the model cannot see images); write a concise physical interpretation in `analysis.md`; remember that incompressible OpenFOAM pressure is p/rho.

- [ ] **Step 1: Write the failing tests:** unit: `test_gate_requires_every_requested_output`, `test_gate_rejects_wrong_kind_and_nonfinite`, `test_gate_requires_analysis_md`; integration with the scripted cavity (the hand-written `post.py` as the scripted output): `test_analyst_stage_passes_scripted_cavity` and `test_scored_quantities_pass_the_scorer` (the produced `quantities.json` passes `score()` for the benchmark; the Scorer is called by the test, not by the stage).
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** the stage and prompt.
- [ ] **Step 4: Run** unit and integration suites. Expected: PASS.
- [ ] **Step 5: Standard close.** Commit: `feat: Analyst stage`.

### Task 20: Orchestrator and core API [integration]

**Files:**
- Create: `src/cfdagent/orchestrator.py`, `tests/unit/test_orchestrator.py`, `tests/integration/test_pipeline_scripted.py`, `tests/fixtures/scripted/cavity_pipeline.json` (the concatenated scripted conversations)
- Modify: `src/cfdagent/record.py` (stage bookkeeping helpers if needed)

**Interfaces:**
- `orchestrator.run_task(task: str, config: RunConfig, *, backend: ChatBackend | None = None, run_root: Path | None = None, upload: bool = True) -> RunResult(run_id: str, status: str, run_dir: Path, record: RunRecord, comparison: dict | None)` (the core API; the CLI only calls this). Flow: create run directory and record; write `task.md`; hold `$CFDAGENT_STATE_DIR/active-run` (default `/var/lib/cfdagent`, the deferral file of Task 10; skipped with a warning when the directory is not writable) for the duration; run Planner, Geometry, Mesh, Case Setup, Solver Runner, Analyst in order; each stage updates the record and `status.json` (state, stage, pid, heartbeat) before and after; the first failed stage ends the run `failed` with `failed_stage` and `failure_reason` (an `exceeds resources:` Planner reason is a normal failed result); a limit hit ends it `failed` with the limit named; `KeyboardInterrupt`, `SIGTERM` and executor errors end it `interrupted` or `failed`; an uncaught exception ends it `failed` with `internal_error`. In `finally`: write `summary.md`, and upload everything produced so far when `upload` is true. The Scorer runs only after the Analyst gate passed, only when `config.benchmark_id` is set: `load_registry().get(id)`, `score(benchmark, quantities)`, written to `results/comparison.json` and to the record; its result never causes a retry.
- `backend=None` builds an `LLMClient` from the environment; tests pass a `ScriptedBackend`. `Pipeline` is a list of stage factories so M2 can insert the Critic and fix loops.
- Blind-evaluation guard: the orchestrator reads `benchmarks/<id>/task.md` as the task for `--benchmark` runs, hands only that text to agents, and opens reference files only inside the Scorer step.

- [ ] **Step 1: Write the failing tests:** unit with fake stages: `test_failed_stage_stops_pipeline_and_records_reason`, `test_exceeds_resources_is_a_normal_failure`, `test_cost_limit_ends_run_failed_with_partial_upload` (fake uploader sees the files), `test_keyboard_interrupt_marks_interrupted_and_still_uploads`, `test_active_run_file_removed_on_every_path`, `test_scorer_runs_only_for_benchmark_runs_and_never_retries`, `test_unhandled_exception_is_internal_error`, `test_status_json_heartbeat_updates_each_stage`, `test_transcript_contains_no_reference_data` (after a scripted run with a benchmark id, no line of `llm_transcript.jsonl` contains any value of the reference CSVs or the word "ghia"). Integration (local sandbox mode in the image, `ScriptedBackend` replaying the full cavity conversation, upload faked): `test_scripted_cavity_pipeline_passes_scorer` and `test_scripted_pipeline_survives_one_bad_geometry_attempt`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** as in Interfaces.
- [ ] **Step 4: Run** unit and integration suites. Expected: PASS; the scripted pipeline takes about 2 minutes.
- [ ] **Step 5: Standard close.** Commit: `feat: orchestrator and run_task core API`.

### Task 21: CLI

**Files:**
- Create: `src/cfdagent/cli.py`, `src/cfdagent/__main__.py`, `tests/unit/test_cli.py`
- Modify: `pyproject.toml` (`[project.scripts] cfdagent = "cfdagent.cli:app"`), `README.md` (Quick start)

**Interfaces:**
- Typer app with: `run [TASK_FILE] [--benchmark ID] [--model DEPLOYMENT] [--reasoning-effort low|medium|high] [--detach] [--no-upload]` (either a task file or `--benchmark`; `--benchmark` takes the task text from the registry and sets `benchmark_id`, `task_set="development"`); `status RUN_ID` (prints state, stage, cost; a record in `running` whose pid is gone or whose heartbeat is older than 5 minutes is shown as `interrupted`); `logs RUN_ID [--follow]` (tails `run.log`, which the orchestrator writes with stage events); `list` (recent runs with status and cost). `--detach` starts `python -m cfdagent run ... ` in a new session, redirected to `run.log`, prints the run id and returns. `--pause-after` is not in M1. `bench` comes with M3.
- The CLI reads `CFDAGENT_RUNS_DIR` (default `~/runs`) and keeps only the most recent 10 run directories locally after a successful upload (spec 6.9), never deleting a run whose upload failed.

- [ ] **Step 1: Write the failing tests** (typer `CliRunner`, backend and orchestrator faked): `test_run_requires_task_or_benchmark`, `test_run_benchmark_loads_task_and_sets_benchmark_id`, `test_status_marks_dead_pid_as_interrupted`, `test_logs_follow_stops_when_run_finished`, `test_list_shows_recent_runs`, `test_retention_keeps_latest_ten_and_failed_uploads`, `test_detach_returns_run_id_immediately`.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement** the CLI and README quick start (placeholder values only).
- [ ] **Step 4: Run** the unit suite. Expected: PASS.
- [ ] **Step 5: Standard close.** Commit: `feat: cfdagent CLI (run, status, logs, list)`.

---

# Phase D: the first autonomous run

### Task 22: Dry run and sandbox verification on the VM

**Files:**
- Create: `scripts/verify_sandbox.py` (run on the VM host; prints a pass/fail table), `scripts/replay_scripted_run.py` (calls `run_task` with a `ScriptedBackend`, the docker sandbox and real Blob upload), `docs/runbooks/m1-first-run.md`

**Interfaces:**
- Consumes: all merged PRs of Tasks 1-21; the image published by CI for the merged commit (`ghcr.io/mkrajew/cfd-agent:sha-<short>`); `scripts/vm-host-setup.sh` from Task 10.
- `verify_sandbox.py` runs through `SandboxClient` in `docker` mode: no network (public IP and IMDS unreachable), no credentials in the environment, read-only root, `#codeStream` refused, `mpirun -np 4` works with `--use-hwthread-cpus`, a timeout leaves no container, the run directory is the only mount.

- [ ] **Step 1: Start the VM** (`vm-up`, inside the envelope), update the host checkout and venv to the merged commit with `scripts/vm-host-setup.sh`, pull the pinned image.
- [ ] **Step 2: Run** `python scripts/verify_sandbox.py`. Expected: every row passes. Any failure blocks Task 23; fix it as a `fix:` PR.
- [ ] **Step 3: Dry run with the scripted backend** (`python scripts/replay_scripted_run.py tests/fixtures/scripted/cavity_pipeline.json`): expected status `completed`, Scorer passed, the run folder present in the `runs` container (check with the owner's data-reader role or `az storage blob list --auth-mode login`, read-only). Cost: VM time only.
- [ ] **Step 4: Write `docs/runbooks/m1-first-run.md`** with the exact commands, the observed timings and image size, and PR the scripts as `chore: M1 dry-run and sandbox verification`. Deallocate the VM unless Task 23 continues immediately.

### Task 23: First live runs and prompt iteration (cavity Re = 100)

**Files:**
- Modify: `src/cfdagent/prompts/*.md` and tool code only through general-rule fixes (each as its own small PR with `cfd-reviewer` approval); `docs/journal/` via `scribe`

**Interfaces:**
- Consumes: Task 22 results. Produces: the accepted M1 evidence in `docs/journal/2026-<date>-m1-first-run.md` and in the run folders in Blob storage (not in git).

- [ ] **Step 1: Live run 1** on the VM: `cfdagent run --benchmark cavity-re100` with `gpt-oss-120b`, medium effort; wait for the end (about 15-30 minutes expected). Download nothing to the laptop except `summary.md` and `run_record.json` through the owner's data-reader role.
- [ ] **Step 2: Triage every failure by stage** using the run's attempts, transcript and logs. Classify: (a) prompt gap (fix as a general rule in the prompt), (b) tool or gate bug (fix with a test first), (c) model limitation (record, do not work around with case-specific text). Each fix is a small PR through the normal flow; the generality lint and `cfd-reviewer` guard against cavity-specific wording. After each merge, refresh the VM checkout and repeat. Stop and report to the owner at $5 spent, hard stop at $10.
- [ ] **Step 3: Acceptance run.** On the final commit (clean `main`, image from CI) run the task once more with no edits between start and finish. Accept only if `status` is `completed`, no human touched anything during the run (the transcript and the record are the evidence), and `comparison.json` shows all criteria passed. Show the owner the profiles plot and the Scorer table (milestone approval point).
- [ ] **Step 4: Repeat data.** Run 4 more repeats on the same commit (not an acceptance gate; this is the first success-rate datapoint). Record per run: status, failure stage, attempts per stage, tokens, cost, wall time, error vs. reference. Report honestly if fewer than 5 of 5 pass; a partial result is a valid M1 outcome for the repeats, not for the acceptance run.
- [ ] **Step 5: Ask `scribe`** to record the session: decisions, obstacles, experiments with numbers and agent-behaviour observations, and to write an ADR if the run changed an architectural decision.

### Task 24: M1 close-out

**Files:**
- Modify: `README.md` (Quick start now real; Validation section states the cavity result with the comparison vs. Ghia, the number of repeats and that the Critic is not yet implemented), `CLAUDE.md` (key commands for the CLI and sandbox tests), `docs/design/m1-carryover-from-m0-review.md` (mark each item done or moved to M2)
- Create: `docs/design/m2-carryover-from-m1.md`

**Interfaces:**
- `m2-carryover-from-m1.md` lists what M1 deliberately left open: Critic and cross-stage fix loops; curated knowledge notes; calibration of `cavity-re1000`, `pipe-re500` and both cylinder cases with hand-written solutions (references from secondary tables, carry-over F) before their first agent run; the steadiness gate for time-marched steady cases; streaming convergence checks for M3; the known gap of a self-consistent but wrong CaseSpec; any new failure classes found in Task 23.

- [ ] **Step 1: Update the documents** above; verify the README quick start commands on the VM exactly as written.
- [ ] **Step 2: Report the spend** to the owner from the run records (LLM) and VM hours; compare with the $10 cap and the $30 M0-M2 envelope.
- [ ] **Step 3: Ask the owner to approve** the tag `v0.1.0` ("first autonomous cavity"); after approval prepare the release notes (the owner merges and tags).
- [ ] **Step 4: Standard close** (docs only; `scribe` entry for the milestone). Commit: `docs: M1 complete, README quick start and M2 carry-over`.

---

## Self-review notes

- **Spec coverage:** orchestrator, agents 1-6, CLI, Blob upload and run record are Tasks 20, 14-19, 21, 12 and 12. Gates of 6.2 stages 1-6 are Tasks 3, 4, 5, 14-19. Sandbox 6.7 is Tasks 7-8 and 10. Limits 6.5 are Tasks 12, 13, 20. Run artefacts 6.9 are Tasks 12, 20. Scorer link 6.8/9.2 is Task 20. Critic, fix loops, full knowledge base, other benchmarks are M2 by the spec and listed in Task 24. Carry-over: A in Tasks 1, 2, 3, 6; B in Tasks 3, 4, 5; C in Task 5; D in Task 11 and 14; E in Tasks 8, 9, 10; F in Tasks 6, 9, 14, 24; S1 Task 7; S2 Task 10 and 22; S3 Task 1; S4 Tasks 7-8; S5 Task 7; S6 Tasks 1, 8; S7 Task 10; S8 Task 10; S9 Tasks 9, 11, 20.
- **Deliberate small additions to the spec** (for the owner to see): the Scorer is wired in M1 (the done criterion needs it); per-stage retries (3 attempts) are in M1 because a single-shot LLM script is unrealistic; a thin tutorial search is in M1 because the Case Setup prompt requires it; failed attempts are kept under `attempts/` in the run folder.
- **Names checked across tasks:** `GateResult.data`, `SolverRun.ok`, `OpResponse`, `SandboxClient.call`, `RunDir.attempt_dir`, `CaseSpec.expected_bbox()`, `ScriptedBackend`, `CostTracker`, `run_stage`, `run_task` are defined once and used with the same signatures.
