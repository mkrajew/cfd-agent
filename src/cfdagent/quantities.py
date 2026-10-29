"""Type and validation of the quantities file that post-processing writes and scoring reads.

``validate_quantities`` is pure Python and runs in the orchestrator on JSON written by an agent, so
it treats its input as hostile: anything it does not understand is rejected with a message that
names the quantity and the rule, so that the agent can fix it.
"""

from __future__ import annotations

import math

# Profile: {"x": [...], "value": [...]}; scalar: float.
Quantities = dict[str, float | dict[str, list[float]]]

# Keys every profile must have (and the only keys it may have).
REQUIRED_PROFILE_KEYS = ("x", "value")

# A profile may hold at most this many points. It bounds the memory and time that checking and
# scoring spend on agent-written data; real profiles have tens to a few thousand points.
MAX_PROFILE_POINTS = 1_000_000

MIN_PROFILE_POINTS = 2


def _finite_float(value: object, what: str) -> float:
    """``value`` as a finite float; a bool, a non-number or a non-finite number raises."""
    if isinstance(value, bool):
        raise ValueError(f"{what}: a bool is not a number; write a number such as 1.0")
    if not isinstance(value, int | float):
        raise ValueError(f"{what}: expected a number, got {type(value).__name__}")
    try:
        number = float(value)
    except OverflowError:
        raise ValueError(f"{what}: the number is too large to be a float") from None
    if not math.isfinite(number):
        raise ValueError(f"{what}: the value is not finite (NaN or infinity)")
    return number


def _profile(name: str, data: dict) -> dict[str, list[float]]:
    keys = set(data)
    if keys != set(REQUIRED_PROFILE_KEYS):
        raise ValueError(
            f"quantity {name!r}: a profile must have exactly the keys 'x' and 'value', "
            f"got {sorted(map(str, keys))}"
        )
    for key in REQUIRED_PROFILE_KEYS:
        if not isinstance(data[key], list):
            raise ValueError(
                f"quantity {name!r}: {key!r} must be a list of numbers, "
                f"got {type(data[key]).__name__}"
            )
    lengths = {key: len(data[key]) for key in REQUIRED_PROFILE_KEYS}
    if lengths["x"] != lengths["value"]:
        raise ValueError(
            f"quantity {name!r}: 'x' has {lengths['x']} points but 'value' has {lengths['value']}; "
            "the lists must have equal length"
        )
    if lengths["x"] < MIN_PROFILE_POINTS:
        raise ValueError(
            f"quantity {name!r}: a profile needs at least {MIN_PROFILE_POINTS} points, "
            f"got {lengths['x']}"
        )
    if lengths["x"] > MAX_PROFILE_POINTS:
        raise ValueError(
            f"quantity {name!r}: a profile may have at most {MAX_PROFILE_POINTS} points, "
            f"got {lengths['x']}"
        )
    return {
        key: [_finite_float(v, f"quantity {name!r}, {key}[{i}]") for i, v in enumerate(data[key])]
        for key in REQUIRED_PROFILE_KEYS
    }


def validate_quantities(data: object) -> Quantities:
    """Check ``data`` (parsed quantities JSON) and return a clean copy with all numbers as floats.

    ``data`` must be a dict whose keys are strings and whose values are either a finite number
    (a bool is not a number here) or a profile: a dict with exactly the keys ``x`` and ``value``,
    both lists of finite numbers with equal length between 2 and ``MAX_PROFILE_POINTS``. Raises
    ``ValueError`` naming the quantity and the broken rule, writing and changing nothing.
    """
    if not isinstance(data, dict):
        raise ValueError(
            f"quantities must be a JSON object mapping names to numbers or profiles, "
            f"got {type(data).__name__}"
        )
    clean: Quantities = {}
    for name, value in data.items():
        if not isinstance(name, str):
            raise ValueError(f"quantity names must be strings, got {type(name).__name__}")
        if isinstance(value, dict):
            clean[name] = _profile(name, value)
        elif isinstance(value, list | tuple):
            raise ValueError(
                f"quantity {name!r}: a list is neither a number nor a profile; a profile is "
                "{'x': [...], 'value': [...]}"
            )
        else:
            clean[name] = _finite_float(value, f"quantity {name!r}")
    return clean
