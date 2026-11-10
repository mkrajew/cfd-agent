"""The Analyst stage, stage 6 of the pipeline (design 6.2, 6.3).

The Solver Runner has left the run directory a solved ``case/`` and ``logs/`` (the solver logs and
``solver_run.json``, the run's summary). Every attempt gets a fresh copy of the case as
``attempts/analyst-N/case/`` (``prepare_attempt``), so that what the agent runs on it (the
``run_utility`` tool, the post-processing script) never changes the solved case.

The agent writes ``post.py`` and ``analysis.md`` into the attempt directory. The gate runs the
script in the sandbox (``run_script``, working directory = the attempt directory, no arguments); it
must write ``quantities.json`` there (``cfdagent.tools.post.write_quantities``). The host then
reads the file with a size limit and checks it: ``validate_quantities`` (finite numbers, well formed
profiles), every ``OutputSpec`` of the plan present with the right kind (a scalar is a number, a
profile is ``{"x": [...], "value": [...]}`` sorted by ``x``), and ``analysis.md`` present, not empty
and mentioning convergence. The gate does not judge the physics, the agreement with a reference or
the convergence of the run: the Critic judges the first, the Scorer the second, and the analysis
states the third. Under the decision that a non-converged steady run may reach this stage, the
prompt requires an honest verdict instead.

The Analyst has the file tools, the calculator and ``run_utility`` (a short allowlist of OpenFOAM
post-processing utilities). It has no knowledge tools and cannot read ``benchmarks/``: its readable
paths are the plan, the solved case, the logs and its own attempt directories.

``promote``: ``results/`` (``quantities.json`` and ``plots/*.png``) and ``analysis.md`` in the run
directory.

This module is host-side: it imports no tool, CAD, mesh or knowledge code.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, Field

from cfdagent.agents.casespec import CaseSpec, OutputSpec
from cfdagent.agents.gatehelp import last_bytes, names
from cfdagent.agents.stage import Stage, StageContext
from cfdagent.agents.tools import FileScope, Tool
from cfdagent.errors import AgentInputError, ToolFailure
from cfdagent.executor.protocol import OpResponse, RunScriptArgs, RunUtilityArgs
from cfdagent.gates import GateResult
from cfdagent.quantities import validate_quantities
from cfdagent.rundir import FileTooLarge, RunDirError
from cfdagent.sandbox.client import outer_timeout

STAGE = "analyst"
AGENT = "analyst"
CASE_NAME = "case"
SCRIPT_NAME = "post.py"
ANALYSIS_NAME = "analysis.md"
QUANTITIES_NAME = "quantities.json"
PLOTS_DIR = "plots"
RESULTS_DIR = "results"  # in the run directory once promoted
SOLVER_SUMMARY = "logs/solver_run.json"
CONTROL_DICT = f"{CASE_NAME}/system/controlDict"
POLYMESH = f"{CASE_NAME}/constant/polyMesh"
SCRIPT_TIMEOUT_S = 900.0
UTILITY_TIMEOUT_S = 600.0  # the executor's own limit for a utility
# A profile may hold a million points (quantities.MAX_PROFILE_POINTS); this bounds the file that
# the host reads. The same cap holds for the other texts it reads.
QUANTITIES_MAX_BYTES = 32 * 1024 * 1024
ANALYSIS_MAX_BYTES = 200_000
SUMMARY_MAX_BYTES = 200_000
MAX_PLOTS = 50
PLOT_MAX_BYTES = 20 * 1024 * 1024
TAIL_BYTES = 3000  # the loop shows the model at most 4096 bytes of all errors together
UTILITY_TAIL_BYTES = 3000
_ECHO_CHARS = 80
MAX_WARNINGS = 20  # of the solver summary shown in the opening message
WARNING_CHARS = 200
SUMMARY_SHOWN_CHARS = 8000
SNAPSHOT_DIRS = ("logs", CASE_NAME)  # what the post-processing script must only read
HASH_MAX_BYTES = 16 * 1024 * 1024  # a larger file is compared by size only
MAX_CHANGES_SHOWN = 10
# OpenFOAM utilities the agent may run with ``run_utility``: a subset of the executor's own
# allowlist (``executor.ops.UTILITIES``, which the executor enforces again). ``foamDictionary`` and
# ``changeDictionary`` can edit files and ``checkMesh`` belongs to the Mesh stage.
UTILITIES: frozenset[str] = frozenset({"postProcess", "foamListTimes"})
_CONVERGENCE = re.compile(r"converg", re.IGNORECASE)
# Keys of ``logs/solver_run.json`` that the opening message shows.
_SUMMARY_KEYS = (
    "solver",
    "n_procs",
    "ok",
    "completed",
    "steady",
    "time_dependence",
    "steady_expected",
    "stopped_by_residual_control",
    "converged_at",
    "first_time",
    "end_time",
    "case_end_time",
    "max_courant",
    "first_initial_residuals",
    "initial_residuals",
    "warnings",
    "fatal_error",
)


class RunUtilityToolArgs(BaseModel):
    argv: list[Annotated[str, Field(max_length=1000)]] = Field(
        min_length=1,
        max_length=64,
        description=(
            "The command as a list of words, for example "
            '["postProcess", "-func", "writeCellCentres", "-latestTime"]; the first word is the '
            f"utility ({', '.join(sorted(UTILITIES))})"
        ),
    )


def _clip(text: str, limit: int = _ECHO_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + "..."


def _internal(response: OpResponse, label: str) -> ToolFailure:
    return ToolFailure(f"{label} failed outside the agent's work: {response.error}")


def _remove(path: Path) -> None:
    """Remove whatever is at ``path`` (file, link, FIFO, directory tree)."""
    if os.path.lexists(path):
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            path.unlink()


def _first_unsorted(x: list[float]) -> int | None:
    """The index of the first point that lies before its predecessor, or ``None``."""
    for i in range(1, len(x)):
        if x[i] < x[i - 1]:
            return i
    return None


def _snapshot(root: Path) -> dict[str, str]:
    """``{relative path: sha256 of the content}`` of every file below ``logs/`` and ``case/`` of
    the run directory (a file above ``HASH_MAX_BYTES`` is compared by its size, a link by its
    kind). Used to see that the post-processing script left the solved case and the logs as they
    were."""
    found: dict[str, str] = {}
    for top in SNAPSHOT_DIRS:
        for directory, subdirs, files in os.walk(root / top, followlinks=False):
            base = Path(directory)
            for name in subdirs + files:
                path = base / name
                rel = path.relative_to(root).as_posix()
                try:
                    if path.is_symlink():
                        found[rel] = "link"
                    elif path.is_file():
                        size = path.stat().st_size
                        if size > HASH_MAX_BYTES:
                            found[rel] = f"size:{size}"
                            continue
                        found[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
                except OSError:
                    found[rel] = "unreadable"
    return found


class AnalystStage(Stage):
    """Stage 6: post-process the solved case with a script; gate the quantities and the analysis."""

    name = STAGE
    agent = AGENT
    scope = FileScope(
        readable=("casespec.json", "case/*", "logs/*", "attempts/analyst-*/*"),
        writable=(
            f"attempts/analyst-*/{SCRIPT_NAME}",
            f"attempts/analyst-*/{ANALYSIS_NAME}",
            f"attempts/analyst-*/{PLOTS_DIR}/*.png",
        ),
    )

    # ------------------------------------------------------------------ the agent's task

    def opening_message(self, ctx: StageContext) -> str:
        spec = self._spec(ctx)
        rel = "attempts/analyst-<N>"
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
            "solver": spec.solver.model_dump(),
            "requested_outputs": [o.model_dump() for o in spec.requested_outputs],
            "assumptions": spec.assumptions,
        }
        outputs = ", ".join(f"`{o.name}` ({o.kind})" for o in spec.requested_outputs)
        return (
            "Extract the requested quantities from the solved case of this plan, judge how "
            "reliable the solution is and interpret it.\n\n"
            f"{json.dumps(plan, indent=2)}\n\n"
            f"Summary of the solver run ({SOLVER_SUMMARY}):\n{self._summary_text(ctx)}\n\n"
            f"The requested outputs, with exactly these names and kinds: {outputs}.\n\n"
            f"Your attempt directory ({rel}) holds {CASE_NAME}/, "
            "a working copy of the solved case; the solved case itself is in case/ of the run "
            f"directory. Write {SCRIPT_NAME} in the attempt directory: the gate runs it there as "
            f"`python {SCRIPT_NAME}` without arguments, so it reads {CASE_NAME}/ and writes "
            f"{QUANTITIES_NAME} (with write_quantities from cfdagent.tools.post) and the PNG "
            f"plots into {PLOTS_DIR}/. Read the case with cfdagent.tools.foam_results (documented "
            "in your instructions), not with a parser of your own. "
            f"Also write {ANALYSIS_NAME} there: what was computed, a "
            "convergence verdict with the numbers (the stage cannot pass without it), the "
            "conservation and plausibility checks, a short physical interpretation and the "
            "limits. Read only the root time directories of the case, never processor*/ "
            "(a parallel run keeps the decomposed data there). Call check_stage when both files "
            "are written."
        )

    def _summary_text(self, ctx: StageContext) -> str:
        try:
            data = ctx.run.read_json(SOLVER_SUMMARY, SUMMARY_MAX_BYTES)
        except (RunDirError, FileNotFoundError):
            data = None
        if not isinstance(data, dict):
            return f"({SOLVER_SUMMARY} could not be read as an object; read it with read_file.)"
        shown = {key: data[key] for key in _SUMMARY_KEYS if key in data}
        warnings = shown.get("warnings")
        if isinstance(warnings, list):
            kept = [_clip(str(w), WARNING_CHARS) for w in warnings[:MAX_WARNINGS]]
            if len(warnings) > MAX_WARNINGS:
                kept.append(f"... and {len(warnings) - MAX_WARNINGS} more")
            shown["warnings"] = kept
        if isinstance(shown.get("fatal_error"), str):
            shown["fatal_error"] = _clip(shown["fatal_error"], 500)
        text = json.dumps(shown, indent=2)
        if len(text) > SUMMARY_SHOWN_CHARS:
            text = text[:SUMMARY_SHOWN_CHARS] + "\n[cut; read the file for the rest]"
        return text

    # ------------------------------------------------------------------ attempts

    def prepare_attempt(self, ctx: StageContext, attempt_dir: Path) -> None:
        """Copy the solved ``case/`` into the attempt."""
        self._spec(ctx)
        try:
            if not ctx.run.path(CONTROL_DICT).is_file():
                raise ToolFailure(
                    f"{CONTROL_DICT} does not exist: the solved case is missing, so the pipeline "
                    "order is broken"
                )
            if not ctx.run.path(POLYMESH).is_dir():
                raise ToolFailure(
                    f"{POLYMESH} does not exist: the case has no mesh, so the pipeline order "
                    "is broken"
                )
            if not ctx.run.path(SOLVER_SUMMARY).is_file():
                raise ToolFailure(
                    f"{SOLVER_SUMMARY} does not exist: the Solver Runner has not promoted its "
                    "summary, so the pipeline order is broken"
                )
            rel = ctx.run.relative(attempt_dir)
            ctx.run.promote(ctx.run.path(CASE_NAME), f"{rel}/{CASE_NAME}")
        except (RunDirError, OSError) as exc:
            raise ToolFailure(f"the solved case cannot be used: {exc}") from exc

    def extra_tools(self, ctx: StageContext, attempt_dir: Path) -> list[Tool]:
        case_rel = f"{ctx.run.relative(attempt_dir)}/{CASE_NAME}"
        return [
            Tool(
                "run_utility",
                "Run an OpenFOAM post-processing utility in the working copy of the case in your "
                f"attempt directory ({', '.join(sorted(UTILITIES))} only; no shell, no other "
                "program). The utility's log is case/log.<utility>; what it writes (time "
                "directories, postProcessing/) is in the working copy and can be read by "
                f"{SCRIPT_NAME}. Returns the end of its output.",
                RunUtilityToolArgs,
                lambda args: self._run_utility(ctx, case_rel, args),
            )
        ]

    def _run_utility(self, ctx: StageContext, case_rel: str, args: RunUtilityToolArgs) -> str:
        name = args.argv[0]
        if name not in UTILITIES:
            raise AgentInputError(
                f"{_clip(name)!r} is not an allowed utility; the allowed ones are "
                f"{', '.join(sorted(UTILITIES))}. Anything else is computed in {SCRIPT_NAME}"
            )
        response = ctx.sandbox.call(
            "run_utility",
            RunUtilityArgs(argv=args.argv, cwd=case_rel),
            outer_timeout(ctx.inner_timeout(UTILITY_TIMEOUT_S)),
        )
        if response.internal:
            raise _internal(response, f"running {name}")
        data = response.data if isinstance(response.data, dict) else {}
        if not response.ok and "returncode" not in data:
            # The executor refused the call (an argument outside the run directory, for example).
            raise AgentInputError(response.error or "run_utility was refused without a message")
        tail = data.get("tail")
        tail = last_bytes(tail, UTILITY_TAIL_BYTES) if isinstance(tail, str) else ""
        if response.ok:
            head = f"{name} finished (full log: {CASE_NAME}/log.{name} in your attempt directory)."
        else:
            reason = (response.error or f"{name} failed").partition("\n")[0]
            head = f"{name} FAILED: {_clip(reason, 500)}"
        return head + (f"\nEnd of the output:\n{tail}" if tail.strip() else "")

    # ------------------------------------------------------------------ the gate

    def execute_and_gate(self, ctx: StageContext, attempt_dir: Path) -> GateResult:
        spec = self._spec(ctx)
        rel = ctx.run.relative(attempt_dir)
        if not ctx.run.path(f"{rel}/{SCRIPT_NAME}").is_file():
            errors = [
                f"{SCRIPT_NAME} does not exist in {rel}/. Write it there with write_file (path "
                f"{rel}/{SCRIPT_NAME}); it reads {CASE_NAME}/ and writes {QUANTITIES_NAME} into "
                "its working directory"
            ]
            return GateResult(False, [*errors, *self._analysis_problems(ctx, rel)])
        try:
            _remove(ctx.run.path(f"{rel}/{QUANTITIES_NAME}"))  # an old file is not this run's
        except OSError as exc:
            raise ToolFailure(
                f"could not clear {rel}/{QUANTITIES_NAME}: {type(exc).__name__}"
            ) from exc

        before = _snapshot(ctx.run.root)
        script_s = ctx.inner_timeout(SCRIPT_TIMEOUT_S)
        run = ctx.sandbox.call(
            "run_script",
            RunScriptArgs(script=f"{rel}/{SCRIPT_NAME}", cwd=rel, timeout_s=script_s),
            outer_timeout(script_s),
        )
        if run.internal:
            raise _internal(run, "running the post-processing script")
        errors = []
        if not run.ok:
            errors.append(self._script_failure(run))
        # The script ran with write access to the run directory. Everything the later stages
        # rely on is judged after it: the solved case and logs as they were, and the files that
        # ``promote`` copies (it re-reads quantities.json and analysis.md).
        errors += self._changed_problems(before, _snapshot(ctx.run.root))
        if run.ok:
            errors += self._quantity_problems(ctx, spec, rel)
        errors += self._analysis_problems(ctx, rel)
        return GateResult(not errors, errors)

    @staticmethod
    def _changed_problems(before: dict[str, str], after: dict[str, str]) -> list[str]:
        changed = sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))
        if not changed:
            return []
        shown = ", ".join(_clip(p, 120) for p in changed[:MAX_CHANGES_SHOWN])
        more = (
            f" and {len(changed) - MAX_CHANGES_SHOWN} more"
            if len(changed) > MAX_CHANGES_SHOWN
            else ""
        )
        return [
            f"{SCRIPT_NAME} modified the solved case or the logs: {shown}{more}. It must only read "
            f"them: the files of {CASE_NAME}/ and logs/ in the run directory are what the later "
            f"stages rely on. Work on {CASE_NAME}/ in your attempt directory, and write only to "
            f"your attempt directory"
        ]

    @staticmethod
    def _script_failure(run: OpResponse) -> str:
        data = run.data if isinstance(run.data, dict) else {}
        headline, _, rest = (run.error or f"{SCRIPT_NAME} failed").partition("\n")
        tail = data.get("tail")
        if not isinstance(tail, str) or not tail.strip():
            tail = rest
        message = f"{SCRIPT_NAME} did not run successfully: {headline}"
        if tail.strip():
            message += (
                f"\nEnd of the script's output (including your prints):\n"
                f"{last_bytes(tail, TAIL_BYTES)}"
            )
        return message

    @staticmethod
    def _quantity_problems(ctx: StageContext, spec: CaseSpec, rel: str) -> list[str]:
        path = f"{rel}/{QUANTITIES_NAME}"
        missing_file = (
            f"{SCRIPT_NAME} ran but wrote no {QUANTITIES_NAME} in its working directory ({rel}/). "
            f"Write it with write_quantities(Path({QUANTITIES_NAME!r}), {{...}}) from "
            "cfdagent.tools.post, with the working directory as it is when the script is run "
            "(the gate runs `python post.py` there)"
        )
        try:
            data = ctx.run.read_json(path, QUANTITIES_MAX_BYTES)
        except FileNotFoundError:
            return [missing_file]
        except FileTooLarge:
            return [
                f"{QUANTITIES_NAME} is larger than {QUANTITIES_MAX_BYTES} bytes. Write the "
                "profiles with fewer points (a profile of some hundred points is enough)"
            ]
        except RunDirError as exc:
            return [f"{QUANTITIES_NAME} cannot be used: {exc}. Write it with write_quantities"]
        try:
            clean = validate_quantities(data)
        except ValueError as exc:
            return [f"{QUANTITIES_NAME} is not valid: {exc}"]
        return AnalystStage._requested_problems(spec.requested_outputs, clean)

    @staticmethod
    def _requested_problems(outputs: list[OutputSpec], clean: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        missing = [o for o in outputs if o.name not in clean]
        if missing:
            listed = "; ".join(f"`{_clip(o.name)}` ({o.kind})" for o in missing)
            errors.append(
                f"requested output(s) missing from {QUANTITIES_NAME}: {listed}. Use exactly the "
                f"names of the plan's requested_outputs (found: {names(set(clean)) or 'nothing'})"
            )
        for output in outputs:
            if output.name not in clean:
                continue
            value = clean[output.name]
            is_profile = isinstance(value, dict)
            label = f"`{_clip(output.name)}`"
            if output.kind == "scalar" and is_profile:
                errors.append(
                    f"{label} must be a scalar (a single number) but is a profile; write the "
                    "number the plan asks for"
                )
            elif output.kind == "profile" and not is_profile:
                errors.append(
                    f'{label} must be a profile {{"x": [...], "value": [...]}} but is a '
                    "single number"
                )
            elif is_profile:
                bad = _first_unsorted(value["x"])
                if bad is not None:
                    errors.append(
                        f"profile {label} is not sorted by x: x[{bad}] = {value['x'][bad]:g} lies "
                        f"before x[{bad - 1}] = {value['x'][bad - 1]:g}. Sort both lists by x, "
                        "ascending"
                    )
        return errors

    @staticmethod
    def _analysis_problems(ctx: StageContext, rel: str) -> list[str]:
        path = f"{rel}/{ANALYSIS_NAME}"
        try:
            text = ctx.run.read_text(path, ANALYSIS_MAX_BYTES)
        except FileNotFoundError:
            return [
                f"{ANALYSIS_NAME} does not exist in {rel}/. Write it there with write_file (path "
                f"{path}): what was computed, a convergence verdict with the numbers, the "
                "conservation and plausibility checks and a short physical interpretation"
            ]
        except FileTooLarge:
            return [f"{ANALYSIS_NAME} is larger than {ANALYSIS_MAX_BYTES} bytes; keep it to a page"]
        except RunDirError as exc:
            return [f"{ANALYSIS_NAME} cannot be read: {exc}"]
        if not text.strip():
            return [f"{ANALYSIS_NAME} is empty; write the analysis"]
        if not _CONVERGENCE.search(text):
            return [
                f"{ANALYSIS_NAME} does not mention convergence. State whether the final state is "
                "converged (steady runs: whether residualControl was met, the Initial residuals "
                "and the change of the profiles between the last two write times) and give the "
                "numbers; if the run is not converged, say so"
            ]
        return []

    # ------------------------------------------------------------------ promotion

    def promote(self, ctx: StageContext, attempt_dir: Path) -> None:
        """``results/``: ``quantities.json`` and ``plots/*.png``; ``analysis.md``: the analysis."""
        rel = ctx.run.relative(attempt_dir)
        try:
            quantities = ctx.run.path(f"{rel}/{QUANTITIES_NAME}")
            if not quantities.is_file() or quantities.is_symlink():
                raise ToolFailure(f"cannot promote: {rel}/{QUANTITIES_NAME} does not exist")
            text = ctx.run.read_text(f"{rel}/{ANALYSIS_NAME}", ANALYSIS_MAX_BYTES)
            results = ctx.run.path(RESULTS_DIR)
            _remove(results)
            results.mkdir(parents=True)
            shutil.copyfile(quantities, results / QUANTITIES_NAME)
            self._copy_plots(attempt_dir / PLOTS_DIR, results / PLOTS_DIR)
            ctx.run.write_text(ANALYSIS_NAME, text)
        except (RunDirError, OSError) as exc:
            raise ToolFailure(f"promoting the results failed: {type(exc).__name__}: {exc}") from exc

    @staticmethod
    def _copy_plots(source: Path, target: Path) -> None:
        if source.is_symlink() or not source.is_dir():
            return
        copied = 0
        for entry in sorted(os.scandir(source), key=lambda e: e.name):
            if copied >= MAX_PLOTS:
                break
            if not entry.name.lower().endswith(".png") or not entry.is_file(follow_symlinks=False):
                continue
            if entry.stat(follow_symlinks=False).st_size > PLOT_MAX_BYTES:
                continue
            target.mkdir(exist_ok=True)
            shutil.copyfile(entry.path, target / entry.name)
            copied += 1

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _spec(ctx: StageContext) -> CaseSpec:
        if ctx.casespec is None:
            raise ToolFailure("the analyst needs the CaseSpec; run the planner first")
        return ctx.casespec

    def retry_text(self, previous: str) -> str:
        return (
            f"Write the corrected {SCRIPT_NAME} and {ANALYSIS_NAME} there ({CASE_NAME}/ is a fresh "
            f"copy of the solved case); the files of the previous attempt stay readable in "
            f"{previous}. Call check_stage again when done."
        )
