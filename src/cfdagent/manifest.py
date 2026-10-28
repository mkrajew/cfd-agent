"""Boundary manifest: patch names and geometric locators for boundary faces.

STEP import loses face names, so the geometry step writes a manifest next to the STEP file
(``boundary_manifest.json``) and the meshing step re-identifies the faces geometrically.
Coordinates are in the same numeric units as the STEP file as the mesher reads it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MANIFEST_FILE_NAME = "boundary_manifest.json"


class ManifestError(Exception):
    """The boundary manifest is inconsistent with the geometry."""


class Locator(BaseModel):
    """Selects boundary faces by position.

    ``plane``: faces whose bounding box lies on the plane ``axis = value``.
    ``box``: faces whose bounding box lies inside the box (xmin, ymin, zmin, xmax, ymax, zmax).
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["plane", "box"]
    axis: Literal["x", "y", "z"] | None = None
    value: float | None = None
    box: tuple[float, float, float, float, float, float] | None = None

    @model_validator(mode="after")
    def _check_fields_for_kind(self) -> Locator:
        if self.kind == "plane":
            if self.axis is None or self.value is None:
                raise ValueError("a plane locator requires 'axis' and 'value'")
            if self.box is not None:
                raise ValueError("a plane locator must not have 'box'")
        else:
            if self.box is None:
                raise ValueError("a box locator requires 'box'")
            if self.axis is not None or self.value is not None:
                raise ValueError("a box locator must not have 'axis' or 'value'")
            xmin, ymin, zmin, xmax, ymax, zmax = self.box
            if xmin > xmax or ymin > ymax or zmin > zmax:
                raise ValueError("box must be (xmin, ymin, zmin, xmax, ymax, zmax) with min <= max")
        return self


class BoundaryPatch(BaseModel):
    """One OpenFOAM boundary patch; may be made of several geometric faces."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*$")
    type: Literal["patch", "wall", "empty", "symmetryPlane", "symmetry", "wedge"]
    locators: list[Locator] = Field(min_length=1)


class BoundaryManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimensionality: Literal["2D", "axisymmetric", "3D"]
    patches: list[BoundaryPatch]

    @model_validator(mode="after")
    def _check_patches(self) -> BoundaryManifest:
        names = [p.name for p in self.patches]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ValueError(f"duplicate patch names: {duplicates}")
        if self.dimensionality == "2D" and not any(p.type == "empty" for p in self.patches):
            raise ValueError("a 2D manifest requires at least one patch of type 'empty'")
        return self

    @classmethod
    def from_file(cls, path: Path) -> BoundaryManifest:
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))

    def to_file(self, path: Path) -> None:
        Path(path).write_text(self.model_dump_json(indent=2) + "\n", encoding="utf-8")
