"""Hand-written geometry script: 2D flow past an isolated circular cylinder (open far field).

The cylinder (diameter 0.1, axis along z) has its centre at the origin. The fluid domain is a
rectangle around it, one cell thick in z (0 to 0.1), with the cylinder cut out. The extents are in
``domain.json`` next to this script (``up``: distance from the cylinder centre to the inlet,
``down``: to the outlet, ``half_height``: to each lateral boundary, all in metres). They are far
away on purpose: the cylinder is meant to be isolated, so the outer boundaries only have to let
the flow through and must not act as walls. Writes ``geometry.step`` and
``boundary_manifest.json`` into the current working directory.
"""

import json
from pathlib import Path

import cadquery as cq

THICKNESS = 0.1
RADIUS = 0.05  # diameter 0.1
extent = json.loads((Path(__file__).parent / "domain.json").read_text(encoding="utf-8"))
UP, DOWN, HALF_HEIGHT = extent["up"], extent["down"], extent["half_height"]

domain = (
    cq.Workplane("XY")
    .box(UP + DOWN, 2 * HALF_HEIGHT, THICKNESS, centered=False)
    .translate((-UP, -HALF_HEIGHT, 0))
)
hole = cq.Workplane("XY").workplane(offset=-THICKNESS).circle(RADIUS).extrude(3 * THICKNESS)
solid = domain.cut(hole)
cq.exporters.export(solid, "geometry.step")


def plane(axis, value):
    return {"kind": "plane", "axis": axis, "value": value}


margin = 0.01
manifest = {
    "dimensionality": "2D",
    "patches": [
        {"name": "inlet", "type": "patch", "locators": [plane("x", -UP)]},
        {"name": "outlet", "type": "patch", "locators": [plane("x", DOWN)]},
        {
            "name": "sides",
            "type": "patch",
            "locators": [plane("y", -HALF_HEIGHT), plane("y", HALF_HEIGHT)],
        },
        {
            "name": "cylinder",
            "type": "wall",
            "locators": [
                {
                    "kind": "box",
                    "box": [
                        -RADIUS - margin,
                        -RADIUS - margin,
                        -margin,
                        RADIUS + margin,
                        RADIUS + margin,
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
