"""Hand-written geometry script: 2D channel with a circular cylinder (flow past a cylinder).

Channel 2.2 x 0.41 in x-y, one cell thick in z (0 to 0.1), with a cylinder hole of radius 0.05
centred at (0.2, 0.2). Writes ``geometry.step`` and ``boundary_manifest.json`` into the current
working directory.
"""

import json

import cadquery as cq

LENGTH, HEIGHT, THICKNESS = 2.2, 0.41, 0.1
CX, CY, RADIUS = 0.2, 0.2, 0.05

channel = cq.Workplane("XY").box(LENGTH, HEIGHT, THICKNESS, centered=False)
hole = (
    cq.Workplane("XY")
    .workplane(offset=-THICKNESS)
    .center(CX, CY)
    .circle(RADIUS)
    .extrude(3 * THICKNESS)
)
solid = channel.cut(hole)
cq.exporters.export(solid, "geometry.step")


def plane(axis, value):
    return {"kind": "plane", "axis": axis, "value": value}


margin = 0.01
manifest = {
    "dimensionality": "2D",
    "patches": [
        {"name": "inlet", "type": "patch", "locators": [plane("x", 0.0)]},
        {"name": "outlet", "type": "patch", "locators": [plane("x", LENGTH)]},
        {"name": "walls", "type": "wall", "locators": [plane("y", 0.0), plane("y", HEIGHT)]},
        {
            "name": "cylinder",
            "type": "wall",
            "locators": [
                {
                    "kind": "box",
                    "box": [
                        CX - RADIUS - margin,
                        CY - RADIUS - margin,
                        -margin,
                        CX + RADIUS + margin,
                        CY + RADIUS + margin,
                        THICKNESS + margin,
                    ],
                }
            ],
        },
        {
            "name": "frontAndBack",
            "type": "empty",
            "locators": [plane("z", 0.0), plane("z", THICKNESS)],
        },
    ],
}
with open("boundary_manifest.json", "w", encoding="utf-8") as f:
    json.dump(manifest, f, indent=2)
    f.write("\n")
