# M1 carry-over from the M0 final review

Source: the final whole-branch review of M0 (2026-10-29, main 034cc5e). Its blocking findings were fixed on the `docs/m0-complete` branch (bounding-box gates, solver negative test and `--strict-markers`, infra defaults, documentation, hygiene). What remains is collected here as input for the M1 plan: sections A to F are code, test and infrastructure work in the recommended order, and S1 to S9 are the security gaps that must be closed before any LLM-authored script or dictionary is processed. Paths are relative to `src/cfdagent` unless they start with another directory. Findings marked "reproduced" were demonstrated against the real code in the review; "emulated" means not seen on the real VM.

## A. Tool-layer contracts (do first: every orchestrator call goes through them)

- **`run_tool` can still raise.**
  - A missing `cwd` raises `NotADirectoryError`; it should return `ok=False` (`tools/process.py:100-130`, reproduced).
  - `tail` is unbounded: a 3 MB single line gave a 3 MB tail, and `tail` is what is fed to an LLM. Cap it by bytes (`process.py:52-54`).
  - `test_timeout` should assert that the child process is gone; add `KeyboardInterrupt` cleanup.
- **Error channels.**
  - Failures arrive as `ToolResult`, `GateResult` or exceptions (`MeshError` from `apply_patch_types`, `ManifestError`, `ValueError` from parsers and post helpers, `FileNotFoundError` from `latest_time_dir`). The orchestrator needs one adapter and a rule for agent-caused versus programmer errors.
  - `GateResult` lives in `tools/geometry.py` and is imported by `tools/mesh.py:21`; move it to a neutral module.
  - `check_geometry` returns a `GateResult` but `check_mesh` returns a tuple; align them.
  - The mesh runner validates its output file, the geometry runner leaves that to the gate; align them.
- **Bounding-box logic.** The three copies were merged into `tools/bbox.py` in M0. Still open: decide the tolerance rule per axis with `cfd-reviewer`, because `rel_tol` times the largest extent lets a pipe-radius error of up to 10 % through (0.5 mm on a 5 mm radius).
- **Stale outputs.**
  - Unlink `geometry.step` and the manifest before a rerun, as `tools/mesh.py:219` already does for `mesh.msh`.
  - Use a fresh case directory per attempt: `latest_time_dir` picks the numerically largest time directory even if it comes from an earlier attempt.
- **Quantities and the scorer.**
  - `write_quantities` accepts any profile keys (`tools/post.py:97`) but the scorer needs exactly `x` and `value`; enforce the keys at write time (reproduced: `{"y","u"}` was written and the scorer failed later).
  - `tools/post.py:12` imports `Quantities` from `bench/scoring.py`; move the type to a neutral module so executor-side code never imports `cfdagent.bench` (makes blind evaluation lint-checkable).
  - `score()` raises `OverflowError` on a 400-digit JSON integer (`bench/scoring.py:128`, reproduced), breaking its "never raises" contract; also catch `OverflowError` and `csv.Error`.
  - `Benchmark.root` stays relative (`bench/registry.py:122`), so scoring depends on the cwd; resolve it.

## B. Gates against design section 6.2

- **Geometry gate** (`tools/geometry.py:64-70`).
  - It only parses the manifest; `patches: []` passes. Locator coverage is first checked in the agent-written mesh script, so a bad manifest is blamed on the mesh agent. Add the coverage check at the geometry stage by reusing `match_surfaces`.
  - Add an expected-volume check: a bounding box cannot see a wrong cylinder diameter.
- **Mesh gate** (`tools/mesh.py`).
  - It checks names, types and non-empty patches, not placement: a script that assigns the right names to the wrong faces (inlet/outlet swap) passes. Check each patch's face centres against its locators.
  - "One cell thick" is reduced to `solution_directions == 2` (`mesh.py:503-507`). Add `sum(empty-patch faces) == 2 x n_cells` and a one-layer assertion in the cylinder test.
  - Quality thresholds are checkMesh's built-ins only; parsed values are never gated, and `max_aspect_ratio` is NaN when checkMesh prints it with a colon. Gate them, with NaN counted as a failure.
  - `_parse_patch_table` counts dict keys, not rows: two patch names of 20 or more characters that end in a digit and share a prefix collide and are falsely rejected. Add a gate-level test.
- **Skeleton files.**
  - `convert_to_foam` leaves skeleton `controlDict`, `fvSchemes` and `fvSolution` in the case (`mesh.py:243-267`). If Case Setup forgets `controlDict` but writes valid `fvSchemes` and `fvSolution`, `icoFoam` runs one step with `deltaT` 1, prints `End`, and `completed=True`. Build the skeletons in a scratch copy or delete them after `check_mesh`.
  - The solver gate must also compare `end_time` with the case's `endTime`; the end-to-end test has the same hole.

## C. Solver (`tools/foam.py`)

- **`_NON_SOLVERS` fails open** (`foam.py:23-36`, `166-171`): any executable added to the allowlist, such as `foamToVTK`, silently becomes a valid "solver". Replace it with an explicit `SOLVERS` set and test that `ALLOWED == SOLVERS | NON_SOLVERS`.
- **Parallel path.**
  - `mpirun -np N` (`foam.py:252`) fails when N exceeds the core count (OpenMPI 4.1.6 reports "not enough slots"; reproduced with 2 CPUs, emulated for the D4as_v4 with 2 cores and SMT, where `-np 3` or `-np 4` is expected to fail). Add `--use-hwthread-cpus` and verify with `mpirun -np 4 hostname` on the VM.
  - No test covers the parallel path with real OpenFOAM (2 and 4 ranks worked by hand, about 1 s on a coarse mesh). Add an integration test and the missing case "`mpirun` exit 0 plus bad log".
- **Return value.** Return a `SolverRun` (solver log path plus parsed summary) instead of the `reconstructPar` `ToolResult`; put `failure_reason` into the log tail, as the mesh stage does.
- **Convergence gate.** Add a finite-blow-up check (residual 1e16 then `End` is not flagged today) and a streaming log parse before M3.

## D. LLM client (`llm/client.py`)

- **Response handling.**
  - Expose `finish_reason`: reasoning models spend `max_completion_tokens` on reasoning, so a truncated structured reply surfaces as a bare `StructuredOutputError`.
  - Raise a typed error on empty `choices` (`client.py:153`).
  - Missing `usage` must become `None`, not 0 (`client.py:170-174`), because the $1-per-run cap depends on it.
  - Add a helper that echoes assistant tool-call messages back.
- **Schema.** `strict_schema` rewrites `dict[str, X]` to `additionalProperties: false` (`client.py:82-84`), so the model can only emit `{}`; raise on maps in LLM-facing schemas. Test that `Optional` stays nullable, and check `default` keywords against Foundry with the real `CaseSpec`.
- **Tests.** Cover `APIStatusError`, 429 and retries, `api_key` mode and token refresh; record one or two real responses as fixtures.
- **Llama.** Make one Llama-3.3-70B call from the VM identity (cents) to prove the OpenAI-User role covers it, before the redeploy that turns key auth off (see S7).

## E. Infra, CI and image

- **Daily shutdown.** The forced 23:00 shutdown kills `--detach` runs and campaigns; add a deferral before any unattended run.
- **Image.** Add a runtime stage without uv, pytest or pre-commit. Anchor the `.dockerignore` patterns.
- **CI.**
  - Run `pre-commit run --all-files` in the lint job: `mixed-line-ending`, `check-yaml`, `check-toml` and shellcheck for `docker/*.sh` and `scripts/*.sh` currently run only locally.
  - Found while closing M0: `end-of-file-fixer` rewrites the captured logs `tests/fixtures/logs/icoFoam_ok.log` and `icoFoam_fatal.log` (no trailing newline). Exclude `tests/fixtures/logs/` from that hook before running it in CI. The `shellcheck` hook also needs a download that fails behind a TLS-intercepting proxy.
  - Add `infra-lint` to the required checks and to `publish.needs`.
  - Run the integration job under the hardened flags from S4, so every PR proves the sandbox profile.
- **Settings.** `.claude/settings.json:62` and `:72` deny `docker *--pid*`, which also blocks `--pids-limit`; narrow the rule to `--pid=host`.
- **Dependabot.** Ignore tag bumps of the `openfoam-default` base image: the engine version is part of experiment reproducibility.
- **Version drift.** Add a drift test across `pyproject.toml`, `__init__` and `CITATION.cff`.

## F. Benchmarks, tests and docs

- **Calibration.**
  - Only `cavity-re100` has a hand-written solution that passes the scorer. `cavity-re1000`, `pipe` and the two cylinder cases are unproven reachable, and the cylinder references come from secondary tables, so agent failures there would be uninterpretable. Calibrate each with a hand-written solution before the first agent run on it.
  - For the pipe, add a `task.md` versus `make_reference.py` parameter-sync test, and note the kinematic-pressure trap (the Analyst must multiply p by rho).
- **Generality lint.** The knowledge-directory test is vacuous (`tests/unit/test_registry.py:84-91`). Extend it to `prompts/` and add a banned-word lint over `src/` (cavity, pipe, cylinder, ghia) so the generality rule is machine-checked.
- **Test hygiene.**
  - Move the duplicated PYTHONPATH fixture (`tests/integration/test_mesh.py:41-48`, `test_handwritten_cavity.py:33-39`) into `tests/integration/conftest.py`.
  - In the end-to-end test add a solver timeout, show `.tail` on failure, and tie the sampled profile to `end_time`.
- **Units.** Make `CaseSpec` SI metres and let the orchestrator compute `expected_bbox`: the two bbox gates detect only disagreement between geometry and expectation, not a consistently wrong expectation.

## Security posture for M1

Verified in the review (local, no network, throwaway containers):

- The allowlist is not a confinement: `run_tool(["mpirun","-np","1","bash","-c","id"])` ran bash as uid 1000, and `python` and `mpirun` are both allowlisted.
- `#codeStream` is live in the image: `$WM_PROJECT_DIR/etc/controlDict` has `allowSystemOperations 1`, and a `#codeStream` block read by `foamDictionary` compiled and executed C++ as uid 1000. Setting `allowSystemOperations 0` in a user-level or site-level `controlDict` made OpenFOAM refuse it.
- The image has no passwordless sudo for `cfd`, but still contains setuid `su`, `sudo` and `mount`, plus gcc and uv.
- The hardened profile works: the full integration suite passes under `--network none --cap-drop ALL --read-only`. Not yet tested: `no-new-privileges`, `--pids-limit`, `--memory`.

Gaps to close before any LLM-authored script or dictionary is processed:

- **S1. Make `run_tool` the sandbox boundary.** Run every external execution, gates included, in executor containers. `checkMesh` and `foamDictionary` read case dictionaries (a `#codeStream` would run inside the orchestrator), and `check_geometry` parses an agent-written STEP with OCC. The orchestrator should parse agent-authored files only with pure-Python, size-limited code, in a process separate from the executors (it needs IMDS for Foundry and Blob).
- **S2. Block IMDS (169.254.169.254) and the Azure WireServer (168.63.129.16, except DNS)** from every container that has networking. Use `--network none` for executors plus a DOCKER-USER firewall rule as a second layer. The VM identity can write all blobs, call Foundry and deallocate the VM. Docker-bridge reachability of IMDS is the default but was not tested; confirm once on the VM.
- **S3. Scrub the environment.** `run_tool` passes `os.environ` through (`tools/process.py:124`); pass an explicit allowlist.
- **S4. Container flags and cleanup.**
  - Use `--cap-drop ALL`, `no-new-privileges`, `--read-only` with tmpfs, and pids, memory and CPU limits.
  - Fix the timeout kill: `_kill_tree` would kill the `docker run` client but not the container, so name containers and `docker kill` them on timeout.
  - Both sides must use uid 1000 (the VM's `azureuser` already does).
- **S5. Treat the shared run directory as hostile.** Refuse symlinks when uploading or reading, resolve paths inside the run directory, and never mount the repo root or `benchmarks/` into executors.
- **S6. Image.** Set `allowSystemOperations 0` site-wide with a test that `#codeStream` is refused; build a runtime stage without uv, dev tools or compilers where possible; add a code comment that the allowlist is not a security boundary.
- **S7. Entra-only on the AI account.** The Bicep default is now `disableLocalAuth = true`, but the deployed account still has keys. Order: Llama call from the VM identity (D), owner-approved redeploy (pass `budgetStartDate=2026-10-01T00:00:00Z` if after 1 November), then verify. Also scope the VM's Storage Blob Data Contributor role to the `runs` container.
- **S8. SSH** is public by owner decision, key only plus fail2ban. Add `MaxAuthTries 3` and `AllowUsers azureuser` by `scp` of a drop-in (`customData` is immutable) and watch `journalctl -u ssh`. The `docker` group is root-equivalent, so the key passphrase is the real control.
- **S9. Spend and supply chain.**
  - Budget alerts only notify; enforce the $1-per-run cap in the orchestrator using real usage numbers (see D).
  - GitHub Actions are pinned by tag only.
  - The GHCR image is public; the Dockerfile copies only `src`, `pyproject.toml`, `uv.lock`, `README` and `LICENSE`, so it holds no secrets.
