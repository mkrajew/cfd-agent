"""Unit tests for the CaseSpec model: strict-schema friendliness and the gate-facing accessors."""

import pytest
from pydantic import BaseModel, ValidationError

from cfdagent.agents.casespec import CaseSpec
from cfdagent.llm.client import strict_schema

SPEC = {
    "title": "Steady flow in a channel",
    "summary": "Incompressible laminar flow through a straight channel.",
    "dimensionality": "2D",
    "flow_regime": "laminar",
    "time_dependence": "steady",
    "fluid": {"density_kg_m3": 1.0, "kinematic_viscosity_m2_s": 0.01},
    "reference": {"length_m": 1.0, "velocity_m_s": 1.0, "reynolds_number": 100.0},
    "geometry": {
        "description": "A rectangular box.",
        "bbox": {"xmin": 0.0, "ymin": -0.5, "zmin": 0.0, "xmax": 4.0, "ymax": 0.5, "zmax": 0.1},
        "volume_m3": 0.4,
        "key_dimensions": [{"name": "height", "value_m": 1.0}],
        "thickness_m": 0.1,
    },
    "boundaries": [
        {"name": "inlet", "role": "inlet", "description": "plane x=0"},
        {"name": "outlet", "role": "outlet", "description": "plane x=4"},
    ],
    "mesh": {"target_cells": 8000, "rationale": "Uniform cells, about 100 across the height."},
    "solver": {"suggested_solver": "simpleFoam", "end_time_s": 1000.0, "time_step_s": None},
    "requested_outputs": [
        {"name": "centreline_u", "kind": "profile", "description": "u along the centreline"},
        {"name": "drag", "kind": "scalar", "description": "force on the walls"},
    ],
    "assumptions": ["Kinematic viscosity chosen to reach the stated Reynolds number."],
    "resource_estimate": {"cells": 8000, "runtime_minutes": 5.0, "memory_gb": 0.5},
}


def make_spec(**changes) -> CaseSpec:
    return CaseSpec.model_validate({**SPEC, **changes})


def _walk(node, path="$"):
    """Yield (path, schema node) for every dict inside a JSON schema."""
    if isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            yield from _walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk(value, f"{path}[{index}]")


def _violations(schema) -> list[str]:
    """Everything in a JSON schema that strict structured output cannot express."""
    found = []
    for path, node in _walk(schema):
        if isinstance(node.get("additionalProperties"), dict):
            found.append(f"map at {path}")
        if node.get("additionalProperties") is True:
            found.append(f"free-form object at {path}")
        if "prefixItems" in node or isinstance(node.get("items"), list):
            found.append(f"tuple at {path}")
        if node.get("type") == "object" and not node.get("properties"):
            found.append(f"object without declared properties at {path}")
        if "default" in node:  # pydantic would also leave the field out of `required`
            found.append(f"default keyword at {path}")
        properties = node.get("properties")
        if isinstance(properties, dict) and node.get("type") == "object":
            if set(node.get("required", [])) != set(properties):
                found.append(f"optional field at {path}")
    return found


def test_casespec_schema_has_no_maps_or_tuples():
    schema = CaseSpec.model_json_schema()
    assert len(list(_walk(schema))) > 50  # the walk really visited the nested models
    assert _violations(schema) == []
    # The service-facing form must be accepted by the strict-schema converter, which raises
    # SchemaError for maps and free-form objects.
    assert strict_schema(schema)["additionalProperties"] is False


def test_casespec_schema_check_can_fail():
    """The detector must flag the constructs it is meant to flag."""

    class Bad(BaseModel):
        values: dict[str, float]
        pair: tuple[float, float]
        optional: float = 1.0

    found = " ".join(_violations(Bad.model_json_schema()))
    assert "map at" in found
    assert "tuple at" in found
    assert "default keyword" in found
    assert "optional field" in found


def test_casespec_every_field_is_required():
    schema = CaseSpec.model_json_schema()
    assert set(schema["required"]) == set(schema["properties"])
    assert {"title", "assumptions", "resource_estimate", "requested_outputs"} <= set(
        schema["required"]
    )


def test_casespec_rejects_unknown_fields_and_values():
    with pytest.raises(ValidationError):
        CaseSpec.model_validate({**SPEC, "surprise": 1})
    with pytest.raises(ValidationError):
        make_spec(dimensionality="4D")
    with pytest.raises(ValidationError):
        make_spec(flow_regime="transitional")
    with pytest.raises(ValidationError):
        make_spec(requested_outputs=[{"name": "a", "kind": "vector", "description": "x"}])
    incomplete = {k: v for k, v in SPEC.items() if k != "assumptions"}
    with pytest.raises(ValidationError):
        CaseSpec.model_validate(incomplete)


def test_expected_bbox_and_volume():
    spec = make_spec()
    # The order every tool uses: (xmin, ymin, zmin, xmax, ymax, zmax).
    assert spec.expected_bbox() == (0.0, -0.5, 0.0, 4.0, 0.5, 0.1)
    assert isinstance(spec.expected_bbox(), tuple)
    assert spec.expected_volume() == 0.4
    no_volume = make_spec(geometry={**SPEC["geometry"], "volume_m3": None})
    assert no_volume.expected_volume() is None


def test_casespec_roundtrips_through_json():
    spec = make_spec()
    assert CaseSpec.model_validate_json(spec.model_dump_json()) == spec
