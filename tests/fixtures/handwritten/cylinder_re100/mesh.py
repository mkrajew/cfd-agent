"""Hand-written mesh script: 2D flow past an isolated cylinder.

Unstructured quads in x-y and exactly one cell in z.

Reads ``geometry.step`` and ``boundary_manifest.json`` from the current working directory and
writes ``mesh.msh`` (MSH 2.2 ASCII, which ``gmshToFoam`` reads). It needs no domain extents: they
are in the STEP file.

One-layer approach: the STEP solid is only used to find its z-min face. That face is copied, the
solid is removed, and the copy is meshed as an unstructured, fully recombined quad grid and
extruded by the solid's thickness with a single recombined layer, giving one hexahedron through
the thickness. Boundary surfaces of the extruded volume are then named with
``assign_physical_groups``. ``Geometry.OCCTargetUnit`` is deliberately not set, so coordinates stay
in the STEP file's numbers.

Sizing: a background field alone decides the cell size (the sizes taken from the boundary points,
the curvature and the boundary extension are switched off). The size is fine at the cylinder and
grows by 10 % per cell with the distance from it up to SIZE_MAX; a box behind the cylinder keeps
the near wake finer. On top of that a boundary layer of quadrilaterals grows from the cylinder
surface (first cell BL_FIRST, growth BL_RATIO per layer, total thickness BL_THICKNESS): at this
Reynolds number the boundary layer is about D / sqrt(Re) = D / 10 thick, and the wall shear is a
noticeable part of the drag, so it must be resolved by more than a few cells.
"""

import gmsh

from cfdagent.manifest import MANIFEST_FILE_NAME, BoundaryManifest
from cfdagent.tools.gmsh_helpers import assign_physical_groups

RADIUS = 0.05  # cylinder radius; the cylinder centre is the origin
SIZE_WALL = 0.002  # cells on the cylinder surface: D / 50, 157 on the circumference
SIZE_WAKE = 0.006  # cells in the near wake box
WAKE_END = 1.5  # downstream end of the wake box: 15 D behind the cylinder
BL_FIRST = 0.0005  # first cell height at the cylinder surface: D / 200
BL_RATIO = 1.2  # growth of the layer height from one layer to the next
BL_THICKNESS = 0.01  # total thickness of the boundary layer cells: D / 10
WAKE_HALF_WIDTH = 0.25
SIZE_MAX = 0.5  # far field
GROWTH = 0.1  # size grows by this fraction of the distance from the cylinder
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

# The cylinder is the curve of the copied face that lies inside the circle's bounding box.
(face,) = [tag for _, tag in copied]
boundary = gmsh.model.getBoundary([(2, face)], oriented=False)
cylinder_curves = [
    tag
    for _, tag in boundary
    if max(abs(v) for v in gmsh.model.getBoundingBox(1, tag)[:2]) < RADIUS + EPS
    and max(abs(v) for v in gmsh.model.getBoundingBox(1, tag)[3:5]) < RADIUS + EPS
]
assert cylinder_curves, "no cylinder curve found on the base face"

# Background size field: Min(distance-based growth from the cylinder, wake box).
field = gmsh.model.mesh.field
field.add("Distance", 1)
field.setNumbers(1, "CurvesList", cylinder_curves)
field.setNumber(1, "Sampling", 200)
field.add("Threshold", 2)
field.setNumber(2, "InField", 1)
field.setNumber(2, "SizeMin", SIZE_WALL)
field.setNumber(2, "SizeMax", SIZE_MAX)
field.setNumber(2, "DistMin", 0.0)
field.setNumber(2, "DistMax", (SIZE_MAX - SIZE_WALL) / GROWTH)
field.add("Box", 3)
field.setNumber(3, "VIn", SIZE_WAKE)
field.setNumber(3, "VOut", SIZE_MAX)
field.setNumber(3, "XMin", RADIUS)
field.setNumber(3, "XMax", WAKE_END)
field.setNumber(3, "YMin", -WAKE_HALF_WIDTH)
field.setNumber(3, "YMax", WAKE_HALF_WIDTH)
field.setNumber(3, "ZMin", zmin - 1.0)
field.setNumber(3, "ZMax", zmax + 1.0)
field.setNumber(3, "Thickness", 1.0)
field.add("Min", 4)
field.setNumbers(4, "FieldsList", [2, 3])
field.setAsBackgroundMesh(4)
field.add("BoundaryLayer", 5)
field.setNumbers(5, "CurvesList", cylinder_curves)
field.setNumber(5, "Size", BL_FIRST)
field.setNumber(5, "Ratio", BL_RATIO)
field.setNumber(5, "Thickness", BL_THICKNESS)
field.setNumber(5, "Quads", 1)
field.setAsBoundaryLayer(5)

gmsh.option.setNumber("Mesh.MeshSizeFromPoints", 0)
gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 0)
gmsh.option.setNumber("Mesh.MeshSizeExtendFromBoundary", 0)
gmsh.option.setNumber("Mesh.MeshSizeMax", SIZE_MAX)

# Unstructured quad grid on the base face (Frontal-Delaunay for quads, then blossom recombination).
gmsh.option.setNumber("Mesh.Algorithm", 8)
gmsh.option.setNumber("Mesh.RecombinationAlgorithm", 1)
gmsh.model.mesh.setRecombine(2, face)

# One recombined layer through the thickness.
gmsh.model.occ.extrude(copied, 0, 0, zmax - zmin, numElements=[1], recombine=True)
gmsh.model.occ.synchronize()

assign_physical_groups(manifest)

gmsh.option.setNumber("Mesh.MshFileVersion", 2.2)
gmsh.option.setNumber("Mesh.Binary", 0)
gmsh.model.mesh.generate(3)
gmsh.write("mesh.msh")
gmsh.finalize()
