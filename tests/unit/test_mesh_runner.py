"""run_mesh_script and convert_to_foam preconditions that need no OpenFOAM or Gmsh."""

import pytest

from cfdagent.errors import AgentInputError, ToolFailure
from cfdagent.tools.mesh import convert_to_foam, run_mesh_script
from cfdagent.tools.process import TAIL_MAX_BYTES


def test_mesh_script_missing_output_gives_a_bounded_tail(tmp_path):
    script = tmp_path / "mesh_script.py"
    script.write_text("import sys\nsys.stdout.write('x' * 3_000_000)\n", encoding="utf-8")
    result = run_mesh_script(script, tmp_path / "w")
    assert not result.ok
    assert len(result.tail.encode("utf-8")) <= TAIL_MAX_BYTES
    assert "mesh.msh" in result.tail  # the reason is at the end of the log


def test_mesh_script_failure_with_stale_output_removed(tmp_path):
    workdir = tmp_path / "w"
    workdir.mkdir()
    (workdir / "mesh.msh").write_text("stale", encoding="utf-8")
    script = tmp_path / "mesh_script.py"
    script.write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    assert not run_mesh_script(script, workdir).ok
    assert not (workdir / "mesh.msh").exists()


def test_convert_to_foam_into_an_existing_mesh_is_a_tool_failure(tmp_path):
    (tmp_path / "case" / "constant" / "polyMesh").mkdir(parents=True)
    with pytest.raises(ToolFailure) as exc:
        convert_to_foam(tmp_path / "mesh.msh", tmp_path / "case")
    assert not isinstance(exc.value, AgentInputError)
    assert not (tmp_path / "case" / "system").exists()  # nothing was created before refusing
