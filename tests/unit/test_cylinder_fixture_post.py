"""Guards of the hand-written post-processing scripts of the two cylinder fixtures.

Host tests on synthetic data: no OpenFOAM, no mesh. The scripts' pure functions are fed
``coefficient.dat`` files and centreline samples built here.
"""

import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

from cfdagent.tools.foam_results import FoamResultsError

HANDWRITTEN = Path(__file__).resolve().parents[1] / "fixtures" / "handwritten"
HEADER = "# Time Cd Cd(f) Cd(r) Cl Cl(f) Cl(r) CmPitch CmRoll CmYaw Cs Cs(f) Cs(r)"
GUARD_ERRORS = (RuntimeError, FoamResultsError)  # a guard of the script or of the shared reader


def load_post(name):
    spec = importlib.util.spec_from_file_location(f"{name}_post", HANDWRITTEN / name / "post.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def re20():
    return load_post("cylinder_re20")


@pytest.fixture(scope="module")
def re100():
    return load_post("cylinder_re100")


def write_coefficients(case_dir, t, cd, cl):
    directory = case_dir / "postProcessing" / "forceCoeffs" / "0"
    directory.mkdir(parents=True, exist_ok=True)
    lines = [HEADER]
    for time, drag, lift in zip(t, cd, cl, strict=True):
        columns = [time, drag, 0, 0, lift, 0, 0, 0, 0, 0, 0, 0, 0]
        lines.append(" ".join(f"{value:.9g}" for value in columns))
    (directory / "coefficient.dat").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_control_dict(case_dir, a_ref=0.01, l_ref=0.1):
    system = case_dir / "system"
    system.mkdir(parents=True, exist_ok=True)
    (system / "controlDict").write_text(
        "functions\n{\n    forceCoeffs\n    {\n"
        f"        lRef            {l_ref};  // reference length\n"
        f"        Aref            {a_ref};\n"
        "    }\n}\n",
        encoding="utf-8",
    )


# Re 100: shedding record ---------------------------------------------------------------------

FREQUENCY = 1.65  # [1/s]: St = 0.165 for D = 0.1 and U = 1


def shedding_record(frequency=FREQUENCY, amplitude=0.3, envelope=None, end=30.0):
    """``t, cd, cl``: sinusoidal lift, drag at twice the frequency, optional amplitude envelope."""
    t = np.arange(0.0, end, 0.005)
    scale = np.ones_like(t) if envelope is None else envelope(t)
    cl = amplitude * scale * np.sin(2 * np.pi * frequency * t)
    cd = 1.3 + 0.01 * np.sin(2 * np.pi * 2 * frequency * t)
    return t, cd, cl


def test_re100_clean_shedding_gives_the_synthetic_strouhal_number(tmp_path, re100):
    write_coefficients(tmp_path, *shedding_record())
    quantities = re100.shedding_quantities(tmp_path)
    assert quantities["strouhal"] == pytest.approx(FREQUENCY * 0.1, rel=0.01)
    assert quantities["cd_mean"] == pytest.approx(1.3, abs=0.002)


def test_re100_decaying_amplitude_raises(tmp_path, re100):
    record = shedding_record(envelope=lambda t: np.exp(-t / 30.0))
    write_coefficients(tmp_path, *record)
    with pytest.raises(RuntimeError, match="not stationary"):
        re100.shedding_quantities(tmp_path)


def test_re100_growing_amplitude_raises(tmp_path, re100):
    record = shedding_record(amplitude=0.1, envelope=lambda t: np.exp(t / 20.0))
    write_coefficients(tmp_path, *record)
    with pytest.raises(RuntimeError, match="not stationary"):
        re100.shedding_quantities(tmp_path)


def test_re100_an_amplitude_change_just_above_two_percent_raises(tmp_path, re100):
    # relative change between the window halves: 1 - exp(-rate * 7.5) = 3 %
    rate = -np.log(0.97) / 7.5
    write_coefficients(tmp_path, *shedding_record(envelope=lambda t: np.exp(-rate * t)))
    with pytest.raises(RuntimeError, match="not stationary"):
        re100.shedding_quantities(tmp_path)


def test_re100_fewer_than_ten_periods_raises(tmp_path, re100):
    write_coefficients(tmp_path, *shedding_record(frequency=0.5))  # 7.5 periods in the window
    with pytest.raises(RuntimeError, match="shedding periods"):
        re100.shedding_quantities(tmp_path)


def test_re100_no_shedding_raises(tmp_path, re100):
    write_coefficients(tmp_path, *shedding_record(amplitude=0.05))
    with pytest.raises(RuntimeError, match="no vortex shedding"):
        re100.shedding_quantities(tmp_path)


@pytest.mark.parametrize(
    ("a_ref", "l_ref"), [(0.02, 0.1), (0.01, 0.2), (0.1, 0.1)], ids=["Aref", "lRef", "no-thickness"]
)
def test_re100_reference_area_mismatch_raises(tmp_path, re100, a_ref, l_ref):
    write_control_dict(tmp_path, a_ref=a_ref, l_ref=l_ref)
    with pytest.raises(RuntimeError, match="do not match"):
        re100.check_reference_area(tmp_path, 0.1)


def test_re100_matching_reference_area_passes(tmp_path, re100):
    write_control_dict(tmp_path)
    re100.check_reference_area(tmp_path, 0.100001)  # the mesh points have six digits


# Re 20: drag convergence ----------------------------------------------------------------------


def test_re20_converged_drag_is_the_last_value(tmp_path, re20):
    n = 400
    cd = 2.036 + 0.3 * np.exp(-np.arange(n) / 20.0)  # settles long before the end
    write_coefficients(tmp_path, np.arange(1, n + 1), cd, np.zeros(n))
    assert re20.drag_coefficient(tmp_path) == pytest.approx(cd[-1])


def test_re20_a_slowly_drifting_drag_raises(tmp_path, re20):
    # 5e-6 per iteration: 1e-4 over the last 20 rows (a 20-row window passes it), 1e-3 over 200
    n = 500
    cd = 2.0 + 5e-6 * np.arange(n)
    write_coefficients(tmp_path, np.arange(1, n + 1), cd, np.zeros(n))
    with pytest.raises(RuntimeError, match="not converged"):
        re20.drag_coefficient(tmp_path)


def test_re20_a_drag_still_falling_at_the_end_raises(tmp_path, re20):
    n = 400
    cd = 2.036 + 0.3 * np.exp(-np.arange(n) / 200.0)
    write_coefficients(tmp_path, np.arange(1, n + 1), cd, np.zeros(n))
    with pytest.raises(RuntimeError, match="not converged"):
        re20.drag_coefficient(tmp_path)


def test_re20_a_record_shorter_than_the_window_is_judged_as_a_whole(tmp_path, re20):
    n = 60
    cd = 2.0 + 0.01 * np.arange(n)
    write_coefficients(tmp_path, np.arange(1, n + 1), cd, np.zeros(n))
    with pytest.raises(RuntimeError, match="not converged"):
        re20.drag_coefficient(tmp_path)


def test_re20_too_few_rows_raises(tmp_path, re20):
    write_coefficients(tmp_path, np.arange(1, 6), np.full(5, 2.0), np.zeros(5))
    with pytest.raises(RuntimeError, match="rows of force coefficients"):
        re20.drag_coefficient(tmp_path)


@pytest.mark.parametrize(
    ("a_ref", "l_ref"), [(0.02, 0.1), (0.01, 0.2), (0.1, 0.1)], ids=["Aref", "lRef", "no-thickness"]
)
def test_re20_reference_area_mismatch_raises(tmp_path, re20, a_ref, l_ref):
    write_control_dict(tmp_path, a_ref=a_ref, l_ref=l_ref)
    with pytest.raises(RuntimeError, match="do not match"):
        re20.check_reference_area(tmp_path, 0.1)


# Re 20: recirculation length on a synthetic centreline ------------------------------------------

REAR = 0.05  # x of the rear surface; the cylinder centre is the origin


def centreline(zero_crossing, depth=-0.3, x_end=0.8, n=1601, start=0.0502):
    """``x, ux``: zero on the wall, a reversed bubble ending at ``zero_crossing``, then recovery."""
    x = np.linspace(start, x_end, n)
    bubble = depth * np.sin(np.pi * (x - REAR) / (zero_crossing - REAR))
    recovery = 0.5 * (1.0 - np.exp(-(x - zero_crossing) / 0.1))
    ux = np.where(x < zero_crossing, bubble, recovery)
    ux[0] = 0.0  # the wall: a "first crossing" search would stop here
    return x, ux


def test_re20_bubble_length_is_measured_from_the_rear_surface(re20):
    x, ux = centreline(zero_crossing=0.0955)
    length = re20.bubble_length_over_d(x, ux)
    assert length == pytest.approx((0.0955 - REAR) / 0.1, abs=3e-3)  # 0.455 D, not 0.955 D


def test_re20_bubble_length_does_not_depend_on_the_order_of_the_samples(re20):
    x, ux = centreline(zero_crossing=0.0955)
    order = np.random.default_rng(0).permutation(len(x))
    assert re20.bubble_length_over_d(x[order], ux[order]) == pytest.approx(
        re20.bubble_length_over_d(x, ux)
    )


def test_re20_no_reversed_flow_raises(re20):
    x = np.linspace(0.0502, 0.8, 1601)
    ux = 0.5 * (1.0 - np.exp(-(x - 0.0502) / 0.1))
    with pytest.raises(RuntimeError, match="no reversed flow"):
        re20.bubble_length_over_d(x, ux)


def test_re20_a_line_that_ends_inside_the_bubble_raises(re20):
    x, ux = centreline(zero_crossing=0.2)
    inside = x < 0.15  # the line stops before the velocity returns to zero
    with pytest.raises(RuntimeError, match="still negative"):
        re20.bubble_length_over_d(x[inside], ux[inside])


# Both: the zero-argument form ---------------------------------------------------------------


class FakeCase:
    bounds = (np.array([-3.0, -3.0, 0.0]), np.array([6.0, 3.0, 0.1]))


def prepare_working_directory(path, monkeypatch, module):
    case = path / "case"
    write_control_dict(case)
    t, cd, cl = shedding_record()
    write_coefficients(case, t, cd, cl)
    monkeypatch.chdir(path)
    monkeypatch.setattr(module, "load_case", lambda case_dir: FakeCase())


def test_re100_without_arguments_uses_case_and_quantities_json_in_the_working_directory(
    tmp_path, monkeypatch, re100
):
    prepare_working_directory(tmp_path, monkeypatch, re100)
    assert re100.main(["post.py"]) == 0
    written = json.loads((tmp_path / "quantities.json").read_text(encoding="utf-8"))
    assert written["strouhal"] == pytest.approx(0.165, rel=0.01)


def test_re100_with_three_arguments_uses_the_given_paths(tmp_path, monkeypatch, re100):
    prepare_working_directory(tmp_path, monkeypatch, re100)
    out = tmp_path / "elsewhere.json"
    assert re100.main(["post.py", str(tmp_path / "case"), str(out)]) == 0
    assert "cd_mean" in json.loads(out.read_text(encoding="utf-8"))


def test_re20_without_arguments_uses_case_and_quantities_json_in_the_working_directory(
    tmp_path, monkeypatch, re20
):
    prepare_working_directory(tmp_path, monkeypatch, re20)
    n = 400
    write_coefficients(tmp_path / "case", np.arange(1, n + 1), np.full(n, 2.04), np.zeros(n))
    monkeypatch.setattr(re20, "recirculation_length_over_d", lambda case_dir: 0.9)
    assert re20.main(["post.py"]) == 0
    written = json.loads((tmp_path / "quantities.json").read_text(encoding="utf-8"))
    assert written == {"cd": 2.04, "recirculation_length_over_d": 0.9}


def test_re20_with_three_arguments_uses_the_given_paths(tmp_path, monkeypatch, re20):
    prepare_working_directory(tmp_path, monkeypatch, re20)
    n = 400
    write_coefficients(tmp_path / "case", np.arange(1, n + 1), np.full(n, 2.04), np.zeros(n))
    monkeypatch.setattr(re20, "recirculation_length_over_d", lambda case_dir: 0.9)
    out = tmp_path / "elsewhere.json"
    assert re20.main(["post.py", str(tmp_path / "case"), str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["cd"] == 2.04


@pytest.mark.parametrize("name", ["re20", "re100"])
def test_two_arguments_are_a_usage_error(tmp_path, capsys, request, name):
    module = request.getfixturevalue(name)
    assert module.main(["post.py", "case"]) == 2
    assert "usage" in capsys.readouterr().err
