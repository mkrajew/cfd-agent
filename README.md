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

Available from v0.1.0.

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
