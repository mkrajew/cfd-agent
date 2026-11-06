# cfd-agent

[![ci](https://github.com/mkrajew/cfd-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/mkrajew/cfd-agent/actions/workflows/ci.yml)

**Status: M0 complete (v0.0.1).** The deterministic tools, stage gates, benchmark definitions,
Azure infrastructure and the LLM client are in place; the agents themselves start in M1.

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

Install the dependencies and the `cfdagent` command (Python 3.12 and [uv](https://docs.astral.sh/uv/)):

```bash
uv sync
```

The agents need a model endpoint; results are uploaded to Blob Storage when an account is
configured. Authentication is keyless (Entra ID), so no key is involved. Use your own values for
the placeholders:

```bash
export CFDAGENT_LLM_ENDPOINT="https://<account>.services.ai.azure.com/openai/v1/"
export CFDAGENT_STORAGE_ACCOUNT_URL="https://<storage-account>.blob.core.windows.net"  # optional
export CFDAGENT_RUNS_DIR="$HOME/runs"                                                   # default
```

Run a task written in a Markdown file, or a registered benchmark:

```bash
cfdagent run task.md
cfdagent run --benchmark <benchmark-id>
cfdagent run task.md --model <deployment> --reasoning-effort high
cfdagent run task.md --no-upload      # keep the results on this machine only
```

`cfdagent run` waits for the run and prints one line `run <run-id>: <status>` with the cost and the
run directory. The exit code is 0 for a completed run, 1 for a failed one, 130 for an interrupted
one and 2 for a usage error. Without `CFDAGENT_STORAGE_ACCOUNT_URL` the upload is skipped and the
run directory under `CFDAGENT_RUNS_DIR` holds everything.

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

The deterministic toolchain (CadQuery, Gmsh, OpenFOAM `icoFoam` and the scorer) was checked end to
end with a hand-written, LLM-free lid-driven cavity at Re 100 against the tabulated centreline
velocities of Ghia et al. (1982). The maximum absolute errors are 0.0049 for u and 0.0087 for v
(acceptance threshold 0.05). The test passes in CI and on the Azure VM. This validates the tools and
the scoring path for that one case only; it says nothing yet about the agents or other flow types.

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
