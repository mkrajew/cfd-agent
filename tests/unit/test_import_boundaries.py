"""Machine-checked import boundaries between host-side and container-side packages.

Host-side code runs on the VM outside the container and must not need the CAD/mesh/post toolchain;
container-side code must not depend on the orchestration layers. Packages that do not exist yet
pass vacuously.

Only static imports are detected: dynamic imports (importlib, __import__) and transitive imports
(a host module importing a neutral module that itself imports a forbidden one) are not.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "cfdagent"

# Top-level names (package directory or module file) under ``cfdagent``.
HOST_SIDE = ("agents", "orchestrator", "sandbox", "storage", "cli", "rundir", "llm")
CONTAINER_SIDE = ("tools", "executor")

HOST_FORBIDDEN = ("cfdagent.tools", "cadquery", "gmsh", "pyvista")
CONTAINER_FORBIDDEN = ("cfdagent.bench", "cfdagent.agents", "cfdagent.orchestrator")


def _imported_modules(source: str, module_name: str, is_package: bool) -> set[str]:
    """Absolute dotted names imported by ``source``; ``from a import b`` also yields ``a.b``."""
    found: set[str] = set()
    package = module_name if is_package else module_name.rpartition(".")[0]
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")
                parts = parts[: len(parts) - (node.level - 1)]
                base = ".".join([*parts, base] if base else parts)
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names)
    return found


def _is_forbidden(name: str, forbidden: tuple[str, ...]) -> bool:
    return any(name == bad or name.startswith(bad + ".") for bad in forbidden)


def _violations(
    top_level: tuple[str, ...], forbidden: tuple[str, ...], root: Path = SRC
) -> list[str]:
    problems: list[str] = []
    for top in top_level:
        files = [root / f"{top}.py"] if (root / f"{top}.py").is_file() else []
        if (root / top).is_dir():
            files += sorted((root / top).rglob("*.py"))
        for path in files:
            rel = path.relative_to(root.parent).with_suffix("")
            is_package = path.name == "__init__.py"
            module = ".".join(rel.parts[:-1] if is_package else rel.parts)
            source = path.read_text(encoding="utf-8")
            for name in sorted(_imported_modules(source, module, is_package)):
                if _is_forbidden(name, forbidden):
                    problems.append(f"{path.relative_to(root.parent).as_posix()} imports {name}")
    return problems


def test_scanner_detects_forbidden_imports():
    source = (
        "import gmsh\nfrom cfdagent import tools\nfrom . import bench\nfrom .tools.x import y\n"
    )
    names = _imported_modules(source, "cfdagent.agents.loop", is_package=False)
    assert {"gmsh", "cfdagent.tools", "cfdagent.agents.bench", "cfdagent.agents.tools.x"} <= names
    assert _is_forbidden("cfdagent.tools.geometry", HOST_FORBIDDEN)
    assert _is_forbidden("cadquery", HOST_FORBIDDEN)
    assert not _is_forbidden("cfdagent.toolsbox", HOST_FORBIDDEN)


def test_scanner_resolves_parent_relative_imports():
    source = "from ..tools import x\nfrom .. import tools\n"
    names = _imported_modules(source, "cfdagent.agents.loop", is_package=False)
    assert "cfdagent.tools" in names
    assert "cfdagent.tools.x" in names
    # In a package __init__ the first dot is the package itself, the second its parent.
    names = _imported_modules("from .. import tools\n", "cfdagent.agents", is_package=True)
    assert "cfdagent.tools" in names


def test_violations_walks_files_and_reports_paths(tmp_path):
    pkg = tmp_path / "cfdagent"
    (pkg / "agents" / "deep").mkdir(parents=True)
    (pkg / "agents" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "agents" / "deep" / "bad.py").write_text("import gmsh\n", encoding="utf-8")
    (pkg / "agents" / "deep" / "bad2.py").write_text("from ...tools import x\n", encoding="utf-8")
    (pkg / "agents" / "ok.py").write_text("import json\n", encoding="utf-8")
    (pkg / "cli.py").write_text("from cfdagent.tools import geometry\n", encoding="utf-8")
    found = _violations(("agents", "cli", "missing"), HOST_FORBIDDEN, root=pkg)
    assert "cfdagent/agents/deep/bad.py imports gmsh" in found
    assert "cfdagent/agents/deep/bad2.py imports cfdagent.tools" in found
    assert "cfdagent/cli.py imports cfdagent.tools.geometry" in found
    assert all("ok.py" not in line for line in found)


def test_host_packages_do_not_import_tools_or_toolchain():
    assert _violations(HOST_SIDE, HOST_FORBIDDEN) == []


def test_container_packages_do_not_import_bench_or_orchestrator():
    assert _violations(CONTAINER_SIDE, CONTAINER_FORBIDDEN) == []


def test_neutral_modules_hold_gate_result_and_quantities():
    import dataclasses

    import pytest

    from cfdagent.gates import GateResult
    from cfdagent.quantities import REQUIRED_PROFILE_KEYS, Quantities

    gate = GateResult(True, [])
    assert gate.data is None
    assert GateResult(False, ["bad"], data={"k": 1}).data == {"k": 1}
    with pytest.raises(dataclasses.FrozenInstanceError):
        gate.ok = False  # type: ignore[misc]
    assert REQUIRED_PROFILE_KEYS == ("x", "value")
    sample: Quantities = {"a": 1.0, "p": {"x": [0.0], "value": [1.0]}}
    assert set(sample) == {"a", "p"}


def test_gate_result_is_defined_only_in_the_neutral_module():
    defining = [
        path.relative_to(SRC).as_posix()
        for path in sorted(SRC.rglob("*.py"))
        if any(
            isinstance(node, ast.ClassDef) and node.name == "GateResult"
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        )
    ]
    assert defining == ["gates.py"]

    from cfdagent.tools import geometry, mesh

    # The tool modules use the class but do not advertise it as part of their own API.
    assert "GateResult" not in getattr(geometry, "__all__", ())
    assert "GateResult" not in getattr(mesh, "__all__", ())
