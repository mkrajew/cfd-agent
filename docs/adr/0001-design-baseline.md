# ADR-0001: The approved design document is the architecture baseline

- **Status:** Accepted, 2026-10-28
- **Deciders:** the owner

## Context

The project tests whether LLM-based agents can autonomously perform a complete CFD simulation from a natural-language task. Before any code existed, the design was worked out in a conversation between the owner and Claude on 2026-10-27/28. Along the way Azure constraints were verified (no GPU quota, 6 vCPU per region, serverless Foundry models only, `gpt-oss-20b` not deployable), and an independent builder check exposed methodology gaps that led to blind evaluation. The owner then removed task tiers, added the generality rule and replaced the held-out set with a generality test set. The result is written down in `docs/design/2026-10-28-cfd-agent-design.md`.

## Decision

The design document `docs/design/2026-10-28-cfd-agent-design.md`, as approved by the owner on 2026-10-28, is the architecture baseline of the project. In particular:

- a fixed pipeline of eight stages with deterministic gates, driven by a plain-Python orchestrator (CadQuery, Gmsh, OpenFOAM ESI, PyVista in one Docker image);
- LLM agents served serverless from Azure AI Foundry over an OpenAI-compatible interface, runnable on a single Azure VM, keyless via managed identity;
- blind evaluation with a deterministic Scorer, a frozen release before the experiment campaign, and a generality test set of new flow types;
- the generality rule for prompts, knowledge notes and tool code;
- the spending envelopes and the rule that no Azure resource is created or changed without the owner's approval.

Section 16 of the document lists the choices made by Claude (not by the owner); the owner accepted the document as a whole, without checking section 16 item by item.

## Consequences

- Implementation plans and code follow the design document. A change to an architectural decision in it needs a new ADR that supersedes the relevant part, approved by the owner, and the document is updated to match.
- Details the document leaves to the builder (section 15) are decided during implementation and recorded in the journal or in further ADRs when they are significant.
- The Claude Code project files (`CLAUDE.md`, `infra/CLAUDE.md`, `.claude/`) are derived from sections 3, 6.3, 8, 10 and 11 and must stay consistent with it.
