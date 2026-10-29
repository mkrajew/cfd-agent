"""The ``docker run`` command line of an executor container, and the SandboxConfig behind it."""

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from cfdagent.sandbox.config import SandboxConfig
from cfdagent.sandbox.docker import CONTAINER_JOB_DIR, build_argv

RUN_DIR = Path("/data/runs/20260101-000000-demo")


def _config(**overrides):
    values = {"image": "cfdagent:test", "uid": 1234, "gid": 4321}
    values.update(overrides)
    return SandboxConfig(**values)


def _argv(config=None, **overrides):
    args = {
        "name": "cfd-demo-1",
        "run_dir": RUN_DIR,
        "op": "check_mesh",
        "request_rel": ".sandbox/c1.request.json",
    }
    args.update(overrides)
    return build_argv(config or _config(), **args)


def _value_after(argv, flag):
    assert argv.count(flag) == 1, f"{flag} must appear exactly once in {argv}"
    return argv[argv.index(flag) + 1]


def test_config_defaults():
    config = _config()
    assert (config.mode, config.memory, config.cpus, config.pids) == ("docker", "8g", 4, 512)
    assert (config.tmpfs_size, config.shm_size) == ("512m", "512m")


def test_config_uid_gid_default_to_the_host_user(monkeypatch):
    monkeypatch.setattr(os, "getuid", lambda: 4242, raising=False)
    monkeypatch.setattr(os, "getgid", lambda: 4343, raising=False)
    config = SandboxConfig(image="x")
    assert (config.uid, config.gid) == (4242, 4343)
    monkeypatch.delattr(os, "getuid", raising=False)
    monkeypatch.delattr(os, "getgid", raising=False)
    config = SandboxConfig(image="x")
    assert (config.uid, config.gid) == (1000, 1000)


@pytest.mark.parametrize(
    "overrides",
    [
        {"image": ""},
        {"image": "-v/etc:/x"},
        {"image": "a b"},
        {"memory": "lots"},
        {"memory": "-1g"},
        {"memory": "8g --privileged"},
        {"cpus": 0},
        {"cpus": -1},
        {"pids": 0},
        {"tmpfs_size": "512m,exec"},
        {"shm_size": "big"},
        {"uid": -1},
        {"uid": 0},
        {"gid": 0},
        {"mode": "ssh"},
    ],
)
def test_config_rejects_unsafe_values(overrides):
    with pytest.raises(ValidationError):
        _config(**overrides)


def test_config_forbids_unknown_fields():
    with pytest.raises(ValidationError):
        _config(privileged=True)


def test_build_argv_has_every_hardening_flag():
    argv = _argv()
    assert argv[:3] == ["docker", "run", "--rm"]
    assert _value_after(argv, "--name") == "cfd-demo-1"
    assert _value_after(argv, "--network") == "none"
    assert _value_after(argv, "--cap-drop") == "ALL"
    assert _value_after(argv, "--security-opt") == "no-new-privileges"
    assert argv.count("--read-only") == 1
    assert _value_after(argv, "--tmpfs") == "/tmp:rw,size=512m"
    assert _value_after(argv, "--shm-size") == "512m"
    assert _value_after(argv, "--pids-limit") == "512"
    assert _value_after(argv, "--memory") == "8g"
    assert _value_after(argv, "--cpus") == "4"
    assert _value_after(argv, "--user") == "1234:4321"
    assert _value_after(argv, "-w") == CONTAINER_JOB_DIR == "/job"
    assert argv.count("-v") == 1


def test_build_argv_image_is_last_before_the_command():
    argv = _argv()
    image_at = argv.index("cfdagent:test")
    assert argv[image_at + 1 :] == [
        "python",
        "-m",
        "cfdagent.executor",
        "check_mesh",
        ".sandbox/c1.request.json",
    ]
    # Every docker option comes before the image: nothing after it is parsed by docker.
    assert all(a != "cfdagent:test" for a in argv[:image_at])


def test_build_argv_uses_the_configured_limits():
    argv = _argv(_config(memory="2g", cpus=1.5, pids=64, tmpfs_size="64m", shm_size="128m"))
    assert _value_after(argv, "--memory") == "2g"
    assert _value_after(argv, "--cpus") == "1.5"
    assert _value_after(argv, "--pids-limit") == "64"
    assert _value_after(argv, "--tmpfs") == "/tmp:rw,size=64m"
    assert _value_after(argv, "--shm-size") == "128m"


def test_argv_mounts_nothing_but_run_dir():
    argv = _argv()
    mount = _value_after(argv, "-v")
    assert mount.endswith(":/job:rw")
    host_side = mount[: -len(":/job:rw")]
    assert host_side.replace("\\", "/").endswith("runs/20260101-000000-demo")
    joined = " ".join(argv)
    assert "docker.sock" not in joined and "benchmarks" not in joined
    options = {a.split("=", 1)[0] for a in argv if a.startswith("-")}
    for forbidden in (
        "--volume",
        "--mount",
        "--volumes-from",
        "--privileged",
        "--cap-add",
        "--pid",
        "--ipc",
        "--uts",
        "--userns",
        "--device",
        "--env",
        "-e",
        "--env-file",
        "--publish",
        "-p",
    ):
        assert forbidden not in options, forbidden
    assert _value_after(argv, "--network") == "none"
    # Only one mount exists, and the working directory is the mount's container side.
    assert [a for a in argv if a in ("-v", "--tmpfs")] == ["--tmpfs", "-v"]


def test_build_argv_requires_a_safe_name_and_op():
    with pytest.raises(ValueError):
        _argv(name="bad name")
    with pytest.raises(ValueError):
        _argv(name="-rm")
    with pytest.raises(ValueError):
        _argv(op="--privileged")
    with pytest.raises(ValueError):
        _argv(request_rel="/etc/passwd")
    with pytest.raises(ValueError):
        _argv(request_rel="../x")
    with pytest.raises(ValueError):
        _argv(request_rel="-x")
