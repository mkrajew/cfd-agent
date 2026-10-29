"""Type of the quantities file that post-processing writes and scoring reads."""

from __future__ import annotations

# Profile: {"x": [...], "value": [...]}; scalar: float.
Quantities = dict[str, float | dict[str, list[float]]]

# Keys every profile must have.
REQUIRED_PROFILE_KEYS = ("x", "value")
