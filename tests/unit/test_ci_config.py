"""Consistency checks of the CI, pre-commit, Dependabot and branch-protection configuration."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
CI = WORKFLOWS / "ci.yml"
PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
VERSION_COMMENT = re.compile(r"^#\s*v\d+(?:\.\d+\.\d+)?$")  # "# v4" or "# v4.4.0"


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def uses_references(workflow: dict) -> list[str]:
    refs: list[str] = []
    for job in workflow["jobs"].values():
        if "uses" in job:  # a reusable workflow called at job level
            refs.append(job["uses"])
        refs.extend(step["uses"] for step in job.get("steps", []) if "uses" in step)
    return refs


def unpinned(refs: list[str]) -> list[str]:
    # Local actions (./path) live in this repository and have no ref to pin.
    return [ref for ref in refs if not ref.startswith("./") and not PINNED.match(ref)]


def test_every_workflow_action_is_pinned_to_a_commit_sha():
    workflows = sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))
    assert workflows, "no workflow files found"
    refs = [ref for path in workflows for ref in uses_references(load_yaml(path))]
    assert refs, "no 'uses:' found: the check would pass vacuously"
    assert unpinned(refs) == [], "pin actions to a 40-hex commit SHA (tag in a comment)"


def test_every_pinned_action_names_its_tag_in_a_comment():
    pattern = re.compile(r"^\s*(?:- )?uses:\s*(\S+)\s*(#\s*\S+)?\s*$")
    for path in WORKFLOWS.glob("*.y*ml"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            match = pattern.match(line)
            if match and not match.group(1).startswith("./"):
                comment = match.group(2)
                assert comment and VERSION_COMMENT.match(comment), (
                    f"{path.name}:{number}: add the tag as a comment (# vX or # vX.Y.Z)"
                )


def test_pin_check_can_fail():
    assert unpinned(["actions/checkout@v4"]) == ["actions/checkout@v4"]
    assert unpinned(["actions/checkout@" + "a" * 39]) != []
    assert unpinned(["actions/checkout@" + "a" * 40, "./local-action"]) == []
    # Job-level 'uses' (reusable workflows) are collected too.
    workflow = {"jobs": {"call": {"uses": "org/repo/.github/workflows/w.yml@main"}}}
    assert uses_references(workflow) == ["org/repo/.github/workflows/w.yml@main"]
    assert unpinned(uses_references(workflow)) != []
    assert not VERSION_COMMENT.match("# main")
    assert VERSION_COMMENT.match("# v4") and VERSION_COMMENT.match("# v4.4.0")


def test_lint_job_runs_pre_commit_without_the_shellcheck_download():
    steps = load_yaml(CI)["jobs"]["lint"]["steps"]
    runs = [step for step in steps if "pre-commit run --all-files" in step.get("run", "")]
    assert runs, "the lint job must run pre-commit on all files"
    skip = runs[0].get("env", {}).get("SKIP", "")
    assert "shellcheck" in skip.split(","), "the shellcheck hook downloads a binary"


def test_infra_lint_shellchecks_every_shell_script_directory():
    steps = load_yaml(CI)["jobs"]["infra-lint"]["steps"]
    runs = [step.get("run", "").lstrip() for step in steps]
    commands = [run for run in runs if run.startswith("shellcheck")]
    assert commands, "infra-lint has no shellcheck step"
    for pattern in ("infra/vm/*.sh", "scripts/*.sh", "docker/*.sh"):
        assert pattern in commands[0]


def test_end_of_file_fixer_excludes_captured_logs():
    config = load_yaml(REPO_ROOT / ".pre-commit-config.yaml")
    hooks = {hook["id"]: hook for repo in config["repos"] for hook in repo["hooks"]}
    exclude = hooks["end-of-file-fixer"].get("exclude", "")
    assert re.search(exclude, "tests/fixtures/logs/icoFoam_ok.log")
    assert not re.search(exclude, "README.md")


def test_dependabot_ignores_openfoam_base_image_tags():
    config = load_yaml(REPO_ROOT / ".github" / "dependabot.yml")
    (docker,) = (u for u in config["updates"] if u["package-ecosystem"] == "docker")
    ignored = {entry["dependency-name"] for entry in docker.get("ignore", [])}
    assert "opencfd/openfoam-default" in ignored


def branch_protection_contexts() -> list[str]:
    text = (REPO_ROOT / "scripts" / "github-setup.sh").read_text(encoding="utf-8")
    match = re.search(r'"contexts":\s*\[([^\]]*)\]', text)
    assert match, "no required contexts in scripts/github-setup.sh"
    return re.findall(r'"([^"]+)"', match.group(1))


def test_required_checks_are_the_ci_jobs_that_gate_merging():
    jobs = load_yaml(CI)["jobs"]
    job_names = {job.get("name", key) for key, job in jobs.items()}
    contexts = branch_protection_contexts()
    assert set(contexts) <= job_names, "a required check does not exist in ci.yml"
    assert {"lint", "infra-lint", "unit", "image", "integration", "sandbox"} <= set(contexts)
    # Everything that must be green before publishing must also be green before merging.
    assert set(jobs["publish"]["needs"]) <= set(contexts)
