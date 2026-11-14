"""Hand-written mesh script: 2D lid-driven cavity, N x N cells in x-y and exactly one cell in z.

Reads ``geometry.step`` and ``boundary_manifest.json`` from the current working directory and
writes ``mesh.msh`` (MSH 2.2 ASCII, which ``gmshToFoam`` reads).

One-layer approach: the STEP solid is only used to find its z-min face. That face is copied, the
solid is removed, and the copy is meshed as a structured quad grid (transfinite + recombine) and
extruded by the solid's thickness with a single recombined layer, giving one hexahedron through
the thickness. Boundary surfaces of the extruded volume are then named with
``assign_physical_groups``. ``Geometry.OCCTargetUnit`` is deliberately not set, so coordinates stay
in the STEP file's numbers.
"""

import gmsh

from cfdagent.manifest import MANIFEST_FILE_NAME, BoundaryManifest
from cfdagent.tools.gmsh_helpers import assign_physical_groups

# Cells per side. Chosen by mesh independence of the solution itself, not by its error against
# the reference: the sampled profiles at N = 128 differ from those at N = 256 by at most 0.004
# (u) and 0.007 (v) lid speeds, and the extreme values change by 0.4 % to 0.7 %; at N = 64 the
# differences are 0.019 and 0.032. At N = 128 the 129 sample points coincide with mesh vertices.
# The largest difference from the reference profiles, in lid speeds (acceptance threshold 0.05):
#   N     64     96     128    160    256
#   u   0.0183 0.0081 0.0054 0.0069 0.0058
#   v   0.0158 0.0047 0.0105 0.0128 0.0162
# The errors do not fall steadily with N, and that is not a sampling effect (sampling with 513
# instead of 129 points changes them by at most 2e-5). The reference is a 129 x 129 solution that
# is itself under-resolved at the extreme values by 0.005 to 0.011, so a converged solution
# scores about 0.006 (u) and 0.017 (v) against it; on coarse meshes the two errors partly cancel.
# The threshold therefore has about 3 times headroom for a converged solution.
N_IN_PLANE = 128
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

# Structured quad grid on the base face.
for _, curve in gmsh.model.getEntities(1):
    gmsh.model.mesh.setTransfiniteCurve(curve, N_IN_PLANE + 1)
gmsh.model.mesh.setTransfiniteSurface(copied[0][1])
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
