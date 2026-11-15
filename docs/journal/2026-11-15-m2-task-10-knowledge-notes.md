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

## 2026-11-15 obstacle: the wedge note advised a residualControl that does not work

Rule 9 of `axisymmetric-wedge.md` advised `residualControl { p 1e-5; Ux 1e-5; Uy 1e-5; }` (see the earlier observation on the `Uz` residual). The pipe calibration of Task 4 (PR 83, merged) showed that `residualControl` keys are field names and component keys are silently ignored: a run with `Ux` and `Uy` at 1e-14 still stopped at the iteration set by `p`. The `U` entry never converges in a wedge case (the `Uz` residual is noise, 1e-4 to 1e-2), and `p` at 1e-6 was too loose on a 4x finer mesh (profile error several times larger). The notes now say: control on `p` alone and prove convergence by invariance of the result under a tighter target.

## 2026-11-15 milestone: notes extended after the pipe calibration, with a guard test

Added to the notes: margin of the box locator, r/R taken from the mesh wall radius (flat wall at R cos(a/2), area factor sin(a)/a), kinematic `p` multiplied by rho, r-weighted averages, and the laminar entrance length 0.0575 Re D (Langhaar, uniform inlet) as rule 11 of `steady-solver-setup.md`. `tests/unit/test_knowledge_notes_content.py` checks a line limit and that no `residualControl` block contains component keys. `cfd-reviewer`: round 1 `changes` (7 findings, small wording fixes, no physics error), round 2 `approve`.

## 2026-11-15 observation: a check that cannot fail does not validate a note

The wedge rule had been "checked" on one run, yet it was wrong, because that check could not tell an ignored key from an effective one. For the report: a check of a control must include a run in which the control is deliberately made impossible (here, a residual target no run can reach) and must show that the run then behaves differently.
