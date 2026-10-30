"""The package version must be the same in every place that states it."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import cfdagent

REPO_ROOT = Path(__file__).resolve().parents[2]


def pyproject_version(text: str) -> str:
    return tomllib.loads(text)["project"]["version"]


def citation_version(text: str) -> str:
    match = re.search(r"^version:\s*[\"']?([^\s\"']+)[\"']?\s*$", text, flags=re.MULTILINE)
    assert match, "CITATION.cff has no top-level 'version:' line"
    return match.group(1)


def versions(pyproject: str, citation: str, package: str) -> dict[str, str]:
    return {
        "pyproject.toml": pyproject_version(pyproject),
        "CITATION.cff": citation_version(citation),
        "cfdagent.__version__": package,
    }


def test_version_is_consistent():
    found = versions(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"),
        (REPO_ROOT / "CITATION.cff").read_text(encoding="utf-8"),
        cfdagent.__version__,
    )
    assert len(set(found.values())) == 1, f"version drift: {found}"


def test_drift_is_detected():
    # The comparison must be able to fail: feed it a drifted CITATION.cff.
    found = versions(
        '[project]\nversion = "1.2.3"\n',
        'cff-version: 1.2.0\nversion: "1.2.4"\n',
        "1.2.3",
    )
    assert found["pyproject.toml"] == "1.2.3"
    assert found["CITATION.cff"] == "1.2.4"
    assert len(set(found.values())) == 2
