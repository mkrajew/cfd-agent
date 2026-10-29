"""Type and validation of the quantities file that post-processing writes and scoring reads.

``validate_quantities`` is pure Python and runs in the orchestrator on JSON written by an agent, so
it treats its input as hostile: anything it does not understand is rejected with a message that
names the quantity and the rule, so that the agent can fix it.
"""

from __future__ import annotations

import math
from itertools import islice

# Profile: {"x": [...], "value": [...]}; scalar: float.
Quantities = dict[str, float | dict[str, list[float]]]

# Keys every profile must have (and the only keys it may have).
REQUIRED_PROFILE_KEYS = ("x", "value")

# A profile may hold at most this many points. It bounds the memory and time that checking and
# scoring spend on agent-written data; real profiles have tens to a few thousand points. The
# number of quantities and the total number of points are bounded by the caller, which reads the
# file with a size limit; this cap only bounds a single profile.
MAX_PROFILE_POINTS = 1_000_000

MIN_PROFILE_POINTS = 2

_MAX_ECHO_CHARS = 80  # an agent-written name or key is echoed in messages at most this long
_MAX_ECHO_KEYS = 10  # and at most this many profile keys are listed


def _label(name: str) -> str:
    """``name`` for an error message: its repr, cut to a bounded length."""
    text = repr(name)
    return text if len(text) <= _MAX_ECHO_CHARS else text[:_MAX_ECHO_CHARS] + "..."


def _key_list(keys: dict) -> str:
    """The first few keys of a hostile dict for an error message, with a count of the rest."""
    shown = [_label(key) for key in islice(keys, _MAX_ECHO_KEYS)]
    extra = len(keys) - len(shown)
    return "[" + ", ".join(shown) + (f", and {extra} more" if extra > 0 else "") + "]"


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
    label = _label(name)
    if set(data) != set(REQUIRED_PROFILE_KEYS):
        raise ValueError(
            f"quantity {label}: a profile must have exactly the keys 'x' and 'value', "
            f"got {_key_list(data)}"
        )
    for key in REQUIRED_PROFILE_KEYS:
        if not isinstance(data[key], list):
            raise ValueError(
                f"quantity {label}: {key!r} must be a list of numbers, "
                f"got {type(data[key]).__name__}"
            )
    lengths = {key: len(data[key]) for key in REQUIRED_PROFILE_KEYS}
    if lengths["x"] != lengths["value"]:
        raise ValueError(
            f"quantity {label}: 'x' has {lengths['x']} points but 'value' has {lengths['value']}; "
            "the lists must have equal length"
        )
    if lengths["x"] < MIN_PROFILE_POINTS:
        raise ValueError(
            f"quantity {label}: a profile needs at least {MIN_PROFILE_POINTS} points, "
            f"got {lengths['x']}"
        )
    if lengths["x"] > MAX_PROFILE_POINTS:
        raise ValueError(
            f"quantity {label}: a profile may have at most {MAX_PROFILE_POINTS} points, "
            f"got {lengths['x']}"
        )
    return {
        key: [_finite_float(v, f"quantity {label}, {key}[{i}]") for i, v in enumerate(data[key])]
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
        label = _label(name)
        if isinstance(value, dict):
            clean[name] = _profile(name, value)
        elif isinstance(value, list | tuple):
            raise ValueError(
                f"quantity {label}: a list is neither a number nor a profile; a profile is "
                "{'x': [...], 'value': [...]}"
            )
        else:
            clean[name] = _finite_float(value, f"quantity {label}")
    return clean
