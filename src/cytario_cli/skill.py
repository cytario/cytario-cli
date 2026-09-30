"""Install and update the packaged agent skill file into the user's AI tools.

The skill file ships inside the package (cytario_cli/skills/cytario-cli.md)
so the installed CLI always carries the matching skill version — installing
never fetches anything over the network, and re-running install after a CLI
update refreshes stale copies.

Layout per tool (each tool's own discovery rules):
  Claude Code, OpenCode, Codex CLI: <skills>/cytario-cli/SKILL.md — a
    per-skill directory with SKILL.md inside; all three scan that shape.
  Cursor: ~/.cursor/rules/cytario-cli.md — rules are flat markdown files.

A tool counts as present when its CONFIG directory exists (~/.claude,
~/.config/opencode, …), not its skills subdirectory — the skills dir does
not exist on a fresh tool install.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path

SKILL_FILENAME = "cytario-cli.md"
SKILL_PACKAGE = "cytario_cli.skills"


@dataclass(frozen=True)
class ToolTarget:
    """One AI tool the user might have, and where its skills live."""

    id: str
    name: str
    config_dir: Path  # presence marker: the tool's config directory
    skills_dir: Path
    install_subdir: bool  # nest the file in a per-skill directory (SKILL.md)


# Probed in order. User-level (home) locations only — the CLI never writes
# into a project repo. Tool ids are stable — agents pass them to
# `skill install --tool`.
_TOOL_SPECS: tuple[tuple[str, str, str, str, bool], ...] = (
    ("claude-code", "Claude Code", ".claude", ".claude/skills", True),
    ("opencode", "OpenCode", ".config/opencode", ".config/opencode/skills", True),
    ("codex", "Codex CLI", ".codex", ".codex/skills", True),
    ("cursor", "Cursor", ".cursor", ".cursor/rules", False),
)


def all_tools() -> list[ToolTarget]:
    """Return every known tool target; presence is decided by detect_tools()."""
    home = Path.home()
    return [
        ToolTarget(tool_id, name, home / config_dir, home / skills_dir, subdir)
        for tool_id, name, config_dir, skills_dir, subdir in _TOOL_SPECS
    ]


def packaged_skill() -> str:
    """Return the skill file content bundled with the installed CLI."""
    return (resources.files(SKILL_PACKAGE) / SKILL_FILENAME).read_text(encoding="utf-8")


def skill_file(tool: ToolTarget) -> Path:
    """Return the destination path of the skill file for one tool."""
    if tool.install_subdir:
        return tool.skills_dir / "cytario-cli" / "SKILL.md"
    return tool.skills_dir / SKILL_FILENAME


def detect_tools() -> list[ToolTarget]:
    """Return the tools whose config directories exist, in probe order."""
    return [tool for tool in all_tools() if tool.config_dir.is_dir()]


def install_status(tool: ToolTarget, packaged: str) -> str:
    """Compare an installed copy against the packaged skill: identical/stale/missing."""
    destination = skill_file(tool)
    if not destination.is_file():
        return "missing"
    return "identical" if destination.read_text(encoding="utf-8") == packaged else "stale"


def install_skill(tool: ToolTarget, packaged: str, force: bool = False) -> str:
    """Write the packaged skill for one tool; return what happened.

    Never overwrites a divergent copy without force — an agent or user may
    have customized their installed skill deliberately.
    """
    destination = skill_file(tool)
    status = install_status(tool, packaged)
    if status == "identical":
        return "up-to-date"
    if status == "stale" and not force:
        return "stale-unforced"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(packaged, encoding="utf-8")
    return "installed" if status == "missing" else "updated"
