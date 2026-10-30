"""The Planner, stage 1 of the pipeline: the task text becomes a validated ``CaseSpec``.

The Planner has no files and no sandbox, so it is a function (``run_planner``) and not a
``Stage``. It works in two phases on one conversation:

1. A tool loop with the ``calculator`` until the model answers in plain text (at most
   ``PHASE_ONE_MAX_TURNS`` model turns). Reaching the limit is not a failure; phase 2 starts anyway
   with whatever was worked out.
2. One call with ``response_schema=CaseSpec`` over the conversation of phase 1. A reply that does
   not validate (``StructuredOutputError``), is cut off (``TruncatedOutputError``) or is
   unusable in another way, or that ``plan_gate`` rejects, is reported to the model and the call
   is repeated. Each such call is an *attempt*; ``limits.max_attempts_per_stage`` of them at most.

Exception: when the only errors of ``plan_gate`` are resource errors (``exceeds resources:``) the
run ends at once without a retry. The estimate is a result to record, not something to argue
away (design 6.8); ``casespec.json`` is written so the estimate is kept.

Model calls, billing, logging and the wall-clock and cost limits are shared with the gated stage
loop (``agents.loop.ModelConversation``).
"""

from __future__ import annotations

import math
import re

from pydantic import ValidationError

from cfdagent.agents.casespec import CaseSpec
from cfdagent.agents.loop import (
    MAX_REASON_CHARS,
    ModelConversation,
    StageOutcome,
    internal,
    short,
)
from cfdagent.agents.stage import StageContext
from cfdagent.agents.tools import ToolRegistry, calculator_tool
from cfdagent.config import RunLimits
from cfdagent.gates import GateResult
from cfdagent.llm.client import (
    ChatResult,
    LLMError,
    StructuredOutputError,
    ToolCall,
    TruncatedOutputError,
)
from cfdagent.prompts import load_prompt
from cfdagent.record import AttemptRecord

STAGE = "planner"
AGENT = "planner"
CASESPEC_FILE = "casespec.json"
PHASE_ONE_MAX_TURNS = 8
RESOURCE_PREFIX = "exceeds resources:"
REYNOLDS_TOLERANCE = 1e-3  # relative, 0.1 %
MAX_SCHEMA_PROBLEMS = 8
MAX_FEEDBACK_CHARS = 4000

_BACKTICKED = re.compile(r"`([^`\n]+)`")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

SHORTER_ADVICE = (
    "Reply again with the complete CaseSpec as one JSON object; keep every text field to one or "
    "two sentences and do not repeat information."
)


def task_identifiers(task_text: str) -> list[str]:
    """The identifiers the task puts in backticks, each once, in order of appearance.

    A backticked span counts only if it is a plain identifier (letters, digits, underscore, not
    starting with a digit); file names, expressions and other quoted text are ignored.
    """
    found: list[str] = []
    for match in _BACKTICKED.finditer(task_text):
        text = match.group(1)
        if _IDENTIFIER.fullmatch(text) and text not in found:
            found.append(text)
    return found


def _num(value: float) -> str:
    return f"{value:.6g}"


def _positive(value: float) -> bool:
    return math.isfinite(value) and value > 0


def plan_gate(spec: CaseSpec, task_text: str, limits: RunLimits) -> GateResult:
    """The deterministic checks on a ``CaseSpec`` (design 6.2, stage 1).

    The messages are read by the model on a retry, so each says what is wrong and what to do.
    A resource excess is reported as one message that starts with ``exceeds resources:``.
    """
    errors: list[str] = []

    names = [output.name for output in spec.requested_outputs]
    for identifier in task_identifiers(task_text):
        if identifier not in names:
            have = ", ".join(f"`{n}`" for n in names) or "none"
            errors.append(
                f"the task names `{identifier}` in backticks, but no entry of requested_outputs "
                f"has the name `{identifier}` exactly; add an output with exactly that name "
                f"(output names now: {have})"
            )

    ref = spec.reference
    nu = spec.fluid.kinematic_viscosity_m2_s
    if ref.reynolds_number is not None:
        if not (_positive(nu) and _positive(ref.length_m) and _positive(ref.velocity_m_s)):
            errors.append(
                "the Reynolds number cannot be checked: fluid.kinematic_viscosity_m2_s "
                f"({_num(nu)}), reference.length_m ({_num(ref.length_m)}) and "
                f"reference.velocity_m_s ({_num(ref.velocity_m_s)}) must all be positive numbers"
            )
        else:
            expected = ref.velocity_m_s * ref.length_m / nu
            if abs(ref.reynolds_number - expected) > REYNOLDS_TOLERANCE * abs(expected):
                errors.append(
                    f"reference.reynolds_number is {_num(ref.reynolds_number)} but "
                    "reference.velocity_m_s * reference.length_m / "
                    f"fluid.kinematic_viscosity_m2_s = {_num(expected)}; they must agree within "
                    "0.1 %. Recompute the value the task does not fix with the calculator, keep "
                    "every value the task states, and record the choice in assumptions"
                )

    box = spec.geometry.bbox
    for axis, low, high in (
        ("x", box.xmin, box.xmax),
        ("y", box.ymin, box.ymax),
        ("z", box.zmin, box.zmax),
    ):
        if not low < high:
            errors.append(
                f"geometry.bbox: {axis}max ({_num(high)}) must be greater than {axis}min "
                f"({_num(low)}); the fluid domain needs a positive extent along every axis (a 2D "
                "case still has a bounding box with the out-of-plane thickness as its extent)"
            )

    end = spec.solver.end_time_s
    if not end > 0:
        errors.append(
            f"solver.end_time_s is {_num(end)} but must be greater than 0; give the end time in "
            "seconds (the iteration count for a steady case)"
        )

    thickness = spec.geometry.thickness_m
    if spec.dimensionality == "2D" and (thickness is None or not thickness > 0):
        errors.append(
            'dimensionality is "2D" but geometry.thickness_m is missing or not positive; give '
            "the out-of-plane thickness in metres (the mesh is one cell thick over it)"
        )

    estimate = spec.resource_estimate
    excess = []
    if estimate.cells > limits.max_cells:
        excess.append(f"{estimate.cells} cells (limit {limits.max_cells})")
    if estimate.runtime_minutes > limits.max_runtime_minutes:
        excess.append(
            f"a runtime of {_num(estimate.runtime_minutes)} minutes "
            f"(limit {_num(limits.max_runtime_minutes)})"
        )
    if estimate.memory_gb > limits.max_memory_gb:
        excess.append(
            f"{_num(estimate.memory_gb)} GB of memory (limit {_num(limits.max_memory_gb)})"
        )
    if excess:
        errors.append(
            f"{RESOURCE_PREFIX} the plan needs " + ", ".join(excess) + ". Look for a legitimate "
            "reduction first (symmetry, a 2D or axisymmetric model, a justified coarser mesh) and "
            "record it in assumptions; do not lower the estimate without changing the case"
        )
    return GateResult(not errors, errors)


def _only_resource_errors(errors: list[str]) -> bool:
    return bool(errors) and all(e.startswith(RESOURCE_PREFIX) for e in errors)


def _opening_message(task_text: str) -> str:
    return (
        f"Task from the user:\n\n{task_text}\n\n"
        "Plan the simulation. Restate the task, decide every parameter the task leaves open "
        "(record each as an assumption) and compute every derived number with the calculator "
        "tool. When you have all the numbers you need, reply in plain text with a short summary "
        "of the plan and of every assumption. Do not write the final CaseSpec yet."
    )


def _structured_request(task_text: str) -> str:
    text = (
        "Now write the CaseSpec: one JSON object that matches the required schema, with every "
        "field present (use null where a value does not apply). Use the numbers worked out above "
        "and list every assumption in `assumptions`."
    )
    names = task_identifiers(task_text)
    if names:
        text += (
            " The task names these outputs in backticks; each must be the exact name of an entry "
            "in requested_outputs: " + ", ".join(names) + "."
        )
    return text


def _schema_problems(exc: LLMError) -> str:
    cause = exc.__cause__
    if not isinstance(cause, ValidationError):
        return short(exc, 300)
    errors = cause.errors(include_url=False, include_context=False, include_input=False)
    shown = []
    for error in errors[:MAX_SCHEMA_PROBLEMS]:
        where = ".".join(str(part) for part in error["loc"]) or "(whole reply)"
        shown.append(f"{where}: {error['msg']}")
    if len(errors) > len(shown):
        shown.append(f"and {len(errors) - len(shown)} more")
    return "; ".join(shown)


class _PlannerRun(ModelConversation):
    def __init__(self, ctx: StageContext, task_text: str) -> None:
        super().__init__(ctx, STAGE, AGENT)
        self.task_text = task_text
        self.structured = False  # True in phase 2
        self.attempt_started = ctx.clock()

    # ------------------------------------------------------------------ texts for phase 2

    def _discarded_text(self, result: ChatResult) -> str:
        if not self.structured:
            return super()._discarded_text(result)
        if result.finish_reason == "length":
            return "Your last reply reached the output limit and was discarded. " + SHORTER_ADVICE
        return (
            "Your last reply was withheld by the content filter and was discarded. Reply again "
            "with the complete CaseSpec as one JSON object."
        )

    def _unusable_text(self, exc: LLMError) -> str:
        if not self.structured:
            return super()._unusable_text(exc)
        if isinstance(exc, TruncatedOutputError):
            return "Your last reply was cut off at the output limit. " + SHORTER_ADVICE
        if isinstance(exc, StructuredOutputError):
            return (
                "Your last reply does not match the CaseSpec schema: "
                f"{_schema_problems(exc)}. Reply again with the complete CaseSpec as one JSON "
                "object that matches the schema exactly: every field present, null where a "
                "value does not apply, no extra fields."
            )
        return (
            f"Your last reply could not be used ({type(exc).__name__}: {short(exc, 300)}). "
            "Reply again with the complete CaseSpec as one JSON object."
        )

    # ------------------------------------------------------------------ phase 1

    def _answer(self, call: ToolCall) -> str:
        """The tool result for one call; a call that cannot run is still answered."""
        if self.abort is not None:
            return "not run: the stage has stopped"
        if self._time_left() <= 0:
            self.abort = self._wall_clock_reason()
            return "not run: wall-clock limit reached"
        return self.registry.dispatch(call)

    def _phase_one(self) -> None:
        """Tool loop until the model answers in plain text or runs out of turns."""
        self.registry.add(calculator_tool())
        while self.turns < PHASE_ONE_MAX_TURNS:
            result = self._ask()
            if self.abort is not None:
                return
            if result is None:
                continue
            if not result.tool_calls:
                return
            for call in result.tool_calls:
                output = self._answer(call)
                self.messages.append({"role": "tool", "tool_call_id": call.id, "content": output})
            if self.abort is not None:
                return

    # ------------------------------------------------------------------ phase 2

    def _write(self, spec: CaseSpec) -> None:
        with internal("writing casespec.json"):
            self.ctx.run.write_text(CASESPEC_FILE, spec.model_dump_json(indent=2) + "\n")

    def _record_attempt(self, errors: list[str]) -> None:
        now = self.ctx.clock()
        self.attempts += 1
        self.record.attempts.append(
            AttemptRecord(
                n=self.attempts,
                ok=not errors,
                errors=errors,
                seconds=max(0.0, now - self.attempt_started),
            )
        )
        self.attempt_started = now
        self.last_errors = errors

    def _phase_two(self) -> StageOutcome:
        self.structured = True
        self.registry = ToolRegistry()  # no tools in the structured call
        self.model = self.model.model_copy(
            update={"max_tokens": self.ctx.config.planner_structured_max_tokens}
        )
        self.messages.append({"role": "user", "content": _structured_request(self.task_text)})
        limit = self.limits.max_attempts_per_stage
        while True:
            result = self._ask(response_schema=CaseSpec, with_tools=False)
            if self.abort is not None:
                return self._fail(self.abort)
            spec: CaseSpec | None = None
            if result is None:  # unusable reply: the model has been told why
                errors = [short(self.last_problem or "the reply could not be used", 1000)]
            else:
                if not isinstance(result.parsed, CaseSpec):
                    raise TypeError("the backend returned no parsed CaseSpec for a schema call")
                spec = result.parsed
                gate = plan_gate(spec, self.task_text, self.limits)
                errors = [str(e) for e in gate.errors]
            self._record_attempt(errors)
            if spec is not None and not errors:
                self._write(spec)
                self.ctx.casespec = spec
                self.record.ok = True
                self._progress()
                return StageOutcome(ok=True, attempts=self.attempts)
            if spec is not None and _only_resource_errors(errors):
                self._write(spec)  # the recorded estimate; no retry (design 6.8)
                self._progress()
                return self._fail("; ".join(errors))
            if self.attempts >= limit:
                self._progress()
                shown = short("; ".join(errors), MAX_REASON_CHARS - 200)
                return self._fail(
                    f"stage {STAGE!r} failed after {self.attempts} attempts; last errors: {shown}"
                )
            if spec is not None:
                listing = "\n".join(f"- {e}" for e in errors)
                self.messages.append(
                    {
                        "role": "user",
                        "content": short(
                            f"The automatic checks rejected the plan (attempt {self.attempts} of "
                            f"{limit}):\n{listing}\nReply again with the complete corrected "
                            "CaseSpec as one JSON object.",
                            MAX_FEEDBACK_CHARS,
                        ),
                    }
                )
            self._progress()

    def run(self) -> StageOutcome:
        self.messages = [
            {"role": "system", "content": load_prompt(AGENT).text},
            {"role": "user", "content": _opening_message(self.task_text)},
        ]
        self._phase_one()
        if self.abort is not None:
            return self._fail(self.abort)
        return self._phase_two()


def run_planner(ctx: StageContext, task_text: str) -> StageOutcome:
    """Plan the task: phase 1 with the calculator, phase 2 with the ``CaseSpec`` schema.

    On success ``casespec.json`` is written to the run directory and ``ctx.casespec`` is set.
    A plan whose only problem is the resource estimate ends the stage as failed with the
    ``exceeds resources:`` message as reason, without a retry; its ``casespec.json`` is written
    but ``ctx.casespec`` stays unset. See the module docstring for the rest.
    """
    return _PlannerRun(ctx, task_text).run()
