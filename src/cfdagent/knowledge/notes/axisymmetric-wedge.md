# Axisymmetric flow with a wedge mesh

Scope: a flow without swirl, symmetric about an axis, solved on a thin wedge of the solid of
revolution, one cell thick in the angular direction, with `wedge` patches (OpenFOAM ESI
v2512). Statements marked "checked" were run: CadQuery wedge, Gmsh revolve, `gmshToFoam`,
`checkMesh` and a laminar `simpleFoam` run.

## Rules

1. Geometry: the axis of revolution is the x axis, the wedge lies at y >= 0 and is symmetric
   about the x-y plane (z from `-R sin(a/2)` to `+R sin(a/2)` at the largest radius `R`, `a`
   the opening angle). Keep `a` small (a few degrees, 5 or less). Build it from the half cross-section in the x-y
   plane: `Workplane("XY").moveTo(0, 0).lineTo(L, 0).lineTo(L, R).lineTo(0, R).close()
   .revolve(a, (0, 0, 0), (1, 0, 0)).rotate((0, 0, 0), (1, 0, 0), -a / 2)` (angle in degrees;
   checked: the bounding box is then z in `+-R sin(a/2)`). Draw with absolute points rather
   than `center(...)`, which would move the revolve axis (see `cadquery-api.md`).
2. The wedge has two planar side faces, one on the +z side and one on the -z side. Each is
   its own patch of type `wedge`; never put both faces in one patch (a wedge patch has one
   plane). Select the tilted faces and the curved outer wall by bounding box in the manifest, not by a
   `plane` locator; give the box a small margin (the gate already tolerates about 1e-6 of the model size); the
   bounding box of a curved face can be slightly larger than its geometric extent.
3. The axis itself has no face when the mesh is built with Gmsh: the cells next to the axis are
   prisms (checked: a revolved structured quadrilateral layer, made with `setTransfiniteCurve`
   and `setTransfiniteSurface`, gave hexahedra plus one prism per cell along the axis, no
   patch for the axis, and `checkMesh` printed `Mesh OK`). No axis patch is needed in the
   field files. Checked on the same geometry with an unstructured recombined face (size
   field only): a few cells touching the axis at a single point produced degenerate faces,
   `gmshToFoam` put them in `defaultFaces` and `checkMesh` failed. So mesh the face with
   transfinite blocks, or at least the strip of cells along the axis, so that every cell
   next to the axis has a whole edge on it. A mesh from another tool that has a patch of
   zero-area faces on the axis names it `type empty;` in the stock wedge tutorials, and then
   every field needs `type empty;` on it.
4. Gmsh mesh: get the planar face on the -z side with `base_face("axisymmetric")` from
   `cfdagent.tools.gmsh_helpers` (it is tilted, so a search for a face of zero z extent
   fails), copy it, remove the solid, mesh the copy as recombined quadrilaterals, and revolve
   it about the x axis through the angle in radians: `gmsh.model.occ.revolve(copy, 0, 0, 0, 1, 0, 0, a_rad,
   numElements=[1], recombine=True)`. The result is ordered like an extrusion: the far face
   (the +z wedge face) first, then the volume `(3, tag)`, then the other faces (checked). The
   copy is the -z wedge face. Name both wedge faces, the volume and every other face as in
   `gmsh-api.md`.
5. Patch types: `gmshToFoam` makes every patch `patch`; set the two wedge patches to `wedge`
   with `foamDictionary -entry entry0/<patch>/type -set wedge constant/polyMesh/boundary`.
   `checkMesh` then reports "Mesh has 2 geometric (non-empty/wedge) directions" and lists each
   wedge patch with an angle (it printed 2.5 degrees per patch for a 5 degree wedge: half of
   the opening). A wall patch of the outer boundary is a normal `wall`.
6. Fields: every field file of `0/` has `type wedge;` on both wedge patches (the same type
   as the mesh patch, no value). The velocity is a normal 3-vector: the component normal to
   the wedge planes stays at round-off (checked: |Uz| about 1e-14 while Ux, Uy were of order
   1). There is no swirl in a wedge model. Axis-parallel flow uses x as the axial coordinate and
   y as the radius.
7. The mesh has flat faces: its vertices lie on the true radii, but the outer wall in the
   mid-plane (z = 0) is at `R cos(a/2)`, not at `R` (checked: for a 5 degree wedge it lay at 0.99905 R). Sample lines and probe points must lie inside the mesh: take radial end points from
   the mesh bounding box, not from the ideal radius; points outside are dropped silently.
   Put sampling lines in the plane z = 0, between the two wedge faces. For a profile
   against `r / R`, divide by the radius of the mesh wall in that plane, so that the profile
   spans exactly 0 to 1 (a mean velocity from a flow rate must use the mesh flow area, not the
   ideal circle); the cross-section of the slice is smaller than the ideal one by the
   factor `sin(a) / a` (checked: 0.13 % for a 5 degree wedge), which matters only for tight
   comparisons.
8. The solution is for the slice: forces, flow rates and sums over patches cover only `a`
   degrees. Multiply them by `360 / a` for the whole body, and use the reference area of the
   same slice or whole body consistently (see `forces-and-coefficients.md`). Averages
   (`areaAverage`, `volAverage`) and point values need no scaling. The pressure of a kinematic
   incompressible solver is `p / rho`: multiply by the fluid density to report pascals. When
   you average sampled line values or cell values yourself over a wedge cross-section, weight
   them by the radius `r` (`sum(f r dr) / sum(r dr)`), not a plain mean; `areaAverage` and
   `volAverage` already weight by face area or cell volume. A flow rate
   through a wedge cross-section is `sum` of `phi` times `360 / a`.
9. Convergence of a steady wedge case: the solver also solves the component normal to the
   wedge planes. Checked on steady laminar runs: the initial residuals of Ux, Uy and p fell
   to 1e-10 and below, but that of Uz stayed between 1e-4 and 1e-2 for thousands of
   iterations, although Uz itself was about 1e-15 (residual noise on a quantity that is zero).
   A `residualControl` entry on `U` takes the largest component, so it is never met and the
   run goes to `endTime` (checked: the iteration cap was reached). The keys of `residualControl`
   are field names: a component key such as `Ux` is accepted and silently ignored (checked:
   with `Ux` and `Uy` at 1e-14 the run still stopped at the iteration set by `p`). So control a
   wedge run on `p` alone: `residualControl { p <target>; }` with the linear-solver `tolerance`
   far below the target, and compare the last initial residuals of each field in the log with
   the targets. Prove convergence by invariance: tighten the target by two
   orders of magnitude (or double the iterations) and check that the sampled quantities do not
   move. A loose target can pass on a coarse mesh and not on a fine one (checked: one target
   was enough at a few thousand cells and left a profile error several times larger at four
   times the cells), so choose the target from this check, not from the answer.

## Snippet: wedge patches in a field file

```
boundaryField
{
    wedgeNegZ { type wedge; }
    wedgePosZ { type wedge; }
    wall      { type noSlip; }
    // inlet, outlet as in any case
}
```

## Common mistakes

- One `wedge` patch holding both side faces, or side faces of type `patch` or `empty`.
- Opening angle in degrees given to Gmsh `revolve` (it takes radians); CadQuery `revolve`
  takes degrees.
- `Workplane("XY").center(...)` before `revolve`: the axis moves.
- A sample line reaching the ideal radius: part of it is outside the mesh and is dropped.
- Forces or flow rates of the slice reported as those of the whole body.
- A wedge angle of tens of degrees: the flat faces and the curvature error grow; keep it small.
- Expecting `residualControl { U ... }` to stop the run, or `Ux` / `Uy` keys to do anything
  (rule 9): control on `p`.
- An unstructured recombined face next to the axis: `defaultFaces` appears (rule 3).
