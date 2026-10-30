"""Machine check of the generality rule (CLAUDE.md rule 6, design section 8).

Code, prompts and knowledge notes under ``src/cfdagent`` contain only general CFD/OpenFOAM
knowledge: they never name a benchmark case. Case-specific content lives in ``benchmarks/`` and
``tests/fixtures/`` only. Adding an entry to ``ALLOWED_PHRASES`` needs ``cfd-reviewer``.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from cfdagent.bench.registry import load_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src" / "cfdagent"
BENCHMARKS = REPO_ROOT / "benchmarks"

# Whole words only: "pipeline" is a legitimate general word and does not match "pipe".
BANNED = re.compile(r"\b(?:cavity|cavities|pipes?|cylinders?|ghia)\b", re.IGNORECASE)
# Exact strings that are legitimate general words and are removed before scanning. Empty at the
# start; every addition needs a cfd-reviewer verdict.
ALLOWED_PHRASES: tuple[str, ...] = ()
# Python constants such as subprocess.PIPE are not case names (exact case, Python files only).
PYTHON_CONSTANT = re.compile(r"\bPIPE\b")
SCANNED_DIRS = ("prompts", "knowledge")


def scan_tree(root: Path) -> tuple[list[str], int]:
    """Return (violations, number of files scanned) for every text file under ``root``."""
    violations: list[str] = []
    scanned = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        scanned += 1
        for phrase in ALLOWED_PHRASES:
            text = text.replace(phrase, "")
        if path.suffix == ".py":
            text = PYTHON_CONSTANT.sub("", text)
        for number, line in enumerate(text.splitlines(), start=1):
            for match in BANNED.finditer(line):
                violations.append(f"{path.relative_to(root)}:{number}: {match.group(0)!r}")
    return violations, scanned


def benchmark_markers() -> set[str]:
    """Strings that identify benchmark data: ids and reference file names, plus the source name."""
    registry = load_registry(BENCHMARKS)
    markers = {"ghia"}
    for benchmark in registry.values():
        markers.add(benchmark.id.lower())
        for criterion in benchmark.criteria:
            if criterion.reference_file:
                markers.add(Path(criterion.reference_file).name.lower())
    return markers


def find_leaks(root: Path, markers: set[str]) -> list[str]:
    leaks: list[str] = []
    for directory in SCANNED_DIRS:
        base = root / directory
        if not base.is_dir():
            continue  # the directory does not exist yet: nothing to leak into
        for path in sorted(base.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore").lower()
            leaks.extend(
                f"{path.relative_to(root)}: mentions {marker!r}"
                for marker in sorted(markers)
                if marker in text
            )
    return leaks


def test_src_has_no_case_names():
    violations, scanned = scan_tree(SRC)
    assert scanned > 0, "the scan found no files: the lint would pass vacuously"
    assert violations == [], "case names in src/ break the generality rule:\n" + "\n".join(
        violations
    )


def test_lint_can_fail(tmp_path):
    # Plant violations in a copy of the scanned tree: the scan must report each of them.
    copy = tmp_path / "cfdagent"
    shutil.copytree(SRC, copy, ignore=shutil.ignore_patterns("__pycache__"))
    clean, _ = scan_tree(copy)
    assert clean == []
    (copy / "planted.py").write_text("# the lid-driven Cavity case\n", encoding="utf-8")
    (copy / "planted.md").write_text(
        "Flow in a pipe.\nCylinders shed vortices.\n", encoding="utf-8"
    )
    violations, _ = scan_tree(copy)
    assert len(violations) == 3
    assert any("planted.py:1" in v and "Cavity" in v for v in violations)


def test_lint_ignores_pipeline_and_subprocess_pipe(tmp_path):
    (tmp_path / "ok.py").write_text(
        "import subprocess\nproc = subprocess.Popen([], stdout=subprocess.PIPE)\n"
        "# the pipeline runs stages; mesh pipelines too\n",
        encoding="utf-8",
    )
    assert scan_tree(tmp_path)[0] == []
    # Outside Python files the constant exemption does not apply.
    (tmp_path / "note.md").write_text("Use a PIPE here.\n", encoding="utf-8")
    assert len(scan_tree(tmp_path)[0]) == 1


def test_knowledge_and_prompts_dirs_do_not_leak_benchmarks(tmp_path):
    markers = benchmark_markers()
    assert {"ghia", "cavity-re100", "u_profile.csv"} <= markers  # the marker set is not empty
    assert find_leaks(SRC, markers) == []

    # The check must be able to fail: plant a note in a temporary copy of the scanned dirs.
    for directory in SCANNED_DIRS:
        (tmp_path / directory).mkdir()
        (tmp_path / directory / "note.md").write_text(
            "Compare with the profile in cavity-re100.\n", encoding="utf-8"
        )
    leaks = find_leaks(tmp_path, markers)
    assert len(leaks) == len(SCANNED_DIRS)
    assert all("cavity-re100" in leak for leak in leaks)
