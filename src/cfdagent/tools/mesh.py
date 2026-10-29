"""Mesh stage: run a Gmsh script, convert its mesh to OpenFOAM, and gate the result.

Flow: ``run_mesh_script`` (writes ``mesh.msh``, MSH 2.2 ASCII) -> ``convert_to_foam`` (gmshToFoam)
-> ``apply_patch_types`` (patch types from the boundary manifest) -> ``check_mesh`` (checkMesh plus
deterministic checks against the manifest and the expected bounding box).

This is where two silent failure modes are stopped: a boundary face nobody named ends up in
OpenFOAM's ``defaultFaces`` patch, and a unit mix-up between STEP and Gmsh scales the whole domain.
Both are rejected by ``check_mesh``.
"""

from __future__ import annotations

import dataclasses
import math
import re
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from cfdagent.errors import ToolFailure
from cfdagent.gates import GateResult
from cfdagent.manifest import BoundaryManifest
from cfdagent.tools.bbox import BBox, compare_bbox, validate_expected_bbox
from cfdagent.tools.polymesh import MeshError, placement_errors, read_boundary
from cfdagent.tools.process import ToolResult, fail_after_run, run_tool

MESH_FILE_NAME = "mesh.msh"
MESH_LOG_NAME = "mesh.log"
CONVERT_LOG_NAME = "gmshToFoam.log"
CHECKMESH_LOG_NAME = "checkMesh.log"
DICT_LOG_NAME = "foamDictionary.log"

# Patch types for which OpenFOAM conventionally lists the type itself as the only patch group.
# ``patch`` has no group.
_GROUPED_TYPES = frozenset({"wall", "empty", "symmetryPlane", "symmetry", "wedge"})
# The smallest set of system files that gmshToFoam (controlDict) and checkMesh (fvSchemes,
# fvSolution) will start with. They are written into a scratch directory, never into the case.
_FOAM_HEADER = "FoamFile { version 2.0; format ascii; class dictionary; object %s; }\n"
_SKELETON_FILES = {
    "controlDict": (
        "application checkMesh;\n"
        "startFrom startTime;\n"
        "startTime 0;\n"
        "stopAt endTime;\n"
        "endTime 1;\n"
        "deltaT 1;\n"
        "writeControl timeStep;\n"
        "writeInterval 1;\n"
    ),
    "fvSchemes": (
        "ddtSchemes { default steadyState; }\n"
        "gradSchemes { default Gauss linear; }\n"
        "divSchemes { default none; }\n"
        "laplacianSchemes { default Gauss linear corrected; }\n"
        "interpolationSchemes { default linear; }\n"
        "snGradSchemes { default corrected; }\n"
    ),
    "fvSolution": "solvers { }\n",
}


@dataclass(frozen=True)
class MeshThresholds:
    """Largest acceptable mesh quality values; a value above its limit, or NaN, fails the gate.

    70 degrees is where checkMesh warns about severely non-orthogonal faces (it fails only at 90),
    so this gate is the real enforcement of that limit; skewness 4 and aspect ratio 1000 are
    checkMesh's own failure limits. A mesh within them is usable, not necessarily good.

    A limit that is NaN or not positive raises ``ValueError``: it could never reject anything or
    would reject everything silently.
    """

    max_non_orthogonality: float = 70.0
    max_skewness: float = 4.0
    max_aspect_ratio: float = 1000.0

    def __post_init__(self) -> None:
        for field in dataclasses.fields(self):
            value = getattr(self, field.name)
            if not value > 0:  # also true for NaN
                raise ValueError(f"{field.name} must be a positive number, got {value!r}")


DEFAULT_THRESHOLDS = MeshThresholds()


@dataclass(frozen=True)
class CheckMeshReport:
    """Values parsed from a checkMesh log.

    ``max_non_orthogonality``, ``max_skewness`` and ``max_aspect_ratio`` are ``nan`` when checkMesh
    did not print them (it skips some geometry checks after finding open cells).
    """

    n_cells: int
    bbox: BBox  # xmin, ymin, zmin, xmax, ymax, zmax
    solution_directions: int
    patches: dict[str, int]  # patch name -> face count (one entry per distinct parsed name)
    max_non_orthogonality: float
    max_skewness: float
    max_aspect_ratio: float
    failed_checks: int  # 0 when "Mesh OK."
    n_patch_rows: int  # rows of the patch table; can exceed len(patches) for ambiguous names


# --------------------------------------------------------------------------------------------
# checkMesh parsing
# --------------------------------------------------------------------------------------------

_NUMBER = r"[-+]?(?:\d+\.?\d*(?:[eE][-+]?\d+)?|\.\d+(?:[eE][-+]?\d+)?|nan|inf)"
_CELLS_RE = re.compile(r"^\s*cells:\s+(\d+)\s*$", re.MULTILINE)
_BBOX_RE = re.compile(
    rf"Overall domain bounding box\s*\(\s*({_NUMBER})\s+({_NUMBER})\s+({_NUMBER})\s*\)"
    rf"\s*\(\s*({_NUMBER})\s+({_NUMBER})\s+({_NUMBER})\s*\)",
    re.IGNORECASE,
)
_SOLUTION_DIRS_RE = re.compile(r"Mesh has (\d+) solution \(non-empty\) directions")
_NON_ORTHO_RE = re.compile(rf"Mesh non-orthogonality Max:\s*({_NUMBER})", re.IGNORECASE)
_SKEWNESS_RE = re.compile(rf"Max skewness\s*=\s*({_NUMBER})", re.IGNORECASE)
_ASPECT_RE = re.compile(rf"Max aspect ratio\s*[=:]\s*({_NUMBER})", re.IGNORECASE)
_FAILED_RE = re.compile(r"Failed (\d+) mesh checks?\.")
_OK_RE = re.compile(r"^\s*Mesh OK\.", re.MULTILINE)
_PATCH_HEADER_RE = re.compile(r"^\s*Patch\s+Faces\s+Points\b", re.MULTILINE)
_BOUNDARY_COUNT_RE = re.compile(r"^\s*boundary patches:\s+(\d+)\s*$", re.MULTILINE)
_PATCH_ROW_RE = re.compile(r"^\s+(\S+)\s+(\d+)\s+(\d+)\s+\S")
# checkMesh prints the name in a 20-column field, so a longer name abuts the face count.
_PATCH_ROW_ABUTTING_RE = re.compile(r"^\s+(\S*?\D)(\d+)\s+(\d+)\s+\S")


def _last_float(regex: re.Pattern[str], text: str) -> float:
    matches = regex.findall(text)
    return float(matches[-1]) if matches else math.nan


def _parse_patch_table(text: str, expected_rows: int) -> tuple[dict[str, int], int]:
    """Read the patch topology table; the row count must equal ``expected_rows``.

    Returns the name -> face count mapping and the number of table rows. Rows are counted, not
    dict keys: a name of 20 or more characters that ends in a digit cannot be told apart from the
    face count that follows it, so such a row may be split wrongly, and two patches that differ
    only in that last digit (``..._1``, ``..._2``) then parse to the same name and share one key.
    ``check_mesh`` therefore takes patch names and face counts from the ``boundary`` file instead
    of this table.
    """
    header = _PATCH_HEADER_RE.search(text)
    if header is None:
        raise ValueError("not checkMesh output: no patch topology table found")
    patches: dict[str, int] = {}
    n_rows = 0
    for line in text[header.end() :].splitlines()[1:]:
        if not line.strip():
            break
        row = _PATCH_ROW_RE.match(line) or _PATCH_ROW_ABUTTING_RE.match(line)
        if row is None:
            break
        name = row.group(1)
        if name == '".*"':  # the summary row over all patches
            continue
        patches[name] = int(row.group(2))
        n_rows += 1
    if n_rows != expected_rows:
        raise ValueError(
            f"checkMesh output lists {expected_rows} boundary patches but {n_rows} rows "
            "of the patch table could be read"
        )
    return patches, n_rows


def parse_checkmesh(log_text: str) -> CheckMeshReport:
    """Parse the text printed by ``checkMesh``.

    Raises ``ValueError`` if the text is not a complete checkMesh run: the cell count, patch table,
    bounding box, solution directions and the final verdict ("Mesh OK." or "Failed N mesh checks.")
    must all be present, so a run that died half-way is never read as a pass.
    """
    cells = _CELLS_RE.search(log_text)
    if cells is None:
        raise ValueError("not checkMesh output: no 'cells:' line in the mesh statistics")
    bbox_match = _BBOX_RE.search(log_text)
    if bbox_match is None:
        raise ValueError("incomplete checkMesh output: no 'Overall domain bounding box' line")
    directions = _SOLUTION_DIRS_RE.search(log_text)
    if directions is None:
        raise ValueError("incomplete checkMesh output: no solution directions line")
    n_patches = _BOUNDARY_COUNT_RE.search(log_text)
    if n_patches is None:
        raise ValueError("incomplete checkMesh output: no 'boundary patches:' line")
    patches, n_patch_rows = _parse_patch_table(log_text, int(n_patches.group(1)))

    failed = _FAILED_RE.search(log_text)
    if failed is not None:
        failed_checks = int(failed.group(1))
    elif _OK_RE.search(log_text):
        failed_checks = 0
    else:
        raise ValueError(
            "incomplete checkMesh output: neither 'Mesh OK.' nor 'Failed N mesh checks.'"
        )

    bbox = tuple(float(v) for v in bbox_match.groups())
    return CheckMeshReport(
        n_cells=int(cells.group(1)),
        bbox=bbox,  # type: ignore[arg-type]
        solution_directions=int(directions.group(1)),
        patches=patches,
        max_non_orthogonality=_last_float(_NON_ORTHO_RE, log_text),
        max_skewness=_last_float(_SKEWNESS_RE, log_text),
        max_aspect_ratio=_last_float(_ASPECT_RE, log_text),
        failed_checks=failed_checks,
        n_patch_rows=n_patch_rows,
    )


_QUALITY_HINT = (
    " Refine or smooth the mesh near the offending faces; avoid abrupt size changes and "
    "strongly skewed cells."
)


def quality_errors(report: CheckMeshReport, thresholds: MeshThresholds) -> list[str]:
    """One error per quality metric that exceeds its limit or was not printed (NaN).

    checkMesh skips some geometry checks after it finds open cells, and prints no
    non-orthogonality for a mesh without internal faces; a metric that is missing is unknown, and
    an unknown value never passes (the comparison is written so that NaN fails).
    """
    errors = []
    for label, value, limit in (
        ("non-orthogonality", report.max_non_orthogonality, thresholds.max_non_orthogonality),
        ("skewness", report.max_skewness, thresholds.max_skewness),
        ("aspect ratio", report.max_aspect_ratio, thresholds.max_aspect_ratio),
    ):
        if math.isnan(value):
            if label == "non-orthogonality":
                errors.append(
                    "max non-orthogonality was not reported: checkMesh did not report "
                    "non-orthogonality (a mesh without internal faces prints none)"
                )
            else:
                errors.append(f"max {label} was not reported by checkMesh (the check was skipped)")
        elif not value <= limit:
            errors.append(f"max {label} is {value:.6g}, above the limit {limit:g}." + _QUALITY_HINT)
    return errors


def thickness_errors(manifest: BoundaryManifest, report: CheckMeshReport) -> list[str]:
    """Check that a 2D or axisymmetric mesh is exactly one cell thick.

    Every cell has one face on each of the two bounding patches (``empty`` for ``2D``, ``wedge``
    for ``axisymmetric``), so their faces must add up to ``2 * n_cells``. A ``2D`` mesh also needs
    exactly 2 solution directions; an axisymmetric (wedge) mesh reports 3, so that is not
    checked there. An axisymmetric manifest without a ``wedge`` patch is an error.
    """
    if manifest.dimensionality == "3D":
        return []
    errors: list[str] = []
    if manifest.dimensionality == "2D":
        kind = "empty"
        if report.solution_directions != 2:
            errors.append(
                f"the manifest is 2D but checkMesh reports {report.solution_directions} solution "
                "directions (expected exactly 2: one direction must be removed by 'empty' "
                "patches)"
            )
    else:
        kind = "wedge"
    names = [p.name for p in manifest.patches if p.type == kind]
    if not names:
        errors.append(
            f"the manifest is {manifest.dimensionality} but has no patch of type '{kind}'"
        )
        return errors
    faces = sum(report.patches.get(name, 0) for name in names)
    if faces != 2 * report.n_cells:
        errors.append(
            f"the mesh is not one cell thick: the '{kind}' patches {names} hold {faces} faces, "
            f"but a {manifest.dimensionality} mesh with {report.n_cells} cells and one cell "
            f"through the thickness has exactly {2 * report.n_cells}"
        )
    return errors


# --------------------------------------------------------------------------------------------
# Stage steps
# --------------------------------------------------------------------------------------------


def _msh_header_problem(msh: Path) -> str | None:
    """Return a message if ``msh`` is not MSH 2.2 ASCII, else ``None``."""
    try:
        with open(msh, encoding="ascii", errors="replace") as f:
            first = f.readline().strip()
            second = f.readline().split()
    except OSError as exc:
        return f"{msh.name} could not be read: {exc}"
    if first != "$MeshFormat" or len(second) < 2:
        return f"{msh.name} is not a Gmsh MSH file (no $MeshFormat header)"
    if not second[0].startswith("2.2") or second[1] != "0":
        kind = "binary" if second[1] == "1" else "ASCII"
        return (
            f"{msh.name} is MSH {second[0]} {kind}, but gmshToFoam needs MSH 2.2 ASCII; "
            "set gmsh.option.setNumber('Mesh.MshFileVersion', 2.2) and Mesh.Binary = 0"
        )
    return None


def run_mesh_script(script: Path, workdir: Path, timeout_s: float = 900.0) -> ToolResult:
    """Run a mesh script with ``cwd=workdir``; it must write ``workdir/mesh.msh`` (MSH 2.2 ASCII).

    Output goes to ``mesh.log`` in ``workdir``. A stale ``mesh.msh`` from an earlier run is removed
    first. If the script exits normally but ``mesh.msh`` is missing or has the wrong format, the
    returned result has ``ok=False`` and the reason is appended to the log (and shown in ``tail``).
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    msh = workdir / MESH_FILE_NAME
    msh.unlink(missing_ok=True)

    result = run_tool(
        ["python", str(Path(script).resolve())],
        cwd=workdir,
        log_path=workdir / MESH_LOG_NAME,
        timeout_s=timeout_s,
    )
    if not result.ok:
        return result

    problem = (
        f"the mesh script finished but did not write {MESH_FILE_NAME} in {workdir}"
        if not msh.is_file()
        else _msh_header_problem(msh)
    )
    if problem is None:
        return result
    return fail_after_run(result, problem)


@contextmanager
def _scratch_case(polymesh_source: Path | None = None) -> Iterator[Path]:
    """A temporary case directory with the skeleton ``system/`` files, removed on exit.

    OpenFOAM utilities refuse to start without ``system/controlDict``, ``fvSchemes`` and
    ``fvSolution``. They run here, so the real case directory never receives skeleton files that
    the case-setup stage would later have to tell from its own. If ``polymesh_source`` is given,
    a copy of it is placed at ``constant/polyMesh``; a missing source raises ``ToolFailure``.
    """
    if polymesh_source is not None and not Path(polymesh_source).is_dir():
        raise ToolFailure(f"{polymesh_source} does not exist; run convert_to_foam first")
    with tempfile.TemporaryDirectory(prefix="cfdagent-scratch-") as tmp:
        scratch = Path(tmp)
        system = scratch / "system"
        system.mkdir()
        for name, body in _SKELETON_FILES.items():
            (system / name).write_text(_FOAM_HEADER % name + body, encoding="utf-8")
        if polymesh_source is not None:
            (scratch / "constant").mkdir()
            shutil.copytree(polymesh_source, scratch / "constant" / "polyMesh")
        yield scratch


def convert_to_foam(msh: Path, case_dir: Path) -> ToolResult:
    """Convert a Gmsh MSH 2.2 file into ``case_dir/constant/polyMesh`` with ``gmshToFoam``.

    ``gmshToFoam`` refuses to start without ``system/controlDict``, ``fvSchemes`` and
    ``fvSolution``, so it runs in a scratch directory holding minimal versions of them, and only
    the resulting ``polyMesh`` is copied into ``case_dir`` (on success). ``case_dir`` never
    receives skeleton files. Raises ``ToolFailure`` if ``constant/polyMesh`` already exists.
    Output goes to ``gmshToFoam.log`` in ``case_dir`` (created if needed). Patches come out with
    type ``patch``; ``apply_patch_types`` sets the real types.
    """
    case_dir = Path(case_dir)
    target = case_dir / "constant" / "polyMesh"
    if target.exists():
        raise ToolFailure(
            f"{target} already exists; convert into a fresh case "
            "directory so old and new mesh files are never mixed"
        )
    case_dir.mkdir(parents=True, exist_ok=True)
    with _scratch_case() as scratch:
        result = run_tool(
            ["gmshToFoam", str(Path(msh).resolve()), "-case", str(scratch)],
            cwd=scratch,
            log_path=case_dir / CONVERT_LOG_NAME,
        )
        if not result.ok:
            return result
        produced = scratch / "constant" / "polyMesh"
        if not produced.is_dir():
            return fail_after_run(result, "gmshToFoam finished but wrote no constant/polyMesh")
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.parent / (target.name + ".partial")
        shutil.rmtree(partial, ignore_errors=True)
        try:
            shutil.copytree(produced, partial)
            partial.rename(target)  # a failed copy leaves no polyMesh behind
        except OSError as exc:
            raise ToolFailure(f"could not copy the converted mesh into {target}: {exc}") from exc
        finally:
            shutil.rmtree(partial, ignore_errors=True)
    return result


def _boundary_file(case_dir: Path) -> Path:
    return Path(case_dir) / "constant" / "polyMesh" / "boundary"


def _read_boundary_patches(case_dir: Path) -> dict[str, int]:
    """Patch name -> ``nFaces`` read from ``constant/polyMesh/boundary``, in file order."""
    return {name: r.n_faces for name, r in read_boundary(case_dir).items()}


def _foam_dictionary(case_dir: Path, *args: str) -> ToolResult:
    return run_tool(
        ["foamDictionary", str(_boundary_file(case_dir)), *args],
        cwd=case_dir,
        log_path=case_dir / DICT_LOG_NAME,
        timeout_s=60.0,
    )


def _read_entry(case_dir: Path, entry: str) -> str | None:
    """Entry value as printed by ``foamDictionary -value``; ``None`` if the entry is absent.

    Any other foamDictionary failure (unreadable file, timeout, parse error) raises ``ToolFailure``
    and is never reported as an absent entry.
    """
    result = _foam_dictionary(case_dir, "-entry", entry, "-value")
    log = result.log_path.read_text(encoding="utf-8", errors="replace")
    if not result.ok:
        if "Cannot find entry" in log:
            return None
        reason = "timed out" if result.timed_out else f"exited with code {result.returncode}"
        raise ToolFailure(f"foamDictionary {reason} reading {entry}:\n{result.tail}")
    return log.strip()


def apply_patch_types(case_dir: Path, manifest: BoundaryManifest) -> None:
    """Set each manifest patch's ``type`` (and ``inGroups``) in ``constant/polyMesh/boundary``.

    Uses ``foamDictionary -entry entry0/<patch>/type -set <type>``. Patches of type ``wall``,
    ``empty``, ``symmetryPlane``, ``symmetry`` and ``wedge`` get ``inGroups List<word> 1(<type>)``
    (OpenFOAM's convention, e.g. ``(wall)``); a plain ``patch`` has any ``inGroups`` removed. The
    result is read back with ``foamDictionary -value`` and compared.

    Raises ``MeshError`` if a manifest patch is not in the boundary file. Raises ``ToolFailure``
    if the mesh was not converted yet or an edit, read or read-back of the boundary file fails.
    Boundary patches that are not in the manifest (e.g. ``defaultFaces``) are not touched;
    ``check_mesh`` rejects them.
    """
    case_dir = Path(case_dir).resolve()
    boundary = _boundary_file(case_dir)
    if not boundary.is_file():
        raise ToolFailure(f"{boundary} does not exist; run convert_to_foam first")

    for patch in manifest.patches:
        base = f"entry0/{patch.name}"
        if _read_entry(case_dir, f"{base}/type") is None:
            raise MeshError(f"patch '{patch.name}' from the manifest is not in {boundary}")

        edits = [("-set", f"{base}/type", patch.type)]
        if patch.type in _GROUPED_TYPES:
            edits.append(("-set", f"{base}/inGroups", f"List<word> 1({patch.type})"))
        for flag, entry, value in edits:
            result = _foam_dictionary(case_dir, "-entry", entry, flag, value)
            if not result.ok:
                raise ToolFailure(
                    f"foamDictionary could not set {entry} to '{value}':\n{result.tail}"
                )
        if (
            patch.type not in _GROUPED_TYPES
            and _read_entry(case_dir, f"{base}/inGroups") is not None
        ):
            result = _foam_dictionary(case_dir, "-entry", f"{base}/inGroups", "-remove")
            if not result.ok:
                raise ToolFailure(
                    f"foamDictionary could not remove {base}/inGroups:\n{result.tail}"
                )

        actual = _read_entry(case_dir, f"{base}/type")
        if actual != patch.type:
            raise ToolFailure(
                f"patch '{patch.name}': type reads back as {actual!r}, expected '{patch.type}'"
            )
        groups = _read_entry(case_dir, f"{base}/inGroups")
        if patch.type in _GROUPED_TYPES:
            if groups is None or f"({patch.type})" not in groups:
                raise ToolFailure(
                    f"patch '{patch.name}': inGroups reads back as {groups!r}, "
                    f"expected a group list holding '{patch.type}'"
                )
        elif groups is not None:
            raise ToolFailure(f"patch '{patch.name}': inGroups is still set to {groups!r}")


# --------------------------------------------------------------------------------------------
# Mesh gate
# --------------------------------------------------------------------------------------------


def _unit_hint(expected: tuple[float, ...], actual: tuple[float, ...]) -> str:
    """Point out a factor-1000 size ratio, the signature of a mm/m mix-up between STEP and Gmsh."""
    exp_size = max(expected[3 + i] - expected[i] for i in range(3))
    act_size = max(actual[3 + i] - actual[i] for i in range(3))
    if exp_size <= 0 or act_size <= 0:
        return ""
    ratio = act_size / exp_size
    for factor in (1e3, 1e-3):
        if abs(ratio / factor - 1) < 0.05:
            return (
                f" The mesh is about {ratio:.3g} times the expected size; this looks like a unit "
                "mismatch between the STEP file (CadQuery writes millimetres) and the expected "
                "bounding box. Scale the geometry or the mesh explicitly."
            )
    return ""


def _placement_tolerance(expected: tuple[float, ...]) -> float:
    """Absolute tolerance for face centres on locators: ``1e-5`` times the largest extent."""
    return 1e-5 * max(expected[3 + i] - expected[i] for i in range(3))


def check_mesh(
    case_dir: Path,
    manifest: BoundaryManifest,
    expected_bbox: tuple[float, ...],
    rel_tol: float = 1e-2,
    thresholds: MeshThresholds = DEFAULT_THRESHOLDS,
) -> GateResult:
    """Run ``checkMesh`` in ``case_dir`` and gate the mesh.

    The gate fails if any of these holds: checkMesh reports failed checks; the patch names differ
    from the manifest (in particular a ``defaultFaces`` patch exists, i.e. some boundary faces were
    never named); a patch has no faces; the bounding box differs from ``expected_bbox`` by more
    than the tolerance of ``compare_bbox`` (``rel_tol`` times the expected extent of that axis,
    with a floor for the 6-digit output of checkMesh), or contains NaN/inf
    (a non-finite expected box or a ``rel_tol`` that is not finite and positive is rejected as
    well); a patch type in ``constant/polyMesh/boundary`` differs from the manifest; a patch has a
    face whose bounding box lies on none of the patch's locators (names attached to the wrong faces,
    for example inlet and outlet swapped; tolerance ``1e-5`` times the largest expected extent,
    with the representation floor of ``locator_contains``); a quality metric is above its
    limit in ``thresholds`` or was not printed (NaN); for a ``2D`` manifest the mesh does not have
    exactly 2 solution directions, or its ``empty`` patches do not hold exactly ``2 * n_cells``
    faces (one cell through the thickness: every cell has one front and one back face); for an
    ``axisymmetric`` manifest there must be a ``wedge`` patch and the ``wedge`` patches must hold
    exactly ``2 * n_cells`` faces (a wedge mesh reports 3 solution directions, not checked). All
    violations are reported, not just the first.

    ``rel_tol`` defaults to 1e-2, looser than the geometry gate (1e-3): a mesh of a curved boundary
    is an inscribed polygon, so its bounding box is short of the CAD box by up to
    ``R * (1 - cos(pi / N))`` for ``N`` nodes around. For a 5 mm radius and about 40 nodes around
    that is about 0.015 mm per coordinate, more than ``1e-3`` times a 10 mm extent (0.01 mm). That
    is a mesh effect, not an error.

    ``checkMesh`` runs in a scratch directory holding a copy of ``constant/polyMesh`` and skeleton
    ``system`` files; ``case_dir`` never receives them. Raises ``ToolFailure`` if
    ``constant/polyMesh`` does not exist.

    Returns the gate result; ``data`` is the parsed ``CheckMeshReport`` (``None`` if checkMesh did
    not run to a parseable end). The checkMesh output is kept in ``checkMesh.log`` in ``case_dir``.
    """
    case_dir = Path(case_dir).resolve()
    invalid = validate_expected_bbox(expected_bbox, rel_tol)
    if invalid is not None:
        return GateResult(False, [invalid])

    with _scratch_case(case_dir / "constant" / "polyMesh") as scratch:
        result = run_tool(["checkMesh"], cwd=scratch, log_path=case_dir / CHECKMESH_LOG_NAME)
    if not result.ok:
        reason = "timed out" if result.timed_out else f"exited with code {result.returncode}"
        return GateResult(False, [f"checkMesh {reason}:\n{result.tail}"])
    log_text = result.log_path.read_text(encoding="utf-8", errors="replace")
    try:
        report = parse_checkmesh(log_text)
    except ValueError as exc:
        return GateResult(False, [f"checkMesh output could not be parsed ({exc}):\n{result.tail}"])

    # Patch names and face counts come from the boundary file: the checkMesh table cannot be read
    # reliably for names of 20 or more characters.
    try:
        file_patches = _read_boundary_patches(case_dir)
    except MeshError as exc:
        return GateResult(False, [str(exc)], data=report)
    errors: list[str] = []
    if len(file_patches) != report.n_patch_rows:
        errors.append(
            f"checkMesh lists {report.n_patch_rows} boundary patches but the boundary file "
            f"defines {len(file_patches)}"
        )
    report = dataclasses.replace(report, patches=file_patches)

    if report.failed_checks > 0:
        flagged = [ln.strip() for ln in log_text.splitlines() if ln.lstrip().startswith("***")]
        detail = f": {'; '.join(flagged[:5])}" if flagged else ""
        errors.append(
            f"checkMesh reported {report.failed_checks} failed mesh check(s) "
            f"(see {CHECKMESH_LOG_NAME}){detail}"
        )

    expected_names = {p.name for p in manifest.patches}
    actual_names = set(report.patches)
    if "defaultFaces" in actual_names:
        errors.append(
            f"patch 'defaultFaces' exists with {report.patches['defaultFaces']} face(s): some "
            "boundary faces were not assigned to any named patch (a boundary surface has no "
            "physical group)"
        )
    missing = sorted(expected_names - actual_names)
    unexpected = sorted(actual_names - expected_names - {"defaultFaces"})
    if missing or unexpected:
        errors.append(
            f"patch names differ from the manifest: missing {missing}, unexpected {unexpected}"
        )
    for name, n_faces in report.patches.items():
        if n_faces == 0:
            errors.append(f"patch '{name}' has 0 faces")

    expected = tuple(float(v) for v in expected_bbox)
    mismatch = compare_bbox(report.bbox, expected, rel_tol, _unit_hint(expected, report.bbox))
    if mismatch is not None:
        errors.append(mismatch)
    try:
        misplaced = placement_errors(case_dir, manifest, _placement_tolerance(expected))
    except MeshError as exc:
        misplaced = [str(exc)]
    if misplaced and mismatch is not None:
        errors.append(
            "the patch placement errors below are probably a consequence of the bounding box "
            "mismatch above; fix the size first"
        )
    errors.extend(misplaced)

    for patch in manifest.patches:
        if patch.name not in report.patches:
            continue  # already reported as a missing patch
        actual_type = _read_entry(case_dir, f"entry0/{patch.name}/type")
        if actual_type != patch.type:
            errors.append(
                f"patch '{patch.name}' has type {actual_type!r} in constant/polyMesh/boundary, "
                f"but the manifest says '{patch.type}'"
            )

    errors.extend(thickness_errors(manifest, report))

    errors.extend(quality_errors(report, thresholds))

    return GateResult(not errors, errors, data=report)
