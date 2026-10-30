"""Sandbox settings. Every value ends up on a ``docker run`` command line, so all are validated."""

from __future__ import annotations

import os
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _host_id(getter: str) -> int:
    """The host user's uid or gid; 1000 where the platform has none (the Windows laptop)."""
    function = getattr(os, getter, None)
    return function() if function is not None else 1000


_SIZE = re.compile(r"[1-9][0-9]{0,9}[bkmgBKMG]?")
_IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}")


class SandboxConfig(BaseModel):
    """How executor containers are started.

    ``mode="docker"``: every call is a fresh hardened container (see ``sandbox.docker``).
    ``mode="local"``: the executor runs as a plain subprocess of the caller. That is NOT
    confined: it exists for tests that already run inside the image and must never be used for
    agent-written code on a machine that holds credentials. It is refused unless
    ``allow_local=True`` is set explicitly (and ``allow_local`` is refused in docker mode).

    ``uid`` / ``gid`` default to the host user's, so files that the container creates in the run
    directory belong to the host user (1000 where the platform has no uid). The container never
    runs as root: 0 is refused.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["docker", "local"] = "docker"
    allow_local: bool = False
    image: str
    uid: int = Field(default_factory=lambda: _host_id("getuid"), ge=1, le=2**31 - 1)
    gid: int = Field(default_factory=lambda: _host_id("getgid"), ge=1, le=2**31 - 1)
    memory: str = "8g"
    cpus: float = Field(default=4, gt=0, le=256)
    pids: int = Field(default=512, ge=1, le=1_000_000)
    tmpfs_size: str = "512m"
    shm_size: str = "512m"

    @model_validator(mode="after")
    def _local_mode_is_an_explicit_choice(self) -> SandboxConfig:
        if self.mode == "local" and not self.allow_local:
            raise ValueError(
                "mode='local' runs agent-written code without a container; "
                "pass allow_local=True to confirm it is only used inside a disposable test image"
            )
        if self.mode == "docker" and self.allow_local:
            raise ValueError("allow_local only makes sense with mode='local'")
        return self

    @field_validator("image")
    @classmethod
    def _image_is_a_plain_reference(cls, value: str) -> str:
        if not _IMAGE.fullmatch(value):
            raise ValueError(f"{value!r} is not a plain image reference (name[:tag][@digest])")
        return value

    @field_validator("memory", "tmpfs_size", "shm_size")
    @classmethod
    def _size_is_plain(cls, value: str) -> str:
        if not _SIZE.fullmatch(value):
            raise ValueError(f"{value!r} must be a positive size such as 512m or 8g")
        return value
