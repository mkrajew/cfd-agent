# cfd-agent

Describe a CFD task in plain English; LLM agents build the geometry, mesh and
OpenFOAM case, run it and report the results. The pipeline is CadQuery, Gmsh,
OpenFOAM and PyVista, driven by a deterministic orchestrator and agents backed
by Azure AI Foundry models. This is a research project; see
`docs/design/2026-10-28-cfd-agent-design.md` for the design.

## Architecture

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

## Quick start

Available from v0.1.0.

## Validation

To be added.

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
