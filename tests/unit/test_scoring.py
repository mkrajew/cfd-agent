"""Unit tests for the benchmark models, the registry loader and the deterministic scoring.

All data here is synthetic; the real benchmark data lives in ``benchmarks/``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from cfdagent.bench.registry import Benchmark, Criterion, RegistryError, load_registry
from cfdagent.bench.scoring import profile_error, score

# --------------------------------------------------------------------------- helpers


def write_csv(path: Path, xs: list[float], vs: list[float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = ["x,value"] + [f"{x},{v}" for x, v in zip(xs, vs, strict=True)]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def make_benchmark(root: Path, criteria: list[Criterion]) -> Benchmark:
    return Benchmark(
        id="synthetic",
        family="synthetic",
        task_file="task.md",
        citation="made up for the unit tests",
        criteria=criteria,
        root=root,
    )


def profile_criterion(**overrides) -> Criterion:
    fields = {
        "quantity": "prof",
        "kind": "profile",
        "metric": "max_abs_error",
        "threshold": 0.1,
        "reference_file": "reference/prof.csv",
    }
    fields.update(overrides)
    return Criterion(**fields)


def scalar_criterion(**overrides) -> Criterion:
    fields = {
        "quantity": "cd",
        "kind": "scalar",
        "metric": "rel_error",
        "threshold": 0.05,
        "reference_value": 1.0,
    }
    fields.update(overrides)
    return Criterion(**fields)


# --------------------------------------------------------------------------- profile_error


def test_profile_error_zero_for_identical():
    x = [0.0, 0.25, 0.5, 0.75, 1.0]
    v = [0.0, 0.3, 0.1, -0.2, 1.0]
    assert profile_error(x, v, x, v, "max_abs_error") == 0.0
    assert profile_error(x, v, x, v, "rms_error") == 0.0


def test_profile_error_interpolates():
    err = profile_error([0.0, 1.0], [0.0, 1.0], [0.5], [0.4], "max_abs_error")
    assert err == pytest.approx(0.1)


def test_profile_error_rejects_reference_outside_sim_range():
    with pytest.raises(ValueError, match="outside"):
        profile_error([0.1, 0.9], [0.0, 1.0], [0.0], [0.0], "max_abs_error")
    with pytest.raises(ValueError, match="outside"):
        profile_error([0.1, 0.9], [0.0, 1.0], [0.5, 0.95], [0.5, 0.95], "rms_error")


def test_profile_error_accepts_reference_on_sim_boundary():
    # Exactly on the end points is inside the sampled range.
    assert profile_error([0.0, 1.0], [0.0, 1.0], [0.0, 1.0], [0.0, 1.0], "max_abs_error") == 0.0


def test_profile_error_sorts_unordered_sim_and_rejects_duplicates():
    err = profile_error([1.0, 0.0], [1.0, 0.0], [0.5], [0.5], "max_abs_error")
    assert err == pytest.approx(0.0)
    with pytest.raises(ValueError, match="duplicate"):
        profile_error([0.0, 0.5, 0.5, 1.0], [0.0, 0.4, 0.6, 1.0], [0.25], [0.2], "max_abs_error")


def test_profile_error_rejects_bad_input():
    with pytest.raises(ValueError, match="same length"):
        profile_error([0.0, 1.0], [0.0], [0.5], [0.5], "max_abs_error")
    with pytest.raises(ValueError, match="at least two"):
        profile_error([0.5], [0.5], [0.5], [0.5], "max_abs_error")
    with pytest.raises(ValueError, match="finite"):
        profile_error([0.0, 1.0], [0.0, float("nan")], [0.5], [0.5], "max_abs_error")
    with pytest.raises(ValueError, match="metric"):
        profile_error([0.0, 1.0], [0.0, 1.0], [0.5], [0.5], "rel_error")


def test_rms_error():
    # Identity profile, reference offset by +0.3 and -0.1: rms = sqrt((0.09 + 0.01) / 2)
    err = profile_error([0.0, 1.0], [0.0, 1.0], [0.25, 0.75], [0.55, 0.65], "rms_error")
    assert err == pytest.approx(((0.09 + 0.01) / 2) ** 0.5)
    mx = profile_error([0.0, 1.0], [0.0, 1.0], [0.25, 0.75], [0.55, 0.65], "max_abs_error")
    assert mx == pytest.approx(0.3)


# --------------------------------------------------------------------------- Criterion


def test_criterion_validation_rules():
    # profile needs a reference file and a profile metric
    with pytest.raises(ValidationError):
        profile_criterion(reference_file=None)
    with pytest.raises(ValidationError):
        profile_criterion(metric="rel_error")
    # scalar needs exactly one of value / range, with a matching metric
    with pytest.raises(ValidationError):
        scalar_criterion(reference_value=None)
    with pytest.raises(ValidationError):
        scalar_criterion(metric="rms_error")
    with pytest.raises(ValidationError):
        scalar_criterion(metric="in_range", threshold=None)  # in_range needs a range
    with pytest.raises(ValidationError):
        scalar_criterion(reference_range=(0.0, 1.0))  # both value and range
    with pytest.raises(ValidationError):
        scalar_criterion(metric="in_range", threshold=None, reference_range=None)
    with pytest.raises(ValidationError):
        scalar_criterion(
            metric="rel_error", reference_value=None, reference_range=(0.0, 1.0)
        )  # range needs in_range
    # threshold required unless in_range
    with pytest.raises(ValidationError):
        scalar_criterion(threshold=None)
    scalar_criterion(
        metric="in_range", threshold=None, reference_value=None, reference_range=(0.1, 0.2)
    )
    # normalise_by must be positive
    with pytest.raises(ValidationError):
        profile_criterion(normalise_by=0.0)
    with pytest.raises(ValidationError):
        profile_criterion(normalise_by=-1.0)
    # relative error against a zero reference is undefined
    with pytest.raises(ValidationError):
        scalar_criterion(reference_value=0.0)
    # inverted range
    with pytest.raises(ValidationError):
        scalar_criterion(
            metric="in_range", threshold=None, reference_value=None, reference_range=(0.2, 0.1)
        )


# --------------------------------------------------------------------------- score


def test_scalar_rel_error_and_in_range(tmp_path):
    bench = make_benchmark(
        tmp_path,
        [
            scalar_criterion(quantity="cd", threshold=0.05, reference_value=2.0),
            scalar_criterion(
                quantity="st",
                metric="in_range",
                threshold=None,
                reference_value=None,
                reference_range=(0.1, 0.2),
            ),
        ],
    )
    ok = score(bench, {"cd": 2.09, "st": 0.15})
    assert ok.passed
    assert ok.criteria[0].error == pytest.approx(0.045)
    assert ok.criteria[0].threshold == 0.05
    assert ok.criteria[1].error == 0.0

    bad = score(bench, {"cd": 2.2, "st": 0.25})
    assert not bad.passed
    assert not bad.criteria[0].passed
    assert bad.criteria[0].error == pytest.approx(0.1)
    assert not bad.criteria[1].passed
    assert bad.criteria[1].error == pytest.approx(0.05)  # distance outside the range

    edge = score(bench, {"cd": 2.0, "st": 0.1})  # range ends are inclusive
    assert edge.passed


def test_scalar_max_abs_error(tmp_path):
    crit = scalar_criterion(
        quantity="dp", metric="max_abs_error", threshold=0.5, reference_value=10.0
    )
    bench = make_benchmark(tmp_path, [crit])
    assert score(bench, {"dp": 10.4}).passed
    assert not score(bench, {"dp": 9.0}).passed


def test_normalise_by_divides_error(tmp_path):
    write_csv(tmp_path / "reference/prof.csv", [0.0, 0.5, 1.0], [0.0, 0.5, 1.0])
    sim = {"prof": {"x": [0.0, 0.5, 1.0], "value": [0.0, 0.5, 1.2]}}  # max abs error 0.2

    unnormalised = make_benchmark(tmp_path, [profile_criterion(threshold=0.1)])
    res = score(unnormalised, sim)
    assert res.criteria[0].error == pytest.approx(0.2)
    assert not res.passed

    normalised = make_benchmark(tmp_path, [profile_criterion(threshold=0.1, normalise_by=4.0)])
    res = score(normalised, sim)
    assert res.criteria[0].error == pytest.approx(0.05)
    assert res.passed

    scalar = make_benchmark(
        tmp_path,
        [
            scalar_criterion(
                quantity="dp",
                metric="max_abs_error",
                threshold=0.1,
                reference_value=1.0,
                normalise_by=10.0,
            )
        ],
    )
    res = score(scalar, {"dp": 1.5})
    assert res.criteria[0].error == pytest.approx(0.05)
    assert res.passed


def test_score_passes_only_when_all_criteria_pass(tmp_path):
    write_csv(tmp_path / "reference/prof.csv", [0.0, 0.5, 1.0], [0.0, 0.5, 1.0])
    bench = make_benchmark(tmp_path, [profile_criterion(), scalar_criterion()])
    good_profile = {"x": [0.0, 1.0], "value": [0.0, 1.0]}
    bad_profile = {"x": [0.0, 1.0], "value": [0.0, 2.0]}

    assert score(bench, {"prof": good_profile, "cd": 1.01}).passed
    assert not score(bench, {"prof": good_profile, "cd": 1.5}).passed
    assert not score(bench, {"prof": bad_profile, "cd": 1.01}).passed
    result = score(bench, {"prof": bad_profile, "cd": 1.5})
    assert [c.passed for c in result.criteria] == [False, False]


def test_score_with_no_criteria_does_not_pass(tmp_path):
    assert not score(make_benchmark(tmp_path, []), {}).passed


def test_score_missing_quantity_fails_with_reason(tmp_path):
    write_csv(tmp_path / "reference/prof.csv", [0.0, 1.0], [0.0, 1.0])
    bench = make_benchmark(tmp_path, [profile_criterion()])
    result = score(bench, {})
    assert not result.passed
    assert result.criteria[0].passed is False
    assert result.criteria[0].error is None
    assert result.criteria[0].reason == "missing quantity"
    assert result.criteria[0].quantity == "prof"


def test_score_wrong_type_fails_with_reason(tmp_path):
    write_csv(tmp_path / "reference/prof.csv", [0.0, 1.0], [0.0, 1.0])
    bench = make_benchmark(tmp_path, [profile_criterion(), scalar_criterion()])
    result = score(bench, {"prof": 1.0, "cd": {"x": [0.0, 1.0], "value": [1.0, 1.0]}})
    assert [c.passed for c in result.criteria] == [False, False]
    assert "profile" in result.criteria[0].reason
    assert "scalar" in result.criteria[1].reason
    assert all(c.error is None for c in result.criteria)


def test_score_malformed_profile_fails_with_reason(tmp_path):
    write_csv(tmp_path / "reference/prof.csv", [0.0, 1.0], [0.0, 1.0])
    bench = make_benchmark(tmp_path, [profile_criterion()])
    for bad in ({"x": [0.0, 1.0]}, {"x": [0.0, 1.0], "value": [1.0]}):
        result = score(bench, {"prof": bad})
        assert not result.passed
        assert result.criteria[0].reason


def test_score_nonfinite_scalar_fails(tmp_path):
    bench = make_benchmark(tmp_path, [scalar_criterion(threshold=1e9)])
    result = score(bench, {"cd": float("nan")})
    assert not result.passed
    assert "finite" in result.criteria[0].reason


def test_score_reference_outside_sim_range_fails_without_raising(tmp_path):
    write_csv(tmp_path / "reference/prof.csv", [0.0, 0.5, 1.0], [0.0, 0.5, 1.0])
    bench = make_benchmark(tmp_path, [profile_criterion(threshold=1e9)])
    # Sampled range stops short of the reference at x=0 and x=1: must fail, never extrapolate.
    result = score(bench, {"prof": {"x": [0.1, 0.9], "value": [0.1, 0.9]}})
    assert not result.passed
    assert result.criteria[0].error is None
    assert "outside" in result.criteria[0].reason


def test_score_unreadable_reference_fails_without_raising(tmp_path):
    bench = make_benchmark(tmp_path, [profile_criterion()])  # reference file never written
    result = score(bench, {"prof": {"x": [0.0, 1.0], "value": [0.0, 1.0]}})
    assert not result.passed
    assert result.criteria[0].reason


# --------------------------------------------------------------------------- load_registry


def write_benchmark(root: Path, dir_name: str, bench_id: str | None = None) -> Path:
    directory = root / dir_name
    (directory / "reference").mkdir(parents=True)
    (directory / "task.md").write_text("Simulate something.\n", encoding="utf-8")
    write_csv(directory / "reference/prof.csv", [0.0, 0.5, 1.0], [0.0, 0.5, 1.0])
    (directory / "benchmark.toml").write_text(
        f"""
id = "{bench_id or dir_name}"
family = "synthetic"
task_file = "task.md"
citation = "invented for the test"

[[criteria]]
quantity = "prof"
kind = "profile"
metric = "max_abs_error"
threshold = 0.1
reference_file = "reference/prof.csv"
normalise_by = 2.0

[[criteria]]
quantity = "cd"
kind = "scalar"
metric = "in_range"
reference_range = [1.0, 2.0]
""",
        encoding="utf-8",
    )
    return directory


def edit_toml(directory: Path, old: str, new: str) -> None:
    toml = directory / "benchmark.toml"
    text = toml.read_text(encoding="utf-8")
    assert old in text
    toml.write_text(text.replace(old, new), encoding="utf-8")


def test_load_registry_reads_benchmarks_and_scores_them(tmp_path):
    write_benchmark(tmp_path, "alpha")
    write_benchmark(tmp_path, "beta")
    (tmp_path / "README.md").write_text("not a benchmark directory", encoding="utf-8")

    registry = load_registry(tmp_path)

    assert set(registry) == {"alpha", "beta"}
    alpha = registry["alpha"]
    assert alpha.root == tmp_path / "alpha"
    assert alpha.criteria[0].normalise_by == 2.0
    assert alpha.criteria[1].reference_range == (1.0, 2.0)
    result = score(alpha, {"prof": {"x": [0.0, 1.0], "value": [0.0, 1.0]}, "cd": 1.5})
    assert result.passed


def test_load_registry_rejects_id_mismatch(tmp_path):
    write_benchmark(tmp_path, "alpha", bench_id="gamma")
    with pytest.raises(RegistryError, match="alpha"):
        load_registry(tmp_path)


def test_load_registry_rejects_missing_reference_file(tmp_path):
    directory = write_benchmark(tmp_path, "alpha")
    (directory / "reference/prof.csv").unlink()
    with pytest.raises(RegistryError, match=r"prof\.csv"):
        load_registry(tmp_path)


def test_load_registry_rejects_missing_task_file(tmp_path):
    directory = write_benchmark(tmp_path, "alpha")
    (directory / "task.md").unlink()
    with pytest.raises(RegistryError, match=r"task\.md"):
        load_registry(tmp_path)


def test_load_registry_rejects_reference_path_escaping_the_benchmark(tmp_path):
    directory = write_benchmark(tmp_path, "alpha")
    write_csv(tmp_path / "outside.csv", [0.0, 1.0], [0.0, 1.0])
    edit_toml(directory, "reference/prof.csv", "../outside.csv")
    with pytest.raises(RegistryError, match="outside"):
        load_registry(tmp_path)


def test_load_registry_rejects_root_key_in_toml(tmp_path):
    directory = write_benchmark(tmp_path, "alpha")
    edit_toml(directory, 'family = "synthetic"', 'family = "synthetic"\nroot = "/elsewhere"')
    with pytest.raises(RegistryError, match="root"):
        load_registry(tmp_path)


def test_load_registry_rejects_invalid_criterion(tmp_path):
    directory = write_benchmark(tmp_path, "alpha")
    edit_toml(directory, 'metric = "max_abs_error"', 'metric = "rel_error"')
    with pytest.raises(RegistryError, match="alpha"):
        load_registry(tmp_path)


def test_load_registry_rejects_duplicate_ids(tmp_path):
    # Two directories cannot share a name, so a duplicate id always shows up as a mismatch.
    write_benchmark(tmp_path, "alpha")
    write_benchmark(tmp_path, "beta", bench_id="alpha")
    with pytest.raises(RegistryError, match="beta"):
        load_registry(tmp_path)


def test_load_registry_missing_root_directory_raises(tmp_path):
    with pytest.raises(RegistryError, match="does not exist"):
        load_registry(tmp_path / "nope")
