"""Hand-written mesh script: 2D channel with a cylinder.

Unstructured quads in x-y and exactly one cell in z.

Reads ``geometry.step`` and ``boundary_manifest.json`` from the current working directory and
writes ``mesh.msh`` (MSH 2.2 ASCII, which ``gmshToFoam`` reads).

One-layer approach: the STEP solid is only used to find its z-min face. That face is copied, the
solid is removed, and the copy is meshed as an unstructured, fully recombined quad grid and
extruded by the solid's thickness with a single recombined layer, giving one hexahedron through
the thickness. Boundary surfaces of the extruded volume are then named with
``assign_physical_groups``. ``Geometry.OCCTargetUnit`` is deliberately not set, so coordinates stay
in the STEP file's numbers.
"""

import gmsh

from cfdagent.manifest import MANIFEST_FILE_NAME, BoundaryManifest
from cfdagent.tools.gmsh_helpers import assign_physical_groups

SIZE_MAX = 0.03  # coarse far from the cylinder
SIZE_MIN = 0.006  # fine on the cylinder (set by curvature)
EPS = 1e-6

manifest = BoundaryManifest.from_file(MANIFEST_FILE_NAME)

gmsh.initialize()
gmsh.option.setNumber("General.Terminal", 0)
gmsh.model.add("mesh")
gmsh.model.occ.importShapes("geometry.step")
gmsh.model.occ.synchronize()

(volume,) = gmsh.model.getEntities(3)
xmin, ymin, zmin, xmax, ymax, zmax = gmsh.model.getBoundingBox(*volume)
(base,) = [
    tag
    for _, tag in gmsh.model.getEntities(2)
    if abs(gmsh.model.getBoundingBox(2, tag)[2] - zmin) < EPS
    and abs(gmsh.model.getBoundingBox(2, tag)[5] - zmin) < EPS
]

copied = gmsh.model.occ.copy([(2, base)])
gmsh.model.occ.remove([volume], recursive=True)
gmsh.model.occ.synchronize()

# Unstructured quad grid on the base face (Frontal-Delaunay for quads, then blossom recombination).
gmsh.option.setNumber("Mesh.MeshSizeMax", SIZE_MAX)
gmsh.option.setNumber("Mesh.MeshSizeMin", SIZE_MIN)
gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 36)
gmsh.option.setNumber("Mesh.Algorithm", 8)
gmsh.option.setNumber("Mesh.RecombinationAlgorithm", 1)
gmsh.model.mesh.setRecombine(2, copied[0][1])

# One recombined layer through the thickness.
gmsh.model.occ.extrude(copied, 0, 0, zmax - zmin, numElements=[1], recombine=True)
gmsh.model.occ.synchronize()

assign_physical_groups(manifest)

gmsh.option.setNumber("Mesh.MshFileVersion", 2.2)
gmsh.option.setNumber("Mesh.Binary", 0)
gmsh.model.mesh.generate(3)
gmsh.write("mesh.msh")
gmsh.finalize()
