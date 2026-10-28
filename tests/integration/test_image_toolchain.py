"""The Docker image provides the full toolchain: OpenFOAM ESI, Gmsh, CadQuery, PyVista."""

import os
import shutil
import sys

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "exe",
    [
        "icoFoam",
        "simpleFoam",
        "pimpleFoam",
        "gmshToFoam",
        "checkMesh",
        "foamDictionary",
        "decomposePar",
        "postProcess",
    ],
)
def test_openfoam_executable_available(exe):
    assert shutil.which(exe), exe


def test_openfoam_is_esi():
    assert os.environ["WM_PROJECT_VERSION"].startswith("v")  # ESI versions look like v2512


def test_python_toolchain_imports():
    import cadquery  # noqa: F401
    import gmsh  # noqa: F401
    import matplotlib  # noqa: F401
    import pyvista  # noqa: F401

    assert sys.version_info[:2] == (3, 12)


def test_cfdagent_installed_in_image():
    import cfdagent

    assert cfdagent.__version__


def test_runs_as_non_root():
    assert os.getuid() != 0


def test_gmsh_initializes_headless():
    import gmsh

    gmsh.initialize()
    try:
        gmsh.model.add("t")
        gmsh.model.occ.addBox(0, 0, 0, 1, 1, 1)
        gmsh.model.occ.synchronize()
        gmsh.model.mesh.generate(3)
        assert len(gmsh.model.mesh.getNodes()[0]) > 8
    finally:
        gmsh.finalize()


def test_pyvista_offscreen_render(tmp_path):
    import pyvista as pv

    plotter = pv.Plotter(off_screen=True, window_size=(320, 240))
    plotter.add_mesh(pv.Sphere())
    png = tmp_path / "sphere.png"
    image = plotter.screenshot(png, return_img=True)
    plotter.close()

    assert png.is_file()
    assert png.stat().st_size > 1024
    assert image.std() > 0  # not a uniform (blank) frame
