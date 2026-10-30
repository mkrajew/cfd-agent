"""Case files that carry their own code, shared by the integration and the sandbox tests.

Every variant lists edits (file of the case, text appended to it) and the ops that must refuse the
case. The code in each variant would leave a marker file if it ever ran; ``marker_dir`` is a place
the test environment can write to (``/tmp`` for the integration tests, ``/job`` in the sandbox).
"""

import pytest

MARKER = "EXECUTED_MARKER"
ALL_OPS = ("check_dictionaries", "trial_run", "run_solver")
SOLVER_OPS = ("trial_run", "run_solver")
# The case's own controlDict asks for the switch: it must have no effect.
CASE_INFO_SWITCH = "InfoSwitches { allowSystemOperations 1; }\n"


def hostile_cases(marker_dir: str) -> dict[str, tuple[list[tuple[str, str]], tuple[str, ...]]]:
    path = f"{marker_dir}/{MARKER}"
    code_stream = (
        "trap #codeStream\n{\n    codeInclude #{ #include <fstream> #};\n"
        f'    code #{{ std::ofstream("{path}") << "ran"; os << 1; #}};\n}};\n'
    )
    calc = f'trap #calc "system(\\"touch {path}\\")";\n'
    coded_patch = (
        "boundaryField { movingWall { type codedFixedValue; name probe; "
        f'code #{{ std::ofstream("{path}") << "ran"; #}}; }} }}\n'
    )
    return {
        "codestream in controlDict": ([("system/controlDict", code_stream)], ALL_OPS),
        "calc in a 0/ file": ([("0/p", calc)], ALL_OPS),
        "coded patch type": ([("0/p", coded_patch)], SOLVER_OPS),
        # The switch is requested in system/controlDict, the code sits in ANOTHER file.
        "info switch in the case, coded patch elsewhere": (
            [("system/controlDict", CASE_INFO_SWITCH), ("0/p", coded_patch)],
            SOLVER_OPS,
        ),
        "info switch in the case, calc elsewhere": (
            [("system/controlDict", CASE_INFO_SWITCH), ("0/U", calc)],
            SOLVER_OPS,
        ),
    }


def hostile_params(marker_dir: str) -> list:
    return [
        pytest.param(name, op, id=f"{name}-{op}")
        for name, (_, ops) in hostile_cases(marker_dir).items()
        for op in ops
    ]
