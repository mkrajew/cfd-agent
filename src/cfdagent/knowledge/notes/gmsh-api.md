# Gmsh Python API for OpenFOAM meshes

Scope: the `gmsh` Python module (4.x) with the OpenCASCADE kernel, used to turn a STEP solid
into a mesh that `gmshToFoam` converts to an OpenFOAM `polyMesh`. Not covered: the Gmsh GUI,
the built-in `geo` kernel, parallel meshing.

## Rules

1. Use one kernel: `gmsh.model.occ` for everything (`importShapes`, `addRectangle`, `extrude`,
   `revolve`, `copy`, `cut`, `remove`). Do not mix it with `gmsh.model.geo`. Start with
   `gmsh.initialize()` and `gmsh.model.add("name")`, end with `gmsh.finalize()`.
   `gmsh.option.setNumber("General.Terminal", 0)` silences the log.
2. `gmsh.model.occ.importShapes("geometry.step")` returns `(dim, tag)` pairs. The numbers of the
   STEP file are kept as they are (no unit conversion); never set `Geometry.OCCTargetUnit`
   (with `"M"` a file written in metres is scaled by 1e-3).
3. Call `gmsh.model.occ.synchronize()` after every group of `occ` calls, before anything that
   reads or names entities: `getEntities`, `getBoundingBox`, `getBoundary`, `setTransfiniteCurve`,
   `setRecombine`, fields, `addPhysicalGroup`. Before it, `getEntities()` is empty and
   `addPhysicalGroup` is silently a no-op: no error, but the group does not exist.
4. One cell through the thickness of a 2D case: take the base face, `copy` it, remove the solid,
   mesh the face as quadrilaterals, extrude with `numElements=[1], recombine=True`:
   `out = gmsh.model.occ.extrude([(2, face)], 0, 0, thickness, numElements=[1], recombine=True)`.
   `out` is ordered: the far (top) face first, then the volume `(3, tag)`, then the side faces.
   Also call `gmsh.model.mesh.setRecombine(2, face)` on the base face, or the layer is made of
   prisms. Both the base face and `out[0]` are the front and back (`empty`) faces.
5. Name every boundary face and the volume with physical groups:
   `gmsh.model.addPhysicalGroup(2, [tags], name="patchName")` for each boundary surface
   (the front and back faces together in one group) and
   `gmsh.model.addPhysicalGroup(3, [volume], name="internal")` for the cells. The 2D group
   names become the patch names. A boundary face in no group lands in a patch `defaultFaces`
   (`gmshToFoam` prints "Found N undefined faces"); with no groups at all every face does.
   Names: letters, digits, underscore. Keep `Mesh.SaveAll` at its default 0. When the task
   gives a boundary manifest and the mesh prompt names a helper for this (such as
   `assign_physical_groups(manifest)`), use the helper after the geometry is final instead of
   naming faces by hand; the manual form above is what the helper does.
6. Write the mesh in the format that `gmshToFoam` expects, before `gmsh.write`:
   `gmsh.option.setNumber("Mesh.MshFileVersion", 2.2)` and `Mesh.Binary` 0, then
   `gmsh.model.mesh.generate(3)` and `gmsh.write("mesh.msh")`. The pipeline rejects other versions.
7. `gmshToFoam mesh.msh -case <dir>` needs `system/controlDict` in `<dir>`. It gives every patch
   the type `patch`; set `wall`, `empty`, `wedge`, `symmetryPlane` afterwards in
   `constant/polyMesh/boundary`, for example
   `foamDictionary -entry entry0/<patch>/type -set empty constant/polyMesh/boundary`.
8. Cell sizes. Points: `gmsh.model.mesh.setSize(gmsh.model.getEntities(0), h)`. Structured
   blocks: `setTransfiniteCurve(curve, n_cells + 1, "Progression", ratio)` (the second argument
   is the number of nodes, one more than cells), then `setTransfiniteSurface(surface)` and
   `setRecombine(2, surface)`. A progression follows the direction of the curve, and opposite
   curves of a block run in opposite directions: grade each curve on its own.
9. Size fields for unstructured regions: `Distance` (`CurvesList`, `Sampling`) feeding
   `Threshold` (`InField`, `SizeMin`, `SizeMax`, `DistMin`, `DistMax`), then
   `gmsh.model.mesh.field.setAsBackgroundMesh(threshold)`. Set `Mesh.MeshSizeFromPoints`,
   `Mesh.MeshSizeFromCurvature` and `Mesh.MeshSizeExtendFromBoundary` to 0 so that only the
   field decides. `Mesh.Algorithm` 8 (frontal-Delaunay for quads) with `setRecombine` gave an
   all-hexahedral one-cell layer.
10. Count cells with `types, tags, _ = gmsh.model.mesh.getElements(3)`; type 5 is a hexahedron,
    6 a prism, 4 a tetrahedron, so `sum(len(t) for t in tags)` is the cell count.
11. Check the result with `checkMesh`: cell types, `Mesh OK`, patch names, domain bounding box.

## Snippet: one-cell-thick layer from a STEP solid (adapt names and sizes)

```python
import gmsh
gmsh.initialize()
gmsh.model.add("m")
gmsh.model.occ.importShapes("geometry.step")
gmsh.model.occ.synchronize()
solid = gmsh.model.getEntities(3)
base = [t for _, t in gmsh.model.getEntities(2)
        if abs(gmsh.model.getBoundingBox(2, t)[5] - zmin) < 1e-6]    # face on the plane z = zmin
copy = gmsh.model.occ.copy([(2, base[0])])
gmsh.model.occ.remove(solid, recursive=True)
gmsh.model.occ.synchronize()
gmsh.model.mesh.setRecombine(2, copy[0][1])
out = gmsh.model.occ.extrude(copy, 0, 0, thickness, numElements=[1], recombine=True)
gmsh.model.occ.synchronize()
# out[0] = far face, out[1] = volume, out[2:] = side faces: name them by bounding box
gmsh.option.setNumber("Mesh.MshFileVersion", 2.2)
gmsh.option.setNumber("Mesh.Binary", 0)
gmsh.model.mesh.generate(3)
gmsh.write("mesh.msh")
gmsh.finalize()
```

Gmsh pads every bounding box by 1e-7, so compare coordinates with a tolerance of about 1e-6,
never with exact equality.

## Common mistakes

- No `synchronize()` before `addPhysicalGroup` or `getEntities`: empty results, missing groups,
  and a mesh with only `defaultFaces`.
- No 3D physical group: the cells are not written and `gmshToFoam` stops with "No cells read".
- Forgetting the front/back faces in a group: the `empty` patch is missing and `defaultFaces`
  appears.
- Reading the extrude result as `[volume, ...]`: the first entry is the far face.
- Extruding an unrecombined surface mesh: prisms, not hexahedra.
- Default MSH 4.1 output, or `Mesh.Binary` 1: use 2.2 ASCII.
- Removing the solid before `copy` of its face: `remove(..., recursive=True)` deletes the face
  too, so copy first.
- `setTransfiniteCurve` with the number of cells instead of the number of nodes.
- Using the tags of the inputs of `occ.cut` afterwards: they are removed; use the returned
  `outDimTags`, then synchronize.
