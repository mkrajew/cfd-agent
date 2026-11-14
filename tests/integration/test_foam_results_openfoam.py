"""``cfdagent.tools.foam_results`` against OpenFOAM itself, inside the image.

A small 2D case is solved with the real solver. The geometry is compared with what
``postProcess -func writeCellCentres/writeCellVolumes`` writes, the fields with PyVista's OpenFOAM
reader (an independent reader), the sampled values with the cell values they must reproduce, and
a post-processing script like the one in the Analyst prompt runs in a clean working directory
the way the gate runs it.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from foam_cases import BLOCK_MESH_DICT, CONTROL_DICT, build_case

from cfdagent.tools.foam_results import (
    FoamResultsError,
    load_case,
    read_function_object,
    read_solver_log,
)

pytestmark = pytest.mark.integration

SKEWED_BLOCK_MESH = BLOCK_MESH_DICT.replace(
    "(0 0 0) (1 0 0) (1 1 0) (0 1 0)", "(0 0 0) (1 0 0) (1.3 1 0) (0.3 1 0)"
).replace("(0 0 0.1) (1 0 0.1) (1 1 0.1) (0 1 0.1)", "(0 0 0.1) (1 0 0.1) (1.3 1 0.1) (0.3 1 0.1)")
SKEWED_BLOCK_MESH = SKEWED_BLOCK_MESH.replace(
    "(20 20 1) simpleGrading (1 1 1)", "(10 6 1) simpleGrading (4 0.5 1)"
)


def run(command, cwd):
    done = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    assert done.returncode == 0, done.stdout[-1500:] + done.stderr[-1500:]
    return done


@pytest.fixture(scope="module")
def solved(tmp_path_factory):
    """A solved 20 x 20 x 1 transient case with the cell centres and volumes OpenFOAM writes."""
    root = tmp_path_factory.mktemp("solved")
    case = build_case(root, end_time=0.25)
    log = run(["icoFoam"], case).stdout
    (case / "log.icoFoam").write_text(log, encoding="utf-8")
    for function in ("writeCellCentres", "writeCellVolumes"):
        run(["postProcess", "-func", function, "-latestTime"], case)
    return case


@pytest.fixture(scope="module")
def skewed(tmp_path_factory):
    """A sheared and graded mesh (non-orthogonal, unequal cells), geometry from OpenFOAM only."""
    case = build_case(tmp_path_factory.mktemp("skewed"), end_time=0.01)
    (case / "system" / "blockMeshDict").write_text(SKEWED_BLOCK_MESH, encoding="utf-8")
    shutil.rmtree(case / "constant" / "polyMesh")
    run(["blockMesh"], case)
    for function in ("writeCellCentres", "writeCellVolumes"):
        run(["postProcess", "-func", function, "-time", "0"], case)
    return case


def test_times_and_field_sizes(solved):
    case = load_case(solved)
    assert case.times == [0.0, 0.25]
    assert case.latest_time == 0.25
    assert case.n_cells == 400
    assert case.read_field("U").shape == (400, 3)
    assert case.read_field("p").shape == (400,)
    assert case.read_field("U", time=0).shape == (400, 3)
    assert {"U", "p", "phi", "C", "V"} <= set(case.list_fields())


def test_cell_geometry_matches_openfoam_on_a_regular_mesh(solved):
    case = load_case(solved)
    assert case.cell_centres == pytest.approx(case.read_field("C"), abs=1e-7)
    assert case.cell_volumes == pytest.approx(case.read_field("V"), rel=1e-6)
    assert case.cell_volumes.sum() == pytest.approx(1.0 * 1.0 * 0.1)
    assert case.is_2d
    low, high = case.bounds
    assert low == pytest.approx([0, 0, 0]) and high == pytest.approx([1, 1, 0.1])


def test_cell_geometry_matches_openfoam_on_a_sheared_graded_mesh(skewed):
    case = load_case(skewed)
    assert case.n_cells == 60
    # OpenFOAM writes 8 significant digits (writePrecision 8)
    assert case.cell_centres == pytest.approx(case.read_field("C", time=0), rel=1e-6, abs=1e-8)
    assert case.cell_volumes == pytest.approx(case.read_field("V", time=0), rel=1e-6)
    assert np.ptp(case.cell_volumes) > 0.5 * case.cell_volumes.min()  # unequal cells


def test_fields_agree_with_pyvista_reader(solved, tmp_path):
    """The independent reader: an empty ``<name>.foam`` file next to ``system/`` is how PyVista
    opens a case (opening the directory or ``controlDict`` fails)."""
    import pyvista as pv

    copy = tmp_path / "case"
    shutil.copytree(solved, copy)
    (copy / "case.foam").touch()
    reader = pv.OpenFOAMReader(str(copy / "case.foam"))
    reader.cell_to_point_creation = False  # keep the cell values, no point data next to them
    reader.set_active_time_value(0.25)
    internal = reader.read()["internalMesh"]

    case = load_case(solved)
    assert internal.n_cells == case.n_cells
    # VTK holds the values as 32-bit floats
    assert case.read_field("p") == pytest.approx(internal.cell_data["p"], abs=1e-6)
    assert case.read_field("U") == pytest.approx(internal.cell_data["U"], abs=1e-6)
    assert case.cell_centres == pytest.approx(internal.cell_centers().points, abs=1e-6)


def test_patch_values_follow_the_boundary_conditions(solved):
    case = load_case(solved)
    assert set(case.patches) == {"movingWall", "fixedWalls", "frontAndBack"}
    moving = case.read_patch_field("U", "movingWall")
    assert moving.shape == (20, 3)
    assert moving == pytest.approx(np.tile([1.0, 0.0, 0.0], (20, 1)))
    assert case.read_patch_field("U", "fixedWalls") == pytest.approx(np.zeros((60, 3)))
    # zeroGradient pressure on the walls: the value of the adjacent cell
    p = case.read_field("p")
    wall = case.patches["fixedWalls"]
    assert case.read_patch_field("p", "fixedWalls") == pytest.approx(p[wall.owner_cells])
    assert case.patches["movingWall"].face_centres[:, 1] == pytest.approx(np.ones(20))
    # the lid's outward area vectors point up and add up to the lid area times the thickness
    assert case.patches["movingWall"].face_areas.sum(axis=0) == pytest.approx([0, 0.1, 0])
    # the net flux through all patches of the incompressible solution is zero
    flux = sum(
        case.read_patch_field("phi", name).sum()
        for name, patch in case.patches.items()
        if patch.type != "empty"
    )
    assert flux == pytest.approx(0.0, abs=1e-12)


def test_sampling_at_cell_centres_returns_the_cell_values(solved):
    case = load_case(solved)
    u = case.read_field("U")
    column = np.arange(10, 400, 20)  # cells of the column x = 0.525, bottom to top
    centres = case.cell_centres[column]
    for method in ("linear", "nearest"):
        line = case.sample_line(
            "U", centres[0], centres[-1], n_points=1 + 19, time=0.25, method=method
        )
        assert line.inside.all()
        # the sample points are the cell centres (equal spacing on a uniform mesh)
        assert line.points == pytest.approx(centres, abs=1e-12)
        assert line.value == pytest.approx(u[column], abs=1e-9)
        assert line.s[-1] == pytest.approx(0.95)


def test_linear_sampling_reaches_the_lid_and_the_floor(solved):
    case = load_case(solved)
    u = case.read_field("U")
    line = case.sample_line("U", (0.525, 0.0), (0.525, 1.0), n_points=41, method="linear")
    assert line.inside.all()  # both end points lie on the boundary of the mesh
    # next to the lid the interpolation approaches the lid speed (the patch value is in the fit)
    assert line.value[-1, 0] == pytest.approx(1.0)  # on the lid: the patch value itself
    assert line.value[0, 0] == pytest.approx(0.0)  # no-slip floor
    # no overshoot beyond the range of the cell values and the lid speed
    assert line.value[:, 0].max() <= 1.0 + 1e-9
    assert line.value[:, 0].min() >= u[:, 0].min() - 0.05


def test_solver_log_summary(solved):
    log = read_solver_log(solved)
    assert log.path.name == "log.icoFoam"
    assert log.solver == "icoFoam"
    assert log.n_time_steps == 50
    assert log.last_time == pytest.approx(0.25)
    assert log.completed
    assert log.converged_at is None
    assert {"Ux", "Uy", "p"} <= set(log.initial_residuals)
    assert 0 < log.final_residuals["p"] < 1e-3


POST_SCRIPT = '''"""A post-processing script the way the Analyst prompt shows it."""
from pathlib import Path

import numpy as np

from cfdagent.tools.foam_results import load_case, read_solver_log
from cfdagent.tools.post import write_quantities

case = load_case("case")
print("times", case.times, "latest", case.latest_time)
sample = case.sample_line("U", (0.5, 0.0), (0.5, 1.0), n_points=21).drop_outside()
order = np.argsort(sample.s)
log = read_solver_log("case")
write_quantities(
    Path("quantities.json"),
    {
        "profile": {"x": sample.s[order] / 1.0, "value": sample.value[order, 0]},
        "iterations": log.n_time_steps,
    },
)
'''


def test_a_post_script_runs_in_a_clean_working_directory(solved, tmp_path):
    shutil.copytree(solved, tmp_path / "case")
    (tmp_path / "post.py").write_text(POST_SCRIPT, encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[2] / "src")}
    done = subprocess.run(
        [sys.executable, "post.py"], cwd=tmp_path, capture_output=True, text=True, env=env
    )
    assert done.returncode == 0, done.stdout + done.stderr
    data = json.loads((tmp_path / "quantities.json").read_text(encoding="utf-8"))
    assert data["iterations"] == 50
    assert len(data["profile"]["x"]) == 21
    assert data["profile"]["x"] == sorted(data["profile"]["x"])


FOAM_HEADER = "FoamFile { version 2.0; format ascii; class dictionary; object %s; }\n"

FUNCTIONS = """
functions
{
    wallForces
    {
        type forces; libs (forces); patches (movingWall fixedWalls);
        rho rhoInf; rhoInf 1; CofR (0.5 0.5 0.05);
        writeControl timeStep; writeInterval 1;
    }
    wallCoeffs
    {
        type forceCoeffs; libs (forces); patches (movingWall fixedWalls);
        rho rhoInf; rhoInf 1; CofR (0.5 0.5 0.05);
        liftDir (0 1 0); dragDir (1 0 0); pitchAxis (0 0 1); magUInf 1; lRef 1; Aref 0.1;
        writeControl timeStep; writeInterval 1;
    }
    probePoints
    {
        type probes; libs (sampling); fields (p U);
        probeLocations ((0.5 0.5 0.05) (0.2 0.2 0.05));
        writeControl timeStep; writeInterval 1;
    }
}
"""

PIMPLE_SOLUTION = """solvers
{
    p { solver PCG; preconditioner DIC; tolerance 1e-8; relTol 0.05; }
    pFinal { $p; relTol 0; }
    "(U|UFinal)" { solver smoothSolver; smoother symGaussSeidel; tolerance 1e-8; relTol 0; }
}
PIMPLE { nOuterCorrectors 1; nCorrectors 2; nNonOrthogonalCorrectors 0; pRefCell 0; pRefValue 0; }
"""


@pytest.fixture(scope="module")
def forces_case(tmp_path_factory):
    """A few pimpleFoam steps of the small 2D case with forces, forceCoeffs and probes."""
    case = build_case(tmp_path_factory.mktemp("forces"), end_time=0.02)
    control = CONTROL_DICT.format(end_time=0.02).replace("icoFoam", "pimpleFoam")
    (case / "system" / "controlDict").write_text(control + FUNCTIONS, encoding="utf-8")
    (case / "system" / "fvSolution").write_text(
        FOAM_HEADER % "fvSolution" + PIMPLE_SOLUTION, encoding="utf-8"
    )
    schemes = case / "system" / "fvSchemes"
    schemes.write_text(
        schemes.read_text(encoding="utf-8").replace(
            "div(phi,U)      Gauss linear;",
            "div(phi,U)      Gauss linear;\n    div((nuEff*dev2(T(grad(U))))) Gauss linear;",
        ),
        encoding="utf-8",
    )
    (case / "constant" / "transportProperties").write_text(
        FOAM_HEADER % "transportProperties" + "transportModel Newtonian;\nnu 0.01;\n",
        encoding="utf-8",
    )
    (case / "constant" / "turbulenceProperties").write_text(
        FOAM_HEADER % "turbulenceProperties" + "simulationType laminar;\n", encoding="utf-8"
    )
    run(["pimpleFoam"], case)
    return case


def test_forces_table_from_real_pimplefoam_run(forces_case):
    with pytest.raises(FoamResultsError, match="force.dat") as ambiguous:
        read_function_object(forces_case, "wallForces")  # force.dat and moment.dat
    assert "moment.dat" in str(ambiguous.value)
    forces = read_function_object(forces_case, "wallForces", file="force.dat")
    assert forces.columns == [
        "Time",
        "total_x", "total_y", "total_z",
        "pressure_x", "pressure_y", "pressure_z",
        "viscous_x", "viscous_y", "viscous_z",
    ]  # fmt: skip
    assert forces.column("Time") == pytest.approx([0.005, 0.01, 0.015, 0.02])
    total, pressure, viscous = (forces.column(f"{n}_x") for n in ("total", "pressure", "viscous"))
    assert total == pytest.approx(pressure + viscous, rel=1e-6, abs=1e-9)
    assert np.abs(viscous).max() > 0  # the lid drags on the fluid: a real, non-zero force
    assert forces.column("total_z") == pytest.approx(np.zeros(4), abs=1e-12)  # 2D case

    # the coefficients file of the same run: Cd = Fx / (0.5 rho U^2 Aref), names with parentheses
    coefficients = read_function_object(forces_case, "wallCoeffs", file="coefficient")
    assert {"Cd", "Cd(f)", "Cd(r)", "Cl"} <= set(coefficients.columns)
    assert coefficients.column("Time") == pytest.approx(forces.column("Time"))
    assert coefficients.column("Cd") == pytest.approx(total / (0.5 * 0.1), rel=1e-6)


def test_probe_tables_from_real_pimplefoam_run(forces_case):
    vectors = read_function_object(forces_case, "probePoints", file="U")
    assert vectors.columns == ["Time", "0_x", "0_y", "0_z", "1_x", "1_y", "1_z"]
    assert vectors.column("0_x").shape == (4,) and np.isfinite(vectors.data).all()
    scalars = read_function_object(forces_case, "probePoints", file="p")
    assert scalars.columns == ["Time", "0", "1"]
    assert scalars.column("Time") == pytest.approx(vectors.column("Time"))
