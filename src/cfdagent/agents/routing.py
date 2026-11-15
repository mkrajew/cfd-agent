"""Routing between pipeline stages (ADR-0006, design 6.2 and 6.5).

A failed gate or a Critic verdict may name an earlier stage; the orchestrator then runs that stage
again, and every stage after it, in a numbered pass. This module holds the vocabulary of that
mechanism and the one piece of file handling it needs:

* ``STAGE_ORDER`` and ``ROUTE_TARGETS``: the fixed order of the pipeline and the stages a fix may
  be aimed at. The Solver Runner edits nothing, so a fix aimed at it goes to ``case_setup``; the
  Critic reads and never changes anything, so it is never a target.
* ``RouteRequest``: who asks, which stage should run again, why and with which instruction.
* ``fix_request_text``: the sentence that tells the target agent what was asked of it.
* ``move_downstream_aside``: moves the canonical outputs of every stage after the target to
  ``superseded/pass-<n>/``, so that no later stage can read a result of the pass that is being
  replaced and nothing is deleted (the failure taxonomy needs the old files).

This module is host-side: it imports no tool, CAD, mesh or knowledge code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from cfdagent.record import redact
from cfdagent.rundir import RunDir, RunDirError

STAGE_ORDER = ("planner", "geometry", "mesh", "case_setup", "solver_runner", "analyst", "critic")
ROUTE_TARGETS = frozenset({"planner", "geometry", "mesh", "case_setup", "analyst"})
MAX_TEXT_CHARS = 1000  # reason and instruction
ORIGINS = ("gate", "critic")
SUPERSEDED_DIR = "superseded"
CASE_DIR = "case"
POLYMESH_REL = "constant/polyMesh"  # inside ``case/``: the Mesh stage's output

# What each stage leaves in the run directory as its own canonical output (as its ``promote``
# defines it). ``case/`` is listed apart: Mesh, Case Setup and Solver Runner each rewrite part of
# it, and the part that survives a route back depends on the target.
STAGE_OUTPUTS: dict[str, tuple[str, ...]] = {
    "planner": (),
    "geometry": ("geometry",),
    "mesh": ("mesh", "boundary_manifest.json"),
    "case_setup": ("case_setup",),
    "solver_runner": ("logs",),
    "analyst": ("analyst", "results", "analysis.md"),
    "critic": (),
}
CASE_WRITERS = ("mesh", "case_setup", "solver_runner")


@dataclass(frozen=True)
class RouteRequest:
    """A request to run ``target`` (and every stage after it) again.

    ``source`` is the stage whose gate failed or the Critic; ``target`` must be a route target that
    comes before ``source`` in ``STAGE_ORDER`` (``ValueError`` otherwise). ``reason`` and
    ``instruction`` are free text of an agent or of a gate: capped at ``MAX_TEXT_CHARS`` and cleaned
    like the errors of the run record.
    """

    source: str
    target: str
    origin: Literal["gate", "critic"]
    reason: str
    instruction: str | None = None

    def __post_init__(self) -> None:
        if self.source not in STAGE_ORDER:
            raise ValueError(f"unknown source stage {self.source!r}; expected one of {STAGE_ORDER}")
        if self.target not in ROUTE_TARGETS:
            raise ValueError(
                f"{self.target!r} cannot be the target of a fix; the targets are "
                f"{sorted(ROUTE_TARGETS, key=STAGE_ORDER.index)}"
            )
        if STAGE_ORDER.index(self.target) >= STAGE_ORDER.index(self.source):
            raise ValueError(
                f"a fix must go back: {self.target!r} does not come before {self.source!r}"
            )
        if self.origin not in ORIGINS:
            raise ValueError(f"unknown origin {self.origin!r}; expected one of {ORIGINS}")
        object.__setattr__(self, "reason", redact(str(self.reason), MAX_TEXT_CHARS))
        if self.instruction is not None:
            object.__setattr__(self, "instruction", redact(str(self.instruction), MAX_TEXT_CHARS))


def fix_request_text(request: RouteRequest) -> str:
    """``Fix requested by <source>: <reason>. <instruction>``, for the target agent."""
    reason = request.reason.strip() or "no reason was given"
    if reason[-1] not in ".!?":
        reason += "."
    text = f"Fix requested by {request.source}: {reason}"
    if request.instruction and request.instruction.strip():
        text += f" {request.instruction.strip()}"
    return text


# ------------------------------------------------------------------ what a route back moves aside


def _moves_for(target: str) -> list[str]:
    """Run-relative canonical outputs of the stages after ``target``, in pipeline order. ``case``
    is named whole here; a route to ``case_setup`` keeps the mesh in it (see ``_move_case``)."""
    later = STAGE_ORDER[STAGE_ORDER.index(target) + 1 :]
    names: list[str] = []
    for stage in later:
        for name in STAGE_OUTPUTS[stage]:
            names.append(name)
        if stage in CASE_WRITERS and CASE_DIR not in names:
            names.append(CASE_DIR)
    return names


def _move(run: RunDir, source_rel: str, destination_rel: str) -> None:
    source = run.path(source_rel)
    destination = run.path(destination_rel)
    if os.path.lexists(destination):
        raise RunDirError(f"cannot move {source_rel!r}: {destination_rel!r} exists already")
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, destination)  # a rename; a link on the way was refused by run.path
    except OSError as exc:
        raise RunDirError(f"cannot move {source_rel!r} aside ({type(exc).__name__})") from None


def _entries(run: RunDir, directory_rel: str) -> list[str]:
    try:
        return sorted(entry.name for entry in os.scandir(run.path(directory_rel)))
    except OSError as exc:
        raise RunDirError(f"cannot list {directory_rel!r} ({type(exc).__name__})") from None


def _move_case(run: RunDir, pass_dir: str, keep_mesh: bool) -> list[str]:
    """Move ``case/`` aside. With ``keep_mesh`` the files of the Mesh stage's output,
    ``case/constant/polyMesh``, stay where they are and everything else of ``case/`` moves."""
    if not keep_mesh:
        _move(run, CASE_DIR, f"{pass_dir}/{CASE_DIR}")
        return [CASE_DIR]
    moved: list[str] = []
    keep_parent, keep_name = POLYMESH_REL.split("/")
    case_path = run.path(CASE_DIR)
    for name in _entries(run, CASE_DIR):
        entry = f"{CASE_DIR}/{name}"
        is_real_dir = (case_path / name).is_dir() and not (case_path / name).is_symlink()
        if name == keep_parent and is_real_dir:
            for inner in _entries(run, entry):
                if inner == keep_name:
                    continue
                _move(run, f"{entry}/{inner}", f"{pass_dir}/{entry}/{inner}")
                moved.append(f"{entry}/{inner}")
        else:
            _move(run, entry, f"{pass_dir}/{entry}")
            moved.append(entry)
    return moved


def move_downstream_aside(run: RunDir, target: str, pass_no: int) -> list[str]:
    """Move the canonical outputs of every stage after ``target`` to ``superseded/pass-<pass_no>/``
    (``pass_no`` is the number of the pass that is about to start) and return what moved, as
    run-relative paths.

    The outputs of the stage itself and of the stages before it stay, and so do the attempt
    directories, the record, the transcript and the logs of the run. For a ``case_setup`` target the
    mesh stays in ``case/constant/polyMesh`` (the Case Setup stage starts from it) and the rest of
    ``case/`` moves; for a ``mesh`` target or an earlier one the whole of ``case/`` moves, because
    the mesh is rebuilt. What does not exist is skipped. A pass directory that exists already is an
    error: nothing is moved and nothing is overwritten.
    """
    if target not in ROUTE_TARGETS:
        raise ValueError(f"{target!r} cannot be the target of a fix")
    if isinstance(pass_no, bool) or not isinstance(pass_no, int) or pass_no < 1:
        raise ValueError(f"the new pass number must be an integer of at least 1, got {pass_no!r}")
    pass_dir = f"{SUPERSEDED_DIR}/pass-{pass_no}"
    if os.path.lexists(run.path(pass_dir)):
        raise RunDirError(f"{pass_dir!r} exists already; a pass is superseded once")
    moved: list[str] = []
    for name in _moves_for(target):
        if not os.path.lexists(run.path(name)):
            continue
        if name == CASE_DIR:
            moved += _move_case(run, pass_dir, keep_mesh=target == "case_setup")
        else:
            _move(run, name, f"{pass_dir}/{name}")
            moved.append(name)
    return moved
