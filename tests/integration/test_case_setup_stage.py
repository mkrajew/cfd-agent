"""The Case Setup stage end to end inside the image: scripted models write the geometry, mesh and
case dictionaries, the local-mode executor runs the real dictionary check, the consistency check
and a real trial run of the solver. Local mode imports the ``cfdagent`` under test (the
checked-out sources), see ``SandboxClient._command``.
"""

from pathlib import Path

import pytest
from test_geometry_stage import CAVITY_SCRIPT, call, make_ctx, reply
from test_mesh_stage import run_geometry

from cfdagent.agents.case_setup import CaseSetupStage
from cfdagent.agents.loop import run_stage
from cfdagent.agents.mesh import MeshStage
from cfdagent.llm.scripted import ScriptedBackend

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CASE_FIXTURE = FIXTURES / "handwritten" / "cavity_re100" / "case"
CASE_FILES = (
    "0/U",
    "0/p",
    "constant/transportProperties",
    "system/controlDict",
    "system/fvSchemes",
    "system/fvSolution",
)
FIXED_WALLS_P_ENTRY = "    fixedWalls\n    {\n        type            zeroGradient;\n    }\n"
TUTORIAL_CONTROL_DICT = "incompressible/icoFoam/cavity/cavity/system/controlDict"


def fixture_text(rel):
    return (CASE_FIXTURE / rel).read_bytes().decode("utf-8")


def write_case_step(number, changes=None, skip=()):
    """One model reply that writes the case files of attempt ``number``.

    ``changes`` maps a file to its new text (or a function of the hand-written text); files in
    ``skip`` are not written.
    """
    changes = changes or {}
    calls = []
    for index, rel in enumerate(CASE_FILES):
        if rel in skip:
            continue
        change = changes.get(rel)
        text = fixture_text(rel) if change is None else change
        if callable(change):
            text = change(fixture_text(rel))
        path = f"attempts/case_setup-{number}/case/{rel}"
        calls.append(call(f"w{number}-{index}", "write_file", path=path, content=text))
    return reply(*calls)


def check_step(number):
    return reply(call(f"k{number}", "check_stage"))


def pipeline(tmp_path):
    """A context whose run directory holds the geometry and the mesh of the 64 x 64 test case."""
    ctx = make_ctx(tmp_path, ScriptedBackend([]))
    run_geometry(ctx, CAVITY_SCRIPT)
    ctx.backend = ScriptedBackend.from_json(FIXTURES / "scripted" / "mesh_ok.json")
    outcome = run_stage(MeshStage(), ctx)
    assert outcome.ok, outcome
    return ctx


def run_case_setup(ctx, *steps):
    ctx.backend = ScriptedBackend(list(steps))
    return run_stage(CaseSetupStage(), ctx)


def tool_results(backend):
    """The tool messages the model was shown in its last turn, by tool call id."""
    messages = backend.calls[-1].messages
    return {m["tool_call_id"]: m["content"] for m in messages if m["role"] == "tool"}


def test_case_setup_stage_passes_scripted_cavity(tmp_path):
    ctx = pipeline(tmp_path)
    ctx.backend = ScriptedBackend.from_json(FIXTURES / "scripted" / "case_setup_ok.json")
    outcome = run_stage(CaseSetupStage(), ctx)
    assert outcome.ok, outcome
    assert outcome.attempts == 1

    root = ctx.run.root
    assert sorted(p.name for p in (root / "case").iterdir()) == ["0", "constant", "system"]
    assert (root / "case" / "constant" / "polyMesh" / "boundary").is_file()
    assert (root / "boundary_manifest.json").is_file()  # still beside case/
    assert "application     icoFoam;" in (root / "case" / "system" / "controlDict").read_text(
        encoding="utf-8"
    )
    assert [(a.n, a.ok) for a in ctx.record.stages[2].attempts] == [(1, True)]
    assert ctx.record.stages[2].name == "case_setup"
    assert (root / "case_setup").is_dir()


def test_case_setup_rejects_missing_controldict(tmp_path):
    ctx = pipeline(tmp_path)
    outcome = run_case_setup(
        ctx,
        write_case_step(1, skip=("system/controlDict",)),
        check_step(1),
        write_case_step(2),
        check_step(2),
    )
    assert outcome.ok and outcome.attempts == 2
    first = ctx.record.stages[2].attempts[0]
    assert not first.ok
    assert "system/controlDict does not exist" in "\n".join(first.errors)
    assert ctx.record.stages[2].attempts[1].ok


def test_trial_run_failure_reaches_the_agent(tmp_path):
    ctx = pipeline(tmp_path)

    def drop_convection_scheme(text):
        broken = text.replace("    div(phi,U)      Gauss linear;\n", "")
        assert broken != text
        return broken

    broken = {"system/fvSchemes": drop_convection_scheme}
    backend = ScriptedBackend(
        [
            write_case_step(1, broken),
            check_step(1),
            write_case_step(2),
            check_step(2),
        ]
    )
    ctx.backend = backend
    outcome = run_stage(CaseSetupStage(), ctx)
    assert outcome.ok and outcome.attempts == 2
    errors = "\n".join(ctx.record.stages[2].attempts[0].errors)
    assert "trial run" in errors and "fatal error" in errors.lower()
    assert "div(phi,U)" in errors and "not found" in errors
    # The model saw it, in the result of its first check, together with the fresh attempt.
    feedback = tool_results(backend)["k1"]
    assert "fatal error" in feedback.lower() and "div(phi,U)" in feedback
    assert "attempt 2" in feedback


def test_every_missing_patch_entry_is_reported_at_once(tmp_path):
    ctx = pipeline(tmp_path)

    def drop_p(text):
        broken = text.replace(FIXED_WALLS_P_ENTRY, "")
        assert broken != text
        return broken

    def drop_u(text):
        return text.replace("    movingWall\n", "    lid\n")

    outcome = run_case_setup(
        ctx,
        write_case_step(1, {"0/p": drop_p, "0/U": drop_u}),
        check_step(1),
        write_case_step(2),
        check_step(2),
    )
    assert outcome.ok and outcome.attempts == 2
    errors = ctx.record.stages[2].attempts[0].errors
    assert any("0/p" in e and "fixedWalls" in e for e in errors)
    assert any("0/U" in e and "movingWall" in e for e in errors)
    assert not any("trial run" in e for e in errors)  # the trial is meaningless before this


def test_an_empty_patch_with_the_wrong_type_and_a_wrong_application_are_reported(tmp_path):
    ctx = pipeline(tmp_path)
    changes = {
        "0/p": lambda text: text.replace(
            "frontAndBack\n    {\n        type            empty;",
            "frontAndBack\n    {\n        type            zeroGradient;",
        ),
        "system/controlDict": lambda text: text.replace(
            "application     icoFoam;", "application     pimpleFoam;"
        ).replace("endTime         40;", "endTime         0;"),
    }
    outcome = run_case_setup(
        ctx, write_case_step(1, changes), check_step(1), write_case_step(2), check_step(2)
    )
    assert outcome.ok and outcome.attempts == 2
    text = "\n".join(ctx.record.stages[2].attempts[0].errors)
    assert "frontAndBack" in text and "type empty" in text
    assert "pimpleFoam" in text and "icoFoam" in text
    assert "endTime" in text and "startTime" in text


def test_the_mesh_cannot_be_written_and_stays_as_the_mesh_stage_left_it(tmp_path):
    ctx = pipeline(tmp_path)
    points = ctx.run.root / "case" / "constant" / "polyMesh" / "points"
    before = points.read_bytes()
    target = "attempts/case_setup-1/case/constant/polyMesh/points"
    backend = ScriptedBackend(
        [
            reply(call("m1", "write_file", path=target, content="not a mesh\n")),
            write_case_step(1),
            check_step(1),
        ]
    )
    ctx.backend = backend
    outcome = run_stage(CaseSetupStage(), ctx)
    assert outcome.ok, outcome
    refused = [m["content"] for m in backend.calls[1].messages if m["role"] == "tool"]
    assert refused and refused[0].startswith("error:") and "not writable" in refused[0]
    assert points.read_bytes() == before


def test_the_agent_can_search_read_and_validate(tmp_path):
    ctx = pipeline(tmp_path)
    backend = ScriptedBackend(
        [
            reply(call("s1", "search_knowledge", query="icoFoam controlDict endTime")),
            reply(call("r1", "read_knowledge", path=TUTORIAL_CONTROL_DICT)),
            reply(call("r2", "read_knowledge", path="../outside")),
            write_case_step(1),
            reply(call("v1", "validate_dictionaries")),
            check_step(1),
        ]
    )
    ctx.backend = backend
    outcome = run_stage(CaseSetupStage(), ctx)
    assert outcome.ok, outcome
    results = tool_results(backend)
    assert "controlDict" in results["s1"] and ":" in results["s1"]
    assert "application" in results["r1"] and "icoFoam" in results["r1"]
    assert results["r2"].startswith("error:")
    assert "No problems found" in results["v1"]


FUNCTION_OBJECTS = """\
functions
{
    solverInfo
    {
        type            solverInfo;
        libs            (utilityFunctionObjects);
        fields          (p U);
        writeControl    timeStep;
        writeInterval   1;
    }

    probes
    {
        type            probes;
        libs            (sampling);
        fields          (p U);
        probeLocations  ((0.5 0.5 0.05));
        writeControl    timeStep;
        writeInterval   1;
    }

    forces
    {
        type            forces;
        libs            (forces);
        patches         (movingWall);
        rho             rhoInf;
        rhoInf          1;
        CofR            (0.5 0.5 0.05);
        writeControl    timeStep;
        writeInterval   1;
    }

    lidPressure
    {
        type            surfaceFieldValue;
        libs            (fieldFunctionObjects);
        regionType      patch;
        name            movingWall;
        operation       areaAverage;
        fields          (p);
        writeFields     false;
        writeControl    timeStep;
        writeInterval   1;
    }

    meanSpeed
    {
        type            volFieldValue;
        libs            (fieldFunctionObjects);
        operation       volAverage;
        fields          (p);
        writeFields     false;
        writeControl    timeStep;
        writeInterval   1;
    }
"""


def test_the_function_objects_the_prompt_names_run_in_the_trial(tmp_path):
    ctx = pipeline(tmp_path)

    def add_functions(text):
        # Replace the head of the hand-written functions block, keep its sets.
        return text.replace("functions\n{\n", FUNCTION_OBJECTS + "\n", 1)

    outcome = run_case_setup(
        ctx, write_case_step(1, {"system/controlDict": add_functions}), check_step(1)
    )
    assert outcome.ok, outcome


WALL_SHEAR = """\
functions
{
    wallShearStress
    {
        type            wallShearStress;
        libs            (fieldFunctionObjects);
        patches         (movingWall fixedWalls);
        writeControl    timeStep;
        writeInterval   1;
    }

"""
PIMPLE_SOLUTION = """FoamFile { version 2.0; format ascii; class dictionary; object fvSolution; }

solvers
{
    p { solver PCG; preconditioner DIC; tolerance 1e-08; relTol 0.05; }
    pFinal { $p; relTol 0; }
    "(U|UFinal)" { solver smoothSolver; smoother symGaussSeidel; tolerance 1e-08; relTol 0; }
}

PIMPLE
{
    nOuterCorrectors 1;
    nCorrectors     2;
    nNonOrthogonalCorrectors 0;
    pRefCell        0;
    pRefValue       0;
}
"""


def pimple_case_changes():
    """The hand-written dictionaries turned into a laminar ``pimpleFoam`` case."""
    return {
        "system/controlDict": lambda t: t.replace("icoFoam;", "pimpleFoam;").replace(
            "functions\n{\n", WALL_SHEAR, 1
        ),
        "system/fvSchemes": lambda t: t.replace(
            "    div(phi,U)      Gauss linear;\n",
            "    div(phi,U)      Gauss linear;\n    div((nuEff*dev2(T(grad(U))))) Gauss linear;\n",
        ),
        "system/fvSolution": PIMPLE_SOLUTION,
        "constant/transportProperties": lambda t: t.replace(
            "nu              0.01;", "transportModel  Newtonian;\nnu              0.01;"
        ),
    }


def test_a_laminar_pimple_case_with_wall_shear_stress_runs_in_the_trial(tmp_path):
    from test_geometry_stage import CAVITY_SPEC

    from cfdagent.agents.casespec import CaseSpec

    ctx = pipeline(tmp_path)
    spec = {**CAVITY_SPEC, "solver": {**CAVITY_SPEC["solver"], "suggested_solver": "pimpleFoam"}}
    ctx.casespec = CaseSpec.model_validate(spec)
    turbulence = (
        "FoamFile { version 2.0; format ascii; class dictionary; object turbulenceProperties; }\n"
        "simulationType laminar;\n"
    )
    first = write_case_step(1, pimple_case_changes())
    extra = reply(
        call(
            "w1-turb",
            "write_file",
            path="attempts/case_setup-1/case/constant/turbulenceProperties",
            content=turbulence,
        )
    )
    outcome = run_case_setup(ctx, first, extra, check_step(1))
    assert outcome.ok, ctx.record.stages[2].attempts[0].errors
