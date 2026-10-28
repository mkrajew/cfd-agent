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


AZ_CMD = r'"C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin\az.cmd" vm create'
PUSH_COMMANDS = ["git push", "git push origin main", "git -C /x push origin main"]
AZ_COMMANDS = ["az vm create -g x", AZ_CMD, "az deployment group what-if -g x", f"& {AZ_CMD}"]


def test_no_allow_rule_matches_git_push():
    for tool in ("Bash", "PowerShell"):
        for command in PUSH_COMMANDS:
            assert not _matches("allow", tool, command), (tool, command)
            assert _matches("ask", tool, command), (tool, command)


def test_every_az_invocation_asks_and_is_never_allowed():
    for command in AZ_COMMANDS:
        assert _matches("ask", "Bash", command), command
        assert not _matches("allow", "Bash", command), command
    assert _matches("ask", "PowerShell", f"& {AZ_CMD}")
    assert _matches("ask", "PowerShell", "az vm create -g x")


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
