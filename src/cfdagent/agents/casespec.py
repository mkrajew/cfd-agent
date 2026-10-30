"""``CaseSpec``: the Planner's validated description of the simulation (design 6.2, stage 1).

Every later stage and gate reads it: the geometry and mesh gates take their expected bounding box
and volume from it, the Case Setup stage takes fluid properties and solver hints, the Analyst
takes the requested outputs.

The model is the schema of a strict structured-output call, so it follows the rules of
``llm.client.strict_schema``: every field is required (a value that may be absent is typed
``X | None`` and the model must say ``null``), objects are closed (``extra="forbid"``), and there
are no maps and no tuples (the bounding box is a sub-model with six named floats). All numbers
are SI: metres, kilograms, seconds. Numeric ranges are not expressed in the schema (services
differ in which keywords they accept); ``planner.plan_gate`` checks them.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Fluid(_Closed):
    density_kg_m3: float = Field(description="Density in kg/m^3")
    kinematic_viscosity_m2_s: float = Field(description="Kinematic viscosity in m^2/s")


class Reference(_Closed):
    length_m: float = Field(description="Reference length in metres")
    velocity_m_s: float = Field(description="Reference velocity in m/s")
    reynolds_number: float | None = Field(
        description="Reynolds number U*L/nu built from the two values above; null if the task "
        "gives none and none is meaningful"
    )


class BoundingBox(_Closed):
    xmin: float = Field(description="Metres")
    ymin: float = Field(description="Metres")
    zmin: float = Field(description="Metres")
    xmax: float = Field(description="Metres")
    ymax: float = Field(description="Metres")
    zmax: float = Field(description="Metres")


class KeyDimension(_Closed):
    name: str = Field(description="Short identifier of the dimension")
    value_m: float = Field(description="Value in metres")


class Geometry(_Closed):
    description: str = Field(description="What the fluid domain is, in plain words")
    bbox: BoundingBox = Field(description="Axis-aligned bounding box of the fluid domain")
    volume_m3: float | None = Field(
        description="Volume of the fluid domain in m^3 when it can be computed exactly, else null"
    )
    key_dimensions: list[KeyDimension] = Field(description="Dimensions the geometry must match")
    thickness_m: float | None = Field(
        description="Extent in the out-of-plane direction in metres for a 2D case, else null"
    )


class BoundarySpec(_Closed):
    name: str = Field(description="Patch name used in every later stage")
    role: str = Field(description="Physical role, for example inlet, outlet, wall, symmetry")
    description: str = Field(description="Where it is and what condition applies")


class MeshSpec(_Closed):
    target_cells: int = Field(description="Intended number of cells")
    rationale: str = Field(description="Why this resolution is adequate, from the physics")


class SolverSpec(_Closed):
    suggested_solver: str = Field(description="OpenFOAM solver name")
    end_time_s: float = Field(description="End time in seconds (iterations for a steady case)")
    time_step_s: float | None = Field(
        description="Time step in seconds for a transient case, else null"
    )


class OutputSpec(_Closed):
    name: str = Field(description="Exact identifier the user wrote for this output")
    kind: Literal["profile", "scalar"]
    description: str = Field(description="What is extracted, where and in which unit")


class ResourceEstimate(_Closed):
    cells: int = Field(description="Expected number of cells")
    runtime_minutes: float = Field(description="Expected solver wall time in minutes")
    memory_gb: float = Field(description="Expected peak memory in GB")


class CaseSpec(_Closed):
    """The whole plan; see the module docstring."""

    title: str
    summary: str = Field(description="The task restated in the Planner's own words")
    dimensionality: Literal["2D", "axisymmetric", "3D"]
    flow_regime: Literal["laminar", "turbulent"]
    time_dependence: Literal["steady", "transient"]
    fluid: Fluid
    reference: Reference
    geometry: Geometry
    boundaries: list[BoundarySpec]
    mesh: MeshSpec
    solver: SolverSpec
    requested_outputs: list[OutputSpec]
    assumptions: list[str] = Field(
        description="Every parameter the task did not give and the Planner chose, with the value"
    )
    resource_estimate: ResourceEstimate

    def expected_bbox(self) -> tuple[float, ...]:
        """``(xmin, ymin, zmin, xmax, ymax, zmax)``, the order of every bounding-box tool."""
        box = self.geometry.bbox
        return (box.xmin, box.ymin, box.zmin, box.xmax, box.ymax, box.zmax)

    def expected_volume(self) -> float | None:
        """Volume of the fluid domain in m^3, or ``None`` when the plan gives none."""
        return self.geometry.volume_m3
