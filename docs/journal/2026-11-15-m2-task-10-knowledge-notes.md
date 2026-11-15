# 2026-11-15 M2 Task 10: curated knowledge notes searched before the tutorials

## 2026-11-15 milestone: Task 10 implemented, both reviewers approve (PR 82)

Seven curated notes and a README were added in `src/cfdagent/knowledge/notes/`: Gmsh, CadQuery, OpenFOAM v2512 function objects, steady SIMPLE set-up, transient time step, force coefficients and the axisymmetric wedge. They are searched before the tutorials through `search_knowledge` and `read_knowledge` (a note is read with `note:<file>`). Case Setup, Mesh, Geometry and Analyst now offer both tools and record `knowledge_reads`; the Analyst did not record them before. Process: two parallel implementers (code, notes); `cfd-reviewer` approved the notes after editing them; `code-reviewer` approved with non-blocking items, then one fix round and a short re-review, `approve`.

## 2026-11-15 observation: solver behaviour checked in the image before it went into the notes

Checked in the image (OpenFOAM v2512, Gmsh 4.15.2, CadQuery 2.8.0). Function objects and solver set-up:
- `type residuals;` is only a warning and writes nothing; `solverInfo` is the correct name.
- `relaxationFactors` nested inside `SIMPLE`, or absent, crashed a small laminar `simpleFoam` with a floating point exception and no message; top-level placement converged.
- `forces` without `rho rhoInf` stops; `forceCoeffs` without `CofR` and `rhoInf` stops; `pitchAxis` may print with flipped sign.
- `writeControl timeStep` with `adjustTimeStep` gives irregular write times and can end past `endTime`; `adjustableRunTime` hits write times and `endTime` exactly.

## 2026-11-15 observation: meshing and geometry pitfalls checked in the image

- `gmshToFoam` needs `system/controlDict`, sets all patches to type `patch` and puts ungrouped faces in `defaultFaces`.
- `addPhysicalGroup` before `occ.synchronize` is a silent no-op.
- A STEP file written by CadQuery is labelled millimetre but the numbers are unchanged.
- On a wedge case the `Uz` initial residual stays at about 5e-4 although `Uz` is about 1e-15, so `residualControl` on `U` never triggers; workaround: control `p`, `Ux`, `Uy`.
- Wedge meshes with unstructured recombined faces touching the axis at a point gave degenerate cells; use a transfinite mesh or a strip along the axis.

## 2026-11-15 decision: notes are general knowledge, but benchmark results may partly measure retrieval

The notes follow the generality rule (no case-specific content). A later benchmark pass may still partly measure how well the agent retrieves a note rather than how well it reasons; the report should keep that distinction.

## 2026-11-15 observation: deferred items

The notes README is visible to agents and ranks first for some queries; there is no way to list the notes; note operations need `FOAM_TUTORIALS` to be set. Deferred under the lean review workflow.
