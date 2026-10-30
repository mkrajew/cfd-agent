"""The Docker image provides the full toolchain: OpenFOAM ESI, Gmsh, CadQuery, PyVista."""

import os
import shutil
import subprocess
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


def test_cfdagent_installed_in_image(tmp_path):
    # pytest puts the mounted /work/src first (pythonpath), so check in a clean interpreter:
    # -I ignores PYTHONPATH and the current directory, tmp_path is outside the repository.
    result = subprocess.run(
        [sys.executable, "-I", "-c", "import cfdagent; print(cfdagent.__file__)"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip().startswith("/opt/venv/"), result.stdout


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


# --- Case-supplied code must be refused site-wide (allowSystemOperations 0) ---------------------

REFUSAL = "case-supplied code"  # OpenFOAM's own wording when dynamic code is not allowed
FOAM_HEADER = "FoamFile { version 2.0; format ascii; class dictionary; object d; }\n"


def _foam_dictionary(path, *extra, cwd=None):
    return subprocess.run(
        ["foamDictionary", *extra, str(path)],
        cwd=cwd or path.parent,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_system_operations_switch_is_off():
    listed = subprocess.run(["icoFoam", "-listSwitches"], capture_output=True, text=True)
    assert listed.returncode == 0, listed.stderr
    line = next((x for x in listed.stdout.splitlines() if "allowSystemOperations" in x), "")
    assert "Disallowing user-supplied system call operations" in line, listed.stdout[-500:]


def test_codestream_refused_site_wide(tmp_path):
    dictionary = tmp_path / "hostile"
    dictionary.write_text(
        FOAM_HEADER
        + "trap #codeStream\n{\n    codeInclude #{ #include <fstream> #};\n"
        + f'    code #{{ std::ofstream("{tmp_path}/EXECUTED") << "ran"; os << 1; #}};\n}};\n',
        encoding="utf-8",
    )
    done = _foam_dictionary(dictionary)
    assert done.returncode != 0
    assert REFUSAL in done.stdout + done.stderr
    assert not (tmp_path / "EXECUTED").exists()
    assert not (tmp_path / "dynamicCode").exists()  # nothing was even generated for compiling


def test_calc_is_refused_site_wide(tmp_path):
    dictionary = tmp_path / "hostile"
    dictionary.write_text(
        FOAM_HEADER + f'x #calc "system(\\"touch {tmp_path}/EXECUTED\\")";\n', encoding="utf-8"
    )
    done = _foam_dictionary(dictionary)
    assert done.returncode != 0
    assert REFUSAL in done.stdout + done.stderr
    assert not (tmp_path / "EXECUTED").exists()


def test_eval_still_works_with_system_operations_off(tmp_path):
    dictionary = tmp_path / "plain"
    dictionary.write_text(FOAM_HEADER + 'x #eval "1 + 2";\n', encoding="utf-8")
    done = _foam_dictionary(dictionary, "-entry", "x", "-value")
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.strip() == "3"


def test_user_level_controldict_could_reenable_the_switch_but_nothing_can_compile(tmp_path):
    """HOME must stay out of agent-writable places (a ~/.OpenFOAM/controlDict beats the site file).

    Second line of defence: the image has no compiler, so even an enabled switch cannot run code.
    """
    home = tmp_path / "home"
    (home / ".OpenFOAM").mkdir(parents=True)
    (home / ".OpenFOAM" / "controlDict").write_text(
        "InfoSwitches { allowSystemOperations 1; }\n", encoding="utf-8"
    )
    environment = {**os.environ, "HOME": str(home)}
    listed = subprocess.run(
        ["icoFoam", "-listSwitches"],
        capture_output=True,
        text=True,
        errors="replace",
        env=environment,
    )
    assert "Allowing user-supplied system call operations" in listed.stdout  # the override works
    assert shutil.which("gcc") is None and shutil.which("g++") is None
    dictionary = tmp_path / "hostile"
    dictionary.write_text(
        FOAM_HEADER + f'x #calc "system(\\"touch {tmp_path}/EXECUTED\\")";\n', encoding="utf-8"
    )
    done = subprocess.run(
        ["foamDictionary", str(dictionary)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        errors="replace",
        env=environment,
    )
    assert done.returncode != 0
    assert not (tmp_path / "EXECUTED").exists()
