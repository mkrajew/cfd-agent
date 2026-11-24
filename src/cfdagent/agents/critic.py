"""The Critic, the last step of the pipeline (design 6.2 row 7 and 6.3, ADR-0007).

The Critic is a fresh-context reviewer with read-only tools. It looks at the finished run the way a
person reading the result would: did it compute what was asked, with the parameters that were
asked for, is it converged, is it plausible. It works in two phases on one conversation, like the
Planner:

1. A tool loop with ``read_file``, ``list_dir`` and ``calculator`` until the model answers in plain
   text (at most ``CRITIC_MAX_TURNS`` model turns). Reaching the limit is not a failure.
2. One call with ``response_schema=CriticVerdict``. A reply that does not validate, or that the
   guard (``verdict_problems``) refuses, is reported to the model and the call is repeated; each
   such call is an attempt, ``limits.max_attempts_per_stage`` of them at most.

What it reads (``CRITIC_SCOPE``): the canonical files of the finished pass only, namely the task,
the plan, the boundary manifest, the Mesh stage's reports, the solver summary and log, the Analyst's
analysis and the computed quantities. It never reads ``results/comparison.json``, ``superseded/``,
``benchmarks/``, the transcript and record of the run, the case itself, or the ``attempts/``
directories, which hold the work of every stage and pass (blind evaluation, design 6.3: a Critic
that saw an earlier pass or the Scorer's output would not be independent). No tool can write.
The end of the solver log goes into the opening message, because ``read_file`` pages from the start.

What a verdict does (``run_critic``). A final verdict is stored in ``ctx.record.critic_verdict``.
A send-back (``send_back_to`` set) is followed only while ``record.critic_rounds`` is below
``limits.max_critic_rounds`` and the run-wide budget of fix loops (``limits.max_fix_loops``) is not
used up: the outcome is then a failed ``StageOutcome`` that carries a ``RouteRequest`` with
``origin="critic"``, ``critic_rounds`` goes up by one and no verdict is stored (the send-back is
the fix loop the orchestrator records; the verdict of the run is the one of the final pass).
Otherwise the send-back is part of a final verdict, stored as it is, and the step passes: a Critic
that wants more than the run may give does not fail the run.

When the Critic gives no usable verdict (``limits.max_attempts_per_stage`` invalid replies, or the
cost or wall-clock limit inside the Critic) the step still passes, ``critic_verdict`` is ``None``
and the run goes on to the Scorer; the stage record says ``ok = False`` and the outcome's ``reason``
says why.

This module is host-side: it imports no tool, CAD, mesh or knowledge code.
"""

from __future__ import annotations

import os
import re
import stat

from pydantic import ValidationError

from cfdagent.agents.loop import (
    MAX_REASON_CHARS,
    ModelConversation,
    StageOutcome,
    internal,
    short,
)
from cfdagent.agents.routing import RouteRequest
from cfdagent.agents.stage import StageContext
from cfdagent.agents.tools import FileScope, ToolRegistry, calculator_tool, file_tools
from cfdagent.llm.client import (
    ChatResult,
    LLMError,
    StructuredOutputError,
    ToolCall,
    TruncatedOutputError,
)
from cfdagent.prompts import load_prompt
from cfdagent.record import AttemptRecord, CriticVerdict, redact
from cfdagent.rundir import RunDir, RunDirError

STAGE = "critic"
AGENT = "critic"
CRITIC_MAX_TURNS = 12  # read-only model turns, then the verdict (ADR-0007)
# The checks every review makes (ADR-0007, decision 3), by the names the prompt gives them. A
# success must list all of them: a success that shows no evidence is not a judgement.
REQUIRED_CHECKS = ("requested_outputs", "task_parameters", "convergence", "plausibility")
SOLVER_SUMMARY = "logs/solver_run.json"
LOG_TAIL_BYTES = 3000  # the end of the solver log that the opening message carries
MIN_SUMMARY_CHARS = 3  # a reply shorter than this (empty, ".", "ok") is no summary of the findings
SUMMARY_AGAIN = (
    "Your last reply was empty. Reply in plain text with a short summary of your findings (what "
    "you read and what you found for each of the required checks); the verdict is asked for next."
)
MAX_SCHEMA_PROBLEMS = 8
MAX_FEEDBACK_CHARS = 4000
MAX_PROBLEM_CHARS = 1000
MAX_ROUTE_REASON_CHARS = 300  # of the sentence that names a followed send-back

# The files of a finished run that the Critic may read: the canonical outputs of the last pass and
# nothing else. The names of the Mesh stage's reports are those the Case Setup stage reads too.
CRITIC_SCOPE = FileScope(
    readable=(
        "task.md",
        "casespec.json",
        "boundary_manifest.json",
        "mesh/mesh.log",
        "mesh/notes.md",
        "mesh/case/checkMesh.log",
        SOLVER_SUMMARY,
        "logs/log.*",
        "analysis.md",
        "results/quantities.json",
    )
)

_SOLVER_NAME = re.compile(r"[A-Za-z0-9_]{1,40}")

SHORTER_ADVICE = (
    "Reply again with the complete verdict as one JSON object; keep every text field to one or "
    "two sentences and do not repeat information."
)


def verdict_problems(verdict: CriticVerdict) -> list[str]:
    """What makes a parsed verdict invalid (the guard of ADR-0007, decision 5); the messages are
    read by the model, so each says what is wrong and what to do. Empty for a usable verdict."""
    problems = []
    failed = [check.name for check in verdict.checks if not check.passed]
    if verdict.verdict == "success" and failed:
        problems.append(
            f"the verdict is success but these checks did not pass: {', '.join(failed)}. A "
            "success needs every check to pass: give the verdict partial or failed, or, if "
            "something in the files you read shows that a check did pass, say what it is in "
            "that check's evidence and set passed to true"
        )
    if verdict.verdict == "success":
        listed = {check.name for check in verdict.checks}
        missing = [name for name in REQUIRED_CHECKS if name not in listed]
        if missing:
            problems.append(
                f"the verdict is success but it does not list these required checks: "
                f"{', '.join(missing)}. List all of {', '.join(REQUIRED_CHECKS)}, each with its "
                "evidence (a check that does not apply to this case passes with the evidence "
                "'not applicable: <why>')"
            )
    elif not failed and not verdict.reasons and not verdict.checks:
        problems.append(
            f"the verdict is {verdict.verdict} but `checks` and `reasons` are both empty, so it "
            f"says nothing. List all of {', '.join(REQUIRED_CHECKS)} in `checks`, each with its "
            "name, whether it passed and its evidence, with passed false for the check that "
            "found the fault; and give at least one sentence in `reasons` that says what is "
            "wrong"
        )
    elif not failed and not verdict.reasons:
        problems.append(
            f"the verdict is {verdict.verdict} but no check failed and no reason is given; "
            "name what is wrong in `reasons` or in a failed check, or give the verdict success "
            "if nothing is"
        )
    if verdict.send_back_to is not None:
        if verdict.verdict == "success":
            problems.append(
                "a success cannot send the run back: set send_back_to and instruction to null, "
                "or change the verdict"
            )
        if not (verdict.instruction or "").strip():
            problems.append(
                f"send_back_to is {verdict.send_back_to!r} but instruction is empty; say "
                "concretely what that stage must change (it sees only the instruction and the "
                "reasons), or set send_back_to to null"
            )
    return problems


def _solver_log_tail(run: RunDir) -> tuple[str, str] | None:
    """``(path, text)``: the log of the solver named in ``logs/solver_run.json`` and the last
    ``LOG_TAIL_BYTES`` bytes of it, from its first whole line; ``None`` when there is no such log.
    Reads only through the run directory's safe access."""
    try:
        summary = run.read_json(SOLVER_SUMMARY)
    except (RunDirError, OSError, ValueError):
        return None
    solver = summary.get("solver") if isinstance(summary, dict) else None
    if not isinstance(solver, str) or not _SOLVER_NAME.fullmatch(solver):
        return None
    rel = f"logs/log.{solver}"
    try:
        path = run.path(rel)
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - LOG_TAIL_BYTES))
            data = handle.read(LOG_TAIL_BYTES)
    except (RunDirError, OSError):
        return None
    text = data.decode("utf-8", "replace")
    if size > LOG_TAIL_BYTES and "\n" in text:
        text = text.split("\n", 1)[1]  # the first line is cut in the middle
    text = redact(text.strip(), LOG_TAIL_BYTES)
    return (rel, text) if text else None


def _opening_message(task_text: str, tail: tuple[str, str] | None) -> str:
    text = (
        f"Task from the user:\n\n{task_text}\n\n"
        "The run has finished and every stage has passed its automatic checks. Review it as your "
        "instructions describe: make the four required checks "
        f"({', '.join(REQUIRED_CHECKS)}) against the task text above, using the "
        "files you may read (start with list_dir on '.'). When you have what you need, reply in "
        "plain text with a short summary of your findings; do not write the verdict yet."
    )
    if tail is not None:
        path, log = tail
        text += f"\n\nEnd of the solver log ({path}):\n{log}"
    return text


def _structured_request() -> str:
    return (
        "Now give the verdict: one JSON object that matches the required schema, with every "
        "field present (null where a value does not apply). List every required check in "
        "`checks`, each with its name, whether it passed and the evidence."
    )


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


class _CriticRun(ModelConversation):
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
            "with the complete verdict as one JSON object."
        )

    def _unusable_text(self, exc: LLMError) -> str:
        if not self.structured:
            return super()._unusable_text(exc)
        if isinstance(exc, TruncatedOutputError):
            return "Your last reply was cut off at the output limit. " + SHORTER_ADVICE
        if isinstance(exc, StructuredOutputError):
            return (
                "Your last reply does not match the CriticVerdict schema: "
                f"{_schema_problems(exc)}. Reply again with the complete verdict as one JSON "
                "object that matches the schema exactly: every field present, null where a "
                "value does not apply, no extra fields."
            )
        return (
            f"Your last reply could not be used ({type(exc).__name__}: {short(exc, 300)}). "
            "Reply again with the complete verdict as one JSON object."
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
        for tool in file_tools(self.ctx.run, CRITIC_SCOPE):
            if tool.name != "write_file":  # the Critic changes nothing
                self.registry.add(tool)
        summary_asked = False
        while self.turns < CRITIC_MAX_TURNS:
            result = self._ask()
            if self.abort is not None:
                return
            if result is None:
                continue
            if not result.tool_calls:
                if len((result.content or "").strip()) < MIN_SUMMARY_CHARS and not summary_asked:
                    summary_asked = True  # a blank summary is asked for once more, not twice
                    self.messages.append({"role": "user", "content": SUMMARY_AGAIN})
                    continue
                return
            for call in result.tool_calls:
                output = self._answer(call)
                self.messages.append({"role": "tool", "tool_call_id": call.id, "content": output})
            if self.abort is not None:
                return

    # ------------------------------------------------------------------ phase 2

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

    def _gave_up(self, reason: str) -> StageOutcome:
        """The Critic gave no usable verdict: the run goes on without one."""
        self.ctx.record.critic_verdict = None
        self.record.ok = False
        self._progress()
        return StageOutcome(
            ok=True,
            attempts=self.attempts,
            errors=list(self.last_errors),
            reason=redact(reason, MAX_REASON_CHARS),
        )

    def _may_follow(self, verdict: CriticVerdict) -> bool:
        record, limits = self.ctx.record, self.limits
        return (
            verdict.send_back_to is not None
            and record.critic_rounds < limits.max_critic_rounds
            and len(record.fix_loops) < limits.max_fix_loops
        )

    def _accept(self, verdict: CriticVerdict) -> StageOutcome:
        """A valid verdict: a send-back that may be followed becomes a route, anything else is the
        final verdict of the run."""
        record = self.ctx.record
        if verdict.send_back_to is not None and self._may_follow(verdict):
            reason = " ".join(verdict.reasons) or "the Critic asked for a fix"
            with internal("building the route request"):
                route = RouteRequest(
                    source=STAGE,
                    target=verdict.send_back_to,
                    origin="critic",
                    reason=reason,
                    instruction=verdict.instruction,
                )
            record.critic_rounds += 1
            record.critic_verdict = None  # only the verdict of the final pass is the run's
            self._progress()
            return self._fail(
                f"the Critic sent the run back to {route.target}: "
                f"{short(route.reason, MAX_ROUTE_REASON_CHARS)}",
                route,
            )
        record.critic_verdict = verdict
        self.record.ok = True
        self._progress()
        return StageOutcome(ok=True, attempts=self.attempts)

    def _phase_two(self) -> StageOutcome:
        self.structured = True
        self.registry = ToolRegistry()  # no tools in the structured call
        self.messages.append({"role": "user", "content": _structured_request()})
        limit = self.limits.max_attempts_per_stage
        while True:
            result = self._ask(response_schema=CriticVerdict, with_tools=False)
            if self.abort is not None:
                return self._gave_up(self.abort)
            verdict: CriticVerdict | None = None
            if result is None:  # an unusable reply: the model has been told why
                errors = [short(self.last_problem or "the reply could not be used", 1000)]
            else:
                if not isinstance(result.parsed, CriticVerdict):
                    raise TypeError(
                        "the backend returned no parsed CriticVerdict for a schema call"
                    )
                errors = verdict_problems(result.parsed)
                verdict = result.parsed if not errors else None
            self._record_attempt(errors)
            if verdict is not None:
                return self._accept(verdict)
            if self.attempts >= limit:
                self._progress()
                shown = short("; ".join(errors), MAX_PROBLEM_CHARS)
                return self._gave_up(
                    f"the Critic gave no usable verdict after {self.attempts} invalid replies; "
                    f"the run goes on without one (last problems: {shown})"
                )
            if result is not None:
                listing = "\n".join(f"- {e}" for e in errors)
                self.messages.append(
                    {
                        "role": "user",
                        "content": short(
                            f"The verdict was rejected (attempt {self.attempts} of {limit}):\n"
                            f"{listing}\nReply again with the complete corrected verdict as one "
                            "JSON object.",
                            MAX_FEEDBACK_CHARS,
                        ),
                    }
                )
            self._progress()

    def run(self) -> StageOutcome:
        self.messages = [
            {"role": "system", "content": load_prompt(AGENT).text},
            {
                "role": "user",
                "content": _opening_message(self.task_text, _solver_log_tail(self.ctx.run)),
            },
        ]
        self._phase_one()
        if self.abort is not None:
            return self._gave_up(self.abort)
        return self._phase_two()


def run_critic(ctx: StageContext, task_text: str) -> StageOutcome:
    """Review the finished run: phase 1 with the read-only tools, phase 2 with the verdict schema.

    Returns a passed outcome with ``ctx.record.critic_verdict`` set (a final verdict, a send-back
    that may not be followed included), a failed outcome whose ``route`` carries the send-back
    (``origin="critic"``) when it is followed, or a passed outcome with no verdict when the Critic
    gave none; see the module docstring.
    """
    return _CriticRun(ctx, task_text).run()
