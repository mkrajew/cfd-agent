"""Solver log parsing and run_solver orchestration; logs captured from icoFoam in the image."""

import hashlib
import math
import re
from pathlib import Path

import pytest

from cfdagent.errors import AgentInputError, ToolFailure
from cfdagent.tools import foam
from cfdagent.tools.foam import (
    DIVERGENCE_LIMIT,
    SolverLogSummary,
    SolverRun,
    parse_solver_log,
    parse_solver_log_file,
    run_solver,
    trial_run,
)
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


def test_residual_blowup_then_end_is_diverged():
    # Finite but absurd residuals and Courant numbers: the run ended with "End" and no NaN, yet it
    # is not a solution.
    summary = parse_solver_log(_read("icoFoam_blowup.log"))
    assert summary.diverged
    assert not summary.completed
    assert summary.fatal_error is None
    assert summary.failure_reason is not None
    assert "diverged" in summary.failure_reason.lower()


def _tiny_log(residual="1e-5", courant="0.5"):
    return (
        "Time = 1\n"
        f"Courant Number mean: 0.1 max: {courant}\n"
        f"smoothSolver:  Solving for Ux, Initial residual = {residual}, Final residual = 1e-6, "
        "No Iterations 2\n\nEnd\n"
    )


def test_divergence_limit_is_a_large_named_constant():
    assert DIVERGENCE_LIMIT == 1e6


def test_residual_at_the_limit_is_not_diverged_and_above_it_is():
    at_limit = f"{DIVERGENCE_LIMIT:g}"
    assert parse_solver_log(_tiny_log(residual=at_limit)).completed
    assert parse_solver_log(_tiny_log(residual="1.000001e6")).diverged


def test_courant_above_the_limit_is_diverged_and_still_reported():
    summary = parse_solver_log(_tiny_log(courant="2e6"))
    assert summary.diverged
    assert not summary.completed
    assert summary.max_courant == 2e6
    assert parse_solver_log(_tiny_log(courant=f"{DIVERGENCE_LIMIT:g}")).completed


def test_final_residual_above_the_limit_is_diverged():
    log = (
        "Time = 1\n"
        "smoothSolver:  Solving for Ux, Initial residual = 1, Final residual = 5e7, "
        "No Iterations 2\nEnd\n"
    )
    assert parse_solver_log(log).diverged


# --- parse_solver_log_file ----------------------------------------------------------------------

ALL_LOGS = sorted(p.name for p in LOGS.glob("*.log"))


@pytest.mark.parametrize("name", ALL_LOGS)
def test_parse_log_file_equals_parse_solver_log_on_every_fixture(name):
    # repr: a diverged log holds nan residuals, and nan != nan inside a dataclass comparison.
    assert repr(parse_solver_log_file(LOGS / name)) == repr(parse_solver_log(_read(name)))


def test_parse_log_file_streams(tmp_path, monkeypatch):
    # About 12 MB of log lines, with a fatal error and an End line at the very end.
    block = _read("icoFoam_ok.log").split("Starting time loop")[1].rsplit("End", 1)[0]
    path = tmp_path / "big.log"
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(_read("icoFoam_ok.log").split("Starting time loop")[0])
        while f.tell() < 12_000_000:
            f.write(block)
        f.write("--> FOAM FATAL ERROR:\nlate failure\n\n    From x\n\nEnd\n")

    expected = parse_solver_log(path.read_text(encoding="utf-8"))

    def _no_whole_file_read(*args, **kwargs):
        raise AssertionError("parse_solver_log_file must stream, not read the whole file")

    monkeypatch.setattr(Path, "read_text", _no_whole_file_read)
    monkeypatch.setattr(Path, "read_bytes", _no_whole_file_read)
    summary = parse_solver_log_file(path)
    assert summary == expected
    assert summary.fatal_error == "late failure"
    assert not summary.completed


def test_parse_log_file_handles_crlf_lone_cr_and_invalid_utf8(tmp_path):
    raw = (
        b"Time = 1\r\nCourant Number mean: 0.1 max: 0.5\r\n"
        b"smoothSolver:  Solving for Ux, Initial residual = 1, Final residual = 1e-6, "
        b"No Iterations 2\rTime = 2\r\n\xff\xfe bad bytes\r\n--> FOAM FATAL ERROR:\r\n"
        b"boom\r\n\r\n    From x\r\n\r\nEnd\r\n"
    )
    path = tmp_path / "crlf.log"
    path.write_bytes(raw)
    text = raw.decode("utf-8", errors="replace")
    assert parse_solver_log_file(path) == parse_solver_log(text)
    assert parse_solver_log_file(path).end_time == 2.0
    assert parse_solver_log_file(path).fatal_error == "boom"


def test_parse_log_file_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        parse_solver_log_file(tmp_path / "nope.log")


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


DICT_MISSING_LOG = 'cannot open file "system/controlDict"\n'


class FakeRunner:
    """Replaces run_tool: records calls and writes a canned log per executable.

    ``foamDictionary`` answers ``end_time`` (None: the dictionary cannot be read) unless
    ``emulate_dict`` is set, in which case it reads and sets entries of the real file in ``cwd``.
    ``on_run(argv, cwd)`` is called before every run (it may raise).
    """

    def __init__(
        self,
        logs=None,
        returncodes=None,
        end_time="1",
        emulate_dict=False,
        on_run=None,
        timeouts=(),
    ):
        self.calls = []
        self.logs = logs or {}
        self.returncodes = returncodes or {}
        self.end_time = end_time
        self.emulate_dict = emulate_dict
        self.on_run = on_run
        self.timeouts = set(timeouts)

    def __call__(self, argv, cwd, log_path, timeout_s=600.0):
        self.calls.append((list(argv), Path(cwd), Path(log_path), timeout_s))
        if self.on_run:
            self.on_run(list(argv), Path(cwd))
        name = argv[0]
        if name in self.timeouts:
            Path(log_path).write_text("[cfdagent] timed out\n", encoding="utf-8")
            return ToolResult(False, None, True, Path(log_path), "[cfdagent] timed out\n")
        if name == "foamDictionary":
            rc, text = self._dictionary(argv, Path(cwd))
        else:
            text = self.logs.get(name, "Time = 1\n\nEnd\n")
            rc = self.returncodes.get(name, 0)
        Path(log_path).write_text(text, encoding="utf-8")
        return ToolResult(rc == 0, rc, False, Path(log_path), text)

    def _dictionary(self, argv, cwd):
        if not self.emulate_dict:
            if self.end_time is None:
                return 1, DICT_MISSING_LOG
            return 0, f"{self.end_time}\n"
        path = cwd / argv[1]
        if not path.is_file():
            return 1, DICT_MISSING_LOG
        text = path.read_text(encoding="utf-8")
        entry = argv[argv.index("-entry") + 1]
        pattern = re.compile(rf"^\s*{re.escape(entry)}\s+([^;]+);", re.MULTILINE)
        found = pattern.search(text)
        if "-set" in argv:
            line = f"{entry} {argv[argv.index('-set') + 1]};"
            text = pattern.sub(line, text, count=1) if found else text + line + "\n"
            path.write_text(text, encoding="utf-8")
            return 0, ""
        if found is None:
            return 1, f"Cannot find entry {entry}\n"
        return 0, found.group(1).strip() + "\n"


@pytest.fixture
def fake(monkeypatch):
    runner = FakeRunner()
    monkeypatch.setattr(foam, "run_tool", runner)
    return runner


def test_run_solver_serial(tmp_path, fake):
    result = run_solver(tmp_path, "icoFoam", timeout_s=123.0)
    assert fake.calls[0][0] == ["icoFoam"]
    assert fake.calls[0][1] == tmp_path
    assert fake.calls[0][2] == tmp_path / "log.icoFoam"
    assert fake.calls[0][3] == 123.0
    assert isinstance(result, SolverRun)
    assert result.ok
    assert result.solver_log == tmp_path / "log.icoFoam"
    assert result.summary.completed
    assert not (tmp_path / "system" / "decomposeParDict").exists()


def test_run_solver_reads_the_end_time_with_foam_dictionary(tmp_path, fake):
    result = run_solver(tmp_path, "icoFoam")
    argv, cwd, _, _ = fake.calls[-1]
    assert argv == ["foamDictionary", "system/controlDict", "-entry", "endTime", "-value"]
    assert cwd == tmp_path
    assert result.case_end_time == 1.0


def test_run_solver_parallel_sequence(tmp_path, fake):
    result = run_solver(tmp_path, "simpleFoam", n_procs=3)
    assert [c[0][0] for c in fake.calls] == [
        "decomposePar",
        "mpirun",
        "foamDictionary",
        "reconstructPar",
    ]
    assert fake.calls[0][0] == ["decomposePar", "-force"]
    assert fake.calls[1][0] == [
        "mpirun",
        "--use-hwthread-cpus",
        "-np",
        "3",
        "simpleFoam",
        "-parallel",
    ]
    assert fake.calls[3][0] == ["reconstructPar", "-latestTime"]
    assert [c[2].name for c in fake.calls if c[0][0] != "foamDictionary"] == [
        "log.decomposePar",
        "log.simpleFoam",
        "log.reconstructPar",
    ]
    assert all(c[1] == tmp_path for c in fake.calls)
    assert result.tool.log_path == tmp_path / "log.reconstructPar"
    assert result.solver_log == tmp_path / "log.simpleFoam"
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
    assert result.tool.log_path == tmp_path / "log.icoFoam"
    # A crashed run is not reconstructed.
    assert "reconstructPar" not in [c[0][0] for c in runner.calls]


def test_run_solver_failed_decomposition_stops_before_the_solver(tmp_path, monkeypatch):
    runner = FakeRunner(returncodes={"decomposePar": 1})
    monkeypatch.setattr(foam, "run_tool", runner)
    result = run_solver(tmp_path, "icoFoam", n_procs=2)
    assert not result.ok
    assert result.tool.log_path == tmp_path / "log.decomposePar"
    assert [c[0][0] for c in runner.calls if c[0][0] != "foamDictionary"] == ["decomposePar"]
    # The solver never started: there is no solver log, so the summary is that of an empty log.
    assert not result.summary.completed
    assert result.summary.end_time is None


def test_run_solver_failed_reconstruction_is_returned(tmp_path, monkeypatch):
    runner = FakeRunner(returncodes={"reconstructPar": 1})
    monkeypatch.setattr(foam, "run_tool", runner)
    result = run_solver(tmp_path, "icoFoam", n_procs=2)
    assert not result.ok
    assert result.tool.log_path == tmp_path / "log.reconstructPar"


@pytest.mark.parametrize("log", ["icoFoam_nan.log", "icoFoam_fatal.log", "icoFoam_sigfpe.log"])
def test_run_solver_exit_code_zero_with_a_bad_log_is_not_ok(tmp_path, monkeypatch, log):
    # A solver (or mpirun) that exits 0 while its log shows a crash must not be reported as success.
    runner = FakeRunner(logs={"icoFoam": _read(log)})
    monkeypatch.setattr(foam, "run_tool", runner)
    result = run_solver(tmp_path, "icoFoam")
    assert not result.ok
    assert not result.tool.ok
    assert result.tool.returncode == 0


def test_mpirun_exit_zero_with_bad_log_is_not_ok(tmp_path, monkeypatch):
    runner = FakeRunner(logs={"mpirun": _read("icoFoam_nan.log")}, end_time="0.5")
    monkeypatch.setattr(foam, "run_tool", runner)
    result = run_solver(tmp_path, "icoFoam", n_procs=2)
    assert not result.ok
    assert not result.tool.ok
    assert result.tool.returncode == 0  # mpirun itself exited cleanly
    assert result.summary.diverged
    # The reason is in the tail and in the log file, and the run is not reconstructed.
    assert "diverged" in result.tool.tail
    assert "[cfdagent]" in (tmp_path / "log.icoFoam").read_text(encoding="utf-8")
    assert "reconstructPar" not in [c[0][0] for c in runner.calls]


def test_run_solver_exit_code_zero_without_end_line_is_not_ok(tmp_path, monkeypatch):
    runner = FakeRunner(logs={"icoFoam": "Time = 1\n"})
    monkeypatch.setattr(foam, "run_tool", runner)
    result = run_solver(tmp_path, "icoFoam")
    assert not result.ok
    assert "End" in result.tool.tail


def test_run_solver_ok_log_is_ok(tmp_path, monkeypatch):
    runner = FakeRunner(logs={"icoFoam": _read("icoFoam_ok.log")}, end_time="0.5")
    monkeypatch.setattr(foam, "run_tool", runner)
    result = run_solver(tmp_path, "icoFoam")
    assert result.ok
    assert result.summary.end_time == 0.5
    assert result.case_end_time == 0.5


def test_end_time_mismatch_is_not_ok(tmp_path, monkeypatch):
    # The solver ended cleanly at t = 1, but the case asks for t = 2.
    runner = FakeRunner(end_time="2")
    monkeypatch.setattr(foam, "run_tool", runner)
    result = run_solver(tmp_path, "icoFoam")
    assert not result.ok
    assert not result.tool.ok
    assert result.summary.completed
    assert "endTime" in result.tool.tail
    assert "[cfdagent]" in (tmp_path / "log.icoFoam").read_text(encoding="utf-8")


def _solver_run(end_time, case_end_time, completed=True, tool_ok=True, tmp_path=Path("x")):
    summary = SolverLogSummary(completed, not completed, None, end_time, {}, None)
    tool = ToolResult(tool_ok, 0 if tool_ok else 1, False, tmp_path / "log", "")
    return SolverRun(tool, tmp_path / "log", summary, case_end_time)


def test_solver_run_ok_needs_every_condition():
    assert _solver_run(40.0, 40.0).ok
    assert not _solver_run(40.0, 40.0, tool_ok=False).ok
    assert not _solver_run(40.0, 40.0, completed=False).ok
    assert not _solver_run(None, 40.0).ok
    assert not _solver_run(40.0, None).ok
    assert not _solver_run(None, None).ok
    assert not _solver_run(39.9, 40.0).ok
    assert not _solver_run(40.0, float("nan")).ok


def test_solver_run_end_time_tolerance_is_relative_1e_9():
    assert _solver_run(40.0 * (1 + 1e-10), 40.0).ok
    assert not _solver_run(40.0 * (1 + 1e-8), 40.0).ok
    assert _solver_run(1e-6 * (1 + 1e-10), 1e-6).ok  # relative, not absolute
    assert not _solver_run(1e-6 * (1 + 1e-8), 1e-6).ok


def test_solver_run_is_frozen():
    run = _solver_run(1.0, 1.0)
    with pytest.raises(AttributeError):
        run.case_end_time = 2.0  # type: ignore[misc]


@pytest.mark.parametrize("dictionary_output", ["", "nan", "inf", "$endT", "ten"])
def test_unreadable_end_time_is_not_ok_with_a_clear_reason(
    tmp_path, monkeypatch, dictionary_output
):
    monkeypatch.setattr(foam, "run_tool", FakeRunner(end_time=dictionary_output))
    result = run_solver(tmp_path, "icoFoam")
    assert result.case_end_time is None
    assert not result.ok
    assert "endTime" in result.tool.tail


def test_missing_control_dict_gives_not_ok_not_a_skeleton_pass(tmp_path, monkeypatch):
    # The solver fakes a clean finish, but the case has no readable controlDict: never a pass.
    monkeypatch.setattr(foam, "run_tool", FakeRunner(end_time=None))
    result = run_solver(tmp_path, "icoFoam")
    assert result.case_end_time is None
    assert not result.ok
    assert "controlDict" in result.tool.tail
    # No skeleton controlDict was conjured up in the case.
    assert not (tmp_path / "system").exists()


def test_foam_dictionary_timeout_is_a_tool_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(foam, "run_tool", FakeRunner(timeouts={"foamDictionary"}))
    with pytest.raises(ToolFailure, match="foamDictionary"):
        run_solver(tmp_path, "icoFoam")


# --- trial_run ----------------------------------------------------------------------------------

CONTROL_DICT = """FoamFile
{
    version 2.0;
}

application icoFoam;
startFrom   latestTime;
startTime   0;
endTime     40;
deltaT      0.005;
writeInterval 1000;
"""


def _make_case(root: Path) -> Path:
    case = root / "case"
    for rel, text in {
        "system/controlDict": CONTROL_DICT,
        "system/fvSchemes": "ddtSchemes {}\n",
        "constant/transportProperties": "nu 0.01;\n",
        "constant/polyMesh/points": "points\n",
        "0/U": "U\n",
        "0/p": "p\n",
        "0.5/U": "stale time directory\n",
        "10/U": "stale time directory\n",
        "postProcessing/probes/0/p": "old results\n",
        "log.icoFoam": "old log\n",
        "processor0/0/U": "decomposed\n",
    }.items():
        path = case / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return case


def _snapshot(root: Path) -> dict[str, str]:
    entries = {}
    for path in sorted(root.rglob("*")):
        key = path.relative_to(root).as_posix()
        entries[key] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "dir"
    return entries


def _files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def test_trial_run_copies_only_what_the_solver_needs_and_sets_end_time(tmp_path, monkeypatch):
    case = _make_case(tmp_path)
    seen = {}

    def on_run(argv, cwd):
        if argv[0] == "icoFoam":
            seen["cwd"] = cwd
            seen["files"] = [f for f in _files(cwd) if not f.startswith("log.")]
            seen["control"] = (cwd / "system" / "controlDict").read_text(encoding="utf-8")

    runner = FakeRunner(logs={"icoFoam": "Time = 0.015\n\nEnd\n"}, emulate_dict=True, on_run=on_run)
    monkeypatch.setattr(foam, "run_tool", runner)
    before = _snapshot(case)

    result = trial_run(case, "icoFoam", steps=3)

    assert isinstance(result, SolverRun)
    assert result.ok, result.tool.tail
    assert result.case_end_time == pytest.approx(0.015)
    assert result.summary.end_time == pytest.approx(0.015)
    # The scratch directory is gone, and is not the case directory.
    assert seen["cwd"] != case
    assert not seen["cwd"].exists()
    # No postProcessing, logs, decomposed data or other time directories were copied.
    assert seen["files"] == [
        "0/U",
        "0/p",
        "constant/polyMesh/points",
        "constant/transportProperties",
        "system/controlDict",
        "system/fvSchemes",
    ]
    assert re.search(r"^endTime\s+0\.015;", seen["control"], re.MULTILINE)
    assert re.search(r"^startFrom\s+startTime;", seen["control"], re.MULTILINE)
    # Serial: no mpirun or decomposePar.
    assert {c[0][0] for c in runner.calls} == {"foamDictionary", "icoFoam"}
    # The original case directory is bit-for-bit unchanged.
    assert _snapshot(case) == before


def test_trial_run_end_time_is_start_time_plus_steps_times_delta_t(tmp_path, monkeypatch):
    case = _make_case(tmp_path)
    control = case / "system" / "controlDict"
    text = control.read_text(encoding="utf-8")
    text = text.replace("startTime   0;", "startTime   2.5;").replace(
        "deltaT      0.005;", "deltaT      0.1;"
    )
    control.write_text(text, encoding="utf-8")
    (case / "2.5").mkdir()
    (case / "2.5" / "U").write_text("start data\n", encoding="utf-8")
    seen = {}

    def on_run(argv, cwd):
        if argv[0] == "icoFoam":
            seen["control"] = (cwd / "system" / "controlDict").read_text(encoding="utf-8")
            seen["times"] = sorted(p.name for p in cwd.iterdir())

    monkeypatch.setattr(
        foam,
        "run_tool",
        FakeRunner(logs={"icoFoam": "Time = 2.8\n\nEnd\n"}, emulate_dict=True, on_run=on_run),
    )
    result = trial_run(case, "icoFoam", steps=3)
    assert result.case_end_time == pytest.approx(2.8)
    assert re.search(r"^endTime\s+2\.8;", seen["control"], re.MULTILINE)
    assert "2.5" in seen["times"]
    assert "0" not in seen["times"]  # only the start time directory is kept
    assert result.ok


def test_trial_run_passes_timeout_and_runs_serially(tmp_path, monkeypatch):
    case = _make_case(tmp_path)
    runner = FakeRunner(logs={"icoFoam": "Time = 0.015\n\nEnd\n"}, emulate_dict=True)
    monkeypatch.setattr(foam, "run_tool", runner)
    trial_run(case, "icoFoam", timeout_s=17.0)
    solver_calls = [c for c in runner.calls if c[0][0] == "icoFoam"]
    assert [c[0] for c in solver_calls] == [["icoFoam"]]
    assert solver_calls[0][3] == 17.0


def test_trial_run_reports_fatal_error(tmp_path, monkeypatch):
    case = _make_case(tmp_path)
    runner = FakeRunner(
        logs={"icoFoam": _read("icoFoam_fatal.log")}, returncodes={"icoFoam": 1}, emulate_dict=True
    )
    monkeypatch.setattr(foam, "run_tool", runner)
    result = trial_run(case, "icoFoam")
    assert not result.ok
    assert result.summary.fatal_error is not None
    assert "noSlipp" in result.summary.fatal_error
    assert "noSlipp" in result.tool.tail  # the agent sees the solver's message


def test_trial_run_scratch_is_removed_when_the_solver_raises(tmp_path, monkeypatch):
    case = _make_case(tmp_path)
    scratch = []

    def on_run(argv, cwd):
        if argv[0] == "icoFoam":
            scratch.append(cwd)
            raise KeyboardInterrupt

    monkeypatch.setattr(foam, "run_tool", FakeRunner(emulate_dict=True, on_run=on_run))
    before = _snapshot(case)
    with pytest.raises(KeyboardInterrupt):
        trial_run(case, "icoFoam")
    assert len(scratch) == 1
    assert not scratch[0].exists()
    assert _snapshot(case) == before


def test_trial_run_does_not_touch_case_dir(tmp_path, monkeypatch):
    # Even a failing trial run leaves the case, and every file in it, as it was.
    case = _make_case(tmp_path)
    runner = FakeRunner(returncodes={"icoFoam": 1}, emulate_dict=True)
    monkeypatch.setattr(foam, "run_tool", runner)
    before = _snapshot(case)
    result = trial_run(case, "icoFoam")
    assert not result.ok
    assert _snapshot(case) == before
    assert all(c[1] != case and case not in c[2].parents for c in runner.calls)


def test_trial_run_without_control_dict_is_the_agents_fault(tmp_path, monkeypatch):
    case = _make_case(tmp_path)
    (case / "system" / "controlDict").unlink()
    runner = FakeRunner(emulate_dict=True)
    monkeypatch.setattr(foam, "run_tool", runner)
    with pytest.raises(AgentInputError, match="controlDict"):
        trial_run(case, "icoFoam")
    assert runner.calls == []


@pytest.mark.parametrize("entry", ["startTime", "deltaT"])
def test_trial_run_with_unreadable_entry_is_the_agents_fault(tmp_path, monkeypatch, entry):
    case = _make_case(tmp_path)
    control = case / "system" / "controlDict"
    text = re.sub(rf"^{entry}\s+.*$", "", control.read_text(encoding="utf-8"), flags=re.MULTILINE)
    control.write_text(text, encoding="utf-8")
    monkeypatch.setattr(foam, "run_tool", FakeRunner(emulate_dict=True))
    with pytest.raises(AgentInputError, match=entry):
        trial_run(case, "icoFoam")


def test_trial_run_rejects_bad_arguments(tmp_path, monkeypatch):
    case = _make_case(tmp_path)
    runner = FakeRunner(emulate_dict=True)
    monkeypatch.setattr(foam, "run_tool", runner)
    for steps in (0, -1, 2.5, True):
        with pytest.raises(ValueError):
            trial_run(case, "icoFoam", steps=steps)  # type: ignore[arg-type]
    with pytest.raises(ToolNotAllowed):
        trial_run(case, "bash")
    assert runner.calls == []


def test_trial_run_missing_case_dir_is_a_tool_failure(tmp_path):
    with pytest.raises(ToolFailure):
        trial_run(tmp_path / "does_not_exist", "icoFoam")


def test_failed_solver_step_with_a_fatal_error_shows_the_message_in_the_tail(tmp_path, monkeypatch):
    # A real fatal error is followed by a long list of valid choices that pushes the message out
    # of the last 50 lines; the reason is repeated at the end so that the tail shows it.
    log = (
        "--> FOAM FATAL IO ERROR: (openfoam-2512)\n"
        "Unknown patchField type noSlipp for patch type wall\n\n"
        "Valid patchField types are :\n"
        + "".join(f"someValidType{i}\n" for i in range(80))
        + "\nFOAM exiting\n"
    )
    runner = FakeRunner(logs={"icoFoam": log}, returncodes={"icoFoam": 1})
    monkeypatch.setattr(foam, "run_tool", runner)
    result = run_solver(tmp_path, "icoFoam")
    assert not result.ok
    assert result.summary.fatal_error is not None
    assert "noSlipp" in result.tool.tail
    assert "someValidType0" not in result.tool.tail
    assert "[cfdagent]" in (tmp_path / "log.icoFoam").read_text(encoding="utf-8")


def test_crashed_solver_step_without_a_diagnosis_gets_no_extra_text(tmp_path, monkeypatch):
    runner = FakeRunner(logs={"icoFoam": "Time = 1\n"}, returncodes={"icoFoam": 137})
    monkeypatch.setattr(foam, "run_tool", runner)
    result = run_solver(tmp_path, "icoFoam")
    assert not result.ok
    assert "[cfdagent]" not in (tmp_path / "log.icoFoam").read_text(encoding="utf-8")
