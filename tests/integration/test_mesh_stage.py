"""The Mesh stage end to end inside the image: scripted models write the geometry and mesh scripts,
the local-mode executor runs them and the real mesh gate (gmshToFoam, checkMesh) judges the mesh.
Local mode imports the ``cfdagent`` under test (the checked-out sources), see
``SandboxClient._command``.
"""

import json
import math
import re
from pathlib import Path

import pytest
from test_geometry_stage import CAVITY_SCRIPT, CAVITY_SPEC, call, check, make_ctx, reply

from cfdagent.agents.casespec import CaseSpec
from cfdagent.agents.geometry import GeometryStage
from cfdagent.agents.loop import run_stage
from cfdagent.agents.mesh import MeshStage
from cfdagent.llm.scripted import ScriptedBackend

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CAVITY_MESH_SCRIPT = (FIXTURES / "handwritten" / "cavity_re100" / "mesh.py").read_text(
    encoding="utf-8"
)


def write_script(stage, number, text, name):
    return reply(
        call(f"w{number}", "write_file", path=f"attempts/{stage}-{number}/{name}", content=text)
    )


def mesh_steps(*scripts):
    """Scripted model: write ``mesh.py``, ask for the check; once per script (one per attempt)."""
    steps = []
    for number, text in enumerate(scripts, start=1):
        steps += [write_script("mesh", number, text, "mesh.py"), check_call(number)]
    return steps


def check_call(number):
    return reply(call(f"k{number}", "check_stage"))


def run_geometry(ctx, script):
    ctx.backend = ScriptedBackend([write_script("geometry", 1, script, "geometry.py"), check(1)])
    outcome = run_stage(GeometryStage(), ctx)
    assert outcome.ok, outcome


def run_mesh(ctx, *scripts):
    ctx.backend = ScriptedBackend(mesh_steps(*scripts))
    return run_stage(MeshStage(), ctx)


def checkmesh_log(ctx):
    return (ctx.run.root / "mesh" / "case" / "checkMesh.log").read_text(encoding="utf-8")


# ------------------------------------------------------------------ the 2D case


def test_mesh_stage_passes_scripted_cavity(tmp_path):
    ctx = make_ctx(tmp_path, ScriptedBackend([]))
    run_geometry(ctx, CAVITY_SCRIPT)
    backend = ScriptedBackend.from_json(FIXTURES / "scripted" / "mesh_ok.json")
    ctx.backend = backend
    outcome = run_stage(MeshStage(), ctx)
    assert outcome.ok, outcome
    assert outcome.attempts == 1

    root = ctx.run.root
    assert (root / "mesh" / "mesh.py").is_file()
    assert "$MeshFormat\n2.2 0 8" in (root / "mesh" / "mesh.msh").read_text(encoding="ascii")
    boundary = (root / "case" / "constant" / "polyMesh" / "boundary").read_text(encoding="utf-8")
    for name in ("movingWall", "fixedWalls", "frontAndBack"):
        assert name in boundary
    assert "defaultFaces" not in boundary
    assert sorted(p.name for p in (root / "case").iterdir()) == ["constant"]  # the mesh only
    assert sorted(p.name for p in (root / "case" / "constant").iterdir()) == ["polyMesh"]
    assert (root / "boundary_manifest.json").is_file()  # beside case/, where the ops look
    assert re.search(r"cells:\s+4096\b", checkmesh_log(ctx))
    assert [(a.n, a.ok) for a in ctx.record.stages[1].attempts] == [(1, True)]


def test_mesh_stage_rejects_script_without_physical_groups(tmp_path):
    no_groups = CAVITY_MESH_SCRIPT.replace(
        "assign_physical_groups(manifest)",
        "gmsh.model.addPhysicalGroup(3, [t for _, t in gmsh.model.getEntities(3)], name='cells')",
    )
    assert no_groups != CAVITY_MESH_SCRIPT
    ctx = make_ctx(tmp_path, ScriptedBackend([]))
    run_geometry(ctx, CAVITY_SCRIPT)
    outcome = run_mesh(ctx, no_groups, CAVITY_MESH_SCRIPT)
    assert outcome.ok, outcome
    assert outcome.attempts == 2
    first = ctx.record.stages[1].attempts[0]
    assert not first.ok
    assert "defaultFaces" in "\n".join(first.errors)
    assert "assign_physical_groups" in "\n".join(first.errors)


def test_mesh_stage_reports_a_script_that_crashes(tmp_path):
    crash = "import gmsh\nraise ValueError('no resolution chosen')\n"
    ctx = make_ctx(tmp_path, ScriptedBackend([]))
    run_geometry(ctx, CAVITY_SCRIPT)
    outcome = run_mesh(ctx, crash, CAVITY_MESH_SCRIPT)
    assert outcome.ok and outcome.attempts == 2
    assert "ValueError: no resolution chosen" in "\n".join(ctx.record.stages[1].attempts[0].errors)


def test_mesh_stage_rejects_a_mesh_that_is_not_one_cell_thick(tmp_path):
    two_layers = CAVITY_MESH_SCRIPT.replace("numElements=[1]", "numElements=[2]")
    assert two_layers != CAVITY_MESH_SCRIPT
    ctx = make_ctx(tmp_path, ScriptedBackend([]))
    run_geometry(ctx, CAVITY_SCRIPT)
    outcome = run_mesh(ctx, two_layers, CAVITY_MESH_SCRIPT)
    assert outcome.ok and outcome.attempts == 2
    assert "one cell thick" in "\n".join(ctx.record.stages[1].attempts[0].errors)


def test_mesh_stage_reports_a_script_that_writes_no_mesh(tmp_path):
    ctx = make_ctx(tmp_path, ScriptedBackend([]))
    run_geometry(ctx, CAVITY_SCRIPT)
    outcome = run_mesh(ctx, "print('nothing written')\n", CAVITY_MESH_SCRIPT)
    assert outcome.ok and outcome.attempts == 2
    first_errors = "\n".join(ctx.record.stages[1].attempts[0].errors)
    assert "mesh.msh" in first_errors and "nothing written" in first_errors


def test_mesh_stage_without_a_geometry_is_a_pipeline_error(tmp_path):
    from cfdagent.errors import ToolFailure

    ctx = make_ctx(tmp_path, ScriptedBackend(mesh_steps(CAVITY_MESH_SCRIPT)))
    with pytest.raises(ToolFailure, match="geometry/geometry.step"):
        run_stage(MeshStage(), ctx)


# ------------------------------------------------------------------ the axisymmetric case

ANGLE_DEG = 10.0
HALF = math.radians(ANGLE_DEG) / 2
LENGTH, R_IN, R_OUT = 2.0, 0.9, 1.0  # a thin annulus: the flat faces matter at this thickness
MARGIN = 0.01

WEDGE_GEOMETRY_SCRIPT = f"""\
import json
import math

import cadquery as cq

X0, X1 = 0.0, {LENGTH}
R_IN, R_OUT = {R_IN}, {R_OUT}
ANGLE = {ANGLE_DEG}
HALF = math.radians(ANGLE) / 2
C, S = math.cos(HALF), math.sin(HALF)
M = {MARGIN}

profile = (
    cq.Workplane("XY").moveTo(X0, R_IN).lineTo(X1, R_IN).lineTo(X1, R_OUT).lineTo(X0, R_OUT).close()
)
solid = profile.revolve(ANGLE, (0, 0, 0), (1, 0, 0)).rotate((0, 0, 0), (1, 0, 0), -ANGLE / 2)
cq.exporters.export(solid, "geometry.step")
box = solid.val().BoundingBox()
print("bbox", box.xmin, box.ymin, box.zmin, box.xmax, box.ymax, box.zmax)
print("volume", solid.val().Volume(), "solids", solid.solids().size())


def plane(axis, value):
    return {{"kind": "plane", "axis": axis, "value": value}}


def region(xmin, ymin, zmin, xmax, ymax, zmax):
    return {{"kind": "box", "box": [xmin, ymin, zmin, xmax, ymax, zmax]}}


manifest = {{
    "dimensionality": "axisymmetric",
    "patches": [
        {{"name": "inlet", "type": "patch", "locators": [plane("x", X0)]}},
        {{"name": "outlet", "type": "patch", "locators": [plane("x", X1)]}},
        {{
            "name": "inner",
            "type": "wall",
            "locators": [region(X0, R_IN * C - M, -R_IN * S - M, X1, R_IN + M, R_IN * S + M)],
        }},
        {{
            "name": "outer",
            "type": "wall",
            "locators": [region(X0, R_OUT * C - M, -R_OUT * S - M, X1, R_OUT + M, R_OUT * S + M)],
        }},
        {{
            "name": "wedgeNegative",
            "type": "wedge",
            "locators": [region(X0, R_IN * C - M, -R_OUT * S - M, X1, R_OUT * C + M, 0.0)],
        }},
        {{
            "name": "wedgePositive",
            "type": "wedge",
            "locators": [region(X0, R_IN * C - M, 0.0, X1, R_OUT * C + M, R_OUT * S + M)],
        }},
    ],
}}
with open("boundary_manifest.json", "w", encoding="utf-8") as f:
    json.dump(manifest, f, indent=2)
"""

# One cell through the wedge angle: copy the planar face on the -z side, drop the solid, mesh the
# face with a structured quad grid and revolve it about the x axis through the wedge angle.
WEDGE_MESH_SCRIPT = f"""\
import math

import gmsh

from cfdagent.manifest import MANIFEST_FILE_NAME, BoundaryManifest
from cfdagent.tools.gmsh_helpers import assign_physical_groups

ANGLE = math.radians({ANGLE_DEG})
N_AXIAL, N_RADIAL = 20, 4
EPS = 1e-6  # Gmsh pads every bounding box by 1e-7

manifest = BoundaryManifest.from_file(MANIFEST_FILE_NAME)
gmsh.initialize()
gmsh.model.add("mesh")
gmsh.model.occ.importShapes("geometry.step")
gmsh.model.occ.synchronize()

(volume,) = gmsh.model.getEntities(3)
(side,) = [
    tag
    for _, tag in gmsh.model.getEntities(2)
    if gmsh.model.getBoundingBox(2, tag)[5] < EPS and gmsh.model.getBoundingBox(2, tag)[2] < -EPS
]
copied = gmsh.model.occ.copy([(2, side)])
gmsh.model.occ.remove([volume], recursive=True)
gmsh.model.occ.synchronize()

for _, curve in gmsh.model.getEntities(1):
    box = gmsh.model.getBoundingBox(1, curve)
    along_axis = box[3] - box[0] > EPS
    gmsh.model.mesh.setTransfiniteCurve(curve, (N_AXIAL if along_axis else N_RADIAL) + 1)
gmsh.model.mesh.setTransfiniteSurface(copied[0][1])
gmsh.model.mesh.setRecombine(2, copied[0][1])

gmsh.model.occ.revolve(copied, 0, 0, 0, 1, 0, 0, ANGLE, numElements=[1], recombine=True)
gmsh.model.occ.synchronize()

assign_physical_groups(manifest)
gmsh.option.setNumber("Mesh.MshFileVersion", 2.2)
gmsh.option.setNumber("Mesh.Binary", 0)
gmsh.model.mesh.generate(3)
gmsh.write("mesh.msh")
print("cells", sum(len(t) for t in gmsh.model.mesh.getElements(3)[1]))
gmsh.finalize()
"""


def wedge_spec():
    spec = json.loads(json.dumps(CAVITY_SPEC))
    s, c = math.sin(HALF), math.cos(HALF)
    volume = math.pi * (R_OUT**2 - R_IN**2) * LENGTH * ANGLE_DEG / 360
    spec["title"] = "Annular channel wedge"
    spec["dimensionality"] = "axisymmetric"
    spec["geometry"].update(
        {
            "description": "A thin annulus about the x axis, modelled as a wedge.",
            "bbox": {
                "xmin": 0.0,
                "ymin": R_IN * c,
                "zmin": -R_OUT * s,
                "xmax": LENGTH,
                "ymax": R_OUT,
                "zmax": R_OUT * s,
            },
            "volume_m3": volume,
            "thickness_m": None,
            "wedge_angle_deg": ANGLE_DEG,
        }
    )
    spec["boundaries"] = [
        {"name": n, "role": "x", "description": "x"}
        for n in ("inlet", "outlet", "inner", "outer", "wedgeNegative", "wedgePositive")
    ]
    return CaseSpec.model_validate(spec)


def test_mesh_stage_passes_a_thin_wedge_annulus(tmp_path):
    """R29: the one-cell wedge mesh has flat faces, so its largest y is R cos(a/2), not R."""
    ctx = make_ctx(tmp_path, ScriptedBackend([]))
    ctx.casespec = wedge_spec()
    run_geometry(ctx, WEDGE_GEOMETRY_SCRIPT)
    outcome = run_mesh(ctx, WEDGE_MESH_SCRIPT)
    assert outcome.ok, outcome
    assert outcome.attempts == 1

    # The measurement behind the expected box of the gate.
    log = checkmesh_log(ctx)
    numbers = re.search(r"Overall domain bounding box\s*\(([^)]*)\)\s*\(([^)]*)\)", log)
    assert numbers is not None
    low = [float(v) for v in numbers.group(1).split()]
    high = [float(v) for v in numbers.group(2).split()]
    c, s = math.cos(HALF), math.sin(HALF)
    cad = ctx.casespec.expected_bbox()
    assert low == pytest.approx([0.0, R_IN * c, -R_OUT * s], rel=1e-5, abs=1e-9)
    assert high == pytest.approx([LENGTH, R_OUT * c, R_OUT * s], rel=1e-5)
    assert low[1] == pytest.approx(cad[1], rel=1e-5)  # the plan's y minimum is already r cos(a/2)
    assert high[1] < cad[4]  # but its y maximum is R: the mesh does not reach it
    assert (cad[4] - high[1]) > 1e-2 * (cad[4] - cad[1])  # more than the tolerance of the gate


def test_the_unscaled_plan_box_would_have_failed_the_thin_wedge(tmp_path):
    """The check behind R29: with the plan's own box the thin annulus is rejected."""
    from cfdagent.executor.protocol import CheckMeshArgs

    ctx = make_ctx(tmp_path, ScriptedBackend([]))
    ctx.casespec = wedge_spec()
    run_geometry(ctx, WEDGE_GEOMETRY_SCRIPT)
    assert run_mesh(ctx, WEDGE_MESH_SCRIPT).ok
    plan_box = list(ctx.casespec.expected_bbox())
    response = ctx.sandbox.call(
        "check_mesh",
        CheckMeshArgs(case_dir="case", expected_bbox=plan_box),  # the promoted case
        60.0,
    )
    assert not response.ok
    assert "bounding box" in response.error


# ------------------------------------------------------------------ the prompt's grading helper


def test_the_prompts_grading_helper_clusters_cells_at_the_wall_on_opposite_edges():
    """Opposite edges of a block run in opposite directions; the helper must not care."""
    import gmsh

    from cfdagent.prompts import load_prompt

    block = re.search(r"```python\n(def graded.*?)```", load_prompt("mesh").text, re.S)
    assert block is not None
    namespace = {"gmsh": gmsh}
    exec(block.group(1), namespace)  # noqa: S102 - the repository's own prompt text

    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("block")
        gmsh.model.occ.addRectangle(0, 0, 0, 1, 1)
        gmsh.model.occ.synchronize()
        n_cells, ratio, wall = 10, 1.2, (0.5, 0.0, 0.0)  # the wall is the edge y = 0
        for _, curve in gmsh.model.getEntities(1):
            box = gmsh.model.getBoundingBox(1, curve)
            if box[3] - box[0] < 1e-6:  # a vertical edge: grade it towards y = 0
                namespace["graded"](curve, n_cells, ratio, wall)
            else:
                gmsh.model.mesh.setTransfiniteCurve(curve, n_cells + 1)
        gmsh.model.mesh.setTransfiniteSurface(1)
        gmsh.model.mesh.generate(2)
        for _, curve in gmsh.model.getEntities(1):
            box = gmsh.model.getBoundingBox(1, curve)
            if box[3] - box[0] >= 1e-6:
                continue
            _, coords, _ = gmsh.model.mesh.getNodes(1, curve, includeBoundary=True)
            ys = sorted(coords[1::3])
            assert len(ys) == n_cells + 1
            first, last = ys[1] - ys[0], ys[-1] - ys[-2]
            assert last / first == pytest.approx(ratio ** (n_cells - 1), rel=1e-6)
    finally:
        gmsh.finalize()
