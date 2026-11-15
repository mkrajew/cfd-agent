"""The knowledge tools of the agents: the stages that offer them and what they record."""

from pathlib import Path

import pytest
from test_geometry_stage import FakeSandbox, make_ctx

from cfdagent.agents.analyst import AnalystStage
from cfdagent.agents.case_setup import CaseSetupStage
from cfdagent.agents.geometry import GeometryStage
from cfdagent.agents.mesh import MeshStage
from cfdagent.agents.tools import ToolRegistry
from cfdagent.executor.protocol import OpResponse, ReadKnowledgeArgs, SearchKnowledgeArgs
from cfdagent.knowledge.search import NOTES_DIR
from cfdagent.llm.client import ToolCall
from cfdagent.orchestrator import GatedStep
from cfdagent.prompts import load_prompt

STAGES = {
    "geometry": GeometryStage,
    "mesh": MeshStage,
    "case_setup": CaseSetupStage,
    "analyst": AnalystStage,
}
HITS = [
    {"source": "note", "path": "note:gmsh-api.md", "line": 3, "text": "Use the OCC kernel."},
    {"source": "tutorial", "path": "flow/x/system/fvSchemes", "line": 0, "text": "(name)"},
]


def registry_for(tmp_path, name):
    sandbox = FakeSandbox(
        {
            "search_knowledge": OpResponse(ok=True, data={"hits": HITS}),
            "read_knowledge": OpResponse(ok=True, data={"text": "the text"}),
        }
    )
    ctx = make_ctx(tmp_path, sandbox)
    stage = STAGES[name]()
    registry = ToolRegistry()
    for tool in stage.extra_tools(ctx, ctx.run.attempt_dir(name, 1)):
        registry.add(tool)
    return stage, ctx, registry


def call(tool, **arguments):
    return ToolCall(id="c1", name=tool, arguments=arguments)


@pytest.mark.parametrize("name", list(STAGES))
def test_knowledge_reads_recorded_for_every_stage_with_the_tools(tmp_path, name):
    stage, ctx, registry = registry_for(tmp_path, name)
    assert {"search_knowledge", "read_knowledge"} <= set(registry.names())
    assert stage.knowledge_reads == []
    registry.dispatch(call("read_knowledge", path="note:gmsh-api.md"))
    registry.dispatch(call("read_knowledge", path="note:gmsh-api.md"))  # once in the audit
    registry.dispatch(call("read_knowledge", path="../escape"))  # a request is audited anyway
    registry.dispatch(call("search_knowledge", query="occ"))  # a search reads no file
    assert stage.knowledge_reads == ["note:gmsh-api.md", "../escape"]
    assert GatedStep(stage).knowledge_reads() == stage.knowledge_reads  # what the record collects
    assert STAGES[name]().knowledge_reads == []  # per instance, never shared
    ops = [c[0] for c in ctx.sandbox.calls]
    assert ops.count("read_knowledge") == 3 and ops.count("search_knowledge") == 1
    assert isinstance(ctx.sandbox.calls[0][1], ReadKnowledgeArgs)
    assert isinstance(ctx.sandbox.calls[-1][1], SearchKnowledgeArgs)


@pytest.mark.parametrize("name", list(STAGES))
def test_search_results_name_the_source_and_the_note_paths(tmp_path, name):
    _stage, _ctx, registry = registry_for(tmp_path, name)
    out = registry.dispatch(call("search_knowledge", query="occ"))
    lines = out.splitlines()
    assert lines[0] == "note:gmsh-api.md:3: Use the OCC kernel."
    assert lines[1] == "flow/x/system/fvSchemes (name)"


@pytest.mark.parametrize("name", list(STAGES))
def test_the_prompt_of_each_stage_with_the_tools_mentions_them(name):
    text = load_prompt(name).text
    assert "search_knowledge" in text and "read_knowledge" in text
    assert "note:" in text
    assert "benchmarks/" not in text


def test_the_knowledge_ops_reach_notes_and_tutorials_only(tmp_path, monkeypatch):
    # Blind evaluation: the ops behind the tools know two roots, the notes of the package and the
    # OpenFOAM tutorials; nothing of the repository's benchmarks.
    from cfdagent.executor import ops
    from cfdagent.knowledge import search

    monkeypatch.setenv("FOAM_TUTORIALS", str(tmp_path))
    roots = ops._knowledge_roots()
    assert [(r.name, r.path) for r in roots] == [
        ("note", search.NOTES_DIR),
        ("tutorial", tmp_path),
    ]
    assert search.NOTES_DIR.is_relative_to(Path(search.__file__).resolve().parent)


@pytest.mark.parametrize("note", sorted(NOTES_DIR.glob("*.md")), ids=lambda p: p.name)
def test_each_note_is_under_150_lines(note):
    assert len(note.read_text(encoding="utf-8").splitlines()) < 150
