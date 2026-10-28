"""Tests of the committed development benchmarks (``benchmarks/``) and their registry.

These are consistency checks of the data, not of any simulation result.
"""

from __future__ import annotations

import csv
import importlib.util
import math
import re
from pathlib import Path

import pytest

from cfdagent.bench.registry import load_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
BENCHMARKS = REPO_ROOT / "benchmarks"
KNOWLEDGE_DIR = REPO_ROOT / "src" / "cfdagent" / "knowledge"

DEVELOPMENT_IDS = {"cavity-re100", "cavity-re1000", "pipe-re500", "cylinder-re20", "cylinder-re100"}


@pytest.fixture(scope="module")
def registry():
    return load_registry(BENCHMARKS)


def read_reference(path: Path) -> tuple[list[float], list[float]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == ["x", "value"], f"{path.name}: header must be 'x,value'"
    x = [float(r[0]) for r in rows[1:]]
    v = [float(r[1]) for r in rows[1:]]
    return x, v


def test_registry_loads_all_development_benchmarks(registry):
    assert set(registry) == DEVELOPMENT_IDS


def test_every_reference_file_exists_and_parses(registry):
    checked = 0
    for benchmark in registry.values():
        for criterion in benchmark.criteria:
            if criterion.kind != "profile":
                continue
            path = benchmark.root / criterion.reference_file
            assert path.is_file(), f"{benchmark.id}: {criterion.reference_file} is missing"
            x, v = read_reference(path)
            assert len(x) >= 2, f"{benchmark.id}/{path.name}: needs at least two rows"
            assert all(math.isfinite(a) for a in x + v), f"{benchmark.id}/{path.name}: non-finite"
            assert all(b > a for a, b in zip(x, x[1:], strict=False)), (
                f"{benchmark.id}/{path.name}: x must be strictly increasing"
            )
            checked += 1
    assert checked >= 5  # two cavity profiles at two Reynolds numbers, plus the pipe profile


def test_ghia_re100_sanity(registry):
    benchmark = registry["cavity-re100"]
    (criterion,) = (c for c in benchmark.criteria if c.quantity == "u_vertical_centreline")
    x, v = read_reference(benchmark.root / criterion.reference_file)
    assert len(x) == 17
    assert all(b > a for a, b in zip(x, x[1:], strict=False))
    assert v[x.index(1.0)] == 1.0
    assert v[x.index(0.0)] == 0.0


def test_pipe_reference_matches_formula():
    script = BENCHMARKS / "pipe-re500" / "make_reference.py"
    spec = importlib.util.spec_from_file_location("pipe_re500_make_reference", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    committed = (BENCHMARKS / "pipe-re500" / "reference" / "u_profile.csv").read_bytes()
    assert committed == module.render_profile_csv().encode("utf-8")
    assert module.check_benchmark_toml() == []
    assert module.main(["--check"]) == 0


def test_benchmark_data_not_in_knowledge_dir():
    if not KNOWLEDGE_DIR.is_dir():
        return  # nothing to leak into yet
    for path in KNOWLEDGE_DIR.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="ignore")
            assert "ghia" not in text.lower(), f"{path} mentions the cavity reference source"


def test_every_task_mentions_every_quantity_identifier(registry):
    # The identifiers are the output-format spec the Scorer reads (owner decision, 2026-10-28).
    for benchmark in registry.values():
        task = (benchmark.root / benchmark.task_file).read_text(encoding="utf-8")
        backticked = set(re.findall(r"`([A-Za-z0-9_]+)`", task))
        for criterion in benchmark.criteria:
            assert criterion.quantity in backticked, (
                f"{benchmark.id}: task.md does not name `{criterion.quantity}` in backticks"
            )
