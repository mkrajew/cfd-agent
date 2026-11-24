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
from cfdagent.tools.bbox import BBox, box_shortfalls, format_bbox

_AXIS_INDEX = {"x": 0, "y": 1, "z": 2}

# Gmsh pads every bounding box of an OpenCASCADE entity by this absolute amount on each side
# (OpenCASCADE's confusion tolerance), independent of the model size.
_GMSH_BBOX_PADDING = 1e-7


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


def _effective_tolerance(tol: float) -> float:
    """Absolute matching tolerance: ``tol`` times the largest model extent plus Gmsh's padding."""
    xmin, ymin, zmin, xmax, ymax, zmax = gmsh.model.getBoundingBox(-1, -1)
    largest = max(xmax - xmin, ymax - ymin, zmax - zmin)
    return _GMSH_BBOX_PADDING + tol * largest


_OVERLAP_ADVICE = (
    "a box selects every surface whose whole bounding box lies inside it, so a box that encloses "
    "the whole solid selects every face: shrink that patch's box (or give it a bound that the "
    "other surfaces cross) until it holds only its own surfaces"
)


def _join_and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _nearest_box_hint(manifest: BoundaryManifest, bbox: BBox, tol: float) -> str:
    """Text naming the box locator that comes closest to selecting ``bbox``, or ``""``.

    Closest means the smallest total shortfall (``box_shortfalls``); the first box in manifest
    order wins a tie. Empty when the manifest has no box locator.
    """
    best = None
    for patch in manifest.patches:
        for number, loc in enumerate(patch.locators, start=1):
            if loc.kind != "box":
                continue
            misses = box_shortfalls(loc.box, bbox, tol)
            if not misses:
                continue  # a box that selects the surface (or is degenerate) is no near miss
            total = sum(m.amount for m in misses)
            if best is None or total < best[0]:
                best = (total, patch.name, number, misses)
    if best is None:
        return ""
    _, name, number, misses = best
    parts = [
        f"{m.bound} (surface {m.surface:.10g} {'<' if m.below else '>'} box {m.box:.10g}, "
        f"short by {m.amount:.6g})"
        for m in misses
    ]
    return (
        f"; nearest box: patch '{name}' locator {number} (1-based) misses it at "
        f"{_join_and(parts)}; a box selects a surface only when the surface's whole bounding "
        "box lies inside it, so grow the box past the surface, never shrink it"
    )


def match_surfaces(manifest: BoundaryManifest, tol: float = 1e-6) -> dict[str, list[int]]:
    """Map each patch name to the tags of the boundary surfaces its locators select.

    ``tol`` is relative: the effective tolerance is ``tol`` times the largest extent of the model's
    bounding box, plus the fixed 1e-7 by which Gmsh pads every bounding box. A plane locator
    selects surfaces whose bounding box lies within that tolerance of the plane. A box locator
    selects surfaces whose bounding box lies inside the box (grown by the tolerance).

    Raises ``ManifestError`` (naming the surface tag and bounding box) if the manifest has no
    patches, if a boundary surface is matched by no patch or by more than one patch, or if a patch
    matches no surface.
    """
    if not manifest.patches:
        raise ManifestError("the manifest has no patches, so no boundary surface can be named")
    surfaces = _boundary_surfaces()
    tol = _effective_tolerance(tol)
    bboxes = {tag: tuple(gmsh.model.getBoundingBox(2, tag)) for tag in surfaces}

    matched: dict[str, list[int]] = {}
    # surface tag -> (patch name, 1-based number and kind of its first selecting locator)
    owners: dict[int, list[tuple[str, int, str]]] = {tag: [] for tag in surfaces}
    for patch in manifest.patches:
        tags = []
        for tag in surfaces:
            for number, loc in enumerate(patch.locators, start=1):
                if _locator_matches(loc, bboxes[tag], tol):
                    tags.append(tag)
                    owners[tag].append((patch.name, number, loc.kind))
                    break
        matched[patch.name] = tags

    problems = []
    overlap = False
    for tag in surfaces:
        where = f"surface tag {tag}, bounding box {format_bbox(bboxes[tag])}"
        if not owners[tag]:
            hint = _nearest_box_hint(manifest, bboxes[tag], tol)
            problems.append(f"{where} is not matched by any patch{hint}")
        elif len(owners[tag]) > 1:
            who = ", ".join(f"'{n}' (locator {i}, {kind})" for n, i, kind in owners[tag])
            problems.append(f"{where} is matched by more than one patch: {who}")
            overlap = True
    if overlap:
        problems.append(_OVERLAP_ADVICE)
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

    ``tol`` is relative to the model size, as in ``match_surfaces``.

    Returns the patch name to surface tags mapping. Nothing is created if matching fails.
    """
    matched = match_surfaces(manifest, tol)
    for name, tags in matched.items():
        gmsh.model.addPhysicalGroup(2, tags, name=name)
    volume_tags = [tag for _, tag in gmsh.model.getEntities(3)]
    gmsh.model.addPhysicalGroup(3, volume_tags, name=volume_name)
    return matched


def base_face(dimensionality: str, tol: float = 1e-6) -> int:
    """Return the tag of the boundary surface from which a one-cell-thick layer is built.

    Call it after ``gmsh.model.occ.synchronize()`` on the imported solid, then copy that surface,
    remove the solid, mesh the copy in 2D and extrude or revolve it by one cell.

    * ``"2D"``: the surface on the plane z = zmin of the model, that is the surface whose bounding
      box has both its zmin and its zmax within the tolerance of the model's zmin.
    * ``"axisymmetric"``: the planar wedge face on the -z side (axis = x axis, wedge symmetric
      about the x-y plane). The face is tilted, so its z extent is not small: its bounding box has
      ``zmin`` below -tolerance and ``zmax`` at or below the tolerance (zmax is 0 when the face
      reaches the axis and slightly negative when it does not).

    ``tol`` is relative to the model size, as in ``match_surfaces``.

    Raises ``ManifestError`` if the dimensionality has no base face, if the model has no volume,
    or if no surface or more than one surface qualifies; the message lists every boundary surface
    with its bounding box.
    """
    if dimensionality not in ("2D", "axisymmetric"):
        raise ManifestError(
            f"there is no base face for dimensionality {dimensionality!r}; "
            "it exists only for '2D' and 'axisymmetric'"
        )
    surfaces = _boundary_surfaces()
    tol = _effective_tolerance(tol)
    bboxes = {tag: tuple(gmsh.model.getBoundingBox(2, tag)) for tag in surfaces}

    if dimensionality == "2D":
        model_zmin = gmsh.model.getBoundingBox(-1, -1)[2]

        # Both the face's box and the model's box carry Gmsh's padding, so their coordinates
        # differ by twice the padding even for a face exactly on the plane.
        plane_tol = tol + _GMSH_BBOX_PADDING

        def qualifies(bbox: BBox) -> bool:
            return abs(bbox[2] - model_zmin) <= plane_tol and abs(bbox[5] - model_zmin) <= plane_tol

        rule = f"on the plane z = {model_zmin + _GMSH_BBOX_PADDING:.6g} (the model's zmin)"
    else:

        def qualifies(bbox: BBox) -> bool:
            return bbox[5] <= tol and bbox[2] < -tol

        rule = "that is a -z wedge face (bounding box zmax at most 0, zmin below 0)"

    found = [tag for tag in surfaces if qualifies(bboxes[tag])]
    if len(found) == 1:
        return found[0]
    listing = "; ".join(
        f"surface tag {tag}, bounding box {format_bbox(bboxes[tag])}" for tag in surfaces
    )
    if found:
        raise ManifestError(
            f"more than one surface {rule}: tags {found}. Boundary surfaces: {listing}"
        )
    raise ManifestError(f"no surface {rule}. Boundary surfaces: {listing}")
