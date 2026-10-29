"""Benchmark registry: models and loader.

On disk, a benchmark is a directory ``<root>/<id>/`` with a ``benchmark.toml`` (keys ``id``,
``family``, ``task_file``, ``citation`` and one ``[[criteria]]`` table per criterion), the task
file, and the reference CSVs (header ``x,value``) that the criteria point at. Paths in the TOML
are relative to the benchmark directory.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

BENCHMARK_FILE_NAME = "benchmark.toml"

_PROFILE_METRICS = ("max_abs_error", "rms_error")
_SCALAR_VALUE_METRICS = ("rel_error", "max_abs_error")


class RegistryError(ValueError):
    """The benchmark registry on disk is inconsistent or malformed."""


class Criterion(BaseModel):
    """One acceptance criterion: a simulated quantity compared with reference data."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    quantity: str  # key in quantities.json
    kind: Literal["profile", "scalar"]
    metric: Literal["max_abs_error", "rms_error", "rel_error", "in_range"]
    threshold: float | None = None  # required unless metric == "in_range"
    reference_file: str | None = None  # profile: CSV with header "x,value"
    reference_value: float | None = None  # scalar with abs/rel error
    reference_range: tuple[float, float] | None = None  # scalar with in_range
    normalise_by: float | None = None  # e.g. a reference speed; the error is divided by it

    @model_validator(mode="after")
    def _check_consistency(self) -> Criterion:
        if self.kind == "profile":
            if self.reference_file is None:
                raise ValueError("a profile criterion requires 'reference_file'")
            if self.metric not in _PROFILE_METRICS:
                raise ValueError(f"a profile criterion needs metric in {_PROFILE_METRICS}")
            if self.reference_value is not None or self.reference_range is not None:
                raise ValueError("a profile criterion takes no 'reference_value'/'reference_range'")
        else:
            if self.reference_file is not None:
                raise ValueError("a scalar criterion takes no 'reference_file'")
            if self.reference_value is not None and self.reference_range is not None:
                raise ValueError("give either 'reference_value' or 'reference_range', not both")
            if self.reference_value is not None:
                if self.metric not in _SCALAR_VALUE_METRICS:
                    raise ValueError(
                        f"a scalar with 'reference_value' needs metric in {_SCALAR_VALUE_METRICS}"
                    )
                if self.metric == "rel_error" and self.reference_value == 0.0:
                    raise ValueError("rel_error is undefined for reference_value == 0")
            elif self.reference_range is not None:
                if self.metric != "in_range":
                    raise ValueError("a scalar with 'reference_range' needs metric 'in_range'")
                if self.reference_range[0] > self.reference_range[1]:
                    raise ValueError("reference_range must be (low, high) with low <= high")
            else:
                raise ValueError(
                    "a scalar criterion requires 'reference_value' or 'reference_range'"
                )
        if self.metric == "in_range" and self.kind != "scalar":
            raise ValueError("metric 'in_range' applies to scalar criteria only")
        if self.metric != "in_range" and self.threshold is None:
            raise ValueError("'threshold' is required unless metric is 'in_range'")
        if self.threshold is not None and self.threshold < 0:
            raise ValueError("'threshold' must be >= 0")
        if self.normalise_by is not None and not self.normalise_by > 0:
            raise ValueError("'normalise_by' must be > 0")
        return self


class Benchmark(BaseModel):
    """A benchmark: task, citation and acceptance criteria.

    ``root`` is set by the loader to the absolute, resolved benchmark directory, so that scoring
    does not depend on the current working directory.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    family: str
    task_file: str
    citation: str
    criteria: list[Criterion]
    root: Path


def _inside(root: Path, relative: str) -> Path:
    """Resolve ``relative`` under ``root``; refuse paths that leave the benchmark directory."""
    resolved_root = root.resolve()
    path = (resolved_root / relative).resolve()
    if not path.is_relative_to(resolved_root):
        raise RegistryError(
            f"{root.name}: path {relative!r} points outside the benchmark directory"
        )
    return path


def _load_one(directory: Path) -> Benchmark:
    toml_path = directory / BENCHMARK_FILE_NAME
    try:
        with toml_path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RegistryError(f"{directory.name}: cannot read {BENCHMARK_FILE_NAME}: {exc}") from exc

    if "root" in data:
        raise RegistryError(f"{directory.name}: 'root' is set by the loader, not by the TOML")
    if data.get("id") != directory.name:
        raise RegistryError(
            f"{directory.name}: id {data.get('id')!r} in {BENCHMARK_FILE_NAME} does not match "
            "the directory name"
        )
    try:
        benchmark = Benchmark(**data, root=directory.resolve())
    except ValidationError as exc:
        raise RegistryError(f"{directory.name}: invalid {BENCHMARK_FILE_NAME}: {exc}") from exc

    referenced = [benchmark.task_file]
    referenced += [c.reference_file for c in benchmark.criteria if c.reference_file is not None]
    for relative in referenced:
        if not _inside(directory, relative).is_file():
            raise RegistryError(f"{directory.name}: referenced file {relative!r} does not exist")
    return benchmark


def load_registry(root: Path = Path("benchmarks")) -> dict[str, Benchmark]:
    """Load every ``<root>/<id>/benchmark.toml`` into ``{id: Benchmark}``.

    Raises ``RegistryError`` if the TOML id differs from the directory name, an id occurs twice,
    a referenced task or reference file is missing, or a benchmark file is invalid.
    """
    root = Path(root)
    if not root.is_dir():
        raise RegistryError(f"benchmark root {root} does not exist")
    registry: dict[str, Benchmark] = {}
    for directory in sorted(p for p in root.iterdir() if (p / BENCHMARK_FILE_NAME).is_file()):
        benchmark = _load_one(directory)
        if benchmark.id in registry:
            raise RegistryError(f"duplicate benchmark id {benchmark.id!r}")
        registry[benchmark.id] = benchmark
    return registry
