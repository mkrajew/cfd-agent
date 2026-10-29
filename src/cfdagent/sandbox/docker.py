"""The ``docker run`` command line of one executor container.

One container per call: ``docker run --rm``, no network, no capabilities, no privilege gain, a
read-only root file system with a small non-executable ``/tmp``, bounded memory, CPUs and
processes, the host user's uid, no swap, no core dumps, an init process, no image pull, and
exactly one bind mount: the run directory at ``/job``. No environment variable is passed
(``-e`` is never used), so nothing of the caller's environment (cloud credentials, API keys)
reaches the container. Nothing else is mounted: not the repository, not ``benchmarks/``, not the
Docker socket. The executor runs as ``python -P``: its working directory is the hostile run
directory and must not be searched for modules.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from cfdagent.rundir import UnsafePath, split_relative
from cfdagent.sandbox.config import SandboxConfig

CONTAINER_JOB_DIR = "/job"

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_OP = re.compile(r"[a-z][a-z0-9_]{0,63}")


def container_name_ok(name: str) -> bool:
    """True if ``name`` is a valid Docker container name that cannot be read as an option."""
    return bool(_NAME.fullmatch(name))


def build_argv(
    config: SandboxConfig, name: str, run_dir: Path, op: str, request_rel: str
) -> list[str]:
    """The full ``docker run`` command (a list; never run through a shell).

    ``request_rel`` is the run-directory-relative path of the request file the executor reads;
    ``op`` is the op name. Both are validated because they follow the image on the command line.
    """
    if not container_name_ok(name):
        raise ValueError(f"{name!r} is not a valid container name")
    if not _OP.fullmatch(op):
        raise ValueError(f"{op!r} is not a valid op name")
    try:
        split_relative(request_rel)
    except UnsafePath as exc:
        raise ValueError(str(exc)) from None
    if request_rel.startswith("-"):
        raise ValueError(f"{request_rel!r} must not start with '-'")
    mount_source = str(Path(run_dir).resolve())
    # A drive letter is fine; any other ':' is docker -v syntax and could add a second mount.
    if any(":" in os.path.splitdrive(text)[1] for text in (str(run_dir), mount_source)):
        raise ValueError("the run directory path must not contain ':' (it is docker -v syntax)")
    return [
        "docker",
        "run",
        "--rm",
        "--name",
        name,
        "--network",
        "none",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--read-only",
        "--tmpfs",
        f"/tmp:rw,noexec,nosuid,nodev,size={config.tmpfs_size}",
        "--shm-size",
        config.shm_size,
        "--pids-limit",
        str(config.pids),
        "--memory",
        config.memory,
        "--memory-swap",
        config.memory,  # equal to --memory: no swap
        "--init",
        "--pull",
        "never",
        "--ulimit",
        "core=0",
        "--cpus",
        f"{config.cpus:g}",
        "--user",
        f"{config.uid}:{config.gid}",
        "-v",
        f"{mount_source}:{CONTAINER_JOB_DIR}:rw",
        "-w",
        CONTAINER_JOB_DIR,
        config.image,
        "python",
        "-P",  # the working directory is the hostile run directory: keep it off sys.path
        "-m",
        "cfdagent.executor",
        op,
        request_rel,
    ]
