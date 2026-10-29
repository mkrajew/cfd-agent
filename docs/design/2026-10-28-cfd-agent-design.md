# CFD Agent — Design Document

- **Date:** 2026-10-28
- **Status:** Approved by the owner on 2026-10-28 (the owner approved it on the basis of the design conversation; §16 was not reviewed item by item)
- **Owner:** mkrajew (GitHub) — mechanical engineer and programmer; approves, does not read code
- **Language rule:** everything in the repository is English; the owner and Claude converse in Polish

Items marked **[Claude's call]** were decided by Claude during design, not by the owner. They are listed again in §16 so the owner can check them.

---

## 1. Intent

A university research project that tests the hypothesis:

> **LLM-based agents can autonomously perform a complete CFD simulation from a natural-language task description** — geometry, meshing, case setup, solving, post-processing and analysis — without human intervention.

The user writes a task in English, for example *"Perform a complete CFD simulation of flow in a two-dimensional lid-driven cavity"* followed by parameters (dimensions, viscosity, Reynolds number, anything else). The system creates the geometry, meshes it, sets up and runs OpenFOAM, analyses the results and returns them.

The project is a **proof of concept**. A negative or partial outcome is a valid result; the analysis of where and why agents fail is part of the value.

Open-weight models are used **because of budget**, not as part of the hypothesis. The architecture must make swapping to commercial models (Claude, GPT) a configuration change.

## 2. Goals, non-goals, anti-goals

### Goals
1. A working autonomous pipeline: natural-language task → validated CFD results, running in Azure.
2. Validation on three benchmark families with known reference solutions (§9), and a generality test on new flow types never used during development (§9.1).
3. Statistically meaningful evidence: repeated runs, success rates, error vs. reference, cost and time per run, failure taxonomy.
4. A general architecture: the user may submit any CFD task; nothing is hard-coded to the three benchmarks.
5. A professional public GitHub repository (README, tests, CI, issues, milestones, releases).
6. A research report produced from a curated journal and measured data.
7. Clear growth path to larger problems, stronger models and other interfaces (§13).

### Non-goals (for this project)
- Validating physics beyond incompressible (incl. Boussinesq heat transfer), single-phase, laminar flow in 2D / axisymmetric / simple 3D. Any other task is still *attempted*; its result is assessed by physical checks only (§6.8).
- Multiphase, combustion, FSI, compressible flow, LES as validated capabilities.
- A web UI, GitHub IssueOps, Container Apps Jobs, remote CLI — future extensions only.
- Fine-tuning models.
- Running Ollama on the owner's laptop as a required component.

### Anti-goals
- **Overclaiming.** README and descriptions present the system as general ("describe any CFD task"); the validation scope is stated factually in a "Validation" section, not emphasised as a limitation. But every result the system returns states plainly whether it was compared with reference data or assessed by physical checks only. Unverified results never look verified.
- Spending Azure credit without the owner's explicit approval.
- Secrets in the public repository.
- Repository bloat: no simulation results in git.
- Ceremony for its own sake: no repository artefacts that a solo project does not use.

## 3. Constraints (verified 2026-10-27 with `az` CLI)

| Constraint | Value |
|---|---|
| Subscription | Azure for Students (Politechnika Warszawska tenant), **$100 credit** |
| Allowed regions (policy) | `italynorth`, `spaincentral`, `belgiumcentral`, `denmarkeast`, `austriaeast` |
| Compute quota | **6 vCPU total per region**, **max 4 vCPU per VM family**; spot/low-priority 3 vCPU |
| GPU | Quota 0 for every current GPU family. Increase requests for NCASv3_T4 in Italy North and Spain Central were **rejected**. **No GPU.** |
| LLM hosting | Azure AI Foundry serverless (pay per token). Verified working: **`gpt-oss-120b`** (deployment succeeded, test call ~2 s). Quotas: gpt-oss-120b 5000, Llama-3.3-70B-Instruct 20, Llama-4-Scout 20, Phi-4 20, mistral-small 20, Codestral 20, qwen3-32b likely via shared MaaS pool 600. `gpt-oss-20b` is **not deployable** (fine-tune only). DeepSeek/Kimi/Grok quota 0. |
| Prices (Azure retail API, italynorth) | gpt-oss-120b: $0.15 / 1M input tokens, $0.60 / 1M output tokens. VM Standard_F4s_v2 (Linux): ~$0.194/h. |
| Existing probe resources | RG `rg-cfdagent-probe` with AI Services `cfdagent-probe-wgcbe` and deployment `gpt-oss-120b`. Delete once the IaC-managed environment exists. |
| Owner's machine | Windows 11, RTX A3000 12 GB, i7-12800H, 32 GB RAM, Docker Desktop, WSL2 Ubuntu, git, `gh` logged in as `mkrajew`, Azure CLI installed (not on PATH: `C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd`). |
| Owner's role | Approves design decisions, PR descriptions + CI status, CFD results, every cost. Does not review code line by line. |
| Time | No tight deadline; solo project. |

## 4. Decisions

### Owner's decisions
- Execution environment in **Azure**; local laptop used minimally (Docker for development is fine; Ollama only if unavoidable).
- **CadQuery creates every geometry.** Fixed pipeline: every case passes through CAD modelling. No blockMesh path.
- **OpenFOAM** for solving.
- v1 interface: **SSH to a VM + project CLI**. GitHub IssueOps and Gradio/Streamlit come later.
- Results stored in **Azure Blob Storage only** (everything, not just large files) and fetched via Portal / Storage Explorer / `az` / `scp`. **No results in git; no second "runs" repository.**
- Public GitHub repository, English.
- Report: free hand on format; staged writing process with owner approval at each stage (§11).
- Model: open-weight via Azure Foundry because of budget; commercial models allowed later.
- **No task categories**: every task is attempted the same way; results state whether reference data existed (§6.8).
- **Generality rule**: prompts and knowledge contain only general CFD knowledge, nothing case-specific (§6.3).
- **Generality test set** of new flow types (backward-facing step, Couette/Poiseuille channel, Blasius flat plate, optionally a heated cavity), written by the owner and never run before the frozen release (§9.1).

### Claude's calls (accepted by the owner in conversation)
- **Gmsh** for meshing (imports CadQuery STEP). snappyHexMesh is a future path for complex 3D (CadQuery → STL).
- **OpenFOAM ESI (openfoam.com)**, latest stable release with an official Docker image, exact version pinned in the Dockerfile. Reason: classic solver names (`icoFoam`, `simpleFoam`, `pimpleFoam`) that LLMs know far better than the Foundation's `foamRun -solver` interface.
- Post-processing: OpenFOAM function objects + **PyVista** + **matplotlib**.
- **Deterministic orchestrator** drives the pipeline; LLM agents are called per stage.
- **Start on a VM**, not Container Apps Jobs (debuggability). Everything runs in **one Docker image**, so moving to ACA Jobs later is infrastructure-only.
- Keyless access: VM **managed identity** to Foundry and Blob (Entra ID). Fallback: key in Key Vault if a model does not support Entra auth.
- **Bicep** for infrastructure as code.
- Report format: **Markdown → PDF via Pandoc with a LaTeX template**, English.
- License **MIT**.

## 5. System overview

```
 Owner's laptop                               Azure (italynorth)
 ──────────────                               ─────────────────────────────────────────
 az vm start / VS Code Remote-SSH ──SSH──►    VM Standard_F4s_v2 (Ubuntu 24.04, Docker)
                                               └─ cfdagent CLI → Docker image
                                                    ├─ Orchestrator (deterministic)
                                                    ├─ LLM agents ─────────► Azure AI Foundry
                                                    │                         (gpt-oss-120b, …)
                                                    └─ Sandboxed tools:
                                                       CadQuery → Gmsh → OpenFOAM → PyVista
 Portal / Storage Explorer / az / scp ◄──────── Blob Storage: runs/<run-id>/ (everything)
                                               Budget alerts · auto-shutdown · idle-shutdown
```

## 6. Runtime system (cloud)

### 6.1 Toolchain inside the image
- OpenFOAM ESI (pinned), including `$FOAM_TUTORIALS`.
- Python 3.12 with: `cadquery`, `gmsh`, `pyvista` (off-screen rendering), `matplotlib`, `numpy`, `pydantic`, `openai`, `azure-identity`, `azure-storage-blob`, `typer`. **[Claude's call]**
- The `cfdagent` package itself.

### 6.2 Fixed pipeline and gates

Every run passes the same stages. After each stage a **deterministic gate** checks the output. On failure, the error is fed back to the responsible agent for another attempt, within limits (§6.5).

| # | Stage / agent | Output | Gate (deterministic) |
|---|---|---|---|
| 1 | **Planner** | `CaseSpec` (strict JSON schema) | Schema-valid; all derived numbers (velocity from Re, entrance length, Courant-based Δt estimate…) computed with the calculator tool; resource estimate within limits (§6.8) |
| 2 | **Geometry** (CadQuery) | Python script, `geometry.step`, **boundary manifest** | Script runs; solid valid; bounding box and key dimensions match `CaseSpec` within tolerance; every boundary face named in manifest with a locator (e.g. "inlet: plane x=0") |
| 3 | **Mesh** (Gmsh) | Gmsh script, `.msh`, OpenFOAM `polyMesh` via `gmshToFoam` | `checkMesh` passes; **every patch named per manifest, no default/unnamed patches**; quality thresholds (non-orthogonality, skewness, aspect ratio); 2D cases are one cell thick with `empty` front/back |
| 4 | **Case Setup** | `0/`, `constant/`, `system/` | Dictionaries parse (`foamDictionary`); BC set consistent with patches; a short trial run starts without fatal error |
| 5 | **Solver Runner** | Solved case, logs | Convergence (residual targets for steady; statistically periodic forces for unsteady); no divergence. On error: agent reads the log, diagnoses, proposes a concrete fix; orchestrator routes to stage 4 or 3 |
| 6 | **Analyst** | Quantities of interest, plots, interpretation | All quantities computed with tools; mass-balance and convergence checks; physical plausibility checks (no reference data, see §9.2) |
| 7 | **Critic** | Verdict `success` / `partial` / `failed` with reasons | May send the run back to one named stage (bounded); judges on physics and consistency only |
| 8 | **Scorer** (deterministic, not an agent) | `comparison.json` | Runs **after the Critic's verdict is final**. Compares quantities with reference data and acceptance criteria (§9.2). Result is appended to `summary.md` and the run record; it can never trigger a retry |

Boundary naming through STEP is a known weak point (STEP import loses names). The boundary manifest plus the mesh gate exist specifically for it and get their own integration test.

### 6.3 Cloud agents — role and instruction essentials

Each agent's system prompt lives in `src/cfdagent/prompts/<agent>.md`, versioned and reviewed like code. The prompt version used is recorded in every run.

**Generality rule (owner's decision).** Prompts, knowledge notes and tool code contain **only general CFD / OpenFOAM knowledge**, never anything specific to a test case. Allowed: "2D cases are one cell thick with `empty` front and back patches", "for unsteady flow choose Δt from a Courant-number target". Forbidden: anything naming a benchmark geometry, Reynolds number, mesh size or expected result (e.g. "for the cavity use 129×129 cells"). Improvements made while debugging on the development tasks must be phrased as general rules that would help any task. `cfd-reviewer` checks every prompt and knowledge-note change against this rule; a violation blocks the PR.

| Agent | Instruction essentials |
|---|---|
| **Planner** | Turn the task into `CaseSpec`. Never do arithmetic in-head: use the calculator tool. Fill missing parameters with sensible engineering defaults and **record every assumption explicitly**. Check flow regime (e.g. laminar). Estimate cells, runtime, memory; if over the limits, first look for a legitimate reduction (symmetry, 2D, coarser but justified mesh) and record it as an assumption. |
| **Geometry** | Write a CadQuery script producing exactly the specified dimensions. Export STEP. Produce the boundary manifest naming every face. |
| **Mesh** | Write a Gmsh script importing the STEP. Assign physical groups from the manifest. Justify resolution from physics (refinement near walls/cylinder, boundary-layer resolution). 2D: single extruded layer, recombined cells, `empty` patches. |
| **Case Setup** | Search the knowledge base for the closest tutorial first, then adapt. Consistent units and BCs. Time step from Courant target (tool-computed). Add function objects for every quantity the task asks for. |
| **Solver Runner** | Run the solver (parallel via `decomposePar` when useful, ≤ 4 cores). Monitor residuals and Courant number. On failure, read `FOAM FATAL ERROR`/divergence signs, give a diagnosis and a specific fix. |
| **Analyst** | Extract quantities via function-object output and PyVista. Check plausibility from physics (e.g. expected trends, conservation), **without access to benchmark reference data**. Produce plots for humans. Write a concise physical interpretation. |
| **Critic** | Fresh context, no loyalty to earlier stages. Check: did we compute what was asked, with the requested parameters (Re, BCs, geometry)? Is the solution converged? Are the numbers plausible? Give verdict and reasons. |

**Model constraint:** gpt-oss-120b is text-only. Agents cannot look at images; every automated check is numeric. Images are for humans.

### 6.4 LLM provider abstraction
- One interface over **OpenAI-compatible chat completions** with tool/function calling. Backends: Azure Foundry (default), Ollama (optional), commercial APIs (future). Switching model = configuration.
- Configurable per agent: model, temperature, top_p, max tokens, reasoning effort (gpt-oss: low/medium/high).
- **Verify in M0** (spike): tool calling, structured JSON output, `reasoning_effort` and Entra ID auth for gpt-oss-120b on Foundry. Fallbacks: JSON mode + Pydantic validation + retry; reasoning level via system prompt; API key from Key Vault.
- Every LLM request/response is saved to the run's transcript (`llm_transcript.jsonl`) with token counts.

### 6.5 Orchestrator, limits, run record
- Plain Python state machine; no agent framework. **[Claude's call]** Reason: transparency, deterministic control, easy logging for research.
- Default limits per run **[Claude's call, configurable]**: max 3 attempts per stage; max 10 fix loops overall; wall-clock 2 h; max $1.00 LLM cost; max 4 MPI ranks.
- When a limit is hit, the run ends as `failed` or `partial`, with what was produced so far uploaded.
- **Run record** (`run_record.json`) for every run: task, task set (development / generality / ad hoc, §9), model + parameters, prompt versions, image version, per-stage attempts and errors, tokens, cost, wall time, verdict, errors vs. reference. This is the raw data for the hypothesis test.
- Optional debugging flag `--pause-after <stage>`. Default is fully autonomous.

### 6.6 Knowledge base
- Lexical search (ripgrep/BM25-style) over `$FOAM_TUTORIALS` and over curated notes in `src/cfdagent/knowledge/notes/*.md` (written by agents, reviewed by `cfd-reviewer`). **[Claude's call]** No vector database or embedding model in v1.
- Tools: `search_knowledge(query)`, `read_file(path)`.

### 6.7 Execution sandbox (security requirement)
LLM-generated code (CadQuery and Gmsh scripts, shell commands) must run:
- as a non-root user, confined to the run directory;
- **without network access and without access to credentials** (managed-identity tokens, keys);
- only through an allowlisted tool layer (Python for geometry/mesh scripts, named OpenFOAM utilities and solvers). No free-form shell.

Mechanism is left to the builder. Suggested: the orchestrator launches executor containers from the same image with `--network none`, sharing the run directory volume.

### 6.8 Every task is treated the same (owner's decision)
There are **no task categories** (no "validated / experimental / out of scope" labels). The system attempts every task with the same pipeline, prompts and tools.
- **Resources.** If the Planner's estimate still exceeds the run limits (§6.5) after legitimate reductions, the run ends `failed` at stage 1 with the reason ("exceeds resources: …"). This is a recorded result, not a refusal. The same applies when a limit is hit later.
- **Reference comparison.** Whether a result is compared with literature is not decided by any classifier or LLM. A run started from a benchmark task file (`cfdagent bench`, or `cfdagent run --benchmark <id>`) carries a link to that benchmark's reference data in the registry (§9), and the Scorer uses it. Every other run is an ad hoc run without reference data. The agents never see this link (blind evaluation, §9.2).
- **What the user sees.** `summary.md` always contains either the Scorer's comparison with reference data, or the plain statement *"No reference data for this task; result assessed by physical checks only (convergence, mass balance, plausibility)."*

### 6.9 Run artefacts and storage
Run ID: `YYYYMMDD-HHMMSS-<slug>` (e.g. `20261112-143005-cavity-re100`).

Blob container `runs`, one folder per run:
```
runs/<run-id>/
  task.md               original task
  casespec.json
  geometry/             CadQuery script, geometry.step, boundary_manifest.json
  mesh/                 Gmsh script, .msh, checkMesh log
  case/                 full OpenFOAM case (archived as case.tar.zst)
  logs/                 solver and tool logs
  results/              plots (PNG), quantities.json, comparison.json
  summary.md            human-readable summary
  llm_transcript.jsonl
  run_record.json
```
- Local copy on the VM under `~/runs/<run-id>/`; only the most recent N runs (default 10) kept locally.
- Lifecycle rule: blobs move to Cool tier after 30 days.
- Sharing a run with the supervisor: time-limited SAS link.

### 6.10 CLI
```
cfdagent run <task.md> [--model ...] [--benchmark <id>] [--detach] [--pause-after <stage>]
cfdagent status <run-id>
cfdagent logs <run-id> [--follow]
cfdagent list
cfdagent bench --cases <all|names> --repeats N --models a,b
```
The CLI is a thin layer over a core API (`run_task(task, config) -> RunResult`). No logic lives in the CLI, so IssueOps, a web UI or remote submission can call the same core later.

A run survives SSH disconnects (`--detach`, re-attach via `logs --follow`).

## 7. Infrastructure (Azure, Bicep)

Resource group `rg-cfdagent` in `italynorth`, deployed/destroyed with one command. **Deployment runs only after the owner approves.** No CI-driven deployment in v1. **[Claude's call]**

| Resource | Details |
|---|---|
| VM | `Standard_F4s_v2`, Ubuntu 24.04, Docker, OS disk sized by builder (cost-aware), system-assigned managed identity |
| Network | VNet, public IP, NSG: SSH (22) only from the owner's IP (parameter); SSH key auth only |
| Auto-shutdown | Daily schedule (time parameter, Europe/Warsaw) |
| Idle shutdown | On-VM timer: no SSH sessions and no running `cfdagent` for 30 min → VM deallocates itself (identity scoped to deallocate only that VM) |
| AI Foundry | AI Services account + deployments: `gpt-oss-120b` (primary), `Llama-3.3-70B-Instruct`, (comparison; `qwen3-32b` excluded, see §14.1) |
| Storage | StorageV2, LRS, Hot; container `runs`; lifecycle rule; VM identity has Blob Data Contributor; owner has a data-reader role for Portal browsing |
| Key Vault | Only if a key fallback is needed (§6.4) |
| Budget | Subscription budget $100 with alerts at 25/50/75/90 %, owner's email as parameter |

Helper scripts: `vm-up`, `vm-down`, SSH config snippet for `ssh cfdagent`.

## 8. Repository and engineering practices

### 8.1 Layout
```
src/cfdagent/          cli, core API, orchestrator, agents, prompts/, tools/, llm/, knowledge/, storage/, bench/
benchmarks/<case>/     task files (natural language) + reference data + acceptance criteria
tests/unit|integration|e2e/, tests/fixtures/ (recorded LLM responses)
docker/                Dockerfile
infra/                 main.bicep, modules/, parameters, VM scripts, CLAUDE.md
examples/              a few curated, hand-selected successful runs (text artefacts + summary)
docs/design/ docs/adr/ docs/journal/ docs/report/
.claude/agents/ .claude/settings.json
.github/workflows/ ISSUE_TEMPLATE/ pull_request_template.md dependabot.yml
CLAUDE.md README.md LICENSE CITATION.cff pyproject.toml .pre-commit-config.yaml
```

### 8.2 Practices
- **README:** overview, architecture diagram, quick start, Validation section with benchmark results, roadmap, CI badges.
- **Tests (pytest):** unit (no real LLM; recorded responses; free), integration (full CadQuery → Gmsh → OpenFOAM pipeline in Docker on known-good hand-written scripts, no LLM), e2e (real model + benchmark; manual trigger only, costs money).
- **CI (GitHub Actions):** on PR — lint (ruff), unit tests, image build, integration tests. On `main` — publish image to GHCR (public).
- **Branch protection:** `main` only via PR with green CI; **the owner merges**.
- **PR template:** what, why, how verified, Azure cost impact.
- **Issues** with bug/feature templates, labels, **Milestones = §12**.
- **Dependabot** for Python deps, GitHub Actions and the base image.
- **pre-commit:** ruff format/lint, **gitleaks** secret scanning.
- **Releases:** tag per milestone (`v0.1.0` = first autonomous cavity); release notes replace a CHANGELOG.
- Tooling **[Claude's call]:** `uv` for environments, Python 3.12, conventional commits, branches `feat/…`, `fix/…`.
- Deliberately omitted: CONTRIBUTING, CODE_OF_CONDUCT, CHANGELOG, project boards.

## 9. Benchmarks and reference data

Each benchmark lives in `benchmarks/<case>/` with natural-language task files, reference data (with citation) and acceptance criteria. **Exact reference values, metrics and acceptance thresholds are fixed by `cfd-reviewer` in M0 and approved by the owner.** Candidates:

| Family | Variants | Reference | Quantities |
|---|---|---|---|
| Lid-driven cavity (2D) | Re = 100, Re = 1000 | Ghia, Ghia & Shin (1982) | u on vertical centreline, v on horizontal centreline; primary vortex centre |
| Laminar pipe flow | e.g. Re = 500 (3D or axisymmetric wedge — agent's choice) | Hagen–Poiseuille (analytic) | Fully developed profile u(r) = 2Ū(1−(r/R)²); Δp = 32μLŪ/D²; pipe must exceed entrance length ≈ 0.05·Re·D |
| Cylinder in cross-flow (2D) | Re = 20 (steady), Re = 100 (unsteady) | Literature for unconfined cylinder (e.g. Tritton; Williamson) — final choice by `cfd-reviewer` | Cd and recirculation length (Re 20); St ≈ 0.164–0.166 and mean Cd (Re 100) |

Benchmark tasks are written as a user would write them, not as hints to the agents. The benchmark registry links each task file to its reference data and acceptance criteria; the Scorer uses that link (§6.8).

These are the **development tasks**: they are run while the system is built, to find and fix its failures (under the generality rule, §6.3).

### 9.1 Generality test set (owner's decision)
A second set of tasks, of **flow types the system never saw during development**, tests whether it works in general and not only on the tasks used to build it. Candidates (references, quantities and thresholds fixed by `cfd-reviewer`):

| Task | Reference | Quantities |
|---|---|---|
| Backward-facing step (2D, laminar) | Armaly et al. (1983); e.g. Gartling (1990) | Reattachment length vs. Re |
| Plane Couette or plane Poiseuille channel flow | Analytic | Linear / parabolic velocity profile, wall shear stress, Δp |
| Flat plate boundary layer (laminar) | Blasius similarity solution | Velocity profile, δ₉₉ ≈ 5x/√Reₓ, C_f = 0.664/√Reₓ |
| *Optional:* differentially heated square cavity (natural convection, other physics and solver) | de Vahl Davis (1983) | Mean Nusselt number, maximum velocities, for Ra = 10³–10⁵ |

Rules:
- The **owner writes these tasks in the owner's own words**. Reference data and acceptance criteria are prepared by `cfd-reviewer` in M2.
- **No generality task is run before the frozen release** (§9.2). Nothing learned from them can flow back into prompts or code.
- The set may also include new variants of the development families (e.g. cavity Re = 400, cylinder Re = 40) written by the owner.

### 9.2 Evaluation protocol **[Claude's call]**
- **Blind evaluation.** No agent can read benchmark reference data or acceptance criteria during a run (they are not mounted in the executor sandbox and not in the knowledge base). Comparison happens only in the deterministic Scorer after the Critic's final verdict. Otherwise the system could iterate until it matched the reference, inflating the success rate.
- **Headline metric:** **benchmark success rate** = share of runs that pass the deterministic acceptance criteria. Secondary metrics: error vs. reference, Critic verdict, Critic accuracy (agreement of Critic verdict with Scorer result — itself a finding), attempts per stage, failure stage taxonomy, tokens, cost, wall time.
- **No models are trained or fine-tuned.** During development the system is debugged on the development tasks (§9); every fix obeys the generality rule (§6.3).
- **Frozen release for M3.** Before the campaign, prompts, knowledge notes, image and configuration are frozen as a tagged release (e.g. `v1.0.0`); every M3 run records that tag. No code or prompt changes during the campaign. Results are reported separately for the development tasks and the generality test set (§9.1); the generality result is the main evidence for the hypothesis.

## 10. Local development agents (Claude Code)

Claude (main session) coordinates: talks to the owner in Polish, plans, delegates, ensures nothing merges without review and owner approval. Subagents are defined in `.claude/agents/*.md` (committed).

| Agent | Role | Instruction essentials |
|---|---|---|
| `implementer` | Implements one plan task | TDD; only the task's scope; no secrets; ends with green tests and a plain-language change summary |
| `code-reviewer` | Critical review before PR | Starts without the author's context (diff + spec only); bugs, missing tests, security (especially execution of LLM-generated code); verdict approve / changes / reject; **must not rubber-stamp** |
| `cfd-reviewer` | Domain expert | Physics and numerics: solver choice, BCs, schemes, mesh quality criteria, benchmark references and thresholds; reviews cloud-agent prompts and knowledge notes and **blocks any case-specific content** (generality rule, §6.3) |
| `infra-engineer` | Bicep, Docker, GitHub Actions, Azure | Cost estimate with every change; **never deploys anything that costs money without owner approval**; auto/idle shutdown, budget alerts, least privilege; knows §3 constraints |
| `scribe` | Research journal | Entries in `docs/journal/` of five kinds: decision, obstacle + solution, milestone, experiment (with numbers), observation (agent behaviour). Max a few sentences each; test: "would this sentence appear in the report?". Larger architecture decisions also as ADRs in `docs/adr/`. Invoked by Claude at each milestone and end of each work session. **Backfills the decisions and obstacles of the 2026-10-27/28 design conversation** when the repo is created. |
| `report-writer` | Final report | Process in §11 |

Other Claude Code files:
- `CLAUDE.md` (root, ~100–150 lines): project summary and repo map with pointers to docs; language rule; working rules (TDD, PR flow, owner merges, no Azure spending without approval, no secrets, journal); key commands.
- `infra/CLAUDE.md`: subscription limits, allowed regions, mandatory shutdown, cost estimation.
- `.claude/settings.json`: allow tests, linting and image builds without prompting; always ask for Azure resource-creating commands and deployments.
- Cloud-agent prompts are **not** CLAUDE.md files; they live in `src/cfdagent/prompts/`.

**Owner approval points:** (1) design decisions / ADRs, (2) PR merge (description + green CI), (3) CFD results at milestones, (4) any Azure cost.

## 11. Research documentation and report

- Sources: journal + ADRs (build process), run records and `cfdagent bench` statistics (experiments, collected automatically), this design document.
- `report-writer` process:
  1. **Material inventory** — what exists and **what is missing** (e.g. missing model comparison), so experiments can still be added.
  2. **General outline** — chapters, one-sentence purpose and key message each, proposed length → owner approval.
  3. **Detailed outline** — subsections, key points, assigned figures/tables, source of every number → owner approval.
  4. **Chapter by chapter** — each delivered with a list of claims and their sources; owner approves or comments before the next. Order: methods and results first; introduction, conclusions and abstract last.
  5. **Consistency pass** — every number checked automatically against raw data; terminology, figure numbering, bibliography.
  6. **PDF build** (Pandoc + LaTeX template).
- Fixed rules: no claims beyond the data; results compared with reference data always distinguished from results assessed by physical checks only; development tasks and generality test set always reported separately; **Limitations chapter mandatory**; figures generated by scripts from data; BibTeX bibliography; concise scientific English without filler.
- Architecture and method chapters may be drafted from M2 onward.

## 12. Milestones

| Milestone | Scope | Done when |
|---|---|---|
| **M0 Foundations** | Repo, CI, Docker image (OpenFOAM ESI + Gmsh + CadQuery + Python stack), Bicep infra, LLM capability spike (§6.4) **for all three candidate models**, benchmark references and thresholds fixed | Cavity passes CadQuery → Gmsh → OpenFOAM → post-processing **from hand-written scripts, no LLM**, locally and on the VM |
| **M1 First autonomous run** | Orchestrator, agents 1–6, CLI, Blob upload, run record | Cavity Re = 100 from text to results with no human intervention |
| **M2 Robustness + full benchmark set** | Fix loops, Critic, Scorer, knowledge base; generality test set prepared (owner's task texts, references, criteria) but **not run** | Pipe and cylinder (Re 20, Re 100) pass |
| **M3 Experiment campaign** | Frozen release; `cfdagent bench`; first runs of the generality test set; matrix in §14.1 | Statistics ready for the report |
| **M4 Report** | §11 process | PDF approved by the owner |

## 13. Growth path (not in scope now)
1. Remote CLI: `cfdagent submit` starts the VM, uploads, waits, downloads.
2. Azure Container Apps Jobs: same image, zero idle cost, parallel runs (verify availability on the subscription first).
3. IssueOps in a dedicated repo (simulation requests separate from development issues).
4. Web UI (Gradio/Streamlit/Chainlit) with Entra login, live progress, result viewer.
5. Stronger models via configuration.
6. Larger compute: HPC VMs, Azure Batch/CycleCloud with MPI.
7. New physics: knowledge notes + benchmarks (RANS k-ω SST, heat transfer, …); snappyHexMesh path.
8. Optional exporter of agent-written text artefacts to git.

## 14. Budget sketch
- LLM: estimated $0.10–0.20 per run (≈50 calls × ≈15k tokens); 100 runs ≈ $10–20.
- VM: ≈ $0.19/h while running; ≈100 h ≈ $20; disk a few dollars per month.
- Storage: cents.
- Remaining credit is headroom. Budget alerts guard it.
- Runtime estimate per run (Claude's estimate, to be measured in M1): cavity minutes; pipe 10–30 min; cylinder Re = 100 (unsteady, ~20 shedding periods) ≈ 20–40 min; plus LLM time.

### 14.1 M3 experiment matrix **[Claude's call, accepted by the owner 2026-10-28]**
Priority order — lower rows are dropped first if money or time runs short:
1. **gpt-oss-120b, reasoning medium:** all 5 development variants + the generality test set (3–4 tasks), 5 repeats each.
2. **Model comparison:** Llama-3.3-70B only on a subset (cavity Re 100, cylinder Re 20, one generality task), 3 repeats. `qwen3-32b` is excluded (owner decision 2026-10-29, see ADR-0002).
3. **Reasoning effort:** gpt-oss-120b low and high on the same subset, 3 repeats each.

The optional heated-cavity task is the first thing dropped inside row 1. Estimated ≈ 65–75 runs, ≈ 30–40 VM-hours, ≈ $7 VM + ≈ $11 LLM (recomputed 2026-10-29 after dropping `qwen3-32b`, i.e. 9 runs fewer).

**Comparison-model fallback [Claude's call]:** if the M0 spike shows a candidate cannot do tool calling reliably, or its quota (20) makes runs impractically slow, it is replaced by another model with quota (Llama-4-Scout, mistral-small, Phi-4) or excluded. An exclusion and its reason are recorded as a finding. Weaker models never silently run through a different interface than gpt-oss-120b; any interface difference is stated in the results. The rule was applied in the M0 spike: `qwen3-32b` could not be deployed (ARM rejected both SKUs), and the owner declined a replacement on 2026-10-29, so the comparison has one model.

### 14.2 Spending envelopes (owner's decision, 2026-10-28)
| Phase | Envelope |
|---|---|
| M0–M2 development | ≤ $30 |
| M3 campaign | ≤ $45 |
| Reserve | ≈ $25, never spent without a separate owner decision |

- Inside an approved envelope, starting/stopping the VM and running tests/benchmarks need no per-action approval.
- Creating or changing Azure resources, and any campaign estimated above $5, always need the owner's approval.
- Spend is tracked from run records and reported at each milestone.

## 15. Left to the builder
- Exact OpenFOAM ESI version tag, base image and image size optimisation.
- Sandbox mechanism satisfying §6.7.
- `CaseSpec` schema details, gate thresholds (mesh quality, convergence), within the principles above; benchmark thresholds come from `cfd-reviewer`.
- Internal module structure, test fixtures format, VM disk size.
- Prompt wording (reviewed by `cfd-reviewer`).

## 16. Claude's calls for the owner to check
Gmsh; OpenFOAM ESI; PyVista/matplotlib; deterministic orchestrator without agent framework; VM first, single Docker image; managed identity / keyless; Bicep; no CI deployment in v1; lexical knowledge search without embeddings; default run limits (§6.5); Pandoc + LaTeX report; MIT license; uv / Python 3.12 / conventional commits; run ID format and Blob layout; report written in English.

Added after the builder check: **blind evaluation** with a deterministic Scorer after the Critic (§6.2, §9.2) — this replaces the earlier idea that the Analyst compares with literature inside the loop; the owner still receives the comparison in every summary. Reference comparison selected by the benchmark link, not by a classifier (§6.8). **Headline metric** = deterministic benchmark success rate; **frozen release** before M3 (§9.2). M0 spike covers all three candidate models; comparison-model fallback rule (§14.1). M3 matrix priorities (§14.1).

Changed on 2026-10-28 at the owner's request: task tiers removed; the "held-out set" of same-family variants replaced by the generality test set of new flow types; generality rule for prompts added (now owner's decisions, §4).

## 17. Resolved questions (owner, 2026-10-28)
1. **Spending envelopes and approval granularity** — accepted as proposed (§14.2).
2. **M3 matrix priorities** — accepted as proposed (§14.1).
3. **Repository name** — `cfd-agent`.

No open questions remain.
