"""Integration tests run inside the Docker image, where the OpenFOAM environment is sourced.

On the host (no ``WM_PROJECT_VERSION``) they are skipped instead of failing.
"""

import os

import pytest


def pytest_collection_modifyitems(config, items):
    if os.environ.get("WM_PROJECT_VERSION"):
        return
    skip = pytest.mark.skip(reason="OpenFOAM environment not found; run inside the Docker image")
    for item in items:
        if item.get_closest_marker("integration"):
            item.add_marker(skip)
