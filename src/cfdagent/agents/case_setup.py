"""The Case Setup stage, stage 4 of the pipeline (design 6.2, 6.3).

The Mesh stage has left the run directory a ``case/`` that holds ``constant/polyMesh`` only. Every
attempt gets a fresh copy of it as ``attempts/case_setup-N/case/`` (``prepare_attempt``); the
agent writes the dictionaries: the field files of ``0/``, the property files of ``constant/`` and
the ``system/`` dictionaries. It may search and read the OpenFOAM tutorials of the image
(``search_knowledge``, ``read_knowledge``) and check its dictionaries without spending an attempt
(``validate_dictionaries``).

The mesh is not the agent's to change. ``constant/polyMesh/**`` is excluded from the writable
paths of the file scope, and, as a second line of defence, the files of the run directory's mesh
are measured (size and SHA-256) when the attempt starts and compared with the attempt's
``constant/polyMesh`` at the gate and again at promotion: any difference fails the gate.

The gate runs, in this order, and stops at the first step that makes the later ones meaningless:

1. ``system/controlDict`` exists in the attempt (nothing in the fresh copy can satisfy this);
2. op ``check_dictionaries``: every dictionary of ``0/``, ``constant/`` and ``system/`` parses;
3. op ``check_case_consistency``: every patch of the mesh has an entry in every field file,
   constraint patch types match, ``application`` is the plan's solver and ``endTime`` is above
   ``startTime``; all problems are reported together;
4. op ``trial_run``: ``TRIAL_STEPS`` time steps of the plan's solver in a scratch copy.

The solver is the CaseSpec's ``solver.suggested_solver``; one outside the solvers that the
executor allows is a defect of the plan (``ToolFailure``, before any sandbox call).

Errors the agent can fix come back as a failed ``GateResult``. A failed executor or tool
(``OpResponse.internal``) and a broken precondition (no mesh to start from) raise ``ToolFailure``.

This module is host-side: it imports no tool, CAD, mesh or knowledge code and talks to the
tutorials only through the executor ops.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

from pydantic import BaseModel, Field

from cfdagent.agents.casespec import CaseSpec
from cfdagent.agents.gatehelp import last_bytes
from cfdagent.agents.stage import Stage, StageContext
from cfdagent.agents.tools import FileScope, NoArgs, Tool
from cfdagent.errors import AgentInputError, ToolFailure
from cfdagent.executor.protocol import (
    CheckCaseConsistencyArgs,
    CheckDictionariesArgs,
    OpResponse,
    ReadKnowledgeArgs,
    SearchKnowledgeArgs,
    TrialRunArgs,
)
from cfdagent.gates import GateResult
from cfdagent.rundir import RunDirError
from cfdagent.sandbox.client import outer_timeout

STAGE = "case_setup"
AGENT = "case_setup"
CASE_NAME = "case"
PROMOTED_DIR = "case_setup"  # the notes of the accepted attempt
PROMOTED_CASE_DIR = "case"  # the case of the later stages: 0/, constant/ (mesh included), system/
POLYMESH_REL = "constant/polyMesh"
MESH_NOTES = "mesh/notes.md"  # what the Mesh stage may have left for this stage
CONTROL_DICT_REL = "system/controlDict"
# The solvers the executor runs (``tools.foam.SOLVERS``); host code must not import the tool
# layer, so a unit test keeps the two in step.
KNOWN_SOLVERS = ("icoFoam", "simpleFoam", "pimpleFoam")
TRIAL_STEPS = 3
GATE_TIMEOUT_S = 300.0  # the dictionary check has its own total limit of 300 s
TRIAL_TIMEOUT_S = 600.0  # the op limits the solver to 300 s; the rest is set-up of the scratch copy
KNOWLEDGE_TIMEOUT_S = 60.0
TAIL_BYTES = 3000  # the loop shows the model at most 4096 bytes of all errors together
READ_BYTES = 7000  # a tool result is cut at 8000 bytes; this keeps the truncation marker visible
POLYMESH_MAX_BYTES = 256 * 1024 * 1024  # the mesh of a run is megabytes to a few hundred
POLYMESH_MAX_FILES = 1000  # a polyMesh directory holds about ten files
POLYMESH_MAX_DEPTH = 6
_CHUNK = 1 << 20
_MAX_CHANGES_SHOWN = 8

_Snapshot = dict[str, tuple[int, str]]  # relative path -> (size, SHA-256); "" for a special entry
_SPECIAL = (-1, "")


class SearchKnowledgeToolArgs(BaseModel):
    query: str = Field(
        max_length=200,
        description="Words to look for (all of them must occur, in the file's path or its text)",
    )


class ReadKnowledgeToolArgs(BaseModel):
    path: str = Field(
        min_length=1,
        max_length=1024,
        description="A path as search_knowledge shows it, relative to the tutorial directory; "
        "a directory path lists its entries",
    )


# ------------------------------------------------------------------ the mesh that must not change


def _scan(polymesh: Path) -> dict[str, int]:
    """Relative path -> size of every regular file below ``polymesh``; ``-1`` for anything that
    is not one (a link, a FIFO, a device). Empty directories are not listed. Raises ``ToolFailure``
    for a tree beyond the limits (too many files, too deep)."""
    found: dict[str, int] = {}
    if not polymesh.is_dir() or polymesh.is_symlink() or os.path.isjunction(polymesh):
        return found
    stack = [(polymesh, "", 0)]
    while stack:
        directory, prefix, depth = stack.pop()
        if depth > POLYMESH_MAX_DEPTH:
            raise ToolFailure(f"{POLYMESH_REL} is nested more than {POLYMESH_MAX_DEPTH} levels")
        for entry in sorted(os.scandir(directory), key=lambda e: e.name):
            name = f"{prefix}{entry.name}"
            if entry.is_symlink() or os.path.isjunction(entry.path):
                found[name] = -1
            elif entry.is_dir(follow_symlinks=False):
                stack.append((Path(entry.path), f"{name}/", depth + 1))
            elif entry.is_file(follow_symlinks=False):
                found[name] = entry.stat(follow_symlinks=False).st_size
            else:
                found[name] = -1
            if len(found) > POLYMESH_MAX_FILES:
                raise ToolFailure(f"{POLYMESH_REL} holds more than {POLYMESH_MAX_FILES} files")
    return found


def _digest(path: Path, size: int) -> str:
    """SHA-256 of the first ``size + 1`` bytes of a regular file, read in pieces."""
    sha = hashlib.sha256()
    remaining = size + 1
    with open(path, "rb") as handle:
        while remaining > 0:
            block = handle.read(min(_CHUNK, remaining))
            if not block:
                break
            sha.update(block)
            remaining -= len(block)
    return sha.hexdigest()


def _snapshot(polymesh: Path) -> _Snapshot:
    """Size and digest of every file of the mesh the stage starts from."""
    sizes = _scan(polymesh)
    if not sizes:
        raise ToolFailure(f"{POLYMESH_REL} of the run directory holds no file; run the Mesh stage")
    if -1 in sizes.values():
        odd = sorted(name for name, size in sizes.items() if size == -1)
        raise ToolFailure(
            f"{POLYMESH_REL} of the run directory holds links or special files: {odd}"
        )
    if sum(sizes.values()) > POLYMESH_MAX_BYTES:
        raise ToolFailure(
            f"{POLYMESH_REL} of the run directory is larger than {POLYMESH_MAX_BYTES} bytes"
        )
    return {name: (size, _digest(polymesh / name, size)) for name, size in sizes.items()}


def _changes(polymesh: Path, expected: _Snapshot) -> list[str]:
    """Differences between the files below ``polymesh`` and ``expected``; empty when identical.

    Sizes are compared first and only files of the right size are read, so nothing larger than
    what the run directory's mesh held is ever read.
    """
    try:
        sizes = _scan(polymesh)
    except (OSError, ToolFailure):
        return [f"{POLYMESH_REL} cannot be listed or holds too many files"]
    problems = []
    missing = sorted(set(expected) - set(sizes))
    added = sorted(set(sizes) - set(expected))
    if missing:
        problems.append(f"missing: {', '.join(missing[:_MAX_CHANGES_SHOWN])}")
    if added:
        problems.append(f"new: {', '.join(added[:_MAX_CHANGES_SHOWN])}")
    changed = []
    for name in sorted(set(expected) & set(sizes)):
        size, digest = expected[name]
        if sizes[name] != size:
            changed.append(name)
            continue
        try:
            if _digest(polymesh / name, size) != digest:
                changed.append(name)
        except OSError:
            changed.append(name)
    if changed:
        problems.append(f"modified: {', '.join(changed[:_MAX_CHANGES_SHOWN])}")
    return problems


def _internal(response: OpResponse, label: str) -> ToolFailure:
    return ToolFailure(f"{label} failed outside the agent's work: {response.error}")


def _errors(response: OpResponse, fallback: str) -> list[str]:
    data = response.data or {}
    errors = data.get("errors")
    if isinstance(errors, list) and errors:
        return [str(e) for e in errors]
    return [response.error or fallback]


def _trial_failure(response: OpResponse, solver: str) -> str:
    """The message for a trial run that failed: the headline, what OpenFOAM reported and the end
    of the solver output, where the cause is."""
    data = response.data or {}
    error = response.error or "the trial run failed without a message"
    headline, _, rest = error.partition("\n")
    message = f"the {TRIAL_STEPS}-step trial run of {solver} failed: {headline}"
    fatal = data.get("fatal_error")
    if isinstance(fatal, str) and fatal.strip() and fatal not in headline:
        message += f"\nOpenFOAM reported: {fatal.strip()}"
    tail = data.get("tail")
    if not isinstance(tail, str) or not tail.strip():
        tail = rest
    if tail.strip():
        message += f"\nEnd of the solver output:\n{last_bytes(tail, TAIL_BYTES)}"
    return message


class CaseSetupStage(Stage):
    """Stage 4: OpenFOAM dictionaries on the Mesh stage's mesh, gated by a short trial run."""

    name = STAGE
    agent = AGENT
    scope = FileScope(
        readable=("casespec.json", MESH_NOTES, "attempts/case_setup-*/*"),
        writable=(
            "attempts/case_setup-*/case/0/*",
            "attempts/case_setup-*/case/constant/*",
            "attempts/case_setup-*/case/system/*",
            "attempts/case_setup-*/*.md",
        ),
        excluded=(
            "attempts/case_setup-*/case/constant/polyMesh",
            "attempts/case_setup-*/case/constant/polyMesh/*",
        ),
    )

    def __init__(self) -> None:
        self._meshes: dict[str, _Snapshot] = {}  # attempt directory -> the mesh it started with

    # ------------------------------------------------------------------ the agent's task

    def opening_message(self, ctx: StageContext) -> str:
        spec = self._spec(ctx)
        solver = self._solver(spec)
        plan = {
            "title": spec.title,
            "summary": spec.summary,
            "dimensionality": spec.dimensionality,
            "flow_regime": spec.flow_regime,
            "time_dependence": spec.time_dependence,
            "fluid": spec.fluid.model_dump(),
            "reference": spec.reference.model_dump(),
            "geometry": spec.geometry.model_dump(),
            "boundaries": [b.model_dump() for b in spec.boundaries],
            "mesh": spec.mesh.model_dump(),
            "solver": spec.solver.model_dump(),
            "requested_outputs": [o.model_dump() for o in spec.requested_outputs],
            "assumptions": spec.assumptions,
        }
        notes = ""
        if self._has_mesh_notes(ctx):
            notes = (
                f" The Mesh stage left notes in {MESH_NOTES}: read them before choosing the "
                "convection schemes."
            )
        return (
            "Set up the OpenFOAM case for this plan on the mesh that the Mesh stage built.\n\n"
            f"{json.dumps(plan, indent=2)}\n\n"
            f"The attempt directory holds case/ with {POLYMESH_REL} only: that mesh is final, so "
            "do not change it (the file tools refuse to write there, and the check compares it "
            "byte for byte). Read case/constant/polyMesh/boundary for the patch names and types. "
            "Write the field files into case/0/, the property files into case/constant/ and the "
            f"dictionaries into case/system/. The solver is {solver} (the plan's suggested "
            f"solver): set `application {solver};` in {CONTROL_DICT_REL}. Give every quantity in "
            "requested_outputs a function object that writes it, as the instructions describe."
            f"{notes}"
        )

    # ------------------------------------------------------------------ attempts

    def prepare_attempt(self, ctx: StageContext, attempt_dir: Path) -> None:
        """Copy the run's mesh-only ``case/`` into the attempt and remember what the mesh was."""
        self._solver(self._spec(ctx))
        rel = ctx.run.relative(attempt_dir)
        source_rel = f"{CASE_NAME}/{POLYMESH_REL}"
        try:
            source = ctx.run.path(source_rel)
            if not source.is_dir():
                raise ToolFailure(
                    f"{source_rel} does not exist: the Mesh stage has not produced it, so the "
                    "pipeline order is broken"
                )
            snapshot = _snapshot(source)
            ctx.run.promote(ctx.run.path(CASE_NAME), f"{rel}/{CASE_NAME}")
        except (RunDirError, OSError) as exc:
            raise ToolFailure(f"could not copy the mesh into {rel}/: {exc}") from exc
        if _changes(ctx.run.path(f"{rel}/{CASE_NAME}/{POLYMESH_REL}"), snapshot):
            raise ToolFailure(f"the copy of {source_rel} in {rel}/ differs from the original")
        self._meshes[rel] = snapshot

    def extra_tools(self, ctx: StageContext, attempt_dir: Path) -> list[Tool]:
        case_rel = f"{ctx.run.relative(attempt_dir)}/{CASE_NAME}"
        solver = self._solver(self._spec(ctx))
        return [
            Tool(
                "search_knowledge",
                "Search the OpenFOAM tutorials of this installation. A file matches when all the "
                "words occur in its path (case and file names) or its text, in any case. Returns "
                "up to 10 lines `path:line: text`. Search for the closest case first, then read "
                "its dictionaries.",
                SearchKnowledgeToolArgs,
                lambda args: self._search(ctx, args),
            ),
            Tool(
                "read_knowledge",
                "Read one file of the OpenFOAM tutorials (a path from search_knowledge), or list a "
                f"directory. At most {READ_BYTES} bytes are shown.",
                ReadKnowledgeToolArgs,
                lambda args: self._read(ctx, args),
            ),
            Tool(
                "validate_dictionaries",
                "Check the case in the attempt directory without using an attempt: every "
                "dictionary parses, every patch of the mesh has an entry in every field file, "
                "constraint patch types match, application and endTime are consistent. It does "
                "not run the solver; check_stage does that.",
                NoArgs,
                lambda _args: self._validate(ctx, case_rel, solver),
            ),
        ]

    # ------------------------------------------------------------------ the gate

    def execute_and_gate(self, ctx: StageContext, attempt_dir: Path) -> GateResult:
        solver = self._solver(self._spec(ctx))
        rel = ctx.run.relative(attempt_dir)
        case_rel = f"{rel}/{CASE_NAME}"
        problem = self._mesh_problem(ctx, rel)
        if problem:
            return GateResult(False, [problem])
        errors = self._static_problems(ctx, case_rel, solver)
        if errors:
            return GateResult(False, errors)
        response = ctx.sandbox.call(
            "trial_run",
            TrialRunArgs(case_dir=case_rel, solver=solver, steps=TRIAL_STEPS),
            outer_timeout(ctx.inner_timeout(TRIAL_TIMEOUT_S)),
        )
        if response.internal:
            raise _internal(response, "the trial run")
        if not response.ok:
            return GateResult(False, [_trial_failure(response, solver)])
        return GateResult(True, [])

    def promote(self, ctx: StageContext, attempt_dir: Path) -> None:
        """``case/``: the attempt's case (0/, constant/ with the unchanged mesh, system/), replacing
        the mesh-only one; ``case_setup/``: the notes the agent left in the attempt directory.
        ``boundary_manifest.json`` beside ``case/`` stays where the Mesh stage put it."""
        rel = ctx.run.relative(attempt_dir)
        problem = self._mesh_problem(ctx, rel)
        if problem:
            raise ToolFailure(f"cannot promote: {problem}")
        try:
            target = ctx.run.path(PROMOTED_DIR)
            if os.path.lexists(target):
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            target.mkdir(parents=True)
            for entry in sorted(os.scandir(attempt_dir), key=lambda e: e.name):
                if entry.is_file(follow_symlinks=False) and not entry.name.startswith("."):
                    shutil.copyfile(entry.path, target / entry.name)
            ctx.run.promote(ctx.run.path(f"{rel}/{CASE_NAME}"), PROMOTED_CASE_DIR)
        except (RunDirError, OSError) as exc:
            raise ToolFailure(f"promoting the case failed: {type(exc).__name__}: {exc}") from exc

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _spec(ctx: StageContext) -> CaseSpec:
        if ctx.casespec is None:
            raise ToolFailure("the case setup stage needs the CaseSpec; run the planner first")
        return ctx.casespec

    @staticmethod
    def _solver(spec: CaseSpec) -> str:
        solver = spec.solver.suggested_solver
        if solver not in KNOWN_SOLVERS:
            raise ToolFailure(
                f"the plan's suggested solver {solver!r} is not one this pipeline runs "
                f"({', '.join(KNOWN_SOLVERS)}); the planner should have rejected it"
            )
        return solver

    @staticmethod
    def _has_mesh_notes(ctx: StageContext) -> bool:
        try:
            return ctx.run.path(MESH_NOTES).is_file()
        except RunDirError:
            return False

    def _mesh_problem(self, ctx: StageContext, rel: str) -> str | None:
        """A message when the attempt's ``constant/polyMesh`` is not the mesh the attempt started
        with, else ``None``. A missing snapshot means ``prepare_attempt`` did not run."""
        snapshot = self._meshes.get(rel)
        if snapshot is None:
            raise ToolFailure(f"prepare_attempt has not run for {rel}; there is no mesh to compare")
        differences = _changes(ctx.run.path(f"{rel}/{CASE_NAME}/{POLYMESH_REL}"), snapshot)
        if not differences:
            return None
        return (
            f"{POLYMESH_REL} in the attempt was changed ({'; '.join(differences)}). The mesh "
            "belongs to the Mesh stage: do not edit the mesh, not even to add a file. Write only "
            "case/0/, case/constant/ (other than polyMesh) and case/system/. The next attempt "
            "starts again from the original mesh"
        )

    def _static_problems(self, ctx: StageContext, case_rel: str, solver: str) -> list[str]:
        """The checks that run no solver: ``controlDict``, the dictionary syntax, the consistency
        of the case. Stops after the first step that fails, except that all consistency problems
        come together."""
        control = f"{case_rel}/{CONTROL_DICT_REL}"
        try:
            exists = ctx.run.path(control).is_file()
        except RunDirError as exc:
            raise AgentInputError(str(exc)) from exc
        if not exists:
            return [
                f"{CONTROL_DICT_REL} does not exist in {case_rel}/. Write it with write_file "
                f"(path {control}): application, startFrom, startTime, endTime, deltaT and the "
                "write settings"
            ]
        gate_s = ctx.inner_timeout(GATE_TIMEOUT_S)
        parsed = ctx.sandbox.call(
            "check_dictionaries", CheckDictionariesArgs(case_dir=case_rel), outer_timeout(gate_s)
        )
        if parsed.internal:
            raise _internal(parsed, "checking the dictionaries")
        if not parsed.ok:
            return _errors(parsed, "the dictionaries do not parse")
        consistent = ctx.sandbox.call(
            "check_case_consistency",
            CheckCaseConsistencyArgs(case_dir=case_rel, solver=solver),
            outer_timeout(ctx.inner_timeout(GATE_TIMEOUT_S)),
        )
        if consistent.internal:
            raise _internal(consistent, "checking the case")
        if not consistent.ok:
            return _errors(consistent, "the case is not consistent")
        return []

    # ------------------------------------------------------------------ the agent's tools

    @staticmethod
    def _knowledge(ctx: StageContext, op: str, args: BaseModel) -> dict:
        response = ctx.sandbox.call(op, args, outer_timeout(ctx.inner_timeout(KNOWLEDGE_TIMEOUT_S)))
        if response.internal:
            raise _internal(response, f"{op}")
        if not response.ok:
            raise AgentInputError(response.error or f"{op} failed without a message")
        return response.data or {}

    def _search(self, ctx: StageContext, args: SearchKnowledgeToolArgs) -> str:
        data = self._knowledge(ctx, "search_knowledge", SearchKnowledgeArgs(query=args.query))
        hits = data.get("hits")
        if not isinstance(hits, list):
            raise ToolFailure("search_knowledge answered without a list of hits")
        if not hits:
            return (
                "No tutorial file contains all of these words. Try fewer or different words, "
                "for example a solver name, a dictionary name or a keyword."
            )
        lines = []
        for hit in hits:
            path, line, text = str(hit["path"]), int(hit["line"]), str(hit["text"])
            lines.append(f"{path}:{line}: {text}" if line else f"{path} {text}")
        return "\n".join(lines)

    def _read(self, ctx: StageContext, args: ReadKnowledgeToolArgs) -> str:
        request = ReadKnowledgeArgs(path=args.path, max_bytes=READ_BYTES)
        data = self._knowledge(ctx, "read_knowledge", request)
        text = data.get("text")
        if not isinstance(text, str):
            raise ToolFailure("read_knowledge answered without text")
        return text

    def _validate(self, ctx: StageContext, case_rel: str, solver: str) -> str:
        problems = self._static_problems(ctx, case_rel, solver)
        if not problems:
            return (
                "No problems found: every dictionary parses and the case is consistent with the "
                "mesh and the solver. The solver itself has not been run; check_stage runs "
                f"{TRIAL_STEPS} time steps."
            )
        return "Problems found:\n" + "\n".join(f"- {p}" for p in problems)
