"""The whole pipeline through ``run_task`` inside the image: a scripted model plays all seven agents
(Planner, Geometry, Mesh, Case Setup, Solver Runner, Analyst, Critic), the local-mode executor runs
the real tools and the real solver, and the real Scorer judges the result. The upload is faked.
Local mode imports the ``cfdagent`` under test (the checked-out sources), see
``SandboxClient._command``.

The conversation is ``tests/fixtures/scripted/cavity_pipeline.json``: the Planner's three replies,
the recorded conversations of the five stages and the Critic's three replies (``critic_ok.json``).
About two minutes per run.
"""

import copy
import json
import re
from pathlib import Path

import pytest

from cfdagent.agents.loop import StageOutcome
from cfdagent.agents.routing import RouteRequest
from cfdagent.agents.solver_runner import SolverRunnerStage
from cfdagent.config import RunConfig
from cfdagent.llm.scripted import ScriptedBackend
from cfdagent.orchestrator import GatedStep, default_pipeline, run_task
from cfdagent.record import read_record
from cfdagent.rundir import RunDir
from cfdagent.sandbox.config import SandboxConfig
from cfdagent.storage.blob import UploadReport

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "scripted" / "cavity_pipeline.json"
BENCHMARK = "cavity-re100"
PLANNER_STEPS = 3
GEOMETRY_STEPS = 2
STAGE_STEPS = 2  # every stage after the Planner, up to the Analyst, is two scripted replies
CRITIC_STEPS = 3  # two tool turns and the verdict
STAGES = ["planner", "geometry", "mesh", "case_setup", "solver_runner", "analyst", "critic"]


class FakeUploader:
    def __init__(self):
        self.files = None

    def __call__(self, run):
        self.files = sorted(run.relative(p) for p in run.iter_files())
        return UploadReport(uploaded=len(self.files))


def fixture_steps() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def backend_from(steps: list[dict], tmp_path: Path) -> ScriptedBackend:
    path = tmp_path / "script.json"
    path.write_text(json.dumps(steps), encoding="utf-8")
    return ScriptedBackend.from_json(path)


def benchmark_config(**changes) -> RunConfig:
    sandbox = SandboxConfig(mode="local", allow_local=True, image="local-unused")
    update = {"sandbox": sandbox, "benchmark_id": BENCHMARK, "task_set": "development", **changes}
    return RunConfig.default().model_copy(update=update)


def run_scripted(tmp_path: Path, steps: list[dict], *, pipeline=None, **config_changes):
    uploader = FakeUploader()
    backend = backend_from(steps, tmp_path)
    result = run_task(
        "ignored: a benchmark run uses the task file of the benchmark",
        benchmark_config(**config_changes),
        backend=backend,
        run_root=tmp_path / "runs",
        upload=True,
        uploader=uploader,
        state_dir=tmp_path / "state",
        benchmarks_root=ROOT / "benchmarks",
        pipeline=pipeline,
    )
    return result, backend, uploader


@pytest.fixture(scope="module")
def clean(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("clean")
    return run_scripted(tmp_path, fixture_steps())


def reference_values() -> list[str]:
    """The value column of the reference files, without the trivial wall values."""
    values = []
    for csv_file in sorted((ROOT / "benchmarks" / BENCHMARK / "reference").glob("*.csv")):
        for line in csv_file.read_text(encoding="utf-8").splitlines()[1:]:
            values.append(line.split(",")[1].strip())
    return [v for v in values if v not in {"0.00000", "1.00000", "0", "1"}]


def leaks(text: str) -> list[str]:
    found = []
    if re.search(r"ghia", text, re.IGNORECASE):
        found.append("ghia")
    if BENCHMARK in text:
        found.append(BENCHMARK)
    for value in reference_values():
        if re.search(rf"(?<![0-9.]){re.escape(value)}(?![0-9])", text):
            found.append(value)
    return found


def test_scripted_cavity_pipeline_passes_scorer(clean):
    result, backend, uploader = clean
    assert result.status == "completed", result.record.failure_reason
    assert result.record.failed_stage is None
    assert [s.name for s in result.record.stages] == STAGES
    assert all(s.ok and [a.ok for a in s.attempts] == [True] for s in result.record.stages)
    assert len(backend.calls) == len(fixture_steps())  # every scripted reply was used
    assert result.record.critic_verdict.verdict == "success"

    comparison = result.comparison
    report = "\n".join(
        f"  {c['quantity']}: passed={c['passed']} error={c['error']} ({c['reason']})"
        for c in comparison.get("criteria", [])
    )
    assert comparison.get("passed") is True, f"benchmark criteria failed:\n{report}\n{comparison}"
    assert result.record.comparison == comparison

    run = result.run_dir
    on_disk = json.loads((run / "results" / "comparison.json").read_text(encoding="utf-8"))
    assert on_disk == comparison
    assert (run / "casespec.json").is_file() and (run / "analysis.md").is_file()
    assert (run / "logs" / "solver_run.json").is_file()
    final = read_record(RunDir(run))
    assert final.status == "completed" and final.benchmark_id == BENCHMARK
    assert final.tokens_in > 0 and final.cost_usd > 0
    assert (
        final.knowledge_reads == []
    )  # the scripted Case Setup agent never consulted the tutorials

    summary = (run / "summary.md").read_text(encoding="utf-8")
    assert "Compared with the reference data" in summary
    assert "Residual control: not applicable" in summary  # a transient solver was used
    assert "summary.md" in uploader.files and "results/comparison.json" in uploader.files
    assert not (tmp_state(result) / "active-run").exists()


def tmp_state(result) -> Path:
    return result.run_dir.parents[1] / "state"


def test_scripted_cavity_pipeline_with_critic_success(clean):
    result, backend, _ = clean
    assert result.status == "completed", result.record.failure_reason
    verdict = result.record.critic_verdict
    assert verdict is not None and verdict.verdict == "success"
    assert [c.name for c in verdict.checks] == [
        "requested_outputs",
        "task_parameters",
        "convergence",
        "plausibility",
    ]
    assert all(c.passed for c in verdict.checks)
    assert verdict.send_back_to is None and verdict.instruction is None
    assert result.record.critic_rounds == 0 and result.record.fix_loops == []
    assert [s.name for s in result.record.stages][-2:] == ["analyst", "critic"]
    assert read_record(RunDir(result.run_dir)).critic_verdict == verdict
    assert result.comparison["passed"] is True  # the Scorer ran after the Critic
    assert result.record.critic_agreement is True  # success and a passed Scorer agree
    summary = (result.run_dir / "summary.md").read_text(encoding="utf-8")
    assert "Verdict: **success**" in summary and "Critic agreed with the Scorer: **yes**" in summary

    # the Critic read the real files of this run through its read-only tools
    critic_calls = backend.calls[-CRITIC_STEPS:]
    first = critic_calls[0]
    assert {t["function"]["name"] for t in first.tools} == {"calculator", "read_file", "list_dir"}
    assert len(first.messages) == 2  # a fresh conversation: the prompt and the opening message
    assert "End of the solver log (logs/log.icoFoam)" in first.messages[1]["content"]
    answers = [m["content"] for m in critic_calls[1].messages if m["role"] == "tool"]
    assert len(answers) == 5 and not any(a.startswith("error:") for a in answers), answers
    assert "analysis.md" in answers[0] and "logs/" in answers[0]
    assert "results/" in answers[0] and "attempts" not in answers[0]
    assert '"value"' in answers[4] and answers[3].strip()  # quantities.json and analysis.md
    assert critic_calls[2].response_schema.__name__ == "CriticVerdict"

    lines = (result.run_dir / "llm_transcript.jsonl").read_text(encoding="utf-8").splitlines()
    critic_lines = [json.loads(line) for line in lines if '"stage": "critic"' in line]
    assert critic_lines and {entry["pass_no"] for entry in critic_lines} == {0}
    assert all(leaks(json.dumps(entry)) == [] for entry in critic_lines)
    log = (result.run_dir / "run.log").read_text(encoding="utf-8")
    assert log.index("stage analyst: ok") < log.index("stage critic: start")
    assert log.index("stage critic: ok") < log.index("scorer: start")


def test_scripted_run_hides_the_benchmark_from_every_file_an_agent_can_read(clean):
    result, _, _ = clean
    values = reference_values()
    assert values and leaks("x 0.09233 y") == ["0.09233"]  # the check can fail
    for name in ("llm_transcript.jsonl", "task.md", "status.json", "run.log"):
        text = (result.run_dir / name).read_text(encoding="utf-8")
        assert leaks(text) == [], name
    lines = (result.run_dir / "llm_transcript.jsonl").read_text(encoding="utf-8").splitlines()
    assert {json.loads(line)["stage"] for line in lines} == set(STAGES)
    log = (result.run_dir / "run.log").read_text(encoding="utf-8")
    for stage in STAGES:
        assert f"stage {stage}: start" in log and f"stage {stage}: ok" in log


def with_one_bad_geometry_attempt(steps: list[dict]) -> list[dict]:
    """Insert a geometry attempt with a wrong domain size before the good one."""
    planner = steps[:PLANNER_STEPS]
    good = copy.deepcopy(steps[PLANNER_STEPS : PLANNER_STEPS + GEOMETRY_STEPS])
    rest = steps[PLANNER_STEPS + GEOMETRY_STEPS :]
    bad = copy.deepcopy(good)
    script = bad[0]["tool_calls"][0]["arguments"]
    assert "box(1.0, 1.0, THICKNESS" in script["content"]
    script["content"] = script["content"].replace(
        "box(1.0, 1.0, THICKNESS", "box(2.0, 1.0, THICKNESS"
    )
    second = good[0]["tool_calls"][0]["arguments"]
    second["path"] = second["path"].replace("geometry-1", "geometry-2")
    assert second["path"] == "attempts/geometry-2/geometry.py"
    return planner + bad + good + rest


def test_scripted_pipeline_survives_one_bad_geometry_attempt(tmp_path):
    steps = with_one_bad_geometry_attempt(fixture_steps())
    result, backend, _ = run_scripted(tmp_path, steps)
    assert result.status == "completed", result.record.failure_reason
    geometry = result.record.stages[1]
    assert geometry.name == "geometry"
    assert [(a.n, a.ok) for a in geometry.attempts] == [(1, False), (2, True)]
    assert "bounding box" in " ".join(geometry.attempts[0].errors).lower()
    assert [s.name for s in result.record.stages] == STAGES
    assert result.comparison["passed"] is True
    assert len(backend.calls) == len(steps)


# ---------------------------------------------------------------------- a fix loop (ADR-0006)


class RoutingOnceStep(GatedStep):
    """A fake gate in the place of the Solver Runner: the first time it asks for a fix in the Case
    Setup without running anything (no model call); from then on it is the real stage."""

    def __init__(self, stage, state):
        super().__init__(stage)
        self.state = state

    def run(self, ctx, task_text):
        if self.state["routed"]:
            return super().run(ctx, task_text)
        self.state["routed"] = True
        request = RouteRequest(
            source="solver_runner",
            target="case_setup",
            origin="gate",
            reason="the run did not reach a steady state",
            instruction="use a smaller time step",
        )
        return StageOutcome(
            ok=False, attempts=0, reason="the fake gate asked for a fix", route=request
        )


def routing_once_pipeline():
    state = {"routed": False}
    pipeline = default_pipeline()
    pipeline[4] = lambda: RoutingOnceStep(SolverRunnerStage(), state)
    return pipeline


def in_pass_one(steps: list[dict], stage: str) -> list[dict]:
    """The recorded conversation of a stage, writing into the attempt directories of pass 1."""
    text = json.dumps(steps).replace(f"attempts/{stage}-1", f"attempts/{stage}-p1-1")
    return json.loads(text)


def with_a_changed_file(steps: list[dict]) -> list[dict]:
    """The same conversation, with a comment line added to the first file it writes: a fix pass
    that changes no file is rejected (the work would fail the same way)."""
    steps = json.loads(json.dumps(steps))
    for step in steps:
        for call in step.get("tool_calls") or []:
            if call["name"] == "write_file":
                call["arguments"]["content"] += "\n// changed in the fix pass\n"
                return steps
    raise AssertionError("the conversation writes no file")


def steps_with_one_fix_loop(steps: list[dict]) -> list[dict]:
    """Planner, Geometry, Mesh and Case Setup, then (after the fake gate's route back) the Case
    Setup again, the Solver Runner and the Analyst, all in pass 1."""
    stages = [
        steps[PLANNER_STEPS + i * STAGE_STEPS : PLANNER_STEPS + (i + 1) * STAGE_STEPS]
        for i in range(5)
    ]
    geometry, mesh, case_setup, solver, analyst = stages
    critic = steps[PLANNER_STEPS + 5 * STAGE_STEPS :]
    assert len(critic) == CRITIC_STEPS
    return (
        steps[:PLANNER_STEPS]
        + geometry
        + mesh
        + case_setup
        + with_a_changed_file(in_pass_one(case_setup, "case_setup"))
        + solver
        + in_pass_one(analyst, "analyst")
        + critic
    )


@pytest.mark.parametrize("repair_mode", ["fresh", "in_place"])
def test_scripted_pipeline_recovers_through_route_to_case_setup(tmp_path, repair_mode):
    steps = steps_with_one_fix_loop(fixture_steps())
    result, backend, _ = run_scripted(
        tmp_path, steps, pipeline=routing_once_pipeline(), repair_mode=repair_mode
    )
    assert result.status == "completed", result.record.failure_reason
    assert len(backend.calls) == len(steps)  # every scripted reply was used
    assert [(s.name, s.pass_no, s.ok) for s in result.record.stages] == [
        ("planner", 0, True),
        ("geometry", 0, True),
        ("mesh", 0, True),
        ("case_setup", 0, True),
        ("case_setup", 1, True),  # the fix
        ("solver_runner", 1, True),  # every later stage runs again
        ("analyst", 1, True),
        ("critic", 1, True),
    ]
    (loop,) = result.record.fix_loops
    assert (loop.n, loop.source, loop.target, loop.origin) == (
        1,
        "solver_runner",
        "case_setup",
        "gate",
    )
    assert "case/0" in loop.moved and not any("polyMesh" in item for item in loop.moved)

    run = result.run_dir
    assert (run / "attempts" / "case_setup-1").is_dir()  # pass 0 stays
    assert (run / "attempts" / "case_setup-p1-1").is_dir()
    assert (run / "attempts" / "solver_runner-p1-1").is_dir()
    assert (run / "superseded" / "pass-1" / "case" / "0").is_dir()  # the case of pass 0
    assert (run / "case" / "0" / "U").is_file()  # and the one of pass 1, in the canonical place
    assert (run / "case" / "constant" / "polyMesh" / "boundary").is_file()

    opening = backend.calls[PLANNER_STEPS + 3 * STAGE_STEPS].messages[1]["content"]
    assert "Fix requested by solver_runner: the run did not reach a steady state." in opening
    assert "use a smaller time step" in opening
    seeded_text = "already in the attempt directory"
    assert (seeded_text in opening) == (repair_mode == "in_place")
    if repair_mode == "in_place":
        assert "attempts/case_setup-p1-1/case/0/U" in opening
        assert "polyMesh" not in opening.split(seeded_text)[1].split("This is attempt")[0]
    later = backend.calls[PLANNER_STEPS + 4 * STAGE_STEPS].messages[1]["content"]
    assert "Fix requested" not in later  # the request goes to the target only

    lines = (run / "llm_transcript.jsonl").read_text(encoding="utf-8").splitlines()
    entries = [json.loads(line) for line in lines]
    passes = {(entry["stage"], entry["pass_no"]) for entry in entries}
    assert passes == {
        ("planner", 0),
        ("geometry", 0),
        ("mesh", 0),
        ("case_setup", 0),
        ("case_setup", 1),
        ("solver_runner", 1),
        ("analyst", 1),
        ("critic", 1),
    }
    assert result.comparison["passed"] is True, result.comparison
    assert "fix loop 1" in (run / "run.log").read_text(encoding="utf-8")
