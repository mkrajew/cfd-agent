import json
import re
from fnmatch import fnmatchcase
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

EXPECTED = {
    "implementer",
    "code-reviewer",
    "cfd-reviewer",
    "infra-engineer",
    "scribe",
    "report-writer",
}


def load_agent_frontmatter(agents_dir: Path) -> dict[str, dict[str, str]]:
    """Parse the simple `key: value` YAML frontmatter of every agent file in a directory."""
    agents: dict[str, dict[str, str]] = {}
    for path in sorted(agents_dir.glob("*.md")):
        lines = path.read_text(encoding="utf-8").splitlines()
        assert lines and lines[0] == "---", f"{path.name}: missing frontmatter"
        end = lines.index("---", 1)
        frontmatter: dict[str, str] = {}
        for line in lines[1:end]:
            key, _, value = line.partition(":")
            frontmatter[key.strip()] = value.strip()
        agents[path.stem] = frontmatter
    return agents


def test_all_agents_defined_with_frontmatter():
    agents = load_agent_frontmatter(REPO_ROOT / ".claude" / "agents")
    assert set(agents) == EXPECTED
    for name, fm in agents.items():
        assert fm["name"] == name
        assert len(fm["description"]) > 40
        assert "model" not in fm


def _rules(kind: str) -> list[tuple[str, str]]:
    """Return (tool, pattern) pairs for one permission list of .claude/settings.json."""
    settings = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    rules = []
    for rule in settings["permissions"][kind]:
        match = re.fullmatch(r"(\w+)\((.*)\)", rule)
        assert match, rule
        rules.append((match.group(1), match.group(2)))
    return rules


def _matches(kind: str, tool: str, command: str) -> bool:
    """Simulate wildcard matching of a command against the rules of one permission list."""
    return any(t == tool and fnmatchcase(command, pat) for t, pat in _rules(kind))


IRREVERSIBLE_AZ = [
    "group delete -n rg-cfdagent --yes",
    "storage account delete -n x -g rg-cfdagent",
    "storage container delete -n runs --account-name x",
]
AZ_EXE = r'"C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd"'
AZ_VM_CREATE = AZ_EXE + " vm create"
AZ_FORMS = ["az {c}", AZ_EXE + " {c}"]
FORCE_PUSHES = [
    "git push --force origin x",
    "git push origin x --force-with-lease",
    "git push -f origin x",
]


def test_push_is_allowed_to_origin_but_never_forced():
    """Owner's decision of 2026-11-14: a push to origin does not prompt; force pushes are denied."""
    for tool in ("Bash", "PowerShell"):
        assert _matches("allow", tool, "git push origin docs/x"), tool
        assert not _matches("ask", tool, "git push origin docs/x"), tool
        for command in FORCE_PUSHES:
            assert _matches("deny", tool, command), (tool, command)


def test_az_runs_without_a_prompt_except_irreversible_deletes():
    """Standing approval of rule 4 in CLAUDE.md: az is allowed, nothing asks, deletes of the
    resource group, the storage account and containers are denied (deny wins over allow)."""
    assert _rules("ask") == []
    for tool in ("Bash", "PowerShell"):
        for command in ("az vm create -g x", AZ_VM_CREATE, "az deployment group what-if -g x"):
            assert _matches("allow", tool, command), (tool, command)
        for form in AZ_FORMS:
            for tail in IRREVERSIBLE_AZ:
                command = form.format(c=tail)
                assert _matches("deny", tool, command), (tool, command)
        # Legitimate neighbours are not caught by the deny rules.
        for tail in ("cognitiveservices account show -n x", "storage account show -n x"):
            assert not _matches("deny", tool, f"az {tail}"), (tool, tail)


def test_project_commands_are_allowed():
    for tool in ("Bash", "PowerShell"):
        for command in (
            "uv run pytest",
            "docker build -f docker/Dockerfile -t cfdagent:dev .",
            "docker run --rm -v /w:/work cfdagent:dev python -m pytest",
            "git status",
            "git commit -m x",
        ):
            assert _matches("allow", tool, command), (tool, command)
    assert not _matches("allow", "Bash", "docker run --rm alpine sh")
    assert not _matches("allow", "Bash", "git config user.name x")


def test_dangerous_commands_are_denied():
    denied = [
        "docker run --rm --privileged cfdagent:dev sh",
        "docker run -v /var/run/docker.sock:/s cfdagent:dev sh",
        "docker run --network host cfdagent:dev sh",
        "docker build --push -t x .",
        "git reset --hard HEAD~1",
        "git clean -fd",
        "git config user.name x",
        "git commit --no-verify -m x",
    ]
    for tool in ("Bash", "PowerShell"):
        for command in denied:
            assert _matches("deny", tool, command), (tool, command)
    assert any("--privileged" in pat for _, pat in _rules("deny"))


def test_journal_entries_follow_format():
    journal_dir = REPO_ROOT / "docs" / "journal"
    entries = []
    for path in journal_dir.glob("2*.md"):
        entries += [
            line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("## ")
        ]
    assert entries
    pattern = re.compile(
        r"^## \d{4}-\d{2}-\d{2} (decision|obstacle|milestone|experiment|observation): \S.*$"
    )
    for line in entries:
        assert pattern.match(line), line
