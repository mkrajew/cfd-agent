"""run_mesh_script and convert_to_foam preconditions that need no OpenFOAM or Gmsh."""

import pytest

from cfdagent.errors import AgentInputError, ToolFailure
from cfdagent.tools.mesh import convert_to_foam, run_mesh_script
from cfdagent.tools.process import TAIL_MAX_BYTES, ToolResult


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


def _fake_gmsh_to_foam(monkeypatch, tmp_path):
    """Replace run_tool: pretend gmshToFoam wrote a polyMesh into the scratch case."""
    from cfdagent.tools import mesh

    def fake_run_tool(cmd, cwd, log_path, timeout_s=0.0):
        poly = cwd / "constant" / "polyMesh"
        poly.mkdir(parents=True)
        (poly / "points").write_text("points", encoding="utf-8")
        (poly / "faces").write_text("faces", encoding="utf-8")
        log_path.write_text("ok", encoding="utf-8")
        return ToolResult(ok=True, returncode=0, timed_out=False, log_path=log_path, tail="")

    monkeypatch.setattr(mesh, "run_tool", fake_run_tool)
    return mesh


def test_convert_copies_the_polymesh_and_leaves_no_skeleton(monkeypatch, tmp_path):
    _fake_gmsh_to_foam(monkeypatch, tmp_path)
    case = tmp_path / "case"
    assert convert_to_foam(tmp_path / "mesh.msh", case).ok
    assert sorted(p.name for p in (case / "constant" / "polyMesh").iterdir()) == ["faces", "points"]
    assert not (case / "system").exists()
    assert not list((case / "constant").glob("*.partial"))


def test_failed_copy_leaves_no_partial_polymesh(monkeypatch, tmp_path):
    mesh = _fake_gmsh_to_foam(monkeypatch, tmp_path)
    real_copytree = mesh.shutil.copytree

    def failing_copytree(src, dst, *args, **kwargs):
        real_copytree(src, dst, *args, **kwargs)
        raise OSError("disk full")

    monkeypatch.setattr(mesh.shutil, "copytree", failing_copytree)
    case = tmp_path / "case"
    with pytest.raises(ToolFailure, match="disk full"):
        convert_to_foam(tmp_path / "mesh.msh", case)
    assert not (case / "constant" / "polyMesh").exists()
    assert not list((case / "constant").glob("*.partial"))
