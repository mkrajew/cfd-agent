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


def test_every_task_mentions_every_quantity_identifier(registry):
    # The identifiers are the output-format spec the Scorer reads (owner decision, 2026-10-28).
    for benchmark in registry.values():
        task = (benchmark.root / benchmark.task_file).read_text(encoding="utf-8")
        backticked = set(re.findall(r"`([A-Za-z0-9_]+)`", task))
        for criterion in benchmark.criteria:
            assert criterion.quantity in backticked, (
                f"{benchmark.id}: task.md does not name `{criterion.quantity}` in backticks"
            )


# ------------------------------------------------------------------- task sets (design 9.1)

_TOML = (
    'id = "{id}"\nfamily = "demo"\ntask_file = "task.md"\ncitation = "Demo (1999)"\n{extra}\n'
    '[[criteria]]\nquantity = "q"\nkind = "scalar"\nmetric = "in_range"\n'
    "reference_range = [0.0, 1.0]\n"
)


def make_benchmark(parent: Path, benchmark_id: str, extra: str = "") -> Path:
    directory = parent / benchmark_id
    directory.mkdir(parents=True)
    (directory / "task.md").write_text("Report `q`.\n", encoding="utf-8")
    (directory / "benchmark.toml").write_text(
        _TOML.format(id=benchmark_id, extra=extra), encoding="utf-8"
    )
    return directory


def test_registry_task_set_from_directory(tmp_path):
    make_benchmark(tmp_path, "dev-one")
    make_benchmark(tmp_path / "generality", "gen-one")
    registry = load_registry(tmp_path, include_generality=True)
    assert set(registry) == {"dev-one", "gen-one"}
    assert registry["dev-one"].task_set == "development"
    assert registry["gen-one"].task_set == "generality"
    assert registry["gen-one"].root == (tmp_path / "generality" / "gen-one").resolve()


def test_toml_cannot_set_task_set(tmp_path):
    from cfdagent.bench.registry import RegistryError

    make_benchmark(tmp_path, "sneaky", extra='task_set = "generality"')
    with pytest.raises(RegistryError, match="task_set"):
        load_registry(tmp_path)
    other = tmp_path / "other"
    make_benchmark(other / "generality", "sneaky", extra='task_set = "development"')
    with pytest.raises(RegistryError, match="task_set"):
        load_registry(other, include_generality=True)


def test_default_registry_excludes_generality(tmp_path):
    make_benchmark(tmp_path, "dev-one")
    make_benchmark(tmp_path / "generality", "gen-one")
    assert set(load_registry(tmp_path)) == {"dev-one"}
    assert set(load_registry(tmp_path, include_generality=False)) == {"dev-one"}
    # The default view does not even open the generality set: a broken entry there is invisible.
    broken = tmp_path / "generality" / "broken"
    broken.mkdir()
    (broken / "benchmark.toml").write_text("not = = toml", encoding="utf-8")
    assert set(load_registry(tmp_path)) == {"dev-one"}


def test_a_benchmark_file_that_is_not_utf8_is_a_registry_error(tmp_path):
    from cfdagent.bench.registry import RegistryError

    make_benchmark(tmp_path, "dev-one")
    (tmp_path / "dev-one" / "benchmark.toml").write_bytes(b'id = "caf\xe9"\n')
    with pytest.raises(RegistryError, match="cannot read"):
        load_registry(tmp_path)


def test_the_generality_directory_name_and_links_into_it_are_refused(tmp_path):
    from cfdagent.bench.registry import RegistryError

    reserved = tmp_path / "reserved"
    make_benchmark(reserved / "generality", "gen-one")
    (reserved / "generality" / "benchmark.toml").write_text("id = 'generality'", encoding="utf-8")
    with pytest.raises(RegistryError, match="reserved"):
        load_registry(reserved)

    linked = tmp_path / "linked"
    make_benchmark(linked / "generality", "gen-one")
    try:
        (linked / "gen-one").symlink_to(linked / "generality" / "gen-one", target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are not available")
    with pytest.raises(RegistryError, match="hide a generality"):
        load_registry(linked)


def test_a_generality_id_cannot_shadow_a_development_id(tmp_path):
    from cfdagent.bench.registry import RegistryError

    make_benchmark(tmp_path, "same")
    make_benchmark(tmp_path / "generality", "same")
    with pytest.raises(RegistryError, match="duplicate"):
        load_registry(tmp_path, include_generality=True)
