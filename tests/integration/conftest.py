"""Integration tests run inside the Docker image, where the OpenFOAM environment is sourced.

On the host (no ``WM_PROJECT_VERSION``) they are skipped instead of failing.
"""

import os
from pathlib import Path

import pytest

import cfdagent


def pytest_collection_modifyitems(config, items):
    if os.environ.get("WM_PROJECT_VERSION"):
        return
    skip = pytest.mark.skip(reason="OpenFOAM environment not found; run inside the Docker image")
    for item in items:
        if item.get_closest_marker("integration"):
            item.add_marker(skip)


@pytest.fixture(scope="module", autouse=True)
def _scripts_import_checked_out_cfdagent():
    """Scripts run in subprocesses; point them at the cfdagent under test, not the image copy."""
    src = str(Path(cfdagent.__file__).parents[1])
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PYTHONPATH", os.pathsep.join(filter(None, [src, os.environ.get("PYTHONPATH")])))
        yield
