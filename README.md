# cfd-agent

[![ci](https://github.com/mkrajew/cfd-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/mkrajew/cfd-agent/actions/workflows/ci.yml)

**Status: M2 closed as a partial result (2026-11-26).** In the M2 acceptance series (15 live runs of
five development benchmarks on one frozen build) 3 runs passed the comparison with reference data,
all three the lid-driven cavity at Re 100. The M2 goal, a pass for the pipe and for both cylinder
cases, was **not met**: 0 of 3 passes for each (see [Validation](#validation)). The next steps are in
`docs/design/m3-carryover-from-m2.md`.

Describe a CFD task in plain English; LLM agents build the geometry, mesh and
OpenFOAM case, run it and report the results. The pipeline is CadQuery, Gmsh,
OpenFOAM and PyVista, driven by a deterministic orchestrator and agents backed
by Azure AI Foundry models. This is a research project; see
`docs/design/2026-10-28-cfd-agent-design.md` for the design.

## Architecture

```
 Owner's laptop                               Azure (VM, storage: belgiumcentral; AI: italynorth)
 ──────────────                               ─────────────────────────────────────────────────────
 az vm start / VS Code Remote-SSH ──SSH──►    VM Standard_D4as_v4 (Ubuntu 24.04, Docker)
                                               └─ cfdagent CLI → Docker image
                                                    ├─ Orchestrator (deterministic)
                                                    ├─ LLM agents ─────────► Azure AI Foundry
                                                    │                         (gpt-oss-120b, …)
                                                    └─ Sandboxed tools:
                                                       CadQuery → Gmsh → OpenFOAM → PyVista
 Portal / Storage Explorer / az / scp ◄──────── Blob Storage: runs/<run-id>/ (everything)
                                               Budget alerts · auto-shutdown · idle-shutdown
```

## Quick start

`cfdagent run` needs three things: Docker with the sandbox image (the agents' scripts run in
containers without network access), a model endpoint, and optionally a Blob Storage account for the
results. Authentication is keyless (Entra ID, for example `az login` or a managed identity), so no
key is involved.

**On the project VM** (the setup that was verified in M1) `scripts/vm-host-setup.sh <commit>` installs
the command, the image and `/etc/cfdagent/env`; see `docs/runbooks/vm-host.md`. The `cfdagent`
wrapper loads that file itself, so the commands below work as written from the host checkout
`/opt/cfdagent-src`.

**On another machine** with Docker, Python 3.12 and [uv](https://docs.astral.sh/uv/) (not run end to
end in M1; prefix the commands with `uv run`):

```bash
uv sync
docker pull ghcr.io/mkrajew/cfd-agent:latest      # or: docker build --target runtime -t cfdagent:runtime -f docker/Dockerfile .
export CFDAGENT_IMAGE="ghcr.io/mkrajew/cfd-agent:latest"   # default: cfdagent:runtime; `latest` moves with every merge, use `sha-901dcf8` for the build of the M2 acceptance series
export CFDAGENT_LLM_ENDPOINT="https://<account>.services.ai.azure.com/openai/v1/"
export CFDAGENT_STORAGE_ACCOUNT_URL="https://<storage-account>.blob.core.windows.net"  # optional
export CFDAGENT_RUNS_DIR="$HOME/runs"                                                    # default
```

Use your own values for the placeholders. `CFDAGENT_LLM_AUTH=api_key` with `CFDAGENT_LLM_API_KEY`
selects key authentication instead of Entra ID.

Run a task written in a Markdown file, or a registered benchmark. `--benchmark` reads `benchmarks/`
relative to the current directory, so start it from the repository root (the image does not ship
`benchmarks/`, so that agents cannot read the reference data):

```bash
cfdagent run task.md
cfdagent run --benchmark cavity-re100
cfdagent run task.md --model <deployment> --reasoning-effort high   # only deployments with a known price; high effort is untested
cfdagent run task.md --no-upload      # keep the results on this machine only
```

`cfdagent run` waits for the run and prints `run <run-id>: <status>`, then the cost and the run
directory. The exit code is 0 for a completed run, 1 for a failed one, 130 for an interrupted one and
2 for a usage error. Without `CFDAGENT_STORAGE_ACCOUNT_URL` the upload is skipped and the run
directory under `CFDAGENT_RUNS_DIR` holds everything. The two completed runs of M1 took 12 and 16
minutes and cost $0.11 and $0.18 in model tokens with the default model (`gpt-oss-120b`, medium
reasoning effort); failed runs ended after 1 to 21 minutes. The default cost cap is $1.00 per run.

A long run can go to the background and be followed from another terminal or after logging out:

```bash
cfdagent run task.md --detach         # prints the run id and returns
cfdagent status <run-id>              # state, stage, cost; a dead run shows as "interrupted"
cfdagent logs <run-id> --follow       # stage events, until the run has ended
cfdagent list                         # recent runs with status and cost (--limit N)
```

Local copies: after a run that was uploaded, only the newest 10 run directories stay under
`CFDAGENT_RUNS_DIR`. A run whose upload failed, was skipped or was incomplete is never deleted, so
nothing is lost before it reached Blob Storage.

## Validation

**Tools (M0).** The deterministic toolchain (CadQuery, Gmsh, OpenFOAM `icoFoam` and the scorer) was
checked with a hand-written, LLM-free lid-driven cavity at Re 100 against the tabulated centreline
velocities of Ghia et al. (1982). The maximum absolute errors are 0.0049 for u and 0.0087 for v
(acceptance threshold 0.05). The test passes in CI and on the Azure VM.

**Agents (M2 acceptance series, 2026-11-25).** One frozen build (image
`ghcr.io/mkrajew/cfd-agent:sha-901dcf8`), model `gpt-oss-120b` with medium reasoning effort, repair
mode `in_place` (`--repair-mode in_place`; the default is `fresh`), default run limits. The pipeline
now has the Critic, cross-stage fix loops and a solver convergence gate. Each of the five development
benchmarks was run 3 times (n = 3 per benchmark, 15 runs), with no code or prompt change during the
series. "Passed" means the deterministic Scorer compared the results **against reference data** that
the agents cannot read.

| Benchmark | Runs | Completed | Passed the Scorer | Where the runs that did not complete ended |
|---|---|---|---|---|
| Cavity, Re 100 | 3 | 3 | **3** | none |
| Cavity, Re 1000 | 3 | 1 | 0 | Analyst, Case Setup |
| Pipe, Re 500 | 3 | 0 | 0 | Geometry (2), Case Setup |
| Cylinder, Re 20 | 3 | 0 | 0 | Case Setup (2), Planner |
| Cylinder, Re 100 | 3 | 0 | 0 | Case Setup (2), Geometry |

- The three passing runs had maximum absolute errors of 0.0035 to 0.030 (u on the vertical
  centreline) and 0.0075 to 0.019 (v on the horizontal centreline), against a threshold of 0.05.
- The completed Re 1000 run failed the comparison: errors 0.072 (u) and 0.069 (v), threshold 0.05.
- Runs that did not complete were not scored, so nothing is known about the accuracy of a solution
  in those cases. The pipe and cylinder results are 0 passes in 3 runs each.
- The Critic **disagreed with the Scorer in 2 of the 4 completed runs**: it reported success for the
  Re 1000 run that failed the comparison, and "partial" for a Re 100 run that passed it. A Critic
  verdict is therefore not evidence of correctness. On a task without reference data a result is
  assessed by physical checks only.
- Tracked model cost of the 15 runs was $1.93 (an upper bound: cached tokens are counted at the full
  price), $0.02 to $0.37 per run. The four completed runs took 9 to 48 minutes.
- A first attempt at the series was lost because the VM disk filled up; its four runs are
  infrastructure failures and are not counted.
- n = 3 per benchmark with one model is an observation, not a success rate.
- The OpenFOAM tutorials are searchable by the agents, and some are close relatives of the
  benchmarks. A pass partly measures retrieval and adaptation of a stock set-up, and says little
  about unfamiliar problems. The generality test set, meant to measure that, is not written yet.

The series is described in `docs/journal/2026-11-25-m2-task-19-acceptance-series.md`.

**Agents (M1, history).** On commit `eae8031`, five unchanged runs of `cfdagent run --benchmark cavity-re100`
(`gpt-oss-120b`, medium reasoning effort, nobody touched the runs and nothing was edited between
them) gave 2 completed and 3 failed runs. Both completed runs were scored by the deterministic Scorer
**against the reference data** of Ghia et al. (1982), which the agents cannot read, and passed:

| Run | u on the vertical centreline | v on the horizontal centreline | Time | Model cost |
|---|---|---|---|---|
| Run 23 (first of the five) | max error 0.0042 | max error 0.0081 | 16 min | $0.18 |
| Run 27 (last of the five) | max error 0.0151 | max error 0.0119 | 12 min | $0.11 |

The threshold is 0.05 for each profile (maximum absolute difference, in units of the lid speed). The
three failures ended in the mesh stage (a wrong file path in the agent's script), in Case Setup (the
turn budget ran out) and in Case Setup (required files missing in all three attempts).

What this does and does not show:

- n = 5, one model, one benchmark. 2 of 5 is a first data point, not a success rate. A sixth run on
  the same commit with default settings, started afterwards to check this quick start, failed in
  Case Setup (turn budget), which makes it 2 of 6. Not counted: two default-setting runs of the same
  check that were stopped on purpose, and two runs with `--reasoning-effort high` (one failed in the
  Planner, the other was stopped). Over the 27 live runs of the M1 campaign only the two completed; the 22 earlier runs failed while bugs in the tools and prompts were found and
  fixed between series, so they are not repeats of one configuration.
- Convergence by the solver's own criterion is **not shown**: no `residualControl` stop was recorded in
  either completed run (the solver finished; the final initial residuals were about
  4e-06 in one run and up to 7e-05 in the other). The results stand on the comparison with
  reference data, which is a check of the answer, not of the iteration.
- The M1 pipeline had no Critic and no repair loop: each stage got up to three attempts, every one
  from scratch, and then the run failed.
- One of the searchable tutorials is a close relative of this case. In M1 the other four benchmarks
  were not run by the agents.

The run records, transcripts and plots are in Blob Storage, not in git. The M1 campaign is
described in `docs/journal/2026-11-12-m1-task-23-first-completed-runs.md` and the entries before it.

## Roadmap

1. Remote CLI: `cfdagent submit` starts the VM, uploads, waits, downloads.
2. Azure Container Apps Jobs: same image, zero idle cost, parallel runs.
3. IssueOps in a dedicated repo (simulation requests separate from development issues).
4. Web UI (Gradio/Streamlit/Chainlit) with Entra login, live progress, result viewer.
5. Stronger models via configuration.
6. Larger compute: HPC VMs, Azure Batch/CycleCloud with MPI.
7. New physics: knowledge notes and benchmarks (RANS k-ω SST, heat transfer, ...); snappyHexMesh path.
8. Optional exporter of agent-written text artefacts to git.

## License

MIT. See [LICENSE](LICENSE).
