"""The whole pipeline through ``run_task`` inside the image: a scripted model plays all six agents
(Planner, Geometry, Mesh, Case Setup, Solver Runner, Analyst), the local-mode executor runs the real
tools and the real solver, and the real Scorer judges the result. The upload is faked. Local mode
imports the ``cfdagent`` under test (the checked-out sources), see ``SandboxClient._command``.

The conversation is ``tests/fixtures/scripted/cavity_pipeline.json``: the Planner's three replies
followed by the recorded conversations of the five stages. About two minutes per run.
"""

import copy
import json
import re
from pathlib import Path

import pytest

from cfdagent.config import RunConfig
from cfdagent.llm.scripted import ScriptedBackend
from cfdagent.orchestrator import run_task
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
STAGES = ["planner", "geometry", "mesh", "case_setup", "solver_runner", "analyst"]


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


def benchmark_config() -> RunConfig:
    sandbox = SandboxConfig(mode="local", allow_local=True, image="local-unused")
    return RunConfig.default().model_copy(
        update={"sandbox": sandbox, "benchmark_id": BENCHMARK, "task_set": "development"}
    )


def run_scripted(tmp_path: Path, steps: list[dict]):
    uploader = FakeUploader()
    backend = backend_from(steps, tmp_path)
    result = run_task(
        "ignored: a benchmark run uses the task file of the benchmark",
        benchmark_config(),
        backend=backend,
        run_root=tmp_path / "runs",
        upload=True,
        uploader=uploader,
        state_dir=tmp_path / "state",
        benchmarks_root=ROOT / "benchmarks",
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
