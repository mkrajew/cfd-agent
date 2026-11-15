"""Hand-written geometry script: laminar pipe flow as an axisymmetric wedge.

The pipe (radius R = 5 mm, length L = 0.5 m, axis along x) is modelled as a wedge of angle
WEDGE_ANGLE_DEG about the x axis, symmetric about the xy plane: points (x, r cos(t), r sin(t)) with
|t| <= angle / 2. Writes ``geometry.step`` and ``boundary_manifest.json`` into the current working
directory.

The solid has four kinds of face: ``inlet`` (x = 0), ``outlet`` (x = L), ``wall`` (the cylindrical
sector at r = R) and two planar ``wedge`` faces. The tilted wedge faces and the curved wall have no
axis-aligned plane, so they are selected by boxes: a wedge face lies within y <= R cos(angle / 2)
on one side of z = 0, the wall reaches y = R and has z of both signs.
"""

import json
import math

import cadquery as cq

LENGTH = 0.5  # [m]
RADIUS = 0.005  # [m]
WEDGE_ANGLE_DEG = 5.0
MARGIN = 1e-6  # [m], above Gmsh's padding of bounding boxes and below any feature size

half = math.radians(WEDGE_ANGLE_DEG) / 2
cos_half, sin_half = math.cos(half), math.sin(half)

# Rectangle (x, r) in the xy plane, revolved about the x axis, then turned to be symmetric about
# the xy plane. One edge of the rectangle lies on the axis, so that face of the sweep has no area.
profile = cq.Workplane("XY").rect(LENGTH, RADIUS, centered=False)
solid = profile.revolve(WEDGE_ANGLE_DEG, (0, 0, 0), (1, 0, 0)).rotate(
    (0, 0, 0), (1, 0, 0), -WEDGE_ANGLE_DEG / 2
)
cq.exporters.export(solid, "geometry.step")


def plane(axis, value):
    return {"kind": "plane", "axis": axis, "value": value}


def region(xmin, ymin, zmin, xmax, ymax, zmax):
    return {"kind": "box", "box": [xmin, ymin, zmin, xmax, ymax, zmax]}


manifest = {
    "dimensionality": "axisymmetric",
    "patches": [
        {"name": "inlet", "type": "patch", "locators": [plane("x", 0.0)]},
        {"name": "outlet", "type": "patch", "locators": [plane("x", LENGTH)]},
        {
            "name": "wall",
            "type": "wall",
            "locators": [
                region(
                    0.0,
                    RADIUS * cos_half - MARGIN,
                    -RADIUS * sin_half - MARGIN,
                    LENGTH,
                    RADIUS + MARGIN,
                    RADIUS * sin_half + MARGIN,
                )
            ],
        },
        {
            "name": "wedgeNegative",
            "type": "wedge",
            "locators": [
                region(
                    0.0,
                    -MARGIN,
                    -RADIUS * sin_half - MARGIN,
                    LENGTH,
                    RADIUS * cos_half + MARGIN,
                    MARGIN,
                )
            ],
        },
        {
            "name": "wedgePositive",
            "type": "wedge",
            "locators": [
                region(
                    0.0,
                    -MARGIN,
                    -MARGIN,
                    LENGTH,
                    RADIUS * cos_half + MARGIN,
                    RADIUS * sin_half + MARGIN,
                )
            ],
        },
    ],
}
with open("boundary_manifest.json", "w", encoding="utf-8") as f:
    json.dump(manifest, f, indent=2)
    f.write("\n")
