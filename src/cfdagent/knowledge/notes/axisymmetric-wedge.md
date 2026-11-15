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
   plane). Select the tilted faces by bounding box in the manifest, not by a `plane` locator.
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
4. Gmsh mesh: copy the planar face on the -z side (z of its bounding box at most 0), remove the
   solid, mesh the copy as recombined quadrilaterals, and revolve it about the x axis through
   the angle in radians: `gmsh.model.occ.revolve(copy, 0, 0, 0, 1, 0, 0, a_rad,
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
   Put sampling lines in the plane z = 0, between the two wedge faces.
8. The solution is for the slice: forces, flow rates and sums over patches cover only `a`
   degrees. Multiply them by `360 / a` for the whole body, and use the reference area of the
   same slice or whole body consistently (see `forces-and-coefficients.md`). Averages
   (`areaAverage`, `volAverage`) and point values need no scaling. A flow rate
   through a wedge cross-section is `sum` of `phi` times `360 / a`.
9. Convergence of a steady wedge case: the solver also solves the component normal to the
   wedge planes. Checked on a steady laminar run: the initial residuals of Ux, Uy and p fell
   to 1e-10 and below, but that of Uz stayed of order 1e-4 to 1e-3 for over a thousand
   iterations, although Uz itself was about 1e-15 (it is residual noise on a quantity that is
   zero). A `residualControl` entry on `U` (the largest component) is then never met and the
   run goes to `endTime`. Write the entries per component instead:
   `residualControl { p 1e-5; Ux 1e-5; Uy 1e-5; }` (checked: accepted, and the run stopped with
   "SIMPLE solution converged"). Confirm convergence also by the quantity of interest not
   changing between the last write times (`solverInfo` columns `Ux_initial`, `Uy_initial`,
   `p_initial`).

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
- Expecting `residualControl { U ... }` to stop the run (rule 9).
- An unstructured recombined face next to the axis: `defaultFaces` appears (rule 3).
