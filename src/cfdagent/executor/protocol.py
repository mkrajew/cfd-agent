"""The executor protocol: what travels between the orchestrator and an executor container.

A call is two small JSON files in the run directory, never a stream: the OpenCASCADE and OpenFOAM
libraries print diagnostics on the process stdout, so stdout cannot carry data.

    <run>/.sandbox/<call-id>.request.json    OpRequest   written by the host
    <run>/.sandbox/<call-id>.response.json   OpResponse  written by the executor, atomically

This module is imported on both sides and must stay light: no tool code, no CAD, no OpenFOAM.
The argument models of the ops live here (registered with ``@op_args``) so the host can reject a
bad call before it starts a container. The functions that run an op live in ``executor.ops``.

Adding an op: one ``@op_args("name")`` model here, one ``@operation("name")`` function in ``ops``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    ValidationError,
    field_validator,
)

from cfdagent.rundir import split_relative

SANDBOX_DIR = ".sandbox"
REQUEST_SUFFIX = ".request.json"
RESPONSE_SUFFIX = ".response.json"
REQUEST_MAX_BYTES = 1_000_000
RESPONSE_MAX_BYTES = 1_000_000
MAX_ERROR_CHARS = 8000
MAX_RANKS = 4  # the pipeline invariant; a test keeps it equal to ``tools.foam.MAX_RANKS``


def _relative_for_pydantic(value: str) -> str:
    try:
        split_relative(value)
    except Exception as exc:  # UnsafePath is an AgentInputError, not the ValueError pydantic needs
        raise ValueError(str(exc)) from None
    return value


RelPath = Annotated[str, AfterValidator(_relative_for_pydantic)]
"""A run-directory-relative path, syntactically checked (see ``rundir.split_relative``)."""


class ArgsModel(BaseModel):
    """Base of every op's arguments: unknown fields are an error."""

    model_config = ConfigDict(extra="forbid")


ARG_MODELS: dict[str, type[ArgsModel]] = {}


def op_args(name: str):
    """Class decorator: register ``name`` and its argument model."""

    def register(model: type[ArgsModel]) -> type[ArgsModel]:
        ARG_MODELS[name] = model
        return model

    return register


class UnknownOp(ValueError):
    """The requested op does not exist."""


class InvalidArgs(ValueError):
    """The arguments do not fit the op's argument model."""


def validate_args(op: str, args: Any) -> ArgsModel:
    """Validate ``args`` for ``op``; raises ``UnknownOp`` or ``InvalidArgs`` (short message)."""
    model = ARG_MODELS.get(op) if isinstance(op, str) else None
    if model is None:
        known = ", ".join(sorted(ARG_MODELS))
        raise UnknownOp(f"unknown op {str(op)[:80]!r}; known ops: {known}")
    try:
        return model.model_validate(args)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'arguments'}: {err['msg']}"
            for err in exc.errors(include_url=False, include_input=False)[:8]
        )
        raise InvalidArgs(f"invalid arguments for {op}: {problems}") from None


class OpRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: str
    args: dict[str, Any] = Field(default_factory=dict)


class OpResponse(BaseModel):
    """The result of an op.

    ``ok``: the op's own verdict (a failing gate is ``ok=False``). ``error``: a message for the
    agent or the log (bounded). ``internal``: the failure is not the agent's fault (a tool or the
    environment broke); the orchestrator records an internal error instead of blaming the agent.
    """

    model_config = ConfigDict(extra="forbid")

    ok: StrictBool
    data: dict[str, Any] | None = None
    error: str | None = None
    internal: StrictBool = False

    @field_validator("error")
    @classmethod
    def _bound_error(cls, value: str | None) -> str | None:
        if value is not None and len(value) > MAX_ERROR_CHARS:
            return value[:MAX_ERROR_CHARS] + "\n[... message truncated]"
        return value


def failure(message: str, *, internal: bool = False, data: dict[str, Any] | None = None):
    """A failed ``OpResponse``."""
    return OpResponse(ok=False, data=data, error=message, internal=internal)


def request_rel(call_id: str) -> str:
    return f"{SANDBOX_DIR}/{call_id}{REQUEST_SUFFIX}"


def response_rel_for(request: str) -> str | None:
    """The response path belonging to a request path, or None if it is not a request path.

    A request must be a direct child of ``.sandbox/`` named ``*.request.json``.
    """
    try:
        parts = split_relative(request)
    except Exception:
        return None
    if len(parts) != 2 or parts[0] != SANDBOX_DIR or not parts[1].endswith(REQUEST_SUFFIX):
        return None
    stem = parts[1][: -len(REQUEST_SUFFIX)]
    return f"{SANDBOX_DIR}/{stem}{RESPONSE_SUFFIX}" if stem else None


# --------------------------------------------------------------------------------------------
# Argument models of the ops
# --------------------------------------------------------------------------------------------

_Finite = Annotated[float, Field(allow_inf_nan=False)]
_PositiveFinite = Annotated[float, Field(gt=0, allow_inf_nan=False)]
BBox = Annotated[list[_Finite], Field(min_length=6, max_length=6)]
_Timeout = Annotated[float, Field(gt=0, le=86_400, allow_inf_nan=False)]


@op_args("run_script")
class RunScriptArgs(ArgsModel):
    """Run a Python script with ``cwd``. ``kind`` adds the stage's output checks (see ops)."""

    script: RelPath
    cwd: RelPath = "."
    timeout_s: _Timeout = 300.0
    kind: Literal["geometry", "mesh"] | None = None


@op_args("check_geometry")
class CheckGeometryArgs(ArgsModel):
    workdir: RelPath
    expected_bbox: BBox
    expected_volume: _PositiveFinite | None = None
    rel_tol: _PositiveFinite = 1e-3


@op_args("convert_mesh")
class ConvertMeshArgs(ArgsModel):
    msh: RelPath
    case_dir: RelPath
    manifest: RelPath | None = None  # default: boundary_manifest.json beside case_dir


@op_args("check_mesh")
class CheckMeshArgs(ArgsModel):
    case_dir: RelPath
    expected_bbox: BBox
    rel_tol: _PositiveFinite = 1e-2
    manifest: RelPath | None = None  # default: boundary_manifest.json beside case_dir


@op_args("check_dictionaries")
class CheckDictionariesArgs(ArgsModel):
    case_dir: RelPath


@op_args("trial_run")
class TrialRunArgs(ArgsModel):
    case_dir: RelPath
    solver: str = Field(max_length=64)
    steps: Annotated[StrictInt, Field(ge=1, le=1000)] = 3


@op_args("run_solver")
class RunSolverArgs(ArgsModel):
    case_dir: RelPath
    solver: str = Field(max_length=64)
    n_procs: Annotated[StrictInt, Field(ge=1, le=MAX_RANKS)] = 1
    timeout_s: _Timeout = 7200.0


@op_args("run_utility")
class RunUtilityArgs(ArgsModel):
    argv: Annotated[
        list[Annotated[str, Field(max_length=1000)]], Field(min_length=1, max_length=64)
    ]
    cwd: RelPath = "."
