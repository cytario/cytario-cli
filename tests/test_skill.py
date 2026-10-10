"""Tests for the packaged skill file and its installation into AI tools."""

from __future__ import annotations

from pathlib import Path

from cytario_cli.skill import (
    ToolTarget,
    all_tools,
    detect_tools,
    install_skill,
    install_status,
    packaged_skill,
    skill_file,
)


def make_target(tmp_path: Path, subdir: bool = False) -> ToolTarget:
    skills_dir = tmp_path / "skills"
    return ToolTarget("test-tool", "Test Tool", tmp_path / "tool-home", skills_dir, subdir)


class TestPackagedSkill:
    def test_ships_in_the_package_and_is_the_agent_skill(self):
        content = packaged_skill()
        assert "name: cytario-cli" in content.splitlines()[1]
        assert "How Cytario data is laid out" in content
        assert "settings.<userId>.json" in content
        assert 'schemaVersion: "1.2"' in content
        assert 'kind: "settings"' in content

    def test_documents_rclone_mounting(self):
        content = packaged_skill()
        assert "cytario rclone setup --all" in content
        assert "Mounting with rclone" in content
        assert "env_auth = true" in content
        assert "nfsmount" in content  # macOS recommendation
        assert "WinFsp" in content  # Windows gate
        assert "AWS_ENDPOINT_URL_STS" in content  # non-AWS STS escape hatch
        assert "cytario auth refresh" in content  # long-mount token lifecycle

    def test_markdown_header_intact(self):
        assert packaged_skill().startswith("---\n")


class TestInstall:
    def test_install_creates_missing_copy(self, tmp_path):
        target = make_target(tmp_path)
        assert install_status(target, packaged_skill()) == "missing"
        assert install_skill(target, packaged_skill()) == "installed"
        assert install_status(target, packaged_skill()) == "identical"
        assert skill_file(target).read_text(encoding="utf-8") == packaged_skill()

    def test_reinstall_identical_is_noop(self, tmp_path):
        target = make_target(tmp_path)
        install_skill(target, packaged_skill())
        assert install_skill(target, packaged_skill()) == "up-to-date"

    def test_stale_copy_needs_force(self, tmp_path):
        target = make_target(tmp_path)
        install_skill(target, packaged_skill())
        skill_file(target).write_text("modified", encoding="utf-8")
        assert install_status(target, packaged_skill()) == "stale"
        assert install_skill(target, packaged_skill()) == "stale-unforced"
        assert skill_file(target).read_text(encoding="utf-8") == "modified"
        assert install_skill(target, packaged_skill(), force=True) == "updated"
        assert skill_file(target).read_text(encoding="utf-8") == packaged_skill()

    def test_subdir_target_nests_per_skill_directory(self, tmp_path):
        target = make_target(tmp_path, subdir=True)
        install_skill(target, packaged_skill())
        assert skill_file(target) == tmp_path / "skills" / "cytario-cli" / "SKILL.md"
        assert skill_file(target).is_file()

    def test_subdir_target_reuses_existing_directory(self, tmp_path):
        target = make_target(tmp_path, subdir=True)
        skill_file(target).parent.mkdir(parents=True)
        assert install_skill(target, packaged_skill()) == "installed"


class TestDetect:
    def test_detects_tool_whose_config_directory_exists(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        (home / ".claude").mkdir()
        tools = detect_tools()
        assert [tool.id for tool in tools] == ["claude-code"]

    def test_detects_opencode_by_config_dir(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        # Fresh OpenCode install: config dir present, no skills subdir yet.
        (home / ".config" / "opencode").mkdir(parents=True)
        tools = detect_tools()
        assert [tool.id for tool in tools] == ["opencode"]
        assert skill_file(tools[0]) == home / ".config" / "opencode" / "skills" / "cytario-cli" / "SKILL.md"

    def test_undetected_tools_absent(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        assert detect_tools() == []

    def test_detects_multiple_tools_in_probe_order(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        for config in (".config/opencode", ".cursor", ".claude", ".codex"):
            (home / config).mkdir(parents=True)
        assert [tool.id for tool in detect_tools()] == [
            "claude-code",
            "opencode",
            "codex",
            "cursor",
        ]


class TestToolLayouts:
    """Each tool's destination matches its documented discovery rules."""

    def test_claude_layout(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        claude = next(tool for tool in all_tools() if tool.id == "claude-code")
        assert skill_file(claude) == home / ".claude" / "skills" / "cytario-cli" / "SKILL.md"

    def test_opencode_layout(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        opencode = next(tool for tool in all_tools() if tool.id == "opencode")
        assert skill_file(opencode) == (home / ".config" / "opencode" / "skills" / "cytario-cli" / "SKILL.md")

    def test_codex_layout(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        codex = next(tool for tool in all_tools() if tool.id == "codex")
        assert skill_file(codex) == home / ".codex" / "skills" / "cytario-cli" / "SKILL.md"

    def test_cursor_flat_rule(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: home)
        cursor = next(tool for tool in all_tools() if tool.id == "cursor")
        assert skill_file(cursor) == home / ".cursor" / "rules" / "cytario-cli.md"


class TestModuleTools:
    def test_all_tools_have_distinct_directories(self):
        dirs = [tool.skills_dir for tool in all_tools()]
        assert len(dirs) == len(set(dirs))

    def test_tool_ids_stable(self):
        assert [tool.id for tool in all_tools()] == ["claude-code", "opencode", "codex", "cursor"]
