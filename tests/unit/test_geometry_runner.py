"""run_geometry_script must never leave outputs of an earlier run in place."""

from cfdagent.tools.geometry import GEOMETRY_LOG_NAME, STEP_FILE_NAME, run_geometry_script

MANIFEST = "boundary_manifest.json"


def _script(tmp_path, body):
    script = tmp_path / "geometry_script.py"
    script.write_text(body, encoding="utf-8")
    return script


def test_geometry_script_exiting_zero_without_outputs_is_not_ok(tmp_path):
    result = run_geometry_script(_script(tmp_path, "print('done')\n"), tmp_path / "w")
    assert not result.ok
    assert STEP_FILE_NAME in result.tail
    assert MANIFEST in result.tail
    log = (tmp_path / "w" / GEOMETRY_LOG_NAME).read_text(encoding="utf-8")
    assert STEP_FILE_NAME in log and MANIFEST in log


def test_geometry_script_writing_only_one_output_is_not_ok(tmp_path):
    body = "open('geometry.step', 'w').write('x')\n"
    result = run_geometry_script(_script(tmp_path, body), tmp_path / "w")
    assert not result.ok
    assert MANIFEST in result.tail


def test_geometry_script_writing_both_outputs_is_ok(tmp_path):
    body = (
        "open('geometry.step', 'w').write('x')\nopen('boundary_manifest.json', 'w').write('{}')\n"
    )
    result = run_geometry_script(_script(tmp_path, body), tmp_path / "w")
    assert result.ok, result.tail


def test_geometry_runner_removes_stale_outputs(tmp_path):
    workdir = tmp_path / "w"
    workdir.mkdir()
    (workdir / STEP_FILE_NAME).write_text("stale", encoding="utf-8")
    (workdir / MANIFEST).write_text("stale", encoding="utf-8")
    result = run_geometry_script(_script(tmp_path, "print('writes nothing')\n"), workdir)
    assert not result.ok
    assert not (workdir / STEP_FILE_NAME).exists()
    assert not (workdir / MANIFEST).exists()


def test_geometry_script_failure_keeps_its_own_error(tmp_path):
    result = run_geometry_script(_script(tmp_path, "raise RuntimeError('boom')\n"), tmp_path / "w")
    assert not result.ok
    assert "RuntimeError" in result.tail
