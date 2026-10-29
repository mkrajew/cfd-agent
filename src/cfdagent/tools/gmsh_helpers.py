"""Gmsh helpers that re-identify boundary faces from a boundary manifest.

STEP import loses face names, so boundary surfaces are matched geometrically. The functions here
are meant to be imported from mesh scripts and need an initialised Gmsh model whose geometry has
been synchronised (``gmsh.model.occ.synchronize()``).

Boundary surface: a surface on the boundary of the union of all volumes in the model
(``gmsh.model.getBoundary(..., combined=True)``). A surface shared by two volumes is internal and is
not a boundary surface; it is neither matched nor named. Every boundary surface must be matched by
exactly one patch, so a boundary face never falls into OpenFOAM's ``defaultFaces``.

Coordinates in locators are in the same numeric units as the STEP coordinates as Gmsh reads them
(no unit conversion is applied).
"""

from __future__ import annotations

import gmsh

from cfdagent.manifest import BoundaryManifest, Locator, ManifestError
from cfdagent.tools.bbox import BBox, format_bbox

_AXIS_INDEX = {"x": 0, "y": 1, "z": 2}


def _boundary_surfaces() -> list[int]:
    volumes = gmsh.model.getEntities(3)
    if not volumes:
        raise ManifestError("the Gmsh model contains no volume; import a solid and synchronize")
    boundary = gmsh.model.getBoundary(volumes, combined=True, oriented=False, recursive=False)
    return sorted({tag for dim, tag in boundary if dim == 2})


def _locator_matches(locator: Locator, bbox: BBox, tol: float) -> bool:
    if locator.kind == "plane":
        i = _AXIS_INDEX[locator.axis]
        return abs(bbox[i] - locator.value) <= tol and abs(bbox[i + 3] - locator.value) <= tol
    lo, hi = locator.box[:3], locator.box[3:]
    return all(bbox[i] >= lo[i] - tol and bbox[i + 3] <= hi[i] + tol for i in range(3))


def match_surfaces(manifest: BoundaryManifest, tol: float = 1e-6) -> dict[str, list[int]]:
    """Map each patch name to the tags of the boundary surfaces its locators select.

    A plane locator selects surfaces whose bounding box lies within ``tol`` of the plane. A box
    locator selects surfaces whose bounding box lies inside the box (grown by ``tol``).

    Raises ``ManifestError`` (naming the surface tag and bounding box) if the manifest has no
    patches, if a boundary surface is matched by no patch or by more than one patch, or if a patch
    matches no surface.
    """
    if not manifest.patches:
        raise ManifestError("the manifest has no patches, so no boundary surface can be named")
    surfaces = _boundary_surfaces()
    bboxes = {tag: tuple(gmsh.model.getBoundingBox(2, tag)) for tag in surfaces}

    matched: dict[str, list[int]] = {}
    owners: dict[int, list[str]] = {tag: [] for tag in surfaces}
    for patch in manifest.patches:
        tags = [
            tag
            for tag in surfaces
            if any(_locator_matches(loc, bboxes[tag], tol) for loc in patch.locators)
        ]
        matched[patch.name] = tags
        for tag in tags:
            owners[tag].append(patch.name)

    problems = []
    for tag in surfaces:
        where = f"surface tag {tag}, bounding box {format_bbox(bboxes[tag])}"
        if not owners[tag]:
            problems.append(f"{where} is not matched by any patch")
        elif len(owners[tag]) > 1:
            problems.append(f"{where} is matched by more than one patch: {owners[tag]}")
    problems += [
        f"patch '{name}' matches no boundary surface" for name, tags in matched.items() if not tags
    ]
    if problems:
        raise ManifestError("; ".join(problems))
    return matched


def assign_physical_groups(
    manifest: BoundaryManifest, volume_name: str = "internal", tol: float = 1e-6
) -> dict[str, list[int]]:
    """Create one 2D physical group per patch and one 3D group holding all volumes.

    Returns the patch name to surface tags mapping. Nothing is created if matching fails.
    """
    matched = match_surfaces(manifest, tol)
    for name, tags in matched.items():
        gmsh.model.addPhysicalGroup(2, tags, name=name)
    volume_tags = [tag for _, tag in gmsh.model.getEntities(3)]
    gmsh.model.addPhysicalGroup(3, volume_tags, name=volume_name)
    return matched
