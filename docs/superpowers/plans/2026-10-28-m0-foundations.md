# M0 Foundations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the M0 foundations: a public repository with CI, a Docker image holding the whole toolchain, deterministic geometry/mesh/solver/post tools with their gates, benchmark reference data and scoring, Azure infrastructure in Bicep, and a verified LLM client. M0 is done when a **hand-written** lid-driven cavity (Re = 100) goes CadQuery → Gmsh → OpenFOAM → post-processing and matches Ghia et al. (1982), locally and on the VM, with no LLM involved.

**Architecture:** One Python package `cfdagent` (src layout) holds deterministic tools that later milestones' agents will call. Each tool returns a typed result, and each gate returns `ok` + `errors`. Everything that needs CadQuery, Gmsh or OpenFOAM runs inside one Docker image. Pure-Python logic is unit-tested on the host. Infrastructure is Bicep, deployed only after owner approval.

**Tech Stack:** Python 3.12, uv, pydantic v2, numpy, typer, openai SDK + azure-identity, CadQuery, Gmsh (Python API), OpenFOAM ESI (pinned), PyVista, matplotlib, pytest, ruff, pre-commit + gitleaks, GitHub Actions, GHCR, Bicep, Azure CLI.

**Spec:** `docs/design/2026-10-28-cfd-agent-design.md` (approved 2026-10-28). Milestones M1–M4 get their own plans after M0. M1 depends on the LLM spike results from Task 11.

## Global Constraints

- Everything in the repository is English. Claude talks to the owner in Polish.
- Python 3.12, managed with `uv` (installed locally: uv 0.7.15). Lint and format with `ruff`. Tests with `pytest`.
- OpenFOAM **ESI (openfoam.com)**, latest stable release with an official Docker image, exact version pinned in the Dockerfile.
- CadQuery creates every geometry. There is no blockMesh path in the pipeline. blockMesh may be used only to generate test fixtures.
- 2D cases are one cell thick with `empty` front/back patches.
- Max 4 MPI ranks.
- Azure: subscription "Azure for Students", region `italynorth`, resource group `rg-cfdagent`, VM `Standard_F4s_v2`, Ubuntu 24.04.
- Keyless auth: managed identity / Entra ID. Fall back to an API key in Key Vault only if a model rejects Entra.
- **No Azure resource is created or changed without the owner's explicit approval.** Approved envelope for M0–M2 is ≤ $30. Inside it, VM start/stop and running tests/benchmarks need no per-action approval. Any campaign estimated above $5 needs approval.
- No secrets, keys, IP addresses or e-mail addresses in the repository. Parameters that need them read environment variables.
- No simulation results in git (`examples/` is curated later, not in M0).
- `benchmarks/` reference data and acceptance criteria must never be readable by agents during a run. This is enforced by the M1 sandbox. In M0, keep them out of `src/cfdagent/knowledge/`.
- Generality rule: prompts, knowledge notes and tool code contain only general CFD/OpenFOAM knowledge, never anything specific to a test case. Tool code in `src/` must not mention cavity/pipe/cylinder. Case-specific content lives only in `benchmarks/` and `tests/fixtures/`.
- Line endings LF in the repository (`.gitattributes`). Shell scripts run inside Linux containers.
- Conventional commits; branches `feat/…`, `fix/…`, `infra/…`, `docs/…`. From Task 4 on, every task lands as a PR that **the owner merges**.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. PR descriptions end with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- Repository `mkrajew/cfd-agent`, public. Image `ghcr.io/mkrajew/cfd-agent`.
- Azure CLI is not on PATH: `C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd`.

## Review Focus

1. **Units across STEP.** CadQuery writes STEP in millimetres and Gmsh keeps the numbers unless `Geometry.OCCTargetUnit` is set, so a domain meant as 1 m can silently become 0.001 m. The expected behaviour is that the mesh gate rejects any polyMesh whose bounding box differs from the expected one. The test is in Task 7 (`test_check_mesh_rejects_scaled_mesh`).
2. **A boundary face nobody named.** Expected: it never ends up in `defaultFaces`. The gmsh helper refuses to continue, and the mesh gate also rejects a `defaultFaces` patch. Tests in Task 5 (`test_unmatched_surface_raises`) and Task 7 (`test_check_mesh_rejects_default_faces`).
3. **Windows line endings in scripts mounted into Linux.** Expected: the repository enforces LF, and the image's scripts run from a Windows checkout. Test in Task 1 (`.gitattributes`, pre-commit `mixed-line-ending`). Task 4's CI runs integration tests from a checkout.
4. **Reference points outside the sampled range.** Expected: no silent extrapolation. `profile_error` raises instead of clamping. Test in Task 8 (`test_profile_error_rejects_reference_outside_sim_range`).
5. **Solver crash or divergence reported as success.** Expected: `FOAM FATAL ERROR`, NaN residuals or a missing `End` mark give `completed=False` with the reason. Tests in Task 9 (`test_parse_solver_log_fatal_error`, `test_parse_solver_log_nan_is_diverged`).

## Owner touch points in M0

| When | What the owner does |
|---|---|
| Task 1 | Gives the author name for `CITATION.cff` (or approves using the GitHub handle). |
| Task 3 | Confirms the first push to the new public repo. |
| Tasks 4–11 | Merges each PR (Claude gives a plain-language summary and CI status). |
| Task 8 | Approves the benchmark acceptance thresholds proposed by `cfd-reviewer`. |
| Task 10 | Approves the Bicep deployment after seeing `what-if` output and the cost estimate. |
| Task 12 | Approves deleting the probe resource group `rg-cfdagent-probe`. |

Expected M0 Azure spend: < $5 (a few VM hours, disk, spike calls of a few cents).

---

### Task 1: Repository bootstrap (local)

**Files:**
- Create: `pyproject.toml`, `uv.lock`, `.python-version` (`3.12`), `.gitignore`, `.gitattributes`, `.pre-commit-config.yaml`, `LICENSE` (MIT, year 2026), `CITATION.cff`, `README.md` (skeleton), `src/cfdagent/__init__.py`, `tests/unit/test_package.py`
- Keep: `docs/design/2026-10-28-cfd-agent-design.md`, `docs/superpowers/plans/2026-10-28-m0-foundations.md`

**Interfaces:**
- Produces: package `cfdagent` with `__version__: str = "0.0.1"`; uv dependency groups `dev` (pytest, ruff, pre-commit). Runtime deps: `pydantic>=2`, `numpy`, `typer`, `openai`, `azure-identity`, `azure-storage-blob`. Optional extra `toolchain` = `cadquery`, `gmsh`, `pyvista`, `matplotlib` (installed in the image, not required on the host).
- Pytest config: `testpaths = ["tests/unit"]` by default; markers `integration` and `e2e` registered.

- [ ] **Step 1: Ask the owner for the `CITATION.cff` author name** (or use handle `mkrajew`). Do not guess a name.
- [ ] **Step 2: `git init -b main` in `C:\Users\X\projects\cfd`** and write `.gitattributes` with `* text=auto eol=lf` plus `*.ps1 text eol=crlf`.
- [ ] **Step 3: Write the failing test** `tests/unit/test_package.py`:

```python
import cfdagent

def test_version_is_set():
    assert cfdagent.__version__ == "0.0.1"
```

- [ ] **Step 4: Run it to verify it fails.** Run: `uv run pytest`. Expected: FAIL (`ModuleNotFoundError` or missing attribute).
- [ ] **Step 5: Create `pyproject.toml`** (hatchling build backend, src layout, `requires-python = ">=3.12,<3.13"`, ruff line length 100, rule sets `E,F,I,UP,B`) and `src/cfdagent/__init__.py`. Then run `uv sync`.
- [ ] **Step 6: Run it to verify it passes.** Run: `uv run pytest`. Expected: `1 passed`.
- [ ] **Step 7: Configure pre-commit**: ruff (format + lint), gitleaks, `mixed-line-ending --fix=lf`, `end-of-file-fixer`, `check-yaml`, `check-toml`. Run: `uv run pre-commit run --all-files`. Expected: all hooks pass.
- [ ] **Step 8: Write the README skeleton**: one-paragraph overview ("describe a CFD task in plain English; agents build geometry, mesh, case, run it and report"), the architecture diagram copied from spec §5, sections Quick start (placeholder "available from v0.1.0"), Validation (placeholder), Roadmap (spec §13 items), License. `.gitignore` covers `.venv/`, `__pycache__/`, `runs/`, `*.msh`, `postProcessing/`, `processor*/`, `.env`.
- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "chore: bootstrap repository"
```

---

### Task 2: Claude Code project files and journal backfill

**Files:**
- Create: `CLAUDE.md`, `infra/CLAUDE.md`, `.claude/settings.json`, `.claude/agents/{implementer,code-reviewer,cfd-reviewer,infra-engineer,scribe,report-writer}.md`, `docs/journal/README.md`, `docs/journal/2026-10-27-design.md`, `docs/adr/0001-design-baseline.md`, `tests/unit/test_claude_agents.py`

**Interfaces:**
- Produces: six subagents named exactly as the file stems; journal entry format `## <YYYY-MM-DD> <kind>: <title>` with kind ∈ {decision, obstacle, milestone, experiment, observation}.

- [ ] **Step 1: Write the failing test** `tests/unit/test_claude_agents.py`:

```python
EXPECTED = {"implementer", "code-reviewer", "cfd-reviewer", "infra-engineer", "scribe", "report-writer"}

def test_all_agents_defined_with_frontmatter():
    agents = load_agent_frontmatter(Path(".claude/agents"))  # helper in the test module
    assert set(agents) == EXPECTED
    for name, fm in agents.items():
        assert fm["name"] == name
        assert len(fm["description"]) > 40
```

- [ ] **Step 2: Run it to verify it fails.** Run: `uv run pytest tests/unit/test_claude_agents.py`. Expected: FAIL (directory missing).
- [ ] **Step 3: Write the six agent files.** Instruction essentials come verbatim from spec §10, plus:
  - `code-reviewer` receives only the diff + spec, must give a verdict `approve | changes | reject`, and must not approve without reading tests.
  - `cfd-reviewer` enforces the generality rule (spec §6.3) and owns benchmark thresholds.
  - `infra-engineer` carries the §3 constraints and the az path, and never runs `az deployment … create`, `az vm create` or any resource-creating command without owner approval.
  - `scribe` writes entries in the journal format above.
  - `report-writer` follows the six-stage process in spec §11.
- [ ] **Step 4: Write `CLAUDE.md`** (100–150 lines) containing:
  - a project summary and repo map with pointers to the spec,
  - the language rule,
  - working rules: TDD, PR flow, the owner merges, no Azure spend without approval, no secrets, the generality rule, scribe at each milestone and session end,
  - key commands: `uv run pytest`, `docker build -f docker/Dockerfile -t cfdagent:dev .`, running integration tests in the image.

  `infra/CLAUDE.md` carries the subscription limits from spec §3, the allowed regions, mandatory shutdown and the cost-estimate rule.
- [ ] **Step 5: Write `.claude/settings.json`**: allow `uv run *`, `docker build *`, `docker run *`, `git *` except push; ask for every `az` command that contains `create`, `deploy`, `delete`, `update` or `start`, and for `git push`.
- [ ] **Step 6: Run the scribe agent to backfill the journal** from the design conversation (transcript: `C:\Users\X\.claude\projects\C--Users-X-projects-cfd\50461b8e-be1e-44e0-bf1b-6f9a50421a13.jsonl`). It records decisions and obstacles of 2026-10-27/28, such as GPU quota rejected, gpt-oss-20b not deployable, the pivot to Foundry, blind evaluation, no task tiers and the generality test set. Write ADR-0001 stating the approved design document as the architecture baseline.
- [ ] **Step 7: Run the tests to verify they pass.** Run: `uv run pytest`. Expected: all pass.
- [ ] **Step 8: Commit** with `git commit -m "docs: add Claude Code agents, project instructions and design journal"`.

---

### Task 3: Publish to GitHub and repository hygiene

**Files:**
- Create: `.github/workflows/ci.yml`, `.github/pull_request_template.md`, `.github/ISSUE_TEMPLATE/{bug.yml,feature.yml,config.yml}`, `.github/dependabot.yml`, `scripts/github-setup.sh`

**Interfaces:**
- Produces: CI workflow `ci` with jobs `lint` (ruff check + format --check, gitleaks) and `unit` (`uv run pytest`). These job names are required status checks. Task 4 adds `image` and `integration`.

- [ ] **Step 1: Write `ci.yml`.** Triggers: `pull_request` and `push` to `main`. Uses `astral-sh/setup-uv`, Python 3.12.
- [ ] **Step 2: Write the PR template** (sections: What, Why, How verified, Azure cost impact) and the issue forms (bug: expected/actual/run-id; feature: problem/proposal). `config.yml` disables blank issues.
- [ ] **Step 3: Write `dependabot.yml`** with weekly updates for ecosystems `uv` (`/`), `github-actions` (`/`) and `docker` (`/docker`).
- [ ] **Step 4: Write `scripts/github-setup.sh`** (idempotent, uses `gh`):
  - creates labels `bug, enhancement, infra, cfd, agents, docs, experiment`,
  - creates milestones `M0 Foundations`, `M1 First autonomous run`, `M2 Robustness + full benchmark set`, `M3 Experiment campaign`, `M4 Report` (descriptions from spec §12),
  - sets branch protection on `main`: PR required, 0 approvals required, required checks `lint` and `unit`, no force push, no deletion.
- [ ] **Step 5: Ask the owner to confirm the first push.** Then run `gh repo create mkrajew/cfd-agent --public --source . --push --description "LLM agents that run a full CFD simulation from a plain-language task"`.
- [ ] **Step 6: Run `scripts/github-setup.sh`.** Verify: `gh api repos/mkrajew/cfd-agent/branches/main/protection` returns the two required checks, and `gh run list --limit 1` shows `ci` green.
- [ ] **Step 7: Create the M0 issues**, one per remaining task of this plan (Tasks 4–12), in milestone M0. Commit any script fixes with `git commit -m "ci: add workflows, templates and repo setup"`, then push. This is the last direct push to `main`.

---

### Task 4: Docker image with the full toolchain

**Files:**
- Create: `docker/Dockerfile`, `.dockerignore`, `tests/integration/test_image_toolchain.py`, `tests/integration/conftest.py`
- Modify: `.github/workflows/ci.yml` (jobs `image`, `integration`; publish on `main`), `CLAUDE.md` (exact commands)

**Interfaces:**
- Produces: local image tag `cfdagent:dev`. Inside it:
  - OpenFOAM environment sourced for every shell and for `docker run` commands,
  - Python 3.12 venv on `PATH` with `cfdagent[toolchain]` installed from the build context,
  - non-root user `cfd` (uid 1000), workdir `/work`.
- Integration tests run as `docker run --rm -v "$PWD:/work" -w /work cfdagent:dev pytest -m integration tests/integration`. `conftest.py` skips the integration tests when `WM_PROJECT_VERSION` is unset (host run).
- CI publishes `ghcr.io/mkrajew/cfd-agent:latest` and `:sha-<short>` on `main`, and `:<version>` on tags `v*`.

- [ ] **Step 1: Write the failing test** `tests/integration/test_image_toolchain.py`:

```python
pytestmark = pytest.mark.integration

@pytest.mark.parametrize("exe", ["icoFoam", "simpleFoam", "pimpleFoam", "gmshToFoam", "checkMesh", "foamDictionary", "decomposePar", "postProcess"])
def test_openfoam_executable_available(exe):
    assert shutil.which(exe), exe

def test_openfoam_is_esi():
    assert os.environ["WM_PROJECT_VERSION"].startswith("v")  # ESI versions look like v2506

def test_python_toolchain_imports():
    import cadquery, gmsh, pyvista, matplotlib  # noqa: F401
    assert sys.version_info[:2] == (3, 12)

def test_runs_as_non_root():
    assert os.getuid() != 0

def test_pyvista_offscreen_render(tmp_path):
    # render a sphere off-screen to PNG; file exists and is > 1 kB
```

- [ ] **Step 2: Build an empty image and verify the test fails.** Build: `docker build -f docker/Dockerfile -t cfdagent:dev .`. Run the tests with the command above. Expected: FAIL.
- [ ] **Step 3: Write the Dockerfile.**
  - Base: the official `opencfd/openfoam-default:<latest stable vXXXX>` (or the image matching the latest stable ESI release on Docker Hub), pinned by tag **and digest**.
  - If the base's Ubuntu does not ship Python 3.12, use a uv-managed Python 3.12.
  - Install the OS libraries PyVista needs for off-screen rendering (OSMesa or EGL, builder's choice), then `uv pip install ".[toolchain]"`.
  - The entrypoint sources the OpenFOAM `bashrc`.
- [ ] **Step 4: Rebuild and run the tests to verify they pass.** Expected: all integration tests PASS. Record the image size in the PR description.
- [ ] **Step 5: Extend CI.**
  - Job `image`: buildx with GHA cache, saves the image as an artifact or reuses the cache.
  - Job `integration`: runs the command above against the built image.
  - Job `publish`: only on `main` and tags; uses `GITHUB_TOKEN` with `packages: write`; sets the package to public once via `gh api`.
  - Add `image` and `integration` to the required checks (update `scripts/github-setup.sh`).
- [ ] **Step 6: Commit and open a PR.** Branch `feat/docker-image`. Commit message `feat: add Docker image with OpenFOAM ESI, Gmsh, CadQuery and PyVista`. Run `gh pr create` with the template filled in; cost impact: none. The owner merges.

---

### Task 5: Boundary manifest and Gmsh boundary helpers

**Files:**
- Create: `src/cfdagent/manifest.py`, `src/cfdagent/tools/__init__.py`, `src/cfdagent/tools/gmsh_helpers.py`, `tests/unit/test_manifest.py`, `tests/integration/test_gmsh_helpers.py`

**Interfaces:**
- Produces (`cfdagent.manifest`):
  ```python
  class Locator(BaseModel):
      kind: Literal["plane", "box"]
      axis: Literal["x", "y", "z"] | None = None      # plane only
      value: float | None = None                       # plane only
      box: tuple[float, float, float, float, float, float] | None = None  # xmin,ymin,zmin,xmax,ymax,zmax
  class BoundaryPatch(BaseModel):
      name: str                    # ^[A-Za-z][A-Za-z0-9_]*$
      type: Literal["patch", "wall", "empty", "symmetryPlane", "symmetry", "wedge"]
      locators: list[Locator]      # >= 1; a patch may cover several faces
  class BoundaryManifest(BaseModel):
      dimensionality: Literal["2D", "axisymmetric", "3D"]
      patches: list[BoundaryPatch]
      @classmethod
      def from_file(cls, path: Path) -> "BoundaryManifest"
      def to_file(self, path: Path) -> None
  class ManifestError(Exception)
  ```
  File name convention: `boundary_manifest.json`.
- Produces (`cfdagent.tools.gmsh_helpers`). These functions are importable from mesh scripts and require an initialised Gmsh model with geometry synchronised:
  ```python
  def match_surfaces(manifest: BoundaryManifest, tol: float = 1e-6) -> dict[str, list[int]]
  def assign_physical_groups(manifest: BoundaryManifest, volume_name: str = "internal", tol: float = 1e-6) -> dict[str, list[int]]
  ```
  A plane locator matches surfaces whose bounding box lies within `tol` of the plane. A box locator matches surfaces whose bounding box lies inside the box. Every boundary surface must be matched by **exactly one** patch, otherwise `ManifestError`, whose message names the surface tag and its bounding box.

- [ ] **Step 1: Write the failing unit tests** `tests/unit/test_manifest.py`:
  - `test_roundtrip_json(tmp_path)`: to_file → from_file equals the original.
  - `test_rejects_duplicate_patch_names`: `ValidationError`.
  - `test_rejects_invalid_openfoam_name`: name `"inlet 1"` raises `ValidationError`.
  - `test_plane_locator_requires_axis_and_value` and `test_box_locator_requires_box`.
  - `test_2d_manifest_requires_an_empty_patch`.
  - `test_patch_requires_at_least_one_locator`.
- [ ] **Step 2: Run them to verify they fail.** Run: `uv run pytest tests/unit/test_manifest.py`. Expected: FAIL (import error).
- [ ] **Step 3: Implement `cfdagent.manifest`** with pydantic validators for the rules above.
- [ ] **Step 4: Run them to verify they pass.**
- [ ] **Step 5: Write the failing integration tests** `tests/integration/test_gmsh_helpers.py`. Build a 1 × 1 × 0.1 box with CadQuery in `tmp_path`, export STEP, `gmsh.model.occ.importShapes`, synchronize. Use the manifest movingWall (plane y=1, wall), fixedWalls (planes x=0, x=1, y=0, wall), frontAndBack (planes z=0, z=0.1, empty).
  - `test_assign_physical_groups_on_box`: `{k: len(v)}` == `{"movingWall": 1, "fixedWalls": 3, "frontAndBack": 2}`.
  - `test_unmatched_surface_raises`: manifest without movingWall raises `ManifestError` with message containing `"not matched"`.
  - `test_surface_matched_twice_raises`: add a box locator covering everything to movingWall, raises `ManifestError` containing `"more than one"`.
  - `test_box_locator_matches_curved_surface`: box with a cylindrical hole r=0.05 at (0.5, 0.5); a box locator around the hole matches exactly 1 surface.
- [ ] **Step 6: Run them in the image to verify they fail, implement `gmsh_helpers`, then verify they pass.**
- [ ] **Step 7: Commit and open a PR.** Branch `feat/boundary-manifest`, commit message `feat: add boundary manifest and gmsh physical-group helpers`.

---

### Task 6: Tool runner and geometry stage gate

**Files:**
- Create: `src/cfdagent/tools/process.py`, `src/cfdagent/tools/geometry.py`, `tests/unit/test_process.py`, `tests/integration/test_geometry.py`, `tests/fixtures/handwritten/cavity_re100/geometry.py`

**Interfaces:**
- Produces (`cfdagent.tools.process`):
  ```python
  ALLOWED_EXECUTABLES: frozenset[str]   # "python" plus the OpenFOAM utilities/solvers used in M0
  @dataclass(frozen=True)
  class ToolResult:
      ok: bool; returncode: int | None; timed_out: bool; log_path: Path; tail: str  # last 50 log lines
  class ToolNotAllowed(Exception)
  def run_tool(argv: Sequence[str], cwd: Path, log_path: Path, timeout_s: float = 600.0) -> ToolResult
  ```
  `"python"` resolves to `sys.executable`. stdout and stderr go to one log file. There is no `shell=True`. The network/credential sandbox is M1 scope.
- Produces (`cfdagent.tools.geometry`):
  ```python
  @dataclass(frozen=True)
  class GateResult:
      ok: bool; errors: list[str]
  def run_geometry_script(script: Path, workdir: Path, timeout_s: float = 300.0) -> ToolResult
  def check_geometry(workdir: Path, expected_bbox: tuple[float, ...], rel_tol: float = 1e-3) -> GateResult
  ```
  A geometry script is run with `cwd=workdir` and must write `geometry.step` and `boundary_manifest.json` there. `check_geometry` verifies:
  - both files exist,
  - the manifest parses,
  - the STEP contains ≥ 1 solid and every solid `isValid()`,
  - the combined bounding box matches `expected_bbox` within `rel_tol` of the largest dimension.
  `GateResult` is reused by every later gate.

- [ ] **Step 1: Write the failing unit tests** `tests/unit/test_process.py`:
  - `test_rejects_non_allowlisted_executable`: `run_tool(["bash", "-c", "ls"], ...)` raises `ToolNotAllowed`.
  - `test_successful_command_writes_log`: python prints "hello"; `ok` is True, `"hello"` is in the log, `"hello"` is in `tail`.
  - `test_nonzero_exit_is_not_ok`: `sys.exit(3)` gives `ok` False and `returncode == 3`.
  - `test_timeout`: sleep 5 with `timeout_s=0.5` gives `ok` False and `timed_out` True.
- [ ] **Step 2: Run them to verify they fail. Implement `process.py`. Run them to verify they pass.**
- [ ] **Step 3: Write the hand-written fixture `geometry.py`.** It is the 2D cavity per Ghia: unit square, lid at y = 1, thickness 0.1 in z. It writes `boundary_manifest.json` with the three patches from Task 5.
- [ ] **Step 4: Write the failing integration tests** `tests/integration/test_geometry.py`:
  - `test_handwritten_cavity_geometry_passes`: bbox `(0, 0, 0, 1, 1, 0.1)` gives `ok` True.
  - `test_wrong_dimensions_fail`: expected bbox `(0, 0, 0, 2, 1, 0.1)` gives `ok` False and an error containing `"bounding box"`.
  - `test_script_exception_reported`: a script raising `ValueError` gives `ToolResult.ok` False and `"ValueError"` in `tail`.
  - `test_missing_manifest_fails`: a script that writes only STEP fails with an error containing `"boundary_manifest.json"`.
- [ ] **Step 5: Implement `geometry.py`. Run the tests in the image to verify they pass.**
- [ ] **Step 6: Commit and open a PR.** Branch `feat/geometry-gate`, commit message `feat: add allowlisted tool runner and geometry gate`.

---

### Task 7: Mesh stage and mesh gate

**Files:**
- Create: `src/cfdagent/tools/mesh.py`, `tests/unit/test_checkmesh_parser.py`, `tests/fixtures/logs/checkMesh_ok.log`, `tests/fixtures/logs/checkMesh_failed.log`, `tests/integration/test_mesh.py`, `tests/fixtures/handwritten/cavity_re100/mesh.py`, `tests/fixtures/handwritten/cylinder_channel/{geometry.py,mesh.py}`

**Interfaces:**
- Consumes: `BoundaryManifest`, `run_tool`, `ToolResult`, `GateResult`, `assign_physical_groups`.
- Produces (`cfdagent.tools.mesh`):
  ```python
  @dataclass(frozen=True)
  class CheckMeshReport:
      n_cells: int; bbox: tuple[float, float, float, float, float, float]
      solution_directions: int; patches: dict[str, int]       # patch name -> face count
      max_non_orthogonality: float; max_skewness: float; max_aspect_ratio: float
      failed_checks: int                                      # 0 when "Mesh OK."
  def run_mesh_script(script: Path, workdir: Path, timeout_s: float = 900.0) -> ToolResult   # must write workdir/mesh.msh (format 2.2)
  def convert_to_foam(msh: Path, case_dir: Path) -> ToolResult                            # gmshToFoam
  def apply_patch_types(case_dir: Path, manifest: BoundaryManifest) -> None               # edits constant/polyMesh/boundary
  def parse_checkmesh(log_text: str) -> CheckMeshReport
  def check_mesh(case_dir: Path, manifest: BoundaryManifest, expected_bbox: tuple[float, ...], rel_tol: float = 1e-3) -> tuple[GateResult, CheckMeshReport | None]
  ```
  `check_mesh` runs `checkMesh` via `run_tool` and fails when any of these holds:
  - `failed_checks > 0`,
  - the patch-name set ≠ the manifest names (in particular `defaultFaces` is present),
  - a patch has 0 faces,
  - the bbox differs from `expected_bbox`,
  - a patch type in `boundary` ≠ manifest type,
  - for `2D`: `solution_directions != 2`.

  `apply_patch_types` uses `foamDictionary constant/polyMesh/boundary -entry entry0/<patch>/type -set <type>` (or `changeDictionary`), and sets `inGroups` for walls.

- [ ] **Step 1: Capture the fixture logs in the image.**
  - `checkMesh_ok.log`: run `checkMesh` on a copy of `$FOAM_TUTORIALS/incompressible/icoFoam/cavity/cavity` after `blockMesh`.
  - `checkMesh_failed.log`: run `checkMesh` on a deliberately distorted Gmsh mesh, so the log contains `Failed 1 mesh checks.` or more.
- [ ] **Step 2: Write the failing unit tests** `tests/unit/test_checkmesh_parser.py`:
  - `test_parse_ok_log`: `n_cells == 400`, `failed_checks == 0`, `solution_directions == 2`, `patches == {"movingWall": 20, "fixedWalls": 60, "frontAndBack": 800}`, `bbox == (0, 0, 0, 0.1, 0.1, 0.01)`.
  - `test_parse_failed_log`: `failed_checks >= 1`.
  - `test_parse_garbage_raises`: `parse_checkmesh("nonsense")` raises `ValueError`.
- [ ] **Step 3: Run them to verify they fail. Implement `parse_checkmesh`. Run them to verify they pass.**
- [ ] **Step 4: Write the hand-written cavity `mesh.py`.** It imports `geometry.step`, builds a single-layer extruded mesh (quads recombined to hexes, **one** cell in z, 64 × 64 in-plane unless `cfd-reviewer` sets otherwise in Task 9), calls `assign_physical_groups`, and writes `mesh.msh` with `Mesh.MshFileVersion = 2.2`. It must not set `Geometry.OCCTargetUnit`. Also write `cylinder_channel` (a 2D channel 2.2 × 0.41 × 0.1 with a cylinder r = 0.05 at (0.2, 0.2), unstructured quads, one layer; patches inlet, outlet, walls, cylinder, frontAndBack).
- [ ] **Step 5: Write the failing integration tests** `tests/integration/test_mesh.py`:
  - `test_cavity_mesh_passes_gate`: geometry → mesh → convert → apply types → `check_mesh`; `ok` True, `report.n_cells == 64 * 64`, `report.solution_directions == 2`, patch names == `{"movingWall", "fixedWalls", "frontAndBack"}`.
  - `test_patch_types_applied`: `foamDictionary … -entry entry0/frontAndBack/type -value` == `"empty"`, movingWall == `"wall"`.
  - `test_check_mesh_rejects_default_faces`: a mesh where one surface has no physical group (built in the test directly with the gmsh API) gives `ok` False and an error containing `"defaultFaces"`.
  - `test_check_mesh_rejects_scaled_mesh`: the same cavity mesh with `expected_bbox` scaled ×1000 gives `ok` False and an error containing `"bounding box"`.
  - `test_cylinder_channel_mesh_passes_gate`: patches == `{"inlet", "outlet", "walls", "cylinder", "frontAndBack"}`, `ok` True.
- [ ] **Step 6: Implement the rest of `mesh.py`. Run the tests in the image to verify they pass.**
- [ ] **Step 7: Commit and open a PR.** Branch `feat/mesh-gate`, commit message `feat: add gmsh-to-OpenFOAM mesh stage and mesh gate`. The PR description notes that this closes the "boundary names through STEP" risk from spec §6.2.

---

### Task 8: Benchmark registry, reference data and scoring

**Files:**
- Create: `src/cfdagent/bench/__init__.py`, `src/cfdagent/bench/registry.py`, `src/cfdagent/bench/scoring.py`, `benchmarks/<id>/{benchmark.toml,task.md,reference/*.csv}` for ids `cavity-re100`, `cavity-re1000`, `pipe-re500`, `cylinder-re20`, `cylinder-re100`; `benchmarks/pipe-re500/make_reference.py`; `benchmarks/README.md`; `tests/unit/test_registry.py`, `tests/unit/test_scoring.py`

**Interfaces:**
- Produces (`cfdagent.bench.registry`):
  ```python
  class Criterion(BaseModel):
      quantity: str                                  # key in quantities.json
      kind: Literal["profile", "scalar"]
      metric: Literal["max_abs_error", "rms_error", "rel_error", "in_range"]
      threshold: float | None = None                 # required unless metric == "in_range"
      reference_file: str | None = None              # profile: CSV with header "x,value"
      reference_value: float | None = None           # scalar with abs/rel error
      reference_range: tuple[float, float] | None = None  # scalar with in_range
      normalise_by: float | None = None              # e.g. lid speed; error is divided by it
  class Benchmark(BaseModel):
      id: str; family: str; task_file: str; citation: str; criteria: list[Criterion]
      root: Path                                     # set by the loader
  def load_registry(root: Path = Path("benchmarks")) -> dict[str, Benchmark]
  ```
- Produces (`cfdagent.bench.scoring`):
  ```python
  Quantities = dict[str, float | dict[str, list[float]]]    # profile: {"x": [...], "value": [...]}
  @dataclass(frozen=True)
  class CriterionResult:
      quantity: str; passed: bool; error: float | None; threshold: float | None; reason: str
  @dataclass(frozen=True)
  class ScoreResult:
      passed: bool; criteria: list[CriterionResult]
  def profile_error(sim_x, sim_v, ref_x, ref_v, metric: str) -> float   # linear interpolation of sim onto ref x
  def score(benchmark: Benchmark, quantities: Quantities) -> ScoreResult
  ```
  `profile_error` raises `ValueError` if any reference x lies outside `[min(sim_x), max(sim_x)]` by more than 1e-9 relative. It never extrapolates. A missing quantity gives `passed=False` with `reason="missing quantity"` and does not raise.

- [ ] **Step 1: Write the failing unit tests** `tests/unit/test_scoring.py`:
  - `test_profile_error_zero_for_identical`
  - `test_profile_error_interpolates`: sim `x=[0, 1]`, `v=[0, 1]`; ref `x=[0.5]`, `v=[0.4]`; `max_abs_error` == pytest.approx(0.1).
  - `test_profile_error_rejects_reference_outside_sim_range`: sim on `[0.1, 0.9]`, ref at `0.0`; raises `ValueError`.
  - `test_rms_error`
  - `test_scalar_rel_error_and_in_range`
  - `test_normalise_by_divides_error`
  - `test_score_passes_only_when_all_criteria_pass`
  - `test_score_missing_quantity_fails_with_reason`
- [ ] **Step 2: Run them to verify they fail. Implement `scoring.py` and the models in `registry.py`. Run them to verify they pass.**
- [ ] **Step 3: Dispatch `cfd-reviewer` to write the five benchmark directories.**
  - `task.md` is written as a user would write it (what to simulate, fluid properties, requested quantities). It contains no hints about solver, mesh or expected values.
  - Reference CSVs are transcribed from the primary source, with table/page cited in `benchmark.toml`: Ghia, Ghia & Shin (1982), Table I (u on the vertical centreline) and Table II (v on the horizontal centreline) for Re 100 and 1000.
  - The pipe reference is generated by `make_reference.py` from Hagen–Poiseuille (profile `u(r) = 2Ū(1 − (r/R)²)` and `Δp = 32μLŪ/D²`). Its parameters are read from `benchmark.toml`.
  - Cylinder scalars come from literature chosen by `cfd-reviewer` (spec §9).
  - Starting threshold proposal, which `cfd-reviewer` may change with reasons:

    | Benchmark | Quantity | Threshold |
    |---|---|---|
    | Cavity | Profiles, `max_abs_error` normalised by lid speed | ≤ 0.05 |
    | Pipe | Profile `max_abs_error` normalised by centreline velocity | ≤ 0.03 |
    | Pipe | Δp `rel_error` | ≤ 0.05 |
    | Cylinder Re 20 | Cd `rel_error` | ≤ 0.05 |
    | Cylinder Re 20 | Recirculation length `rel_error` | ≤ 0.10 |
    | Cylinder Re 100 | St `in_range` | literature range widened by numerical tolerance |
    | Cylinder Re 100 | Mean Cd `rel_error` | ≤ 0.05 |
- [ ] **Step 4: Owner approval of the thresholds.** Present a plain table (benchmark, quantity, reference, threshold, reason) in Polish. Record the approval in the journal. Thresholds are never loosened later to make a run pass without a new owner decision.
- [ ] **Step 5: Write the unit tests** `tests/unit/test_registry.py`:
  - `test_registry_loads_all_development_benchmarks`: ids == `{"cavity-re100", "cavity-re1000", "pipe-re500", "cylinder-re20", "cylinder-re100"}`.
  - `test_every_reference_file_exists_and_parses`.
  - `test_ghia_re100_sanity`: u profile has 17 points, x strictly increasing, value at x=1 == 1.0, value at x=0 == 0.0.
  - `test_pipe_reference_matches_formula`: regenerated CSV == committed CSV.
  - `test_benchmark_data_not_in_knowledge_dir`: no file under `src/cfdagent/knowledge/` mentions "Ghia".
- [ ] **Step 6: Run `uv run pytest` and verify everything passes.**
- [ ] **Step 7: Commit and open a PR.** Branch `feat/benchmarks`, commit message `feat: add benchmark registry, reference data and deterministic scoring`. The PR description includes the approved threshold table.

---

### Task 9: Solver and post-processing tools; hand-written cavity end to end

**Files:**
- Create: `src/cfdagent/tools/foam.py`, `src/cfdagent/tools/post.py`, `tests/unit/test_solver_log.py`, `tests/unit/test_post.py`, `tests/fixtures/logs/{icoFoam_ok.log,icoFoam_fatal.log,icoFoam_nan.log}`, `tests/fixtures/samples/vertical_U.xy`, `tests/fixtures/handwritten/cavity_re100/case/{0/U,0/p,constant/transportProperties,system/controlDict,system/fvSchemes,system/fvSolution}`, `tests/fixtures/handwritten/cavity_re100/post.py`, `tests/integration/test_handwritten_cavity.py`

**Interfaces:**
- Consumes: Tasks 6–8 (`run_tool`, `run_geometry_script`, `check_geometry`, mesh functions, `load_registry`, `score`).
- Produces (`cfdagent.tools.foam`):
  ```python
  @dataclass(frozen=True)
  class SolverLogSummary:
      completed: bool; diverged: bool; fatal_error: str | None
      end_time: float | None; final_residuals: dict[str, float]; max_courant: float | None
  def run_solver(case_dir: Path, solver: str, n_procs: int = 1, timeout_s: float = 7200.0) -> ToolResult  # n_procs <= 4, else ValueError
  def parse_solver_log(log_text: str) -> SolverLogSummary
  ```
  `completed` requires the final `End` line and no fatal error. `diverged` is True for NaN/Inf residuals or `Floating point exception`.
- Produces (`cfdagent.tools.post`):
  ```python
  def latest_time_dir(func_dir: Path) -> Path
  def read_line_sample(path: Path, columns: Sequence[str]) -> dict[str, np.ndarray]   # OpenFOAM "raw" set format
  def write_quantities(path: Path, quantities: Quantities) -> None
  ```

- [ ] **Step 1: Capture the logs in the image.**
  - `icoFoam_ok.log`: the tutorial cavity.
  - `icoFoam_fatal.log`: the tutorial with a misspelled BC type.
  - `icoFoam_nan.log`: the tutorial with `deltaT` ×100 until it diverges, cut at the first NaN.
  - `vertical_U.xy`: output of a `sets` function object.
- [ ] **Step 2: Write the failing unit tests.**
  - `test_parse_solver_log_ok`: `completed`, `end_time == 0.5`, keys `{"Ux", "Uy", "p"} ⊆ final_residuals`.
  - `test_parse_solver_log_fatal_error`: `completed` False, and `fatal_error` contains the misspelled type.
  - `test_parse_solver_log_nan_is_diverged`: `diverged` True, `completed` False.
  - `test_run_solver_rejects_more_than_4_ranks`.
  - `test_read_line_sample_columns`: `columns=("y", "Ux", "Uy", "Uz")` gives equal lengths and increasing `y`.
- [ ] **Step 3: Run them to verify they fail. Implement `foam.py` and `post.py`. Run them to verify they pass.**
- [ ] **Step 4: Dispatch `cfd-reviewer` to write the hand-written case** for `cavity-re100`.
  - Solver and physics: `icoFoam`, lid U = (1 0 0), ν = 0.01 m²/s.
  - Time step: Δt from Courant ≤ 0.5.
  - `endTime`: long enough that the centreline u changes by < 0.1 % between the last two write times.
  - Sampling: a `sets` function object (`setFormat raw`) samples a vertical line x = 0.5 and a horizontal line y = 0.5 over the **full** [0, 1] range, at mid-thickness.
  - Post-processing: `post.py <case_dir> <quantities.json>` computes every quantity named in `benchmarks/cavity-re100/benchmark.toml`.
  - If 64 × 64 does not meet the approved thresholds, `cfd-reviewer` raises the resolution in `mesh.py`. **Never** loosen the thresholds.
- [ ] **Step 5: Write the integration test** `tests/integration/test_handwritten_cavity.py::test_handwritten_cavity_re100_matches_ghia`. In `tmp_path` it runs:
  1. geometry → `check_geometry` ok,
  2. mesh → `check_mesh` ok,
  3. copy the case files → `run_solver(..., "icoFoam")` ok → `parse_solver_log(...).completed`,
  4. `post.py` → `score(load_registry()["cavity-re100"], quantities).passed is True`.

  On failure, print every `CriterionResult`. Marked `integration`.
- [ ] **Step 6: Run it in the image to verify it passes.** Record wall time and the per-criterion errors in the PR description.
- [ ] **Step 7: Commit and open a PR.** Branch `feat/handwritten-cavity`, commit message `feat: add solver/post tools and hand-written cavity end-to-end test`. The scribe adds an `experiment` journal entry with the errors vs. Ghia.

---

### Task 10: Azure infrastructure (Bicep)

**Files:**
- Create: `infra/main.bicep` (targetScope `subscription`), `infra/main.bicepparam`, `infra/modules/{network,vm,storage,foundry,roles,budget}.bicep`, `infra/vm/cloud-init.yaml`, `infra/vm/idle-shutdown.sh`, `infra/vm/idle-shutdown.{service,timer}`, `infra/scripts/{deploy.ps1,vm-up.ps1,vm-down.ps1,ssh-config.example}`, `infra/README.md`
- Modify: `.github/workflows/ci.yml` (job `infra-lint`: `az bicep build` for every file, `shellcheck infra/vm/*.sh`), `.pre-commit-config.yaml` (shellcheck)

**Interfaces:**
- Produces (one subscription-scope deployment that creates resource group `rg-cfdagent` in `italynorth` and deploys every module into it):
  - VNet + NSG + public IP; inbound 22 only from `ownerIp`; SSH key only.
  - VM `vm-cfdagent`, `Standard_F4s_v2`, Ubuntu 24.04, system-assigned identity; OS disk type and size chosen by cost and stated in the PR.
  - DevTestLab auto-shutdown schedule, time from parameter `shutdownTime` (default `2300`, `Central European Standard Time`).
  - Storage account (StorageV2, LRS, Hot) with `allowSharedKeyAccess: false`, container `runs`, and a lifecycle rule moving blobs to Cool after 30 days.
  - AI Services account with deployments:
    - `gpt-oss-120b`: format `OpenAI-OSS`, version `1`, SKU `GlobalStandard`, capacity `50`. Values copied from the working probe deployment.
    - `Llama-3.3-70B-Instruct` and `qwen3-32b`: exact format/version/SKU taken from `az cognitiveservices account list-models`. If a model cannot be deployed into the account, leave it out and report it. Task 11 applies the fallback rule.
  - Role assignments:
    - VM identity: `Storage Blob Data Contributor` on the storage account, `Cognitive Services OpenAI User` on the AI account, and a custom role with only `Microsoft.Compute/virtualMachines/deallocate/action` (plus `read`) scoped to the VM.
    - Owner (`ownerObjectId`): `Storage Blob Data Reader` and `Cognitive Services OpenAI User`.
  - Outputs: `vmPublicIp`, `aiEndpoint` (OpenAI v1 base URL), `storageAccountName`.
- `modules/budget.bicep` (subscription scope): budget `budget-cfdagent`, $100 monthly, alerts at 25/50/75/90 % to `budgetEmail`.
- `main.bicepparam` reads `ownerIp`, `sshPublicKey`, `ownerObjectId` and `budgetEmail` via `readEnvironmentVariable()`. Nothing personal is committed.
- Idle shutdown: a systemd timer runs every 5 min. When there have been no SSH sessions (`who`) and no `cfdagent` process for 30 consecutive minutes, it deallocates the VM through ARM REST with an IMDS managed-identity token. The idle-start timestamp is kept in `/var/lib/cfdagent/idle-since`.
- `vm-up.ps1`: starts the VM, optionally updates the NSG source to the current public IP (`-UpdateIp`), and prints the SSH command. `vm-down.ps1`: deallocates.

- [ ] **Step 1: Write the Bicep modules, scripts and cloud-init.** Cloud-init installs Docker, adds the admin user to the `docker` group, installs the idle-shutdown units and pre-pulls `ghcr.io/mkrajew/cfd-agent:latest`.
- [ ] **Step 2: Run lint locally.** Run: `az bicep build --file infra/main.bicep`. Expected: no errors and no warnings. Run `shellcheck` in the image; expected: no findings.
- [ ] **Step 3: Run what-if.** Run: `az deployment sub what-if -l italynorth -f infra/main.bicep -p infra/main.bicepparam`. Nothing is created by what-if. Expected: only `Create` operations for the resource group, the budget and the resources listed above.
- [ ] **Step 4: Owner approval.** Show the owner, in Polish:
  - the what-if summary,
  - the monthly fixed cost (disk, public IP, storage),
  - the hourly VM cost (≈ $0.19/h),
  - that the Foundry deployments cost only per token.

  Wait for an explicit yes.
- [ ] **Step 5: Deploy.** Run `infra/scripts/deploy.ps1`, which runs `az deployment sub create` with the same template and parameters. Verify:
  - `az vm get-instance-view` shows the VM running,
  - `ssh cfdagent docker run --rm ghcr.io/mkrajew/cfd-agent:latest icoFoam -help` works,
  - `az consumption budget show --budget-name budget-cfdagent` exists.
- [ ] **Step 6: Run `vm-down.ps1`.** Verify the power state is `VM deallocated`.
- [ ] **Step 7: Commit and open a PR.** Branch `infra/bicep`, commit message `feat(infra): add Bicep infrastructure, idle shutdown and VM helper scripts`. The PR's cost-impact section carries the numbers from Step 4.

---

### Task 11: LLM client and capability spike (three models)

**Files:**
- Create: `src/cfdagent/llm/__init__.py`, `src/cfdagent/llm/config.py`, `src/cfdagent/llm/client.py`, `tests/unit/test_llm_client.py`, `tests/fixtures/llm/*.json`, `scripts/llm_spike.py`, `docs/adr/0002-llm-capability-spike.md`, `docs/adr/data/0002-llm-spike.json`

**Interfaces:**
- Produces (`cfdagent.llm.config`):
  ```python
  class ModelConfig(BaseModel):
      deployment: str
      temperature: float | None = None; top_p: float | None = None
      max_tokens: int = 4096
      reasoning_effort: Literal["low", "medium", "high"] | None = None
  class LLMSettings(BaseModel):
      endpoint: str                          # OpenAI v1 base URL, e.g. https://<account>.services.ai.azure.com/openai/v1/
      auth: Literal["entra", "api_key"] = "entra"
      api_key_env: str = "CFDAGENT_LLM_API_KEY"
      @classmethod
      def from_env(cls) -> "LLMSettings"     # CFDAGENT_LLM_ENDPOINT, CFDAGENT_LLM_AUTH
  ```
- Produces (`cfdagent.llm.client`):
  ```python
  @dataclass(frozen=True)
  class ToolCall:
      id: str; name: str; arguments: dict
  @dataclass(frozen=True)
  class Usage:
      prompt_tokens: int; completion_tokens: int; reasoning_tokens: int | None
  @dataclass(frozen=True)
  class ChatResult:
      content: str | None; tool_calls: list[ToolCall]; usage: Usage; parsed: BaseModel | None; raw: dict
  class StructuredOutputError(Exception)
  class LLMClient:
      def __init__(self, settings: LLMSettings, *, credential: TokenCredential | None = None, http_client: httpx.Client | None = None)
      def chat(self, messages: list[dict], model: ModelConfig, *, tools: list[dict] | None = None,
               response_schema: type[BaseModel] | None = None) -> ChatResult
  ```
  It is built on `openai.OpenAI(base_url=settings.endpoint, api_key=<bearer token provider for scope "https://cognitiveservices.azure.com/.default">)`. `reasoning_effort` is sent only when not None. `response_schema` becomes `response_format={"type": "json_schema", "json_schema": {"name": ..., "schema": ..., "strict": True}}` and the reply is validated into `parsed`; invalid JSON raises `StructuredOutputError` carrying the raw text.

- [ ] **Step 1: Write the failing unit tests** `tests/unit/test_llm_client.py`, using `httpx.MockTransport` with recorded JSON fixtures and a fake credential:
  - `test_sends_deployment_as_model`
  - `test_sends_reasoning_effort_when_set` and `test_omits_reasoning_effort_when_none`
  - `test_bearer_token_from_credential`: the `Authorization` header is `Bearer fake-token`.
  - `test_parses_tool_calls`: one `ToolCall(name="calculator", arguments={"expression": "1*1/100"})`.
  - `test_structured_output_parsed`
  - `test_invalid_structured_output_raises`
  - `test_usage_parsed`
- [ ] **Step 2: Run them to verify they fail. Implement `config.py` and `client.py`. Run them to verify they pass.**
- [ ] **Step 3: Write `scripts/llm_spike.py --deployment <name> --repeats 3`.** It runs four checks per repeat and writes JSON results:
  1. **auth**: a plain completion succeeds with Entra.
  2. **tool calling**: given tool `calculator(expression: str)`, asks "Re = 100, lid speed 1 m/s, cavity side 1 m: compute the kinematic viscosity using the calculator tool". Passes if a well-formed `calculator` tool call arrives.
  3. **structured output**: a small pydantic schema (`{"solver": str, "steady": bool, "reynolds": float}`) is returned and validates.
  4. **reasoning effort**: requests with `low` and `high` are both accepted. Record completion and reasoning tokens. For non-gpt-oss models, record "unsupported" if the API rejects the parameter.
- [ ] **Step 4: Run the spike** for `gpt-oss-120b`, `Llama-3.3-70B-Instruct` and `qwen3-32b` from the laptop (owner's `az login`, role from Task 10). Expected cost: cents, inside the M0 envelope.
- [ ] **Step 5: Apply the comparison-model fallback rule** (spec §14.1). If a comparison model fails tool calling in ≥ 2 of 3 repeats, or cannot be deployed, re-run the spike for one replacement in this order: Llama-4-Scout, mistral-small, Phi-4. Record any exclusion as a finding. Write ADR-0002 with a results table (model × check × pass count), the fallbacks the client will use (JSON mode + pydantic retry, reasoning via system prompt, key from Key Vault) and the final model list for M3. Commit the raw JSON to `docs/adr/data/`.
- [ ] **Step 6: Commit and open a PR.** Branch `feat/llm-client`, commit message `feat: add LLM client and record model capability spike`. The scribe adds an `experiment` entry.

---

### Task 12: M0 acceptance on the VM and cleanup

**Files:**
- Modify: `README.md` (status line "M0 complete"), `docs/journal/` (milestone entry)

- [ ] **Step 1: Start the VM** with `infra/scripts/vm-up.ps1`.
- [ ] **Step 2: Run the integration suite on the VM.** Run: `ssh cfdagent "git clone https://github.com/mkrajew/cfd-agent && cd cfd-agent && docker run --rm -v \$PWD:/work -w /work ghcr.io/mkrajew/cfd-agent:latest pytest -m integration tests/integration"`. Expected: all pass, including `test_handwritten_cavity_re100_matches_ghia`. Record the wall time.
- [ ] **Step 3: Verify that the VM can reach Blob and Foundry with its identity.** Inside the container on the VM:
  - upload a 1-line test blob to `runs/_m0-check/` and delete it,
  - make one `LLMClient.chat` call to `gpt-oss-120b`.

  Expected: both succeed with no key configured.
- [ ] **Step 4: Verify idle shutdown.** Disconnect and wait 40 min. Expected: `az vm get-instance-view` reports `VM deallocated` without anyone stopping it. This costs ≈ $0.13.
- [ ] **Step 5: Ask the owner to approve deleting `rg-cfdagent-probe`.** Then run `az group delete -n rg-cfdagent-probe --yes` and verify it is gone.
- [ ] **Step 6: Report M0 spend** to the owner (Cost Management, RG `rg-cfdagent`), with a scribe milestone entry. Close milestone M0 on GitHub.
- [ ] **Step 7: Commit, tag and open the PR.** Branch `docs/m0-complete`, commit message `docs: mark M0 complete`. After the owner merges, tag `v0.0.1` and run `gh release create v0.0.1 --generate-notes`.
- [ ] **Step 8: Write the M1 plan.** Write `docs/superpowers/plans/<date>-m1-first-autonomous-run.md` from spec §6 and ADR-0002, using this skill.
