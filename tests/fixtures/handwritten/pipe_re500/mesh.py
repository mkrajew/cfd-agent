"""Hand-written mesh script: the pipe wedge, structured, one cell through the wedge angle.

Reads ``geometry.step`` and ``boundary_manifest.json`` from the current working directory and
writes ``mesh.msh`` (MSH 2.2 ASCII, which ``gmshToFoam`` reads).

One-layer approach (as in the cavity): the STEP solid is only used to find its planar wedge face on
the negative-z side. That face is copied, the solid is removed, the copy is meshed as a structured
quad grid (transfinite + recombine) and revolved about the x axis through the wedge angle with a
single recombined layer. The cells next to the axis collapse into prisms (the quad edge on the axis
has no extent after the sweep); every other cell is a hexahedron. The boundary surfaces of the
swept volume are named with ``assign_physical_groups``.
"""

import math

import gmsh

from cfdagent.manifest import MANIFEST_FILE_NAME, BoundaryManifest
from cfdagent.tools.gmsh_helpers import assign_physical_groups

WEDGE_ANGLE_DEG = 5.0  # as in geometry.py
# Cells along the pipe and across the radius. Chosen by mesh independence of the solution itself,
# not by its error against the reference. Largest difference of the sampled profile from the
# analytic one (in units of the centreline velocity, threshold 0.03) and the pressure drop in Pa
# (analytic 1.6), with the stopping criterion of case/system/fvSolution (profile sampled at the
# 21 reference positions, as the Scorer does):
#   axial x radial   125x10   250x10   250x20   500x20   250x40   500x40
#   cells             1250     2500     5000    10000    10000    20000
#   profile error    0.0097   0.0097   0.0036   0.0036   0.0014   0.0015
#   pressure drop    1.601    1.602    1.607    1.607    1.609    1.609
# Doubling the axial count changes nothing; across the radius the error falls by about 2.5 per
# doubling. The pressure drop settles 0.6 % above the analytic value for two reasons: the wall of
# the one-cell wedge is flat, at R cos(angle / 2) on the mid-plane, which raises the gradient by
# about 0.2 %, and the flow is not yet exactly fully developed between the two planes (the
# gradient is 0.8 % above its fully developed value at x = 0.35 m and 0.3 % at 0.45 m), about
# 0.4 % (the gradient per 0.1 m falls with x towards about 1.602 Pa). The profile error of
# 250 x 20 is an eighth of the threshold, and the run takes seconds.
N_AXIAL = 250
N_RADIAL = 20
EPS = 1e-6  # Gmsh pads every bounding box by 1e-7

manifest = BoundaryManifest.from_file(MANIFEST_FILE_NAME)

gmsh.initialize()
gmsh.option.setNumber("General.Terminal", 0)
gmsh.model.add("mesh")
gmsh.model.occ.importShapes("geometry.step")
gmsh.model.occ.synchronize()

(volume,) = gmsh.model.getEntities(3)


def is_negative_wedge_face(tag):
    """The planar wedge face on the negative-z side (spans the length, z < 0, not z > 0)."""
    xmin, _, zmin, xmax, _, zmax = gmsh.model.getBoundingBox(2, tag)
    return xmax - xmin > EPS and zmin < -EPS and zmax < EPS


(side,) = [tag for _, tag in gmsh.model.getEntities(2) if is_negative_wedge_face(tag)]
copied = gmsh.model.occ.copy([(2, side)])
gmsh.model.occ.remove([volume], recursive=True)
gmsh.model.occ.synchronize()

for _, curve in gmsh.model.getEntities(1):
    box = gmsh.model.getBoundingBox(1, curve)
    along_axis = box[3] - box[0] > EPS
    gmsh.model.mesh.setTransfiniteCurve(curve, (N_AXIAL if along_axis else N_RADIAL) + 1)
gmsh.model.mesh.setTransfiniteSurface(copied[0][1])
gmsh.model.mesh.setRecombine(2, copied[0][1])

gmsh.model.occ.revolve(
    copied, 0, 0, 0, 1, 0, 0, math.radians(WEDGE_ANGLE_DEG), numElements=[1], recombine=True
)
gmsh.model.occ.synchronize()

assign_physical_groups(manifest)

gmsh.option.setNumber("Mesh.MshFileVersion", 2.2)
gmsh.option.setNumber("Mesh.Binary", 0)
gmsh.model.mesh.generate(3)
gmsh.write("mesh.msh")
gmsh.finalize()
