"""Solver log parsing and run_solver orchestration; logs captured from icoFoam in the image."""

import math
from pathlib import Path

import pytest

from cfdagent.tools import foam
from cfdagent.tools.foam import SolverLogSummary, parse_solver_log, run_solver
from cfdagent.tools.process import ToolNotAllowed, ToolResult

LOGS = Path(__file__).parents[1] / "fixtures" / "logs"


def _read(name):
    return (LOGS / name).read_text(encoding="utf-8")


def test_parse_solver_log_ok():
    summary = parse_solver_log(_read("icoFoam_ok.log"))
    assert isinstance(summary, SolverLogSummary)
    assert summary.completed
    assert not summary.diverged
    assert summary.fatal_error is None
    assert summary.end_time == 0.5
    assert {"Ux", "Uy", "p"} <= set(summary.final_residuals)
    assert all(math.isfinite(v) for v in summary.final_residuals.values())
    assert summary.failure_reason is None


def test_ok_log_header_does_not_look_like_a_floating_point_exception():
    # Every OpenFOAM header says "Floating point exception trapping enabled (FOAM_SIGFPE)".
    assert "Floating point exception" in _read("icoFoam_ok.log")
    assert not parse_solver_log(_read("icoFoam_ok.log")).diverged


def test_final_residual_is_the_last_one_of_the_last_step():
    summary = parse_solver_log(_read("icoFoam_ok.log"))
    # The last step solves p twice (two PISO correctors); the second one is reported.
    assert summary.final_residuals["p"] == pytest.approx(9.59103e-07)
    assert summary.final_residuals["Ux"] == pytest.approx(2.3091e-07)
    assert summary.final_residuals["Uy"] == pytest.approx(5.0684e-07)


def test_courant_is_the_maximum_over_the_run():
    summary = parse_solver_log(_read("icoFoam_ok.log"))
    assert summary.max_courant is not None
    assert 0.5 < summary.max_courant < 1.0


def test_max_courant_is_none_without_courant_lines():
    log = (
        "Time = 1\n\n"
        "GAMG:  Solving for p, Initial residual = 1, Final residual = 0.1, No Iterations 3\n\n"
        "End\n"
    )
    summary = parse_solver_log(log)
    assert summary.max_courant is None
    assert summary.completed
    assert summary.end_time == 1.0


def test_parse_solver_log_fatal_error():
    summary = parse_solver_log(_read("icoFoam_fatal.log"))
    assert not summary.completed
    assert summary.fatal_error is not None
    assert "noSlipp" in summary.fatal_error
    # Only the message, not the list of valid types or the "From ..." location.
    assert "SRFVelocity" not in summary.fatal_error
    assert "From " not in summary.fatal_error
    assert summary.end_time is None
    assert summary.failure_reason is not None
    assert "noSlipp" in summary.failure_reason


def test_parse_solver_log_fatal_error_plain_variant():
    log = (
        "Time = 3\n\n"
        "--> FOAM FATAL ERROR: (openfoam-2512)\n"
        'Cannot find file "points" in directory "polyMesh"\n'
        "second line of message\n\n"
        "    From Foam::Foo\n    in file foo.C at line 1.\n\nFOAM exiting\n"
    )
    summary = parse_solver_log(log)
    assert not summary.completed
    assert summary.fatal_error == (
        'Cannot find file "points" in directory "polyMesh"\nsecond line of message'
    )


def test_fatal_error_message_on_the_marker_line():
    summary = parse_solver_log("--> FOAM FATAL ERROR: boom on same line\n\n    From x\n")
    assert summary.fatal_error == "boom on same line"


def test_fatal_error_stops_at_a_from_line_without_blank_line():
    summary = parse_solver_log("--> FOAM FATAL ERROR: (openfoam-2512)\nboom\n    From x\n")
    assert summary.fatal_error == "boom"


def test_fatal_error_beats_a_trailing_end_line():
    summary = parse_solver_log("--> FOAM FATAL ERROR:\nboom\n\n    From x\n\nEnd\n")
    assert not summary.completed
    assert summary.fatal_error == "boom"


def test_fatal_error_without_message_is_still_reported():
    summary = parse_solver_log("--> FOAM FATAL ERROR: (openfoam-2512)\n\nEnd\n")
    assert not summary.completed
    assert summary.fatal_error  # non-empty


def test_parse_solver_log_nan_is_diverged():
    summary = parse_solver_log(_read("icoFoam_nan.log"))
    assert summary.diverged
    assert not summary.completed
    assert summary.fatal_error is None
    assert summary.end_time == 9.0
    assert summary.failure_reason is not None
    assert "diverged" in summary.failure_reason.lower()


def test_sigfpe_crash_is_diverged():
    summary = parse_solver_log(_read("icoFoam_sigfpe.log"))
    assert summary.diverged
    assert not summary.completed


@pytest.mark.parametrize("token", ["nan", "-nan", "NaN", "inf", "-inf", "Inf"])
def test_non_finite_residual_is_diverged_even_with_end_line(token):
    log = (
        "Time = 1\n\n"
        f"smoothSolver:  Solving for Ux, Initial residual = 1, Final residual = {token}, "
        "No Iterations 2\n\n"
        "End\n"
    )
    summary = parse_solver_log(log)
    assert summary.diverged
    assert not summary.completed


def test_non_finite_initial_residual_is_diverged():
    log = (
        "Time = 1\n"
        "DICPCG:  Solving for p, Initial residual = nan, Final residual = 1, No Iterations 2\n"
        "End\n"
    )
    assert parse_solver_log(log).diverged


def test_non_finite_courant_number_is_diverged():
    log = "Time = 1\nCourant Number mean: nan max: nan\nEnd\n"
    summary = parse_solver_log(log)
    assert summary.diverged
    assert summary.max_courant is None


def test_missing_end_line_is_not_completed():
    log = _read("icoFoam_ok.log").rsplit("End", 1)[0]
    summary = parse_solver_log(log)
    assert not summary.completed
    assert not summary.diverged
    assert summary.fatal_error is None
    assert summary.end_time == 0.5
    assert summary.failure_reason is not None
    assert "End" in summary.failure_reason


def test_end_inside_other_words_is_not_an_end_line():
    log = "Time = 1\nExecutionTime = 1 s\nEnd of something\nFinal End\n"
    assert not parse_solver_log(log).completed


def test_empty_log_is_not_completed():
    summary = parse_solver_log("")
    assert not summary.completed
    assert summary.end_time is None
    assert summary.final_residuals == {}
    assert summary.max_courant is None


def test_parallel_log_tail_after_end_is_still_completed():
    assert parse_solver_log(_read("icoFoam_ok.log") + "Finalising parallel run\n").completed


# --- run_solver ---------------------------------------------------------------------------------


@pytest.mark.parametrize("n_procs", [0, -1, 5, 64])
def test_run_solver_rejects_invalid_rank_count(tmp_path, n_procs):
    with pytest.raises(ValueError):
        run_solver(tmp_path, "icoFoam", n_procs=n_procs)
    assert list(tmp_path.iterdir()) == []  # nothing ran, nothing written


def test_run_solver_rejects_more_than_4_ranks(tmp_path):
    with pytest.raises(ValueError, match="4"):
        run_solver(tmp_path, "icoFoam", n_procs=5)


def test_run_solver_rejects_non_integer_rank_count(tmp_path):
    with pytest.raises(ValueError):
        run_solver(tmp_path, "icoFoam", n_procs=2.0)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "solver", ["bash", "/usr/bin/icoFoam", "rm", "python", "checkMesh", "mpirun", "decomposePar"]
)
def test_run_solver_rejects_non_solver_executables(tmp_path, solver):
    with pytest.raises(ToolNotAllowed):
        run_solver(tmp_path, solver)


class FakeRunner:
    """Replaces run_tool: records calls and writes a canned log per executable."""

    def __init__(self, logs=None, returncodes=None):
        self.calls = []
        self.logs = logs or {}
        self.returncodes = returncodes or {}

    def __call__(self, argv, cwd, log_path, timeout_s=600.0):
        self.calls.append((list(argv), Path(cwd), Path(log_path), timeout_s))
        text = self.logs.get(argv[0], "Time = 1\n\nEnd\n")
        Path(log_path).write_text(text, encoding="utf-8")
        rc = self.returncodes.get(argv[0], 0)
        return ToolResult(rc == 0, rc, False, Path(log_path), text)


@pytest.fixture
def fake(monkeypatch):
    runner = FakeRunner()
    monkeypatch.setattr(foam, "run_tool", runner)
    return runner


def test_run_solver_serial(tmp_path, fake):
    result = run_solver(tmp_path, "icoFoam", timeout_s=123.0)
    assert [c[0] for c in fake.calls] == [["icoFoam"]]
    assert fake.calls[0][1] == tmp_path
    assert fake.calls[0][2] == tmp_path / "log.icoFoam"
    assert fake.calls[0][3] == 123.0
    assert result.ok
    assert not (tmp_path / "system" / "decomposeParDict").exists()


def test_run_solver_parallel_sequence(tmp_path, fake):
    result = run_solver(tmp_path, "simpleFoam", n_procs=3)
    assert [c[0] for c in fake.calls] == [
        ["decomposePar", "-force"],
        ["mpirun", "-np", "3", "simpleFoam", "-parallel"],
        ["reconstructPar", "-latestTime"],
    ]
    assert [c[2].name for c in fake.calls] == [
        "log.decomposePar",
        "log.simpleFoam",
        "log.reconstructPar",
    ]
    assert all(c[1] == tmp_path for c in fake.calls)
    assert result.log_path == tmp_path / "log.reconstructPar"
    assert result.ok


def test_run_solver_parallel_creates_decompose_dict(tmp_path, fake):
    run_solver(tmp_path, "icoFoam", n_procs=2)
    raw = (tmp_path / "system" / "decomposeParDict").read_bytes()
    assert b"numberOfSubdomains 2;" in raw
    assert b"method scotch;" in raw
    assert b"\r" not in raw


def test_run_solver_parallel_keeps_matching_existing_dict(tmp_path, fake):
    (tmp_path / "system").mkdir()
    existing = "numberOfSubdomains 2;\nmethod simple;\n"
    (tmp_path / "system" / "decomposeParDict").write_text(existing, encoding="utf-8")
    run_solver(tmp_path, "icoFoam", n_procs=2)
    assert (tmp_path / "system" / "decomposeParDict").read_text(encoding="utf-8") == existing


def test_run_solver_parallel_rejects_mismatching_existing_dict(tmp_path, fake):
    (tmp_path / "system").mkdir()
    (tmp_path / "system" / "decomposeParDict").write_text(
        "numberOfSubdomains 4;\nmethod scotch;\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="numberOfSubdomains"):
        run_solver(tmp_path, "icoFoam", n_procs=2)
    assert fake.calls == []


def test_run_solver_failed_solver_step_is_returned(tmp_path, monkeypatch):
    runner = FakeRunner(returncodes={"mpirun": 1})
    monkeypatch.setattr(foam, "run_tool", runner)
    result = run_solver(tmp_path, "icoFoam", n_procs=2)
    assert not result.ok
    assert result.log_path == tmp_path / "log.icoFoam"
    # A crashed run is not reconstructed.
    assert "reconstructPar" not in [c[0][0] for c in runner.calls]


def test_run_solver_failed_decomposition_stops_before_the_solver(tmp_path, monkeypatch):
    runner = FakeRunner(returncodes={"decomposePar": 1})
    monkeypatch.setattr(foam, "run_tool", runner)
    result = run_solver(tmp_path, "icoFoam", n_procs=2)
    assert not result.ok
    assert result.log_path == tmp_path / "log.decomposePar"
    assert [c[0][0] for c in runner.calls] == ["decomposePar"]


def test_run_solver_failed_reconstruction_is_returned(tmp_path, monkeypatch):
    runner = FakeRunner(returncodes={"reconstructPar": 1})
    monkeypatch.setattr(foam, "run_tool", runner)
    result = run_solver(tmp_path, "icoFoam", n_procs=2)
    assert not result.ok
    assert result.log_path == tmp_path / "log.reconstructPar"


@pytest.mark.parametrize("log", ["icoFoam_nan.log", "icoFoam_fatal.log", "icoFoam_sigfpe.log"])
def test_run_solver_exit_code_zero_with_a_bad_log_is_not_ok(tmp_path, monkeypatch, log):
    # A solver (or mpirun) that exits 0 while its log shows a crash must not be reported as success.
    runner = FakeRunner(logs={"icoFoam": _read(log)})
    monkeypatch.setattr(foam, "run_tool", runner)
    result = run_solver(tmp_path, "icoFoam")
    assert not result.ok
    assert result.returncode == 0


def test_run_solver_exit_code_zero_without_end_line_is_not_ok(tmp_path, monkeypatch):
    runner = FakeRunner(logs={"icoFoam": "Time = 1\n"})
    monkeypatch.setattr(foam, "run_tool", runner)
    assert not run_solver(tmp_path, "icoFoam").ok


def test_run_solver_ok_log_is_ok(tmp_path, monkeypatch):
    runner = FakeRunner(logs={"icoFoam": _read("icoFoam_ok.log")})
    monkeypatch.setattr(foam, "run_tool", runner)
    assert run_solver(tmp_path, "icoFoam").ok
