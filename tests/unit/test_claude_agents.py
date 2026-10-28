import json
import re
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


def test_settings_json_guards_azure_and_push():
    settings = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    perms = settings["permissions"]
    assert "Bash(uv run:*)" in perms["allow"]
    assert "Bash(git push:*)" in perms["ask"]
    assert not any("git push" in rule for rule in perms["allow"])
    for verb in ("create", "deploy", "delete", "update", "start"):
        assert any("az " in rule and verb in rule for rule in perms["ask"]), verb


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
