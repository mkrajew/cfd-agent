"""Hand-written geometry script: 2D lid-driven cavity (Ghia et al.).

Unit square [0, 1] x [0, 1] in x-y, one cell thick in z (0 to 0.1). Writes ``geometry.step`` and
``boundary_manifest.json`` into the current working directory.
"""

import json

import cadquery as cq

THICKNESS = 0.1

solid = cq.Workplane("XY").box(1.0, 1.0, THICKNESS, centered=False)
cq.exporters.export(solid, "geometry.step")


def plane(axis, value):
    return {"kind": "plane", "axis": axis, "value": value}


manifest = {
    "dimensionality": "2D",
    "patches": [
        {"name": "movingWall", "type": "wall", "locators": [plane("y", 1.0)]},
        {
            "name": "fixedWalls",
            "type": "wall",
            "locators": [plane("x", 0.0), plane("x", 1.0), plane("y", 0.0)],
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
