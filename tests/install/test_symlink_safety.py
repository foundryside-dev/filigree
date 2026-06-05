"""Regression tests for symlink-safe installer writes."""

from __future__ import annotations

import json

import pytest

from filigree.install import ensure_gitignore, inject_instructions, install_skills
from filigree.install_support import integrations
from filigree.install_support.hooks import install_claude_code_hooks
from filigree.install_support.integrations import install_claude_code_mcp, install_codex_mcp

pytestmark = pytest.mark.skipif(not hasattr(__import__("os"), "symlink"), reason="symlinks unavailable")


def _outside_file(tmp_path, name: str, content: str = "unchanged\n"):
    outside = tmp_path / name
    outside.write_text(content)
    return outside


def assert_rejected(ok: bool, msg: str) -> None:
    assert ok is False
    assert "symlink" in msg.lower()


def test_inject_instructions_rejects_symlink_target(tmp_path):
    victim = _outside_file(tmp_path, "victim.md")
    link = tmp_path / "project" / "CLAUDE.md"
    link.parent.mkdir()
    link.symlink_to(victim)

    ok, msg = inject_instructions(link)

    assert_rejected(ok, msg)
    assert victim.read_text() == "unchanged\n"
    assert link.is_symlink()


def test_ensure_gitignore_rejects_symlink_target(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    victim = _outside_file(tmp_path, "outside-gitignore")
    (project / ".gitignore").symlink_to(victim)

    ok, msg = ensure_gitignore(project)

    assert_rejected(ok, msg)
    assert victim.read_text() == "unchanged\n"


def test_install_claude_code_mcp_rejects_symlinked_mcp_json(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    victim = _outside_file(tmp_path, "outside-mcp.json", "{}\n")
    (project / ".mcp.json").symlink_to(victim)
    monkeypatch.setattr(integrations.shutil, "which", lambda _name: None)

    ok, msg = install_claude_code_mcp(project)

    assert_rejected(ok, msg)
    assert victim.read_text() == "{}\n"


def test_install_claude_code_hooks_rejects_symlinked_claude_dir(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    outside_claude = tmp_path / "outside-claude"
    outside_claude.mkdir()
    settings = outside_claude / "settings.json"
    settings.write_text(json.dumps({}) + "\n")
    (project / ".claude").symlink_to(outside_claude, target_is_directory=True)
    monkeypatch.setattr("filigree.install_support.hooks.find_filigree_command", lambda: ["filigree"])

    ok, msg = install_claude_code_hooks(project)

    assert_rejected(ok, msg)
    assert settings.read_text() == "{}\n"


def test_install_skills_rejects_symlinked_parent_directory(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    outside_agents = tmp_path / "outside-claude-skills"
    outside_agents.mkdir()
    (project / ".claude").symlink_to(outside_agents, target_is_directory=True)

    ok, msg = install_skills(project)

    assert_rejected(ok, msg)
    assert list(outside_agents.iterdir()) == []


def test_install_codex_mcp_rejects_symlinked_config_file(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    codex_dir = home / ".codex"
    codex_dir.mkdir()
    victim = _outside_file(tmp_path, "outside-codex.toml")
    (codex_dir / "config.toml").symlink_to(victim)
    monkeypatch.setenv("HOME", str(home))

    ok, msg = install_codex_mcp(tmp_path / "project")

    assert_rejected(ok, msg)
    assert victim.read_text() == "unchanged\n"


def test_install_claude_code_mcp_rejects_symlinked_backup_target(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".mcp.json").write_text("not json")
    victim = _outside_file(tmp_path, "outside-mcp-backup")
    (project / ".mcp.json.bak").symlink_to(victim)
    monkeypatch.setattr(integrations.shutil, "which", lambda _name: None)

    ok, msg = install_claude_code_mcp(project)

    assert_rejected(ok, msg)
    assert victim.read_text() == "unchanged\n"


def test_install_claude_code_hooks_rejects_symlinked_backup_target(tmp_path, monkeypatch):
    project = tmp_path / "project"
    claude_dir = project / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / "settings.json").write_text("not json")
    victim = _outside_file(tmp_path, "outside-settings-backup")
    (claude_dir / "settings.json.bak").symlink_to(victim)
    monkeypatch.setattr("filigree.install_support.hooks.find_filigree_command", lambda: ["filigree"])

    ok, msg = install_claude_code_hooks(project)

    assert_rejected(ok, msg)
    assert victim.read_text() == "unchanged\n"
