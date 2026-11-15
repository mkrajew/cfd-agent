"""Blind evaluation (design 9.2): the executor's knowledge tools never reach ``benchmarks/``."""

from __future__ import annotations

from pathlib import Path

from cfdagent.executor import ops
from cfdagent.knowledge import search

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_knowledge_index_excludes_benchmarks(tmp_path, monkeypatch):
    # The roots are the curated notes of the package and the tutorials of the image: nothing
    # under any directory named ``benchmarks``, and nothing of the repository outside src/.
    monkeypatch.setenv("FOAM_TUTORIALS", str(tmp_path / "tutorials"))
    roots = ops._knowledge_roots()
    assert {r.name for r in roots} == {"note", "tutorial"}
    benchmarks = (REPO_ROOT / "benchmarks").resolve()
    for root in roots:
        resolved = root.path.resolve()
        assert "benchmarks" not in resolved.parts
        assert not resolved.is_relative_to(benchmarks)
        assert not benchmarks.is_relative_to(resolved), f"{root.name} root contains benchmarks/"
    notes = next(r.path for r in roots if r.name == "note")
    assert notes == search.NOTES_DIR
    assert notes.resolve().is_relative_to(Path(search.__file__).resolve().parent)


def test_knowledge_search_does_not_find_benchmark_files(tmp_path, monkeypatch):
    # A benchmark directory with a unique token sits beside the tutorials root: only the two
    # roots are indexed, so a search for the token finds nothing.
    token = "zqxbenchmarktoken"
    (tmp_path / "benchmarks" / "x").mkdir(parents=True)
    (tmp_path / "benchmarks" / "x" / "reference.csv").write_text(token, encoding="utf-8")
    (tmp_path / "tutorials").mkdir()
    (tmp_path / "tutorials" / "controlDict").write_text("application icoFoam;", encoding="utf-8")
    monkeypatch.setenv("FOAM_TUTORIALS", str(tmp_path / "tutorials"))
    roots = ops._knowledge_roots()
    assert search.search_knowledge("icoFoam", roots, 50), "the planted tutorial is indexed"
    assert search.search_knowledge(token, roots, 50) == []
