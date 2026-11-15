"""scripts/run_calibration.py: table, exit status, usage errors and solver-name extraction.

No Docker and no toolchain: the run itself is replaced by a fake ``calibrate``.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "run_calibration.py"


def load():
    spec = importlib.util.spec_from_file_location("run_calibration", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_calibration"] = module
    spec.loader.exec_module(module)
    return module


rc = load()

PASSING = {
    "quantity": "u_profile",
    "passed": True,
    "error": 0.0123,
    "threshold": 0.05,
    "reason": "within threshold",
    "at": 0.9766,
    "value": 0.6401,
    "reference": 0.6593,
}
FAILING = {
    "quantity": "v_profile",
    "passed": False,
    "error": 0.0712,
    "threshold": 0.05,
    "reason": "exceeds threshold",
    "at": 0.1,
    "value": -0.2,
    "reference": -0.27,
}


def result(*criteria, failure=None, **extra):
    return rc.CalibrationResult(
        passed=bool(criteria) and all(c["passed"] for c in criteria) and failure is None,
        criteria=list(criteria),
        wall_seconds=12.5,
        failure=failure,
        **extra,
    )


@pytest.fixture
def fixture_dir(tmp_path):
    directory = tmp_path / "fixture"
    directory.mkdir()
    (directory / "fixture.json").write_text(json.dumps({"expected_bbox": [0, 0, 0, 1, 1, 0.1]}))
    return directory


def test_solver_name_is_read_from_the_application_entry():
    text = "FoamFile { }\n// application icoFoam;\napplication     simpleFoam;  // steady\n"
    assert rc.solver_name(text) == "simpleFoam"


def test_solver_name_missing_is_an_error():
    with pytest.raises(rc.CalibrationUsageError, match="application"):
        rc.solver_name("FoamFile { }\nendTime 10;\n")


def test_report_has_one_row_per_criterion_with_all_columns():
    text = rc.format_report(
        result(
            PASSING,
            FAILING,
            n_cells=4096,
            solver="simpleFoam",
            case_end_time=5000.0,
            end_time=812.0,
            converged_at=812.0,
            solver_wall_seconds=30.2,
        )
    )
    for column in ("criterion", "value", "reference", "error", "threshold", "pass"):
        assert column in text
    row_u = next(line for line in text.splitlines() if "u_profile" in line)
    assert "0.0123" in row_u and "0.05" in row_u and "yes" in row_u.lower()
    assert "0.6401" in row_u and "0.6593" in row_u and "0.9766" in row_u
    row_v = next(line for line in text.splitlines() if "v_profile" in line)
    assert "no" in row_v.lower().split()[-1]
    assert "4096" in text and "simpleFoam" in text
    assert "residualControl" in text  # states whether residualControl ended the run
    assert "812" in text and "12.5" in text and "30.2" in text


def test_report_without_value_columns_says_so():
    bare = {"quantity": "q", "passed": True, "error": 0.01, "threshold": 0.05, "reason": "ok"}
    row = next(ln for ln in rc.format_report(result(bare)).splitlines() if ln.startswith("q "))
    cells = row.split()  # quantity, at, value, reference, error, threshold, pass
    assert cells[1:4] == ["-", "-", "-"]


def test_report_prints_the_reason_of_every_failed_criterion():
    odd = dict(FAILING, quantity="w_profile", reason="sim_x contains duplicate x values")
    text = rc.format_report(result(PASSING, FAILING, odd))
    assert "v_profile: exceeds threshold" in text
    assert "w_profile: sim_x contains duplicate x values" in text
    assert "u_profile: within threshold" not in text  # passing criteria need no explanation


def test_integer_valued_numbers_print_as_integers():
    assert rc._fmt(20000.0) == "20000"
    assert rc._fmt(0.5) == "0.5"


def test_report_shows_the_failed_step():
    text = rc.format_report(result(failure="solver: floating point exception"))
    assert "floating point exception" in text
    assert "FAILED" in text


def test_residual_control_ended_the_run_only_below_the_cap():
    assert result(PASSING, case_end_time=5000.0, converged_at=812.0).ended_by_residual_control
    assert not result(PASSING, case_end_time=5000.0, converged_at=None).ended_by_residual_control
    assert not result(PASSING, case_end_time=5000.0, converged_at=5000.0).ended_by_residual_control


@pytest.fixture
def fake_calibrate(monkeypatch):
    calls = []

    def install(outcome):
        def fake(fixture, benchmark_id, **kwargs):
            calls.append((fixture, benchmark_id, kwargs))
            return outcome

        monkeypatch.setattr(rc, "calibrate", fake)
        return calls

    return install


def test_exit_code_is_zero_only_when_every_criterion_passes(fixture_dir, fake_calibrate, capsys):
    calls = fake_calibrate(result(PASSING, PASSING))
    assert rc.main([str(fixture_dir), "--benchmark", "cavity-re100", "--n-procs", "2"]) == 0
    assert calls[0][2]["n_procs"] == 2
    assert "u_profile" in capsys.readouterr().out


def test_exit_code_is_one_when_a_criterion_fails(fixture_dir, fake_calibrate):
    fake_calibrate(result(PASSING, FAILING))
    assert rc.main([str(fixture_dir), "--benchmark", "cavity-re100"]) == 1


def test_exit_code_is_one_when_a_step_failed(fixture_dir, fake_calibrate):
    fake_calibrate(result(failure="mesh: gate failed"))
    assert rc.main([str(fixture_dir), "--benchmark", "cavity-re100"]) == 1


def test_missing_fixture_directory_is_a_usage_error(tmp_path, capsys):
    assert rc.main([str(tmp_path / "nope"), "--benchmark", "cavity-re100"]) == 2
    assert "not a directory" in capsys.readouterr().err


def test_unknown_benchmark_is_a_usage_error(fixture_dir, capsys):
    assert rc.main([str(fixture_dir), "--benchmark", "no-such-case"]) == 2
    err = capsys.readouterr().err
    assert "no-such-case" in err and "cavity-re100" in err  # lists the known ids


def test_missing_fixture_json_is_a_usage_error(tmp_path, capsys):
    bare = tmp_path / "bare"
    bare.mkdir()
    assert rc.main([str(bare), "--benchmark", "cavity-re100"]) == 2
    assert "fixture.json" in capsys.readouterr().err


def test_fixture_json_without_a_valid_bbox_is_a_usage_error(tmp_path, capsys):
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "fixture.json").write_text(json.dumps({"expected_bbox": [0, 1]}))
    assert rc.main([str(bad), "--benchmark", "cavity-re100"]) == 2
    assert "expected_bbox" in capsys.readouterr().err


def test_profile_detail_locates_the_largest_error(tmp_path):
    reference = tmp_path / "ref.csv"
    reference.write_text("x,value\n0,0\n0.5,1\n1,0\n")
    detail = rc.profile_detail(
        {"x": [0.0, 0.5, 1.0], "value": [0.0, 0.75, 0.0]}, reference, normalise_by=1.0
    )
    assert detail == {"at": 0.5, "value": 0.75, "reference": 1.0}


def test_script_puts_the_repository_src_first_on_sys_path():
    """A stale installed ``cfdagent`` must not win over the checkout next to the script."""
    stale = Path(__file__).parent / "_not_a_package"  # never imported: only the name matters
    code = "\n".join(
        [
            "import importlib.util, sys",
            f"sys.path.insert(0, {str(stale)!r})",
            f"spec = importlib.util.spec_from_file_location('rc', {str(SCRIPT)!r})",
            "module = importlib.util.module_from_spec(spec)",
            "sys.modules['rc'] = module",
            "spec.loader.exec_module(module)",
            "import cfdagent",
            "print(sys.path[0]); print(cfdagent.__file__)",
        ]
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    assert Path(out[0]) == REPO / "src"
    assert (REPO / "src") in Path(out[1]).parents


def load_post():
    path = REPO / "tests" / "fixtures" / "handwritten" / "cavity_re1000" / "post.py"
    spec = importlib.util.spec_from_file_location("cavity_post", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_sample(case_dir, rows):
    """A raw ``sets`` file with columns y, Ux, Uy, Uz at time directory 20000."""
    sample_dir = case_dir / "postProcessing" / "centrelines" / "20000"
    sample_dir.mkdir(parents=True)
    text = "".join(f"{y} {ux} 0 0" + "\n" for y, ux in rows)
    (sample_dir / "verticalCentreline_U.xy").write_text("# y Ux Uy Uz" + "\n" + text)


def test_fixture_post_drops_exact_duplicate_rows_of_a_parallel_sample(tmp_path):
    """A point on a processor interface is written by both ranks; the profile keeps it once."""
    write_sample(tmp_path, [(0, 0), (0.5, 0.25), (0.5, 0.25), (1, 1)])
    profile = load_post().centreline_profile(tmp_path, "verticalCentreline", "y", "Ux")
    assert profile["x"] == [0.0, 0.5, 1.0]
    assert profile["value"] == [0.0, 0.25, 1.0]


def test_fixture_post_keeps_rows_with_equal_position_but_different_value(tmp_path):
    """Only exact duplicates go: a real disagreement must still reach the Scorer."""
    write_sample(tmp_path, [(0, 0), (0.5, 0.1), (0.5, 0.2)])
    profile = load_post().centreline_profile(tmp_path, "verticalCentreline", "y", "Ux")
    assert profile["x"] == [0.0, 0.5, 0.5]
