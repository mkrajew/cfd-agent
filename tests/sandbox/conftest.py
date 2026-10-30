"""Sandbox tests start real hardened containers with ``docker run`` against the image.

They run on any machine with Docker and the image (the Windows laptop through Docker Desktop, the
VM) and are skipped, with the reason, where Docker or the image is missing. The image holds the
installed ``cfdagent`` package: rebuild it after changing ``src/``, because the sandbox mounts
only the run directory, never the repository. The image for the executor containers is
``$CFDAGENT_SANDBOX_IMAGE`` (default ``cfdagent:dev``, the ``test`` target; CI uses the shipped
``runtime`` target, which these tests also pass against):
``docker build -f docker/Dockerfile --target runtime -t cfdagent:runtime .``.
``test_runtime_image_*`` inspects ``$CFDAGENT_RUNTIME_IMAGE`` (default ``cfdagent:runtime``) and is
skipped without it.
"""

import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from cfdagent.rundir import RunDir
from cfdagent.sandbox.client import SandboxClient, outer_timeout
from cfdagent.sandbox.config import SandboxConfig

IMAGE = os.environ.get("CFDAGENT_SANDBOX_IMAGE", "cfdagent:dev")


def _docker_problem() -> str | None:
    if shutil.which("docker") is None:
        return "docker is not on PATH"
    try:
        found = subprocess.run(
            ["docker", "image", "inspect", IMAGE],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"docker does not answer: {exc}"
    if found.returncode != 0:
        return f"the image {IMAGE} is not available ({found.stderr.strip()[:200]})"
    return None


def pytest_collection_modifyitems(config, items):
    problem = _docker_problem()
    if problem is None:
        return
    skip = pytest.mark.skip(reason=f"sandbox tests need Docker and the image: {problem}")
    for item in items:
        if item.get_closest_marker("sandbox"):
            item.add_marker(skip)


class Sandbox:
    """A run directory plus a docker-mode client for it."""

    def __init__(self, root: Path, **config_overrides):
        root.mkdir(parents=True, exist_ok=True)
        self.run_id = f"20260101-000000-t{uuid.uuid4().hex[:8]}"
        self.run_dir = RunDir(root)
        self.config = SandboxConfig(image=IMAGE, **config_overrides)
        self.client = SandboxClient(self.config, self.run_dir, self.run_id)

    def script(self, name: str, source: str) -> None:
        self.run_dir.write_text(name, source)

    def run_script(self, name: str, timeout_s: float = 120.0, **extra):
        """Write nothing; run script ``name`` (already in the run directory) in a container."""
        return self.client.call(
            "run_script", {"script": name, **extra}, timeout_s=outer_timeout(timeout_s)
        )

    def containers(self) -> str:
        """Ids of any container (running or not) whose name belongs to this run."""
        listed = subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"name=cfd-{self.run_id}-"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return listed.stdout.strip()


@pytest.fixture
def make_sandbox(tmp_path):
    counter = iter(range(1000))

    def make(**config_overrides) -> Sandbox:
        return Sandbox(tmp_path / f"run{next(counter)}", **config_overrides)

    return make


@pytest.fixture
def sandbox(make_sandbox) -> Sandbox:
    return make_sandbox()
