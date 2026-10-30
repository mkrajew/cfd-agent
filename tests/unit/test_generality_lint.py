"""Machine check of the generality rule (CLAUDE.md rule 6, design section 8).

Code, prompts and knowledge notes under ``src/cfdagent`` contain only general CFD/OpenFOAM
knowledge: they never name a benchmark case. Case-specific content lives in ``benchmarks/`` and
``tests/fixtures/`` only. Adding an entry to ``ALLOWED_PHRASES`` needs ``cfd-reviewer``.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from cfdagent.bench.registry import load_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src" / "cfdagent"
BENCHMARKS = REPO_ROOT / "benchmarks"

# Letter boundaries, not word boundaries: snake_case and digit-suffixed identifiers
# (cavity_re100, cylinder2d, Ghia1982, PIPE_SIZE) are caught, while "pipeline" is not ("pipe" is
# followed by a letter there).
BANNED = re.compile(r"(?<![A-Za-z])(?:cavity|cavities|pipes?|cylinders?|ghia)(?![A-Za-z])", re.I)
# CamelCase identifiers (PipeFlow, LidDrivenCavity): a capitalised name that is not preceded by a
# capital letter and not followed by a lower-case letter (so "Pipeline" is not matched).
BANNED_CAMEL = re.compile(r"(?<![A-Z])(?:Cavity|Pipes?|Cylinders?|Ghia)(?![a-z])")
# Exact strings that are legitimate general words and are removed before scanning. Empty at the
# start; every addition needs a cfd-reviewer verdict.
ALLOWED_PHRASES: tuple[str, ...] = ()
# Legitimate Python uses of the word "pipe" (Python files only, exact case): the subprocess.PIPE
# constant (also bare PIPE when the file imports it from subprocess) and the os.pipe() call.
SUBPROCESS_PIPE = re.compile(r"\bsubprocess\.PIPE\b")
SUBPROCESS_PIPE_IMPORT = re.compile(r"^\s*from subprocess import[^\n]*\bPIPE\b", re.MULTILINE)
BARE_PIPE = re.compile(r"\bPIPE\b")
OS_PIPE_CALL = re.compile(r"\bos\.pipe2?\(")
SCANNED_DIRS = ("prompts", "knowledge")


def strip_python_exemptions(text: str) -> str:
    text = SUBPROCESS_PIPE.sub("", text)
    text = OS_PIPE_CALL.sub("", text)
    if SUBPROCESS_PIPE_IMPORT.search(text):
        text = BARE_PIPE.sub("", text)
    return text


def scan_text(text: str, suffix: str, allowed: tuple[str, ...] = ALLOWED_PHRASES) -> list[str]:
    """Return 'line: match' strings for every banned name in ``text``."""
    for phrase in allowed:
        text = text.replace(phrase, "")
    if suffix == ".py":
        text = strip_python_exemptions(text)
    found: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        spans = {m.span() for pattern in (BANNED, BANNED_CAMEL) for m in pattern.finditer(line)}
        found.extend(f"{number}: {line[a:b]!r}" for a, b in sorted(spans))
    return found


def scan_tree(root: Path, allowed: tuple[str, ...] = ALLOWED_PHRASES) -> tuple[list[str], int]:
    """Return (violations, number of files scanned) for every file under ``root``."""
    violations: list[str] = []
    scanned = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        scanned += 1
        name = path.relative_to(root)
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            violations.append(f"{name}: not valid UTF-8, the lint cannot read it")
            continue
        violations.extend(f"{name}:{hit}" for hit in scan_text(text, path.suffix, allowed))
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
    note = "Flow in a pipe.\nCylinders shed vortices.\n"
    (copy / "planted.md").write_text(note, encoding="utf-8")
    violations, _ = scan_tree(copy)
    assert len(violations) == 3
    assert any("planted.py:1" in v and "Cavity" in v for v in violations)


@pytest.mark.parametrize(
    "text",
    [
        "cavity_re100",
        "pipe_flow",
        "cylinder_channel",
        "PipeFlow",
        "LidDrivenCavity",
        "Ghia1982",
        "cylinder2d",
        "PIPE_SIZE",
        "case = 'Cavity'",
        "the cavities",
        "two pipes",
        "ghia_et_al",
    ],
)
def test_lint_catches_identifier_forms(text):
    assert scan_text(text + "\n", ".py"), f"{text!r} should be flagged"


@pytest.mark.parametrize(
    "text",
    [
        "the pipeline runs the stages",
        "PIPELINE_STAGES = 6",
        "pipelines",
        "PipelineStage",
        "proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)",
        "read_fd, write_fd = os.pipe()",
        "from subprocess import PIPE, Popen\nproc = Popen(cmd, stdout=PIPE)",
    ],
)
def test_lint_ignores_general_uses(text):
    assert scan_text(text + "\n", ".py") == []


def test_lint_exemptions_are_narrow():
    # A bare PIPE without the subprocess import, and the exemptions outside Python files, flag.
    assert scan_text("proc = Popen(cmd, stdout=PIPE)\n", ".py")
    assert scan_text("import subprocess\nx = PIPE\n", ".py")
    assert scan_text("Use subprocess.PIPE here.\n", ".md")
    assert scan_text("Use os.pipe() here.\n", ".md")


def test_allowed_phrases_are_removed_before_scanning(tmp_path):
    (tmp_path / "note.md").write_text("An organ pipe is not a case.\n", encoding="utf-8")
    assert len(scan_tree(tmp_path)[0]) == 1
    assert scan_tree(tmp_path, allowed=("organ pipe",))[0] == []
    assert ALLOWED_PHRASES == (), "additions to the allowlist need a cfd-reviewer verdict"


def test_lint_fails_on_files_that_are_not_utf8(tmp_path):
    (tmp_path / "bad.md").write_bytes(b"caf\xe9 cavity\n")
    violations, scanned = scan_tree(tmp_path)
    assert scanned == 1
    assert len(violations) == 1
    assert "not valid UTF-8" in violations[0]


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


def test_generality_lint_scans_prompts(tmp_path):
    # The prompt loader is real code under src/ and the prompts directory is part of the scanned
    # tree: a case name or a benchmark marker in a prompt file must be reported.
    assert (SRC / "prompts" / "__init__.py").is_file()
    copy = tmp_path / "cfdagent"
    shutil.copytree(SRC, copy, ignore=shutil.ignore_patterns("__pycache__"))
    assert scan_tree(copy)[0] == []
    (copy / "prompts" / "planner.md").write_text(
        "Mesh the lid-driven Cavity with 129 cells.\n", encoding="utf-8"
    )
    violations = [v.replace("\\", "/") for v in scan_tree(copy)[0]]
    assert any(v.startswith("prompts/planner.md:1:") and "Cavity" in v for v in violations)

    (copy / "prompts" / "planner.md").write_text(
        "Compare with the profile in cavity-re100.\n", encoding="utf-8"
    )
    leaks = [leak.replace("\\", "/") for leak in find_leaks(copy, benchmark_markers())]
    assert any(leak.startswith("prompts/planner.md") for leak in leaks)
