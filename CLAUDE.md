# CLAUDE.md

Instructions for Claude Code working in this repository. Read them at the start of every session.

## Project

`cfd-agent` is a university research project that tests one hypothesis: **LLM-based agents can autonomously perform a complete CFD simulation from a natural-language task description** (geometry, meshing, case setup, solving, post-processing, analysis) without human intervention. The user writes a task in English; a deterministic orchestrator drives agents through CadQuery, Gmsh, OpenFOAM ESI and PyVista inside one Docker image, on an Azure VM, with models served by Azure AI Foundry (open-weight, chosen because of budget).

It is a proof of concept. A negative or partial result is a valid result; where and why agents fail is part of the value. Never overclaim: every result must state whether it was compared with reference data or assessed by physical checks only.

The full design is `docs/design/2026-10-28-cfd-agent-design.md`. It is the source of truth; when this file and the design disagree, the design wins. Ask the owner before deviating from it.

## Repository map

```
src/cfdagent/        cli, core API, orchestrator, agents, prompts/, tools/, llm/, knowledge/, storage/, bench/
benchmarks/<case>/   task files + reference data + acceptance criteria (never visible to agents during a run)
tests/               unit/ (default), integration/, e2e/, fixtures/ (recorded LLM responses, case fixtures)
docker/              Dockerfile (OpenFOAM ESI + Gmsh + CadQuery + Python stack)
infra/               Bicep, VM scripts; read infra/CLAUDE.md before touching it
examples/            a few curated successful runs (later; no results in git otherwise)
docs/design/         design document (approved 2026-10-28)
docs/adr/            architecture decision records
docs/journal/        research journal (format in docs/journal/README.md)
docs/report/         final report (written by report-writer, milestone M4)
docs/superpowers/    implementation plans
.claude/agents/      local development subagents; .claude/settings.json permissions
```

Cloud-agent prompts live in `src/cfdagent/prompts/` and are not CLAUDE.md files.

Where to look in the design: constraints section 3, pipeline and gates 6.2, cloud agents 6.3, sandbox 6.7, infrastructure 7, repository practices 8, benchmarks and evaluation 9, local agents 10, report process 11, milestones 12, budget 14.

## Language

- **Everything in the repository is English**: code, comments, docs, commit messages, PR text, journal, agent prompts.
- **Talk to the owner in Polish.** The owner is a mechanical engineer and programmer who approves decisions and does not read code; explain results in plain language.

## Working rules

1. **TDD.** Write a failing test first, watch it fail, implement, watch it pass. Unit tests use no real LLM and cost nothing.
2. **PR flow.** Conventional commits (`feat:`, `fix:`, `docs:`, `chore:`, `infra:`), branches `feat/...`, `fix/...`, `infra/...`, `docs/...`. Every task lands as a pull request with a description (what, why, how verified, Azure cost impact) and green CI. **The owner merges**; never merge or push to `main` yourself once branch protection exists. Do not push without being asked. Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`; PR descriptions end with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
3. **Review before PR.** Run `code-reviewer` on the diff (with the spec only, not your reasoning). Prompt, knowledge-note and tool-code changes also need `cfd-reviewer`. A `changes` or `reject` verdict blocks the PR.
4. **No Azure spending without approval.** Creating, changing or deleting Azure resources, and any campaign estimated above $5, always needs the owner's explicit approval in chat first. Inside an owner-approved spending envelope (M0-M2: at most $30), starting, stopping or deallocating the project VM and running tests or benchmarks need no extra chat approval; the permission prompt from `.claude/settings.json` is a safety net that the owner answers. Every infra change comes with a cost estimate.
5. **No secrets.** No keys, tokens, passwords, subscription IDs, IP addresses or e-mail addresses in the repository. Parameters that need them read environment variables. Authentication is keyless (managed identity, Entra ID). gitleaks runs in pre-commit.
6. **Generality rule.** Prompts, knowledge notes and tool code contain only general CFD/OpenFOAM knowledge, never anything specific to a test case. Tool code in `src/` must not mention cavity, pipe or cylinder. Improvements found while debugging are phrased as general rules. Case-specific content lives only in `benchmarks/` and `tests/fixtures/`. `cfd-reviewer` blocks violations.
7. **Blind evaluation.** Agents must never be able to read `benchmarks/` reference data or acceptance criteria during a run. In M0 keep them out of `src/cfdagent/knowledge/`.
8. **No simulation results in git.** Results go to Azure Blob Storage; `examples/` is curated by hand later.
9. **Pipeline invariants.** CadQuery creates every geometry; there is no blockMesh path (blockMesh only for test fixtures). 2D cases are one cell thick with `empty` front and back patches. Max 4 MPI ranks. OpenFOAM is ESI (openfoam.com) with the exact version pinned in the Dockerfile.
10. **Journal.** Invoke the `scribe` agent at each milestone and at the end of every work session. Larger architecture decisions also become ADRs.
11. **Line endings are LF** (`.gitattributes`); shell scripts run in Linux containers.
12. **Stay in scope.** Do only the task at hand; if the plan or spec is unclear or wrong, stop and ask the owner.

## Local agents (`.claude/agents/`)

| Agent | Use for |
|---|---|
| `implementer` | Implements one plan task with TDD, in scope, ending with green tests and a plain-language summary |
| `code-reviewer` | Independent review of a diff against the spec; verdict approve / changes / reject |
| `cfd-reviewer` | Physics, numerics, benchmark references and thresholds, generality-rule guard |
| `infra-engineer` | Bicep, Docker, GitHub Actions, Azure; cost estimate with every change; never deploys without approval |
| `scribe` | Research journal and ADRs |
| `report-writer` | Final report, six stages with owner approval |

The main session coordinates: it plans, delegates, and makes sure nothing merges without review and the owner's approval. Owner approval points: design decisions and ADRs, PR merge, CFD results at milestones, any Azure cost.

## Key commands

```bash
uv sync                                              # create the environment
uv run pytest                                        # unit tests (default: tests/unit)
uv run ruff check . && uv run ruff format --check .  # lint and format check
uv run pre-commit install                            # once per clone: run the hooks on every commit
uv run pre-commit run --all-files                    # ruff, gitleaks, line endings

docker build -f docker/Dockerfile -t cfdagent:dev .  # build the image (OpenFOAM ESI, Gmsh, CadQuery, PyVista)
# integration tests run inside the image, with the repository bind-mounted at /work
docker run --rm -v "$PWD:/work" -w /work cfdagent:dev pytest -m integration tests/integration
```

Integration tests need the image; on the host `uv run pytest -m integration` collects nothing (default `testpaths` is `tests/unit`), and `tests/integration` is skipped there because `WM_PROJECT_VERSION` is unset.

**Docker on the Windows laptop.** The command above works as written in PowerShell with `$PWD` written as `${PWD}`:

```powershell
docker run --rm -v "${PWD}:/work" -w /work cfdagent:dev pytest -m integration tests/integration
```

In Git Bash, MSYS rewrites `/work` into `C:/Program Files/Git/work` and Docker rejects the working directory. Prefix the command with `MSYS_NO_PATHCONV=1` (or `export MSYS_NO_PATHCONV=1` once per shell):

```bash
MSYS_NO_PATHCONV=1 docker run --rm -v "$PWD:/work" -w /work cfdagent:dev pytest -m integration tests/integration
```

Image notes: OpenFOAM ESI is sourced for `docker run <image> <cmd>` (entrypoint), `docker exec ... bash -c` (`BASH_ENV`) and interactive shells. The container user is `cfd` (uid 1000); the bind-mounted repository may belong to another uid (GitHub runner 1001), so tests must write only to pytest's `tmp_path`, never into `/work`. `cfdagent` is also installed in the image, but pytest puts `/work/src` first (`pythonpath = ["src"]`), so mounted sources win. PyVista renders off-screen through EGL (Mesa software rasteriser); no X server or GPU is needed.

On the Windows laptop Azure CLI is not on PATH: `C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd`. Every `az` call triggers a permission prompt (safety net); read-only calls are fine, and anything that creates, changes or deletes resources needs the owner's explicit chat approval first.

## Session routine

At the start of a session:

1. Read this file, then the plan task or the issue you are working on, then the design sections it cites.
2. Check `git status` and the current branch; do not start work on a dirty tree you did not make.
3. Look at the latest entries in `docs/journal/` to see what happened last.

At the end of a session:

1. Run `uv run pytest` and the lint commands; report what passed and what did not, with the command output.
2. Invoke `scribe` to record decisions, obstacles, experiments and observations of the session.
3. Summarise for the owner in Polish: what is done, what is verified, what needs a decision, any Azure cost incurred.

## Definition of done for a task

- The failing test existed first and now passes; the whole suite passes.
- Lint and format are clean; pre-commit hooks pass (never skip them).
- `code-reviewer` returned `approve` (and `cfd-reviewer` for prompts, notes and tool code).
- No secrets, no identifiers, no simulation results, no case-specific content in `src/`.
- The PR description states what, why, how verified and the Azure cost impact (zero if none).
- The journal is up to date for anything the report will need.

## Markers and conventions

- pytest markers: `integration` (needs Docker, OpenFOAM, Gmsh or services), `e2e` (real model, costs money, manual trigger only).
- Python 3.12, `uv`, `ruff` with line length 100.
- Deliberately omitted: CONTRIBUTING, CODE_OF_CONDUCT, CHANGELOG, project boards. Release notes replace a changelog.
