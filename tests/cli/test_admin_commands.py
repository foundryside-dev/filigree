"""CLI tests for admin commands (init modes, install, doctor, server, export/import, JSON retrofit)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from filigree.cli import cli
from tests.cli.conftest import _extract_id


class TestWeftStoreInit:
    """WEFT store consolidation (filigree-37e3f26145): init creates the
    federation .weft/filigree/ store, never writes weft.toml, and runs with
    no weft.toml present (deletion test)."""

    def test_init_creates_weft_store_layout(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_runner: CliRunner) -> None:
        monkeypatch.chdir(tmp_path)
        result = cli_runner.invoke(cli, ["init"])
        assert result.exit_code == 0, result.output
        store = tmp_path / ".weft" / "filigree"
        assert (store / "filigree.db").is_file()
        assert (store / "config.json").is_file()
        assert (store / ".gitignore").is_file()
        assert "managed-by: filigree" in (store / ".gitignore").read_text()
        # fresh install is born confless: no legacy dir AND no .filigree.conf —
        # the anchor is the presence of .weft/filigree/ itself (filigree-4bf16e64b6).
        assert not (tmp_path / ".filigree").exists()
        assert not (tmp_path / ".filigree.conf").exists()
        # identity lives in the store's config.json (the sole-writer subtree).
        cfg = json.loads((store / "config.json").read_text())
        assert cfg["prefix"] == tmp_path.name

    def test_init_never_writes_weft_toml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_runner: CliRunner) -> None:
        monkeypatch.chdir(tmp_path)
        cli_runner.invoke(cli, ["init"])
        assert not (tmp_path / "weft.toml").exists()

    def test_install_never_writes_weft_toml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_runner: CliRunner) -> None:
        monkeypatch.chdir(tmp_path)
        cli_runner.invoke(cli, ["init"])
        cli_runner.invoke(cli, ["install", "--gitignore"])
        assert not (tmp_path / "weft.toml").exists()

    def test_init_and_run_with_no_weft_toml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_runner: CliRunner) -> None:
        """Deletion test: install and operate with no weft.toml present."""
        monkeypatch.chdir(tmp_path)
        cli_runner.invoke(cli, ["init", "--prefix", "p"])
        assert not (tmp_path / "weft.toml").exists()
        created = cli_runner.invoke(cli, ["create", "hello", "--json"])
        assert created.exit_code == 0, created.output
        listed = cli_runner.invoke(cli, ["ready", "--json"])
        assert listed.exit_code == 0, listed.output

    def test_init_honors_store_dir_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_runner: CliRunner) -> None:
        """`filigree init` with a weft.toml [filigree].store_dir override must
        create the store (db AND config) under the override and point the conf
        there — init and runtime must agree (advisor gap)."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "weft.toml").write_text('[filigree]\nstore_dir = "custom/store"\n')
        result = cli_runner.invoke(cli, ["init", "--prefix", "ov"])
        assert result.exit_code == 0, result.output
        custom = tmp_path / "custom" / "store"
        # db AND config both live under the override — not split.
        assert (custom / "filigree.db").is_file()
        assert (custom / "config.json").is_file()
        assert not (tmp_path / ".weft" / "filigree").exists()
        # confless: no .filigree.conf; identity in the override store's config.json.
        assert not (tmp_path / ".filigree.conf").exists()
        cfg = json.loads((custom / "config.json").read_text())
        assert cfg["prefix"] == "ov"
        # Runtime round-trips against the same store.
        created = cli_runner.invoke(cli, ["create", "in override", "--json"])
        assert created.exit_code == 0, created.output
        listed = cli_runner.invoke(cli, ["ready", "--json"])
        assert "in override" in listed.output

    def test_init_ignores_absolute_store_dir_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_runner: CliRunner) -> None:
        """An absolute store_dir cannot be carried in the project-relative conf
        db field, so init ignores it and uses the default .weft/filigree/."""
        monkeypatch.chdir(tmp_path)
        elsewhere = tmp_path / "elsewhere"
        (tmp_path / "weft.toml").write_text(f'[filigree]\nstore_dir = "{elsewhere}"\n')
        result = cli_runner.invoke(cli, ["init"])
        assert result.exit_code == 0, result.output
        assert (tmp_path / ".weft" / "filigree" / "filigree.db").is_file()
        assert not (elsewhere / "filigree.db").exists()

    def test_init_refuses_on_unreadable_weft_toml_fresh(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_runner: CliRunner
    ) -> None:
        """A present-but-broken weft.toml on the mutating init path must fail fast,
        not silently boot on defaults (which would ignore a possibly-pinned
        store_dir). Fresh install branch (I1)."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "weft.toml").write_text("this is not [valid toml")
        result = cli_runner.invoke(cli, ["init", "--prefix", "p"])
        assert result.exit_code == 1, result.output
        assert "weft.toml" in result.output
        # Nothing created — neither default nor any store.
        assert not (tmp_path / ".weft").exists()
        assert not (tmp_path / ".filigree.conf").exists()

    def test_init_refuses_on_unreadable_weft_toml_with_existing_install(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_runner: CliRunner
    ) -> None:
        """With an existing install, a broken weft.toml must block the migration
        branch too (the store could be pinned elsewhere in the unreadable bytes)."""
        monkeypatch.chdir(tmp_path)
        result = cli_runner.invoke(cli, ["init", "--prefix", "p"])
        assert result.exit_code == 0, result.output
        # Now corrupt weft.toml and re-run init.
        (tmp_path / "weft.toml").write_text("\xff not [valid")
        result = cli_runner.invoke(cli, ["init"])
        assert result.exit_code == 1, result.output
        assert "weft.toml" in result.output


class TestOnboardingBreadcrumbs:
    def test_init_shows_next(self, tmp_path: Path, cli_runner: CliRunner) -> None:
        original = os.getcwd()
        os.chdir(str(tmp_path))
        try:
            result = cli_runner.invoke(cli, ["init"])
            assert "Next: filigree install" in result.output
        finally:
            os.chdir(original)

    def test_init_creates_scanners_dir(self, tmp_path: Path, cli_runner: CliRunner) -> None:
        original = os.getcwd()
        os.chdir(str(tmp_path))
        try:
            result = cli_runner.invoke(cli, ["init"])
            assert result.exit_code == 0
            assert (tmp_path / ".weft" / "filigree" / "scanners").is_dir()
        finally:
            os.chdir(original)

    def test_create_shows_next(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["create", "Test"])
        assert "Next: filigree ready" in result.output


class TestNestedGitignoreWiring:
    """`filigree init` / `install` ship .filigree/.gitignore (filigree-694f777d5c)."""

    def test_init_creates_nested_gitignore(self, tmp_path: Path, cli_runner: CliRunner) -> None:
        original = os.getcwd()
        os.chdir(str(tmp_path))
        try:
            result = cli_runner.invoke(cli, ["init"])
            assert result.exit_code == 0
            nested = tmp_path / ".weft" / "filigree" / ".gitignore"
            assert nested.exists()
            body = nested.read_text()
            assert "managed-by: filigree" in body
            assert "*.db-wal" in body
        finally:
            os.chdir(original)

    def test_install_gitignore_flag_creates_nested(self, tmp_path: Path, cli_runner: CliRunner) -> None:
        original = os.getcwd()
        os.chdir(str(tmp_path))
        try:
            cli_runner.invoke(cli, ["init"])
            # Simulate a project created before the fix.
            nested = tmp_path / ".weft" / "filigree" / ".gitignore"
            nested.unlink(missing_ok=True)
            result = cli_runner.invoke(cli, ["install", "--gitignore"])
            assert result.exit_code == 0
            assert nested.exists()
            assert "*.db-wal" in nested.read_text()
            # Root-level whole-dir rule is still applied too.
            assert ".filigree/" in (tmp_path / ".gitignore").read_text()
        finally:
            os.chdir(original)


class TestActorFlag:
    def test_create_with_actor(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["--actor", "test-agent", "create", "Actor test"])
        assert r.exit_code == 0
        # Read clean stdout: a genuine --actor differing from the OS user emits a
        # non-blocking ACTOR_MISMATCH warning on stderr, which CliRunner merges
        # into r.output in Click 8.3.1 (ADR-012).
        issue_id = _extract_id(r.stdout)
        result = runner.invoke(cli, ["show", issue_id, "--json"])
        data = json.loads(result.output)
        assert data["title"] == "Actor test"

    def test_comment_with_actor(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Commentable"])
        issue_id = _extract_id(r.output)
        runner.invoke(cli, ["--actor", "bot-1", "add-comment", issue_id, "Hello"])
        result = runner.invoke(cli, ["get-comments", issue_id])
        assert "bot-1" in result.output

    def test_default_actor_is_cli(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Default actor"])
        issue_id = _extract_id(r.output)
        runner.invoke(cli, ["add-comment", issue_id, "Default"])
        result = runner.invoke(cli, ["get-comments", issue_id])
        assert "cli" in result.output


class TestJsonRetrofit:
    def test_create_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["create", "JSON create", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["title"] == "JSON create"
        assert "issue_id" in data
        assert "id" not in data

    def test_close_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Close JSON"])
        issue_id = _extract_id(r.output)
        result = runner.invoke(cli, ["close", issue_id, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, dict)
        assert "succeeded" in data
        # Standalone issue, no dependents: newly_unblocked is empty and therefore
        # OMITTED from the envelope (batch contract; filigree-1025b9f6ab / F6).
        assert "newly_unblocked" not in data
        assert data["succeeded"][0]["issue_id"] == issue_id

    def test_reopen_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Reopen JSON"])
        issue_id = _extract_id(r.output)
        runner.invoke(cli, ["close", issue_id])
        result = runner.invoke(cli, ["reopen", issue_id, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, dict)
        assert "succeeded" in data
        assert isinstance(data["succeeded"], list)

    def test_comment_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Comment JSON"])
        issue_id = _extract_id(r.output)
        result = runner.invoke(cli, ["add-comment", issue_id, "My comment", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "comment_id" in data
        assert data["issue_id"] == issue_id

    def test_comments_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Comments JSON"])
        issue_id = _extract_id(r.output)
        runner.invoke(cli, ["add-comment", issue_id, "A comment"])
        result = runner.invoke(cli, ["get-comments", issue_id, "--json"])
        assert result.exit_code == 0
        # filigree-d2263e721d: Phase E1 ListResponse envelope, not a bare list.
        data = json.loads(result.output)
        assert isinstance(data, dict)
        assert data["has_more"] is False
        assert len(data["items"]) == 1
        assert data["items"][0]["text"] == "A comment"

    def test_dep_add_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r1 = runner.invoke(cli, ["create", "A"])
        id1 = _extract_id(r1.output)
        r2 = runner.invoke(cli, ["create", "B"])
        id2 = _extract_id(r2.output)
        result = runner.invoke(cli, ["add-dep", id1, id2, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "added"

    def test_dep_remove_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r1 = runner.invoke(cli, ["create", "A"])
        id1 = _extract_id(r1.output)
        r2 = runner.invoke(cli, ["create", "B"])
        id2 = _extract_id(r2.output)
        runner.invoke(cli, ["add-dep", id1, id2])
        result = runner.invoke(cli, ["remove-dep", id1, id2, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "removed"

    def test_workflow_statuses_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["workflow-statuses", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "statuses" in data
        assert "open" in data["statuses"]
        assert "wip" in data["statuses"]
        assert "done" in data["statuses"]

    def test_undo_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Undo JSON"])
        issue_id = _extract_id(r.output)
        runner.invoke(cli, ["update", issue_id, "--title", "Changed"])
        result = runner.invoke(cli, ["undo", issue_id, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["undone"] is True

    def test_guide_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["guide", "core", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "pack" in data
        assert "guide" in data

    def test_archive_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["archive", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "archived" in data
        assert "count" in data

    def test_archive_can_be_scoped_to_label(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        scratch = runner.invoke(cli, ["create", "CLI scratch cleanup"])
        unrelated = runner.invoke(cli, ["create", "CLI unrelated closed"])
        scratch_id = _extract_id(scratch.output)
        unrelated_id = _extract_id(unrelated.output)
        runner.invoke(cli, ["add-label", "scratch", scratch_id])
        runner.invoke(cli, ["close", scratch_id])
        runner.invoke(cli, ["close", unrelated_id])

        result = runner.invoke(cli, ["archive", "--days", "0", "--label", "scratch", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["archived"] == [scratch_id]
        assert data["count"] == 1
        scratch_show = runner.invoke(cli, ["show", scratch_id, "--json"])
        unrelated_show = runner.invoke(cli, ["show", unrelated_id, "--json"])
        assert json.loads(scratch_show.output)["status"] == "archived"
        assert json.loads(unrelated_show.output)["status"] == "closed"

    def test_archive_tight_window_requires_label_scope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["archive", "--days", "0"])
        assert result.exit_code == 1
        assert "--days 0 requires a non-empty --label" in result.output

    def test_archive_rejects_negative_days(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["archive", "--days", "-1"])
        assert result.exit_code != 0
        assert "Invalid value for '--days'" in result.output

    def test_compact_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["compact", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "deleted_events" in data

    def test_compact_rejects_negative_keep(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["compact", "--keep", "-1"])
        assert result.exit_code != 0
        assert "Invalid value for '--keep'" in result.output

    def test_clean_stale_findings_rejects_negative_days(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["clean-stale-findings", "--days", "-1"])
        assert result.exit_code != 0
        assert "Invalid value for '--days'" in result.output

    def test_label_add_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Label JSON"])
        issue_id = _extract_id(r.output)
        result = runner.invoke(cli, ["add-label", "urgent", issue_id, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "added"

    def test_label_remove_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Label JSON", "-l", "urgent"])
        issue_id = _extract_id(r.output)
        result = runner.invoke(cli, ["remove-label", issue_id, "urgent", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "removed"

    def test_label_add_json_returns_canonical_label(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Bug filigree-6870a1dcc0: --json must return canonical (stripped) label, not raw argv."""
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Label JSON"])
        issue_id = _extract_id(r.output)
        result = runner.invoke(cli, ["add-label", "  urgent  ", issue_id, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["label"] == "urgent", f"expected canonical 'urgent', got {data['label']!r}"

    def test_label_remove_json_returns_canonical_label(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Bug filigree-6870a1dcc0: --json must return canonical (stripped) label, not raw argv."""
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Label JSON", "-l", "urgent"])
        issue_id = _extract_id(r.output)
        result = runner.invoke(cli, ["remove-label", issue_id, "  urgent  ", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["label"] == "urgent"


class TestInstallCli:
    def test_install_all(self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch) -> None:
        runner, project = cli_in_project
        codex_home = project / ".test-home"
        codex_home.mkdir()
        monkeypatch.setattr("filigree.install_support.integrations.Path.home", lambda: codex_home)
        result = runner.invoke(cli, ["install"])
        assert result.exit_code == 0
        assert "installed successfully" in result.output

    def test_install_gitignore_only(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["install", "--gitignore"])
        assert result.exit_code == 0
        assert ".gitignore" in result.output

    def test_install_claude_md_only(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["install", "--claude-md"])
        assert result.exit_code == 0
        assert "CLAUDE.md" in result.output

    def test_install_codex_skills_flag(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, project = cli_in_project
        result = runner.invoke(cli, ["install", "--codex-skills"])
        assert result.exit_code == 0, result.output
        assert "Codex skills" in result.output
        skill_md = project / ".agents" / "skills" / "filigree-workflow" / "SKILL.md"
        assert skill_md.exists()

    def test_claude_code_flag_only_installs_mcp(self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch) -> None:
        """Bug filigree-e1ef3675f7: ``--claude-code`` must install the MCP
        only, matching the help text. Hooks and skills have their own
        flags and should not be implicitly pulled in.
        """
        runner, _project = cli_in_project

        called: dict[str, bool] = {}

        def _mk_stub(name: str):
            def _stub(*args: object, **kwargs: object) -> tuple[bool, str]:
                called[name] = True
                return True, f"stub {name}"

            return _stub

        monkeypatch.setattr("filigree.install.install_claude_code_mcp", _mk_stub("mcp"))
        monkeypatch.setattr("filigree.install.install_claude_code_hooks", _mk_stub("hooks"))
        monkeypatch.setattr("filigree.install.install_skills", _mk_stub("skills"))
        monkeypatch.setattr("filigree.install.install_codex_mcp", _mk_stub("codex_mcp"))
        monkeypatch.setattr("filigree.install.install_codex_skills", _mk_stub("codex_skills"))

        result = runner.invoke(cli, ["install", "--claude-code"])
        assert result.exit_code == 0, result.output
        assert called.get("mcp") is True
        assert "hooks" not in called
        assert "skills" not in called
        assert "codex_mcp" not in called
        assert "codex_skills" not in called

    def test_codex_flag_only_installs_mcp(self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch) -> None:
        """Bug filigree-e1ef3675f7: ``--codex`` must install the Codex
        MCP only; ``--codex-skills`` is the separate flag for skills.
        """
        runner, _project = cli_in_project

        called: dict[str, bool] = {}

        def _mk_stub(name: str):
            def _stub(*args: object, **kwargs: object) -> tuple[bool, str]:
                called[name] = True
                return True, f"stub {name}"

            return _stub

        monkeypatch.setattr("filigree.install.install_claude_code_mcp", _mk_stub("mcp"))
        monkeypatch.setattr("filigree.install.install_claude_code_hooks", _mk_stub("hooks"))
        monkeypatch.setattr("filigree.install.install_skills", _mk_stub("skills"))
        monkeypatch.setattr("filigree.install.install_codex_mcp", _mk_stub("codex_mcp"))
        monkeypatch.setattr("filigree.install.install_codex_skills", _mk_stub("codex_skills"))

        result = runner.invoke(cli, ["install", "--codex"])
        assert result.exit_code == 0, result.output
        assert called.get("codex_mcp") is True
        assert "codex_skills" not in called
        assert "mcp" not in called

    def test_install_codex_server_mode_passes_mode_and_port(
        self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, project = cli_in_project
        codex_home = project / ".test-home"
        codex_home.mkdir()
        monkeypatch.setattr("filigree.install_support.integrations.Path.home", lambda: codex_home)

        observed: dict[str, object] = {}

        def _fake_install_codex_mcp(project_root: Path, *, mode: str = "ethereal", server_port: int = 8377) -> tuple[bool, str]:
            observed["project_root"] = project_root
            observed["mode"] = mode
            observed["server_port"] = server_port
            return True, "configured"

        monkeypatch.setattr("filigree.install.install_codex_mcp", _fake_install_codex_mcp)
        monkeypatch.setattr("filigree.server.register_project", lambda _p: None)

        from filigree.server import DaemonStatus, ServerConfig, write_server_config

        config_dir = project / ".server-config"
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")
        monkeypatch.setattr("filigree.server.SERVER_PID_FILE", config_dir / "server.pid")
        monkeypatch.setattr("filigree.server.daemon_status", lambda: DaemonStatus(running=False))
        write_server_config(ServerConfig(port=9911))

        result = runner.invoke(cli, ["install", "--codex", "--mode", "server"])
        assert result.exit_code == 0, result.output
        assert observed["project_root"] == project
        assert observed["mode"] == "server"
        assert observed["server_port"] == 9911

        from filigree.install_support.integrations import _codex_config_path

        assert _codex_config_path() == codex_home / ".codex" / "config.toml"


class TestDoctorCli:
    def test_doctor_basic(self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch) -> None:
        runner, _ = cli_in_project

        from filigree.install_support.doctor import CheckResult

        # Mock to all-passing — the fresh-init fixture intentionally skips
        # `install`, so a real run_doctor would (correctly) report missing
        # CLAUDE.md / MCP / hooks. This test covers output formatting only.
        monkeypatch.setattr(
            "filigree.install.run_doctor",
            lambda **_kw: [CheckResult(".filigree/", True, "ok")],
        )
        result = runner.invoke(cli, ["doctor"])
        assert result.exit_code == 0
        assert "filigree doctor" in result.output

    def test_doctor_verbose(self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch) -> None:
        runner, _ = cli_in_project

        from filigree.install_support.doctor import CheckResult

        monkeypatch.setattr(
            "filigree.install.run_doctor",
            lambda **_kw: [CheckResult(".filigree/", True, "ok")],
        )
        result = runner.invoke(cli, ["doctor", "--verbose"])
        assert result.exit_code == 0
        # Verbose should show all checks including passed ones
        assert "OK" in result.output

    def test_doctor_fix(self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch) -> None:
        runner, _ = cli_in_project

        from filigree.install_support.doctor import CheckResult

        monkeypatch.setattr(
            "filigree.install.run_doctor",
            lambda **_kw: [CheckResult(".filigree/", True, "ok")],
        )
        result = runner.invoke(cli, ["doctor", "--fix"])
        assert result.exit_code == 0

    def test_doctor_json_emits_shared_summary_contract(
        self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, _ = cli_in_project

        from filigree.install_support.doctor import CheckResult

        monkeypatch.setattr(
            "filigree.install.run_doctor",
            lambda **_kw: [
                CheckResult("Claude Code MCP", True, "configured"),
                CheckResult("Bundled scanner registrations", False, "stale", fix_hint="run scanner enable"),
            ],
        )

        result = runner.invoke(cli, ["doctor", "--json"])

        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert set(payload) == {"ok", "checks", "next_actions"}
        assert payload["ok"] is False
        assert isinstance(payload["checks"], list)
        assert isinstance(payload["next_actions"], list)
        checks = {check["id"]: check for check in payload["checks"]}
        assert checks["mcp.registration"] == {"id": "mcp.registration", "status": "ok", "fixed": False}
        assert checks["scanner.registration"] == {"id": "scanner.registration", "status": "failed", "fixed": False}
        assert "api.availability" in checks
        assert "auth.config" in checks
        assert "entity_associations.routes" in checks

    def test_doctor_json_real_project_includes_stable_route_ids(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project

        result = runner.invoke(cli, ["doctor", "--json"])

        assert result.output
        payload = json.loads(result.output)
        check_ids = {check["id"] for check in payload["checks"]}
        assert {
            "dashboard.port",
            "mcp.registration",
            "api.availability",
            "scanner.results",
            "entity_associations.routes",
        }.issubset(check_ids)

    def test_doctor_fix_json_reports_repair_and_is_idempotent(
        self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, _ = cli_in_project

        from filigree.install_support.doctor import CheckResult

        repaired = False

        def fake_run_doctor(**_kw: object) -> list[CheckResult]:
            if repaired:
                return [CheckResult("Claude Code MCP", True, "configured")]
            return [CheckResult("Claude Code MCP", False, "missing", fix_hint="hint")]

        def fake_install_claude_code_mcp(*_args: object, **_kwargs: object) -> tuple[bool, str]:
            nonlocal repaired
            repaired = True
            return True, "Configured .mcp.json"

        monkeypatch.setattr("filigree.install.run_doctor", fake_run_doctor)
        monkeypatch.setattr("filigree.install.install_claude_code_mcp", fake_install_claude_code_mcp)

        first = runner.invoke(cli, ["doctor", "--fix", "--json"])
        second = runner.invoke(cli, ["doctor", "--fix", "--json"])

        assert first.exit_code == 0
        first_payload = json.loads(first.output)
        assert first_payload["ok"] is True
        assert {"id": "mcp.registration", "status": "fixed", "fixed": True} in first_payload["checks"]

        assert second.exit_code == 0
        second_payload = json.loads(second.output)
        assert second_payload["ok"] is True
        assert {"id": "mcp.registration", "status": "ok", "fixed": False} in second_payload["checks"]

    def test_doctor_fix_json_does_not_mutate_scanner_results_by_default(
        self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, _ = cli_in_project

        from filigree.install_support.doctor import CheckResult

        monkeypatch.setattr(
            "filigree.install.run_doctor",
            lambda **_kw: [
                CheckResult(
                    "Bundled scanner registrations",
                    False,
                    "Stale bundled scanner registration(s): codex",
                    fix_hint="Run: filigree scanner enable codex --force",
                    code="stale_bundled_scanner",
                )
            ],
        )

        result = runner.invoke(cli, ["doctor", "--fix", "--json"])

        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["ok"] is False
        assert {"id": "scanner.registration", "status": "failed", "fixed": False} in payload["checks"]
        assert payload["next_actions"] == ["scanner.registration: Run: filigree scanner enable codex --force"]

    def test_doctor_fix_json_does_not_repair_gitignore(
        self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``.gitignore`` remains the one deliberate ``--fix`` exclusion.

        filigree-f57cb498d4 restored instruction-file and context.md repair to
        ``--fix``, but ``.gitignore`` is still NOT auto-edited here — the user
        runs ``filigree install --gitignore`` for that. Local bindings (MCP) and
        stale dashboard pointers are still repaired.
        """
        runner, _ = cli_in_project

        from filigree.install_support.doctor import CheckResult

        monkeypatch.setattr(
            "filigree.install.run_doctor",
            lambda **_kw: [
                CheckResult(".gitignore", False, "missing", fix_hint="Run: filigree install --gitignore"),
                CheckResult("Claude Code MCP", False, "missing", fix_hint="Run: filigree install --claude-code"),
                CheckResult("Ephemeral port", False, "stale", fix_hint="Remove .filigree/ephemeral.port"),
            ],
        )

        def fail_gitignore(_root: Path) -> tuple[bool, str]:
            raise AssertionError("doctor --fix must not repair .gitignore")

        monkeypatch.setattr("filigree.install.ensure_gitignore", fail_gitignore)
        monkeypatch.setattr("filigree.install.install_claude_code_mcp", lambda *_args, **_kwargs: (True, "repaired MCP"))

        result = runner.invoke(cli, ["doctor", "--fix", "--json"])

        assert result.exit_code == 1
        payload = json.loads(result.output)
        checks = {check["id"]: check for check in payload["checks"]}
        assert checks["git.ignore"] == {"id": "git.ignore", "status": "failed", "fixed": False}
        assert checks["mcp.registration"] == {"id": "mcp.registration", "status": "fixed", "fixed": True}
        assert checks["dashboard.port"] == {"id": "dashboard.port", "status": "fixed", "fixed": True}

    def test_doctor_fix_repairs_instruction_files_and_context_md(
        self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """filigree-f57cb498d4: ``--fix`` repairs CLAUDE.md/AGENTS.md/context.md.

        These are filigree-owned artifacts — CLAUDE.md/AGENTS.md via the
        non-destructive marked-block injection, context.md regenerated from the
        DB. Run against the real initialised project so the DB-backed context.md
        regen and the on-disk instruction injection are exercised end to end.
        """
        runner, project_root = cli_in_project

        from filigree.core import SUMMARY_FILENAME
        from filigree.install import FILIGREE_INSTRUCTIONS_MARKER
        from filigree.install_support.doctor import CheckResult

        monkeypatch.setattr(
            "filigree.install.run_doctor",
            lambda **_kw: [
                CheckResult("CLAUDE.md", False, "No filigree instructions", fix_hint="Run: filigree install --claude-md"),
                CheckResult("AGENTS.md", False, "File not found", fix_hint="Run: filigree install --agents-md"),
                CheckResult("context.md", False, "Missing", fix_hint="Run any filigree mutation command."),
            ],
        )

        # context.md missing to start; instruction files absent.
        from filigree.core import find_filigree_anchor

        summary_path = find_filigree_anchor(project_root).store_dir / SUMMARY_FILENAME
        summary_path.unlink(missing_ok=True)

        result = runner.invoke(cli, ["doctor", "--fix", "--json"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        checks = {check["id"]: check for check in payload["checks"]}
        assert checks["instructions.claude_md"] == {"id": "instructions.claude_md", "status": "fixed", "fixed": True}
        assert checks["instructions.agents_md"] == {"id": "instructions.agents_md", "status": "fixed", "fixed": True}
        assert checks["context.summary"] == {"id": "context.summary", "status": "fixed", "fixed": True}

        # The artifacts actually exist on disk now.
        assert FILIGREE_INSTRUCTIONS_MARKER in (project_root / "CLAUDE.md").read_text()
        assert FILIGREE_INSTRUCTIONS_MARKER in (project_root / "AGENTS.md").read_text()
        assert summary_path.exists()
        assert summary_path.read_text().strip()

    def test_doctor_fix_reports_manual_intervention_on_fixer_failure(
        self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When a fixer returns ok=False, summary must show manual intervention count."""
        runner, _ = cli_in_project

        from filigree.install_support.doctor import CheckResult

        # Two fixable failures: MCP binding (will fail to fix) and stale dashboard pointer (will succeed)
        mock_results = [
            CheckResult("Claude Code MCP", False, "missing", fix_hint="hint"),
            CheckResult("Ephemeral port", False, "stale", fix_hint="hint"),
        ]
        monkeypatch.setattr("filigree.install.run_doctor", lambda **_kw: mock_results)
        monkeypatch.setattr(
            "filigree.install.install_claude_code_mcp",
            lambda *_args, **_kwargs: (False, "Permission denied"),
        )
        monkeypatch.setattr(
            "filigree.cli_commands.admin._remove_stale_doctor_pointer",
            lambda _path: (True, "Removed stale pointer"),
        )

        result = runner.invoke(cli, ["doctor", "--fix"])

        assert "!! Claude Code MCP: Permission denied" in result.output
        assert "OK Ephemeral port: Removed stale pointer" in result.output
        assert "Fixed 1/2 issues" in result.output
        assert "1 require manual intervention" in result.output

    def test_doctor_exits_nonzero_on_failed_checks(self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch) -> None:
        # filigree-467d1e7487: doctor used to exit 0 even when non-schema
        # checks failed, leaving CI scripts unable to detect breakage.
        runner, _ = cli_in_project

        from filigree.install_support.doctor import CheckResult

        monkeypatch.setattr(
            "filigree.install.run_doctor",
            lambda **_kw: [CheckResult(".gitignore", False, "missing", fix_hint="hint")],
        )
        result = runner.invoke(cli, ["doctor"])

        assert result.exit_code == 1, f"expected exit 1 on failed check, got {result.exit_code}\n{result.output}"

    def test_doctor_fix_exits_nonzero_when_unfixed_remain(
        self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # filigree-467d1e7487: --fix that leaves failures behind must surface
        # exit 1 so scripts don't mistake "tried" for "succeeded".
        runner, _ = cli_in_project

        from filigree.install_support.doctor import CheckResult

        monkeypatch.setattr(
            "filigree.install.run_doctor",
            lambda **_kw: [CheckResult(".gitignore", False, "missing", fix_hint="hint")],
        )
        monkeypatch.setattr(
            "filigree.install.ensure_gitignore",
            lambda _root: (False, "Permission denied"),
        )

        result = runner.invoke(cli, ["doctor", "--fix"])

        assert result.exit_code == 1, f"expected exit 1 with unfixed failures, got {result.exit_code}\n{result.output}"
        assert "1 require manual intervention" in result.output

    def test_doctor_fix_exits_zero_when_all_fixed(self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch) -> None:
        # filigree-467d1e7487: --fix that resolves everything still exits 0.
        runner, _ = cli_in_project

        from filigree.install_support.doctor import CheckResult

        monkeypatch.setattr(
            "filigree.install.run_doctor",
            lambda **_kw: [CheckResult("Claude Code MCP", False, "missing", fix_hint="hint")],
        )
        monkeypatch.setattr(
            "filigree.install.install_claude_code_mcp",
            lambda *_args, **_kwargs: (True, "Configured .mcp.json"),
        )

        result = runner.invoke(cli, ["doctor", "--fix"])

        assert result.exit_code == 0, f"expected exit 0 when all fixed, got {result.exit_code}\n{result.output}"


class TestDoctorFixServerRegistry:
    """doctor --fix cleans stale server-registry entries (vanished directories)."""

    def _patch_server_config(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        config_dir = project / ".server-config"
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")
        monkeypatch.setattr("filigree.server.SERVER_PID_FILE", config_dir / "server.pid")
        return config_dir

    def test_unregisters_vanished_project_and_reports_it(
        self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, project = cli_in_project
        from filigree.install_support.doctor import CheckResult
        from filigree.server import ServerConfig, read_server_config, write_server_config

        self._patch_server_config(project, monkeypatch)
        gone = str(project / "ghost" / ".filigree")
        alive = str(project / ".weft" / "filigree")
        write_server_config(ServerConfig(port=8377, projects={gone: {"prefix": "ghost"}, alive: {"prefix": "test"}}))

        monkeypatch.setattr(
            "filigree.install.run_doctor",
            lambda **_kw: [
                CheckResult(
                    'Project "ghost"',
                    False,
                    f"Directory gone: {gone}",
                    fix_hint=f"Run: filigree server unregister {Path(gone).parent}",
                    code="server_registry_orphan",
                    fix_target=gone,
                ),
            ],
        )

        result = runner.invoke(cli, ["doctor", "--fix"])

        assert result.exit_code == 0, result.output
        assert gone in result.output  # "report exactly what it changed"
        # Stale entry removed; the still-present project is left alone.
        assert set(read_server_config().projects) == {alive}

    def test_json_summary_reports_orphan_check_fixed(self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch) -> None:
        runner, project = cli_in_project
        from filigree.install_support.doctor import CheckResult, doctor_check_id
        from filigree.server import ServerConfig, write_server_config

        self._patch_server_config(project, monkeypatch)
        gone = str(project / "ghost" / ".filigree")
        write_server_config(ServerConfig(port=8377, projects={gone: {"prefix": "ghost"}}))

        orphan = CheckResult('Project "ghost"', False, f"Directory gone: {gone}", code="server_registry_orphan", fix_target=gone)
        monkeypatch.setattr("filigree.install.run_doctor", lambda **_kw: [orphan])

        result = runner.invoke(cli, ["doctor", "--fix", "--json"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        checks = {check["id"]: check for check in payload["checks"]}
        assert checks[doctor_check_id(orphan)]["status"] == "fixed"

    def test_does_not_mutate_issue_data(self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch) -> None:
        runner, project = cli_in_project
        from filigree.install_support.doctor import CheckResult
        from filigree.server import ServerConfig, write_server_config

        created = runner.invoke(cli, ["create", "survivor issue"])
        assert created.exit_code == 0
        issue_id = _extract_id(created.output)

        self._patch_server_config(project, monkeypatch)
        gone = str(project / "ghost" / ".filigree")
        write_server_config(ServerConfig(port=8377, projects={gone: {"prefix": "ghost"}}))
        monkeypatch.setattr(
            "filigree.install.run_doctor",
            lambda **_kw: [
                CheckResult('Project "ghost"', False, f"Directory gone: {gone}", code="server_registry_orphan", fix_target=gone),
            ],
        )

        result = runner.invoke(cli, ["doctor", "--fix"])
        assert result.exit_code == 0, result.output

        # The data plane is untouched: the issue still resolves.
        shown = runner.invoke(cli, ["show", issue_id])
        assert shown.exit_code == 0
        assert "survivor issue" in shown.output

    def test_real_scan_cleans_orphan_and_is_idempotent(
        self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, project = cli_in_project
        from filigree.core import read_config
        from filigree.server import ServerConfig, read_server_config, write_server_config

        # Put the project into server mode so the real run_doctor exercises
        # _doctor_server_checks.
        config_path = project / ".weft" / "filigree" / "config.json"
        config = read_config(project / ".weft" / "filigree")
        config["mode"] = "server"
        config_path.write_text(json.dumps(config))

        self._patch_server_config(project, monkeypatch)
        gone = str(project / "ghost" / ".filigree")
        alive = str(project / ".weft" / "filigree")
        write_server_config(ServerConfig(port=8377, projects={gone: {"prefix": "ghost"}, alive: {"prefix": "test"}}))

        first = runner.invoke(cli, ["doctor", "--fix"])
        assert gone in first.output, first.output
        assert set(read_server_config().projects) == {alive}

        # Second run: the orphan is already gone — the real scan does not
        # re-surface it, so cleanup is idempotent.
        second = runner.invoke(cli, ["doctor", "--fix"])
        assert f"Directory gone: {gone}" not in second.output
        assert set(read_server_config().projects) == {alive}


class TestShowDetailedOutput:
    """Cover the human-readable show output branches."""

    def test_show_with_description_and_notes(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(
            cli,
            [
                "create",
                "Detailed issue",
                "-d",
                "A detailed description",
                "--notes",
                "Some notes",
                "-l",
                "backend",
            ],
        )
        issue_id = _extract_id(r.output)
        result = runner.invoke(cli, ["show", issue_id])
        assert result.exit_code == 0
        assert "Description" in result.output
        assert "A detailed description" in result.output
        assert "Notes" in result.output
        assert "Some notes" in result.output
        assert "backend" in result.output

    def test_show_with_fields(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Field issue", "-f", "severity=high"])
        issue_id = _extract_id(r.output)
        result = runner.invoke(cli, ["show", issue_id])
        assert result.exit_code == 0
        assert "Fields" in result.output
        assert "severity" in result.output

    def test_show_ready_issue(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Ready issue"])
        issue_id = _extract_id(r.output)
        result = runner.invoke(cli, ["show", issue_id])
        assert result.exit_code == 0
        assert "Ready" in result.output

    def test_show_blocked_issue(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r1 = runner.invoke(cli, ["create", "Blocked"])
        id1 = _extract_id(r1.output)
        r2 = runner.invoke(cli, ["create", "Blocker"])
        id2 = _extract_id(r2.output)
        runner.invoke(cli, ["add-dep", id1, id2])
        result = runner.invoke(cli, ["show", id1])
        assert result.exit_code == 0
        assert "Blocked by" in result.output

    def test_show_with_parent_and_children(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r_parent = runner.invoke(cli, ["create", "Parent", "--type", "epic"])
        parent_id = _extract_id(r_parent.output)
        r_child = runner.invoke(cli, ["create", "Child", "--parent", parent_id])
        child_id = _extract_id(r_child.output)
        # Show child to see parent
        result = runner.invoke(cli, ["show", child_id])
        assert result.exit_code == 0
        assert "Parent" in result.output
        # Show parent to see children
        result = runner.invoke(cli, ["show", parent_id])
        assert result.exit_code == 0
        assert "Children" in result.output

    def test_show_closed_issue(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Will close"])
        issue_id = _extract_id(r.output)
        runner.invoke(cli, ["close", issue_id])
        result = runner.invoke(cli, ["show", issue_id])
        assert result.exit_code == 0
        assert "Closed" in result.output

    def test_show_with_assignee(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Assigned"])
        issue_id = _extract_id(r.output)
        runner.invoke(cli, ["claim", issue_id, "--assignee", "agent-1"])
        result = runner.invoke(cli, ["show", issue_id])
        assert result.exit_code == 0
        assert "Assignee" in result.output
        assert "agent-1" in result.output


class TestUpdateEdgeCases:
    def test_update_with_json_output(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "JSON update"])
        issue_id = _extract_id(r.output)
        result = runner.invoke(cli, ["update", issue_id, "--title", "New title", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["title"] == "New title"

    def test_update_invalid_field_format(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Field test"])
        issue_id = _extract_id(r.output)
        result = runner.invoke(cli, ["update", issue_id, "-f", "badformat"])
        assert result.exit_code == 1

    def test_update_invalid_field_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Field test"])
        issue_id = _extract_id(r.output)
        result = runner.invoke(cli, ["update", issue_id, "-f", "badformat", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert "error" in data

    def test_update_with_design_field(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Design test"])
        issue_id = _extract_id(r.output)
        result = runner.invoke(cli, ["update", issue_id, "--design", "Use pattern X"])
        assert result.exit_code == 0

    def test_update_not_found_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["update", "nonexistent-abc", "--title", "nope", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert "error" in data

    def test_update_invalid_status_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Status test"])
        issue_id = _extract_id(r.output)
        result = runner.invoke(cli, ["update", issue_id, "--status", "bogus_state", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert "error" in data


class TestInitMode:
    def test_init_default_mode_is_ethereal(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_runner: CliRunner) -> None:
        monkeypatch.chdir(tmp_path)
        result = cli_runner.invoke(cli, ["init"])
        assert result.exit_code == 0
        config = json.loads((tmp_path / ".weft" / "filigree" / "config.json").read_text())
        assert config["mode"] == "ethereal"

    def test_init_with_server_mode(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_runner: CliRunner) -> None:
        monkeypatch.chdir(tmp_path)
        result = cli_runner.invoke(cli, ["init", "--mode", "server"])
        assert result.exit_code == 0
        config = json.loads((tmp_path / ".weft" / "filigree" / "config.json").read_text())
        assert config["mode"] == "server"

    def test_init_with_explicit_ethereal(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_runner: CliRunner) -> None:
        monkeypatch.chdir(tmp_path)
        result = cli_runner.invoke(cli, ["init", "--mode", "ethereal"])
        assert result.exit_code == 0
        config = json.loads((tmp_path / ".weft" / "filigree" / "config.json").read_text())
        assert config["mode"] == "ethereal"

    def test_init_invalid_mode_rejected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_runner: CliRunner) -> None:
        monkeypatch.chdir(tmp_path)
        result = cli_runner.invoke(cli, ["init", "--mode", "bogus"])
        assert result.exit_code != 0

    def test_init_existing_project_updates_mode(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_runner: CliRunner) -> None:
        """Running init --mode=server on an existing project updates the mode."""
        monkeypatch.chdir(tmp_path)
        cli_runner.invoke(cli, ["init"])
        result = cli_runner.invoke(cli, ["init", "--mode", "server"])
        assert result.exit_code == 0
        config = json.loads((tmp_path / ".weft" / "filigree" / "config.json").read_text())
        assert config["mode"] == "server"

    def test_init_existing_project_updates_name(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_runner: CliRunner) -> None:
        """Running init --name=X on an existing project updates the name."""
        monkeypatch.chdir(tmp_path)
        cli_runner.invoke(cli, ["init"])
        result = cli_runner.invoke(cli, ["init", "--name", "My Project"])
        assert result.exit_code == 0
        config = json.loads((tmp_path / ".weft" / "filigree" / "config.json").read_text())
        assert config["name"] == "My Project"

    def test_init_invalid_mode_no_directory_created(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_runner: CliRunner) -> None:
        monkeypatch.chdir(tmp_path)
        result = cli_runner.invoke(cli, ["init", "--mode", "bogus"])
        assert result.exit_code != 0
        assert not (tmp_path / ".weft").exists()
        assert not (tmp_path / ".filigree").exists()


def _store_db_path(tmp_path: Path) -> Path:
    """Resolve the active DB path for a project, layout-agnostic (.weft or legacy)."""
    from filigree.core import DB_FILENAME, find_filigree_anchor, read_conf

    anchor = find_filigree_anchor(tmp_path)
    if anchor.conf_path is not None:
        return (anchor.conf_path.parent / read_conf(anchor.conf_path)["db"]).resolve()
    return anchor.store_dir / DB_FILENAME


def _downgrade_db(tmp_path: Path, target_version: int = 1) -> None:
    """Rewrite the user_version pragma to simulate an outdated schema."""
    import sqlite3

    db_path = _store_db_path(tmp_path)
    conn = sqlite3.connect(str(db_path))
    conn.execute(f"PRAGMA user_version = {target_version}")
    conn.commit()
    conn.close()


class TestInitConfCutover:
    """filigree-4bf16e64b6: the hard config-anchor cutover. init imports a legacy
    .filigree.conf into .weft/filigree/config.json (conf-wins) and retires the conf;
    legacy-dir installs migrate forward and stay confless; nothing re-creates a conf."""

    def test_init_legacy_dir_stays_confless(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_runner: CliRunner) -> None:
        """A legacy .filigree/ install (no conf) migrates forward and stays confless —
        the deleted backfill never re-creates a .filigree.conf (B4)."""
        from filigree.core import DB_FILENAME, FILIGREE_DIR_NAME, FiligreeDB

        project_root = tmp_path / "myproj"
        project_root.mkdir()
        filigree_dir = project_root / FILIGREE_DIR_NAME
        filigree_dir.mkdir()
        seed = FiligreeDB(filigree_dir / DB_FILENAME, prefix="myproj")
        seed.initialize()
        issue = seed.create_issue("legacy issue")
        seed.close()

        monkeypatch.chdir(project_root)
        result = cli_runner.invoke(cli, ["init"])
        assert result.exit_code == 0, result.output

        # No conf is ever backfilled; identity in the migrated store's config.json.
        assert not (project_root / ".filigree.conf").exists()
        cfg = json.loads((project_root / ".weft" / "filigree" / "config.json").read_text())
        assert cfg["prefix"] == "myproj"
        # The migrated DB is still openable / writable via the confless path.
        update = cli_runner.invoke(cli, ["update", issue.id, "--title", "renamed", "--json"])
        assert update.exit_code == 0, update.output
        assert json.loads(update.output)["title"] == "renamed"

    def test_init_imports_and_retires_conf_conf_wins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_runner: CliRunner) -> None:
        """A present .filigree.conf is imported into config.json (conf-wins on the
        fields from_conf served) then retired to .filigree.conf.imported (T1)."""
        monkeypatch.chdir(tmp_path)
        cli_runner.invoke(cli, ["init"])  # born confless; config.json prefix == cwd.name
        store = tmp_path / ".weft" / "filigree"

        # Simulate a pre-cutover conf install whose prefix DIFFERS from config.json,
        # so conf-wins is provable.
        conf = tmp_path / ".filigree.conf"
        conf.write_text(
            json.dumps(
                {
                    "version": 1,
                    "project_name": "confname",
                    "prefix": "confpfx",
                    "db": ".weft/filigree/filigree.db",
                    "registry_backend": "local",
                }
            )
        )

        result = cli_runner.invoke(cli, ["init"])
        assert result.exit_code == 0, result.output
        # Conf retired (not preserved); an audit breadcrumb is left.
        assert not conf.exists()
        assert (tmp_path / ".filigree.conf.imported").exists()
        # config.json now carries the conf's authoritative fields (conf-wins), no db.
        cfg = json.loads((store / "config.json").read_text())
        assert cfg["prefix"] == "confpfx"
        assert cfg["name"] == "confname"
        assert "db" not in cfg
        # Runtime opens via the confless path with the imported identity.
        listed = cli_runner.invoke(cli, ["ready", "--json"])
        assert listed.exit_code == 0, listed.output

    def test_init_conf_import_is_convergent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_runner: CliRunner) -> None:
        """Running init twice on a conf install never re-creates a live conf and
        leaves config.json byte-stable — the retire is one-shot/convergent (T2/B4)."""
        monkeypatch.chdir(tmp_path)
        cli_runner.invoke(cli, ["init"])
        conf = tmp_path / ".filigree.conf"
        conf.write_text(json.dumps({"version": 1, "project_name": "p", "prefix": "p", "db": ".weft/filigree/filigree.db"}))

        first = cli_runner.invoke(cli, ["init"])
        assert first.exit_code == 0, first.output
        cfg_after_first = (tmp_path / ".weft" / "filigree" / "config.json").read_text()

        second = cli_runner.invoke(cli, ["init"])
        assert second.exit_code == 0, second.output
        assert not conf.exists()
        assert (tmp_path / ".weft" / "filigree" / "config.json").read_text() == cfg_after_first

    def test_init_conf_crash_after_config_self_heals(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_runner: CliRunner) -> None:
        """Crash AFTER config.json is reconciled but BEFORE the conf is retired:
        the conf is still on disk. Re-init re-imports idempotently and completes
        the retire (T3)."""
        monkeypatch.chdir(tmp_path)
        cli_runner.invoke(cli, ["init"])
        store = tmp_path / ".weft" / "filigree"
        cfg = json.loads((store / "config.json").read_text())
        cfg["prefix"] = "confpfx"
        cfg["name"] = "confname"
        (store / "config.json").write_text(json.dumps(cfg))
        conf = tmp_path / ".filigree.conf"
        conf.write_text(json.dumps({"version": 1, "project_name": "confname", "prefix": "confpfx", "db": ".weft/filigree/filigree.db"}))

        result = cli_runner.invoke(cli, ["init"])
        assert result.exit_code == 0, result.output
        assert not conf.exists()
        assert (tmp_path / ".filigree.conf.imported").exists()
        final = json.loads((store / "config.json").read_text())
        assert final["prefix"] == "confpfx"

    def test_corrupt_config_json_refuses_open_not_silent_drift(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_runner: CliRunner
    ) -> None:
        """A corrupt config.json must refuse (VALIDATION), not open under a defaulted
        prefix and write issues into the wrong namespace. config.json is the sole
        identity authority post-cutover, so from_store_dir is strict on a corrupt one
        (symmetric with from_conf's read_conf). Regression guard (advisor)."""
        from filigree.types.api import ErrorCode

        monkeypatch.chdir(tmp_path)
        cli_runner.invoke(cli, ["init", "--prefix", "realpfx"])
        (tmp_path / ".weft" / "filigree" / "config.json").write_text("{not valid json")
        result = cli_runner.invoke(cli, ["create", "drift", "--json"])
        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        assert payload["code"] == ErrorCode.VALIDATION
        assert "config.json" in payload["error"]


class TestInitSchemaMigration:
    """Test that `filigree init` on existing installs reports schema upgrades."""

    def test_init_existing_reports_schema_upgrade(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_runner: CliRunner) -> None:
        """Re-running init on an outdated schema prints 'Schema upgraded vN → vM'."""
        monkeypatch.chdir(tmp_path)
        cli_runner.invoke(cli, ["init"])
        _downgrade_db(tmp_path, target_version=1)

        result = cli_runner.invoke(cli, ["init"])
        assert result.exit_code == 0
        assert "already initialized" in result.output
        assert "Schema upgraded v1" in result.output

    def test_init_existing_no_upgrade_message_when_current(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_runner: CliRunner
    ) -> None:
        """Re-running init on a current schema does NOT print upgrade message."""
        monkeypatch.chdir(tmp_path)
        cli_runner.invoke(cli, ["init"])

        result = cli_runner.invoke(cli, ["init"])
        assert result.exit_code == 0
        assert "already initialized" in result.output
        assert "Schema upgraded" not in result.output


class TestDoctorFixHonoursConfDbPath:
    """filigree-fa6309d551: --fix must not touch a phantom legacy DB."""

    def test_doctor_fix_diagnoses_conf_relocated_schema_without_migrating(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_runner: CliRunner
    ) -> None:
        import shutil
        import sqlite3

        monkeypatch.chdir(tmp_path)
        cli_runner.invoke(cli, ["init"])

        # Move the DB to a custom location and update the conf to point at it.
        # This mirrors an install where users relocate the DB out of the store dir.
        legacy_db = _store_db_path(tmp_path)
        custom_db = tmp_path / "custom-data.db"
        shutil.move(str(legacy_db), str(custom_db))

        # init now retires the conf; place a fresh conf-relocated anchor on disk to
        # represent a not-yet-migrated conf install (doctor reads it but never
        # retires — only `init` does). doctor --fix must honour its db field.
        conf_path = tmp_path / ".filigree.conf"
        conf_data = {"version": 1, "project_name": tmp_path.name, "prefix": tmp_path.name, "db": "custom-data.db"}
        conf_path.write_text(json.dumps(conf_data))

        # Downgrade the *custom* DB so doctor sees an outdated schema.
        conn = sqlite3.connect(str(custom_db))
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        conn.close()

        # Sanity: legacy path must not exist (so an accidental bypass fails loud).
        assert not legacy_db.exists()

        result = cli_runner.invoke(cli, ["doctor", "--fix"])
        # Either exits 0 (all fixed) or 1 (env-level unfixable) — but must NOT
        # touch the legacy path and must NOT raise.
        assert result.exit_code in (0, 1), result.output
        assert not legacy_db.exists(), "doctor --fix must not create a phantom legacy DB"

        # Schema repair is now validate-and-report only; doctor --fix must not
        # mutate the database schema while repairing local bindings/pointers.
        conn = sqlite3.connect(str(custom_db))
        try:
            ver = conn.execute("PRAGMA user_version").fetchone()[0]
            assert ver == 1
        finally:
            conn.close()
        assert "Schema version: v1" in result.output


class TestDoctorFixSchema:
    """Test `filigree doctor --fix` schema handling."""

    def test_doctor_fix_reports_outdated_schema_without_migrating(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_runner: CliRunner
    ) -> None:
        """doctor --fix should diagnose outdated schema without applying migrations."""
        import sqlite3

        monkeypatch.chdir(tmp_path)
        cli_runner.invoke(cli, ["init"])
        _downgrade_db(tmp_path, target_version=1)

        result = cli_runner.invoke(cli, ["doctor", "--fix"])
        # filigree-467d1e7487: doctor exits 1 when unfixable env checks
        # remain (e.g. duplicate venv+uv-tool install in test env). Assert
        # the schema diagnostic happened, not the global exit code.
        assert result.exit_code == 1
        assert "Schema version: v1" in result.output
        assert "Schema upgraded v1" not in result.output
        conn = sqlite3.connect(str(_store_db_path(tmp_path)))
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        finally:
            conn.close()

    def test_doctor_fix_no_schema_issue_when_current(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_runner: CliRunner) -> None:
        """doctor --fix on a current schema should not mention schema upgrades."""
        monkeypatch.chdir(tmp_path)
        cli_runner.invoke(cli, ["init"])

        result = cli_runner.invoke(cli, ["doctor", "--fix"])
        # See note in test_doctor_fix_upgrades_outdated_schema (filigree-467d1e7487).
        assert result.exit_code in (0, 1)
        assert "Schema upgraded" not in result.output


class TestInstallMode:
    @staticmethod
    def _isolate_server_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Redirect SERVER_CONFIG_* to tmp_path so register_project doesn't
        collide with the user's real ``~/.config/filigree/server.json`` or
        with another test's stale entries — the same pattern
        ``TestInstallModeIntegration`` already uses.
        """
        config_dir = tmp_path / ".server-config"
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")
        monkeypatch.setattr("filigree.server.SERVER_PID_FILE", config_dir / "server.pid")

    def test_install_writes_mode_to_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_runner: CliRunner) -> None:
        """install --mode=server persists the mode to config.json."""
        monkeypatch.chdir(tmp_path)
        codex_home = tmp_path / ".test-home"
        codex_home.mkdir()
        monkeypatch.setattr("filigree.install_support.integrations.Path.home", lambda: codex_home)
        self._isolate_server_config(tmp_path, monkeypatch)
        # Set up a minimal project
        cli_runner.invoke(cli, ["init"])
        result = cli_runner.invoke(cli, ["install", "--mode", "server"])
        assert result.exit_code == 0, f"install failed:\n{result.output}\nexc={result.exception}"
        config = json.loads((tmp_path / ".weft" / "filigree" / "config.json").read_text())
        assert config["mode"] == "server"

    def test_install_preserves_existing_mode_when_no_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_runner: CliRunner
    ) -> None:
        """install without --mode keeps the existing mode."""
        monkeypatch.chdir(tmp_path)
        codex_home = tmp_path / ".test-home"
        codex_home.mkdir()
        monkeypatch.setattr("filigree.install_support.integrations.Path.home", lambda: codex_home)
        self._isolate_server_config(tmp_path, monkeypatch)
        cli_runner.invoke(cli, ["init", "--mode", "server"])
        result = cli_runner.invoke(cli, ["install"])
        assert result.exit_code == 0, f"install failed:\n{result.output}\nexc={result.exception}"
        config = json.loads((tmp_path / ".weft" / "filigree" / "config.json").read_text())
        assert config["mode"] == "server"


class TestAdminProjectConfigValidation:
    @staticmethod
    def _write_invalid_loomweave_config(project_root: Path) -> None:
        config_path = project_root / ".weft" / "filigree" / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "prefix": "test",
                    "name": "test",
                    "version": 1,
                    "registry_backend": "loomweave",
                    "loomweave": {},
                }
            )
            + "\n"
        )

    def test_existing_init_reports_project_config_validation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_runner: CliRunner
    ) -> None:
        monkeypatch.chdir(tmp_path)
        init_result = cli_runner.invoke(cli, ["init"])
        assert init_result.exit_code == 0
        self._write_invalid_loomweave_config(tmp_path)

        result = cli_runner.invoke(cli, ["init"])

        assert result.exit_code == 1
        assert not isinstance(result.exception, ValueError)
        assert "Invalid project config" in (result.output or "")
        assert "loomweave.base_url" in (result.output or "")

    def test_install_mode_reports_project_config_validation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_runner: CliRunner
    ) -> None:
        monkeypatch.chdir(tmp_path)
        init_result = cli_runner.invoke(cli, ["init"])
        assert init_result.exit_code == 0
        self._write_invalid_loomweave_config(tmp_path)

        result = cli_runner.invoke(cli, ["install", "--mode", "server"])

        assert result.exit_code == 1
        assert not isinstance(result.exception, ValueError)
        assert "Invalid project config" in (result.output or "")
        assert "loomweave.base_url" in (result.output or "")


@pytest.mark.slow
class TestInstallModeIntegration:
    def test_install_server_mode_registers_project(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_runner: CliRunner) -> None:
        monkeypatch.chdir(tmp_path)
        codex_home = tmp_path / ".test-home"
        codex_home.mkdir()
        monkeypatch.setattr("filigree.install_support.integrations.Path.home", lambda: codex_home)
        cli_runner.invoke(cli, ["init"])

        config_dir = tmp_path / ".server-config"
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")
        monkeypatch.setattr("filigree.server.SERVER_PID_FILE", config_dir / "server.pid")

        result = cli_runner.invoke(cli, ["install", "--mode", "server"])
        assert result.exit_code == 0

        from filigree.server import read_server_config

        sc = read_server_config()
        assert len(sc.projects) == 1

    def test_install_ethereal_mode_does_not_register(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_runner: CliRunner) -> None:
        monkeypatch.chdir(tmp_path)
        codex_home = tmp_path / ".test-home"
        codex_home.mkdir()
        monkeypatch.setattr("filigree.install_support.integrations.Path.home", lambda: codex_home)
        cli_runner.invoke(cli, ["init"])

        config_dir = tmp_path / ".server-config"
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")
        monkeypatch.setattr("filigree.server.SERVER_PID_FILE", config_dir / "server.pid")

        result = cli_runner.invoke(cli, ["install", "--mode", "ethereal"])
        assert result.exit_code == 0

        from filigree.server import read_server_config

        sc = read_server_config()
        assert len(sc.projects) == 0

    def test_install_server_mode_passes_mode_to_mcp(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_runner: CliRunner) -> None:
        monkeypatch.chdir(tmp_path)
        codex_home = tmp_path / ".test-home"
        codex_home.mkdir()
        monkeypatch.setattr("filigree.install_support.integrations.Path.home", lambda: codex_home)
        cli_runner.invoke(cli, ["init"])

        config_dir = tmp_path / ".server-config"
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")
        monkeypatch.setattr("filigree.server.SERVER_PID_FILE", config_dir / "server.pid")

        result = cli_runner.invoke(cli, ["install", "--mode", "server"])
        assert result.exit_code == 0
        assert "Server registration" in result.output

    def test_install_server_mode_uses_configured_server_port(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_runner: CliRunner
    ) -> None:
        monkeypatch.chdir(tmp_path)
        codex_home = tmp_path / ".test-home"
        codex_home.mkdir()
        monkeypatch.setattr("filigree.install_support.integrations.Path.home", lambda: codex_home)
        cli_runner.invoke(cli, ["init"])

        config_dir = tmp_path / ".server-config"
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")
        monkeypatch.setattr("filigree.server.SERVER_PID_FILE", config_dir / "server.pid")

        from filigree.server import ServerConfig, write_server_config

        write_server_config(ServerConfig(port=9911))
        result = cli_runner.invoke(cli, ["install", "--mode", "server"])
        assert result.exit_code == 0

        mcp = json.loads((tmp_path / ".mcp.json").read_text())
        prefix = json.loads((tmp_path / ".weft" / "filigree" / "config.json").read_text())["prefix"]
        assert mcp["mcpServers"]["filigree"]["type"] == "streamable-http"
        assert mcp["mcpServers"]["filigree"]["url"] == f"http://localhost:9911/mcp/?project={prefix}"


class TestDashboardPortValidation:
    """filigree-31da65493c: --port must reject invalid TCP values at the boundary."""

    @pytest.mark.parametrize("bad_port", ["0", "-1", "65536"])
    def test_dashboard_rejects_invalid_port(self, bad_port: str, cli_runner: CliRunner) -> None:
        result = cli_runner.invoke(cli, ["dashboard", "--port", bad_port])
        assert result.exit_code != 0, f"port {bad_port} should be rejected\n{result.output}"

    @pytest.mark.parametrize("bad_port", ["0", "-1", "65536"])
    def test_ensure_dashboard_rejects_invalid_port(self, bad_port: str, cli_runner: CliRunner) -> None:
        result = cli_runner.invoke(cli, ["ensure-dashboard", "--port", bad_port])
        assert result.exit_code != 0, f"port {bad_port} should be rejected\n{result.output}"


class TestInstallServerModeReload:
    """filigree-80753e4b54: install --mode server must reload a running daemon."""

    def test_install_server_mode_reloads_running_daemon(
        self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, _ = cli_in_project

        from filigree.server import DaemonStatus

        observed: dict[str, object] = {}

        def _register(filigree_dir: Path) -> None:
            observed["registered"] = str(filigree_dir)

        class _Resp:
            status = 200

            def __enter__(self) -> _Resp:
                return self

            def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
                pass

        def _urlopen(req: object, timeout: int = 0) -> _Resp:
            observed["reload_url"] = getattr(req, "full_url", "")
            return _Resp()

        # Stub out per-target installers that touch real $HOME state, so we
        # focus the test on the registration+reload flow.
        for target in (
            "install_claude_code_mcp",
            "install_codex_mcp",
            "install_claude_code_hooks",
            "install_skills",
            "install_codex_skills",
        ):
            monkeypatch.setattr(f"filigree.install.{target}", lambda *_a, **_kw: (True, "stubbed"))
        monkeypatch.setattr("filigree.install.inject_instructions", lambda _p: (True, "stubbed"))
        monkeypatch.setattr("filigree.install.ensure_gitignore", lambda _p: (True, "stubbed"))

        monkeypatch.setattr("filigree.server.register_project", _register)
        monkeypatch.setattr(
            "filigree.server.daemon_status",
            lambda: DaemonStatus(running=True, pid=123, port=9911, project_count=1),
        )
        monkeypatch.setattr("urllib.request.urlopen", _urlopen)

        result = runner.invoke(cli, ["install", "--mode", "server"])
        assert result.exit_code == 0, result.output
        assert observed.get("registered"), "register_project was not called"
        assert observed.get("reload_url") == "http://127.0.0.1:9911/api/reload", (
            f"daemon was not asked to reload; observed={observed}\n{result.output}"
        )


class TestServerRegisterReload:
    def test_server_register_reloads_running_daemon(self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch) -> None:
        runner, _ = cli_in_project

        from filigree.server import DaemonStatus

        observed: dict[str, object] = {}

        def _register(filigree_dir: Path) -> None:
            observed["registered"] = str(filigree_dir)

        class _Resp:
            status = 200

            def __enter__(self) -> _Resp:
                return self

            def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
                pass

        def _urlopen(req: object, timeout: int = 0) -> _Resp:
            observed["reload_url"] = getattr(req, "full_url", "")
            observed["reload_timeout"] = timeout
            return _Resp()

        monkeypatch.setattr("filigree.server.register_project", _register)
        monkeypatch.setattr("filigree.server.daemon_status", lambda: DaemonStatus(running=True, pid=123, port=9911, project_count=1))
        monkeypatch.setattr("urllib.request.urlopen", _urlopen)

        result = runner.invoke(cli, ["server", "register", "."])
        assert result.exit_code == 0
        assert "Registered" in result.output
        assert "Reloaded running daemon" in result.output
        assert observed["reload_url"] == "http://127.0.0.1:9911/api/reload"

    def test_server_unregister_reloads_running_daemon(
        self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, _ = cli_in_project

        from filigree.server import DaemonStatus

        observed: dict[str, object] = {}

        def _unregister(filigree_dir: Path) -> None:
            observed["unregistered"] = str(filigree_dir)

        class _Resp:
            status = 200

            def __enter__(self) -> _Resp:
                return self

            def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
                pass

        def _urlopen(req: object, timeout: int = 0) -> _Resp:
            observed["reload_url"] = getattr(req, "full_url", "")
            observed["reload_timeout"] = timeout
            return _Resp()

        monkeypatch.setattr("filigree.server.unregister_project", _unregister)
        monkeypatch.setattr("filigree.server.daemon_status", lambda: DaemonStatus(running=True, pid=123, port=9911, project_count=1))
        monkeypatch.setattr("urllib.request.urlopen", _urlopen)

        result = runner.invoke(cli, ["server", "unregister", "."])
        assert result.exit_code == 0
        assert "Unregistered" in result.output
        assert "Reloaded running daemon" in result.output
        assert observed["reload_url"] == "http://127.0.0.1:9911/api/reload"

    def test_server_register_skips_reload_when_daemon_not_running(
        self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, _ = cli_in_project

        from filigree.server import DaemonStatus

        monkeypatch.setattr("filigree.server.register_project", lambda _p: None)
        monkeypatch.setattr("filigree.server.daemon_status", lambda: DaemonStatus(running=False))

        result = runner.invoke(cli, ["server", "register", "."])
        assert result.exit_code == 0
        assert "Registered" in result.output
        assert "Reloaded running daemon" not in result.output


class TestDashboardServerModePidTracking:
    def test_dashboard_passes_allow_local_fallback(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_runner: CliRunner) -> None:
        observed: dict[str, object] = {}

        def _fake_dashboard_main(
            port: int,
            no_browser: bool,
            server_mode: bool,
            allow_http_force_close: bool = False,
            allow_local_fallback: bool = False,
        ) -> None:
            observed["allow_local_fallback"] = allow_local_fallback

        monkeypatch.setattr("filigree.dashboard.main", _fake_dashboard_main)

        result = cli_runner.invoke(cli, ["dashboard", "--no-browser", "--allow-local-fallback"])

        assert result.exit_code == 0
        assert observed["allow_local_fallback"] is True

    def test_dashboard_server_mode_claims_pid_for_status(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_runner: CliRunner
    ) -> None:
        config_dir = tmp_path / ".server-config"
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")
        monkeypatch.setattr("filigree.server.SERVER_PID_FILE", config_dir / "server.pid")
        # The test process is pytest, not filigree — stub ownership check so
        # PID tracking logic (the real subject under test) isn't blocked.
        monkeypatch.setattr("filigree.server.verify_pid_ownership", lambda *a, **kw: True)

        observed: dict[str, object] = {}

        def _fake_dashboard_main(
            port: int,
            no_browser: bool,
            server_mode: bool,
            allow_http_force_close: bool = False,
            allow_local_fallback: bool = False,
        ) -> None:
            from filigree.server import SERVER_PID_FILE, daemon_status

            status = daemon_status()
            observed["port_arg"] = port
            observed["no_browser_arg"] = no_browser
            observed["server_mode_arg"] = server_mode
            observed["status_running"] = status.running
            observed["status_port"] = status.port
            observed["pid_file_exists_during_run"] = SERVER_PID_FILE.exists()

        monkeypatch.setattr("filigree.dashboard.main", _fake_dashboard_main)

        result = cli_runner.invoke(cli, ["dashboard", "--server-mode", "--no-browser", "--port", "9911"])
        assert result.exit_code == 0
        assert observed["port_arg"] == 9911
        assert observed["no_browser_arg"] is True
        assert observed["server_mode_arg"] is True
        assert observed["status_running"] is True
        assert observed["status_port"] == 9911
        assert observed["pid_file_exists_during_run"] is True
        assert not (config_dir / "server.pid").exists()

    def test_dashboard_server_mode_refuses_when_live_daemon_tracked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_runner: CliRunner
    ) -> None:
        """filigree-ceb2da2411: failed daemon claim must abort, not race a second server."""
        config_dir = tmp_path / ".server-config"
        config_dir.mkdir(parents=True)
        pid_file = config_dir / "server.pid"
        pid_file.write_text('{"pid": 54321, "cmd": "filigree"}')

        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")
        monkeypatch.setattr("filigree.server.SERVER_PID_FILE", pid_file)
        monkeypatch.setattr("filigree.server.is_pid_alive", lambda pid: pid == 54321)
        monkeypatch.setattr("filigree.server.verify_pid_ownership", lambda *a, **kw: True)

        called = {"main": False}

        def _fake_dashboard_main(
            port: int,
            no_browser: bool,
            server_mode: bool,
            allow_http_force_close: bool = False,
            allow_local_fallback: bool = False,
        ) -> None:
            called["main"] = True

        monkeypatch.setattr("filigree.dashboard.main", _fake_dashboard_main)

        result = cli_runner.invoke(cli, ["dashboard", "--server-mode", "--no-browser"])
        assert result.exit_code != 0, "must refuse to start when a live daemon is already tracked"
        assert "already running" in (result.output or "").lower() or "already running" in (result.stderr or "").lower()
        assert called["main"] is False, "dashboard_main must not run after failed claim"
        assert json.loads(pid_file.read_text())["pid"] == 54321, "existing PID record must be preserved"

    def test_dashboard_server_mode_without_port_uses_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_runner: CliRunner
    ) -> None:
        """filigree-f863b9d1f8: --port omitted must not overwrite configured daemon port."""
        config_dir = tmp_path / ".server-config"
        config_dir.mkdir(parents=True)
        # Pre-existing config with port 9500; no live daemon claimed.
        (config_dir / "server.json").write_text(json.dumps({"port": 9500, "projects": {}}))

        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", config_dir)
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_FILE", config_dir / "server.json")
        monkeypatch.setattr("filigree.server.SERVER_PID_FILE", config_dir / "server.pid")
        monkeypatch.setattr("filigree.server.verify_pid_ownership", lambda *a, **kw: True)

        observed: dict[str, object] = {}

        def _fake_dashboard_main(
            port: int,
            no_browser: bool,
            server_mode: bool,
            allow_http_force_close: bool = False,
            allow_local_fallback: bool = False,
        ) -> None:
            observed["port_arg"] = port

        monkeypatch.setattr("filigree.dashboard.main", _fake_dashboard_main)

        result = cli_runner.invoke(cli, ["dashboard", "--server-mode", "--no-browser"])
        assert result.exit_code == 0, result.output
        assert observed["port_arg"] == 9500, "must inherit port from server.json when --port omitted"
        # Config must still hold 9500 afterwards.
        assert json.loads((config_dir / "server.json").read_text())["port"] == 9500


class TestNoFiligreeDir:
    def test_commands_fail_without_init(self, tmp_path: Path, cli_runner: CliRunner) -> None:
        original = os.getcwd()
        os.chdir(str(tmp_path))
        try:
            result = cli_runner.invoke(cli, ["list"])
            assert result.exit_code == 1
            assert "filigree init" in result.output.lower()
        finally:
            os.chdir(original)


class TestExportImportCli:
    def test_export_import(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, project_root = cli_in_project
        runner.invoke(cli, ["create", "Export me"])
        export_path = str(project_root / "export.jsonl")
        result = runner.invoke(cli, ["export", export_path])
        assert result.exit_code == 0
        assert "Exported" in result.output

    def test_import_merge(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, project_root = cli_in_project
        runner.invoke(cli, ["create", "Export me"])
        export_path = str(project_root / "export.jsonl")
        runner.invoke(cli, ["export", export_path])
        result = runner.invoke(cli, ["import", export_path, "--merge"])
        assert result.exit_code == 0
        assert "Imported" in result.output

    def test_import_conflict_without_merge_shows_clean_error(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Import without --merge on duplicate data should show clean error, not traceback."""
        runner, project_root = cli_in_project
        runner.invoke(cli, ["create", "Conflict me"])
        export_path = str(project_root / "export.jsonl")
        runner.invoke(cli, ["export", export_path])
        # Import same data again without --merge → should fail cleanly
        result = runner.invoke(cli, ["import", export_path])
        assert result.exit_code != 0
        assert "Import failed" in result.output
        # Must NOT contain a raw Python traceback
        assert "Traceback" not in (result.output or "")

    def test_export_empty_db(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, project_root = cli_in_project
        export_path = str(project_root / "empty.jsonl")
        result = runner.invoke(cli, ["export", export_path])
        assert result.exit_code == 0
        # The auto-seeded "Future" release singleton means 1 record exists
        assert "1 records" in result.output

    def test_import_oserror_shows_clean_error(self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch) -> None:
        """OSError during import should show clean error, not traceback."""
        runner, project_root = cli_in_project
        bad_file = project_root / "data.jsonl"
        bad_file.write_text("{}\n")

        def _raise_oserror(*a: object, **kw: object) -> None:
            raise OSError("disk read error")

        monkeypatch.setattr("filigree.core.FiligreeDB.import_jsonl", _raise_oserror)
        result = runner.invoke(cli, ["import", str(bad_file)])
        assert result.exit_code != 0
        assert "Import failed" in (result.output or "")
        assert "Traceback" not in (result.output or "")

    def test_export_oserror_shows_clean_error(self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch) -> None:
        # filigree-48613c1c55: export must surface OSError as a clean
        # "Export failed: …" line and exit 1, not as a raw Python traceback —
        # the contract already enforced for `import`.
        runner, project_root = cli_in_project

        def _raise_oserror(*a: object, **kw: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr("filigree.core.FiligreeDB.export_jsonl", _raise_oserror)
        result = runner.invoke(cli, ["export", str(project_root / "out.jsonl")])
        assert result.exit_code != 0
        assert "Export failed" in (result.output or "")
        assert "Traceback" not in (result.output or "")


class TestInstallForeignDatabaseMessage:
    """filigree-dad647cf35: install + doctor --fix must surface
    ForeignDatabaseError's rich remediation message instead of swallowing
    it into the generic FileNotFoundError handler.
    """

    def _raise_foreign(self, tmp_path: Path) -> object:
        from filigree.core import ForeignDatabaseError

        def _raiser(*_args: object, **_kwargs: object) -> None:
            raise ForeignDatabaseError(
                cwd=tmp_path / "inner",
                found_anchor=tmp_path / "outer" / ".filigree.conf",
                git_boundary=tmp_path / "inner",
            )

        return _raiser

    def test_install_surfaces_foreign_database_message(
        self, tmp_path: Path, cli_runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("filigree.cli_commands.admin.find_filigree_anchor", self._raise_foreign(tmp_path))
        original = os.getcwd()
        os.chdir(str(tmp_path))
        try:
            result = cli_runner.invoke(cli, ["install"])
        finally:
            os.chdir(original)
        assert result.exit_code == 1
        assert "Refusing to latch" in (result.output or "")

    def test_doctor_fix_surfaces_foreign_database_message(
        self, tmp_path: Path, cli_runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from filigree.install_support.doctor import CheckResult

        # run_doctor must return a fixable failure so doctor() enters the --fix
        # block (where the bug lives). admin.py does ``from filigree.install
        # import run_doctor`` inside the command, so patch at the source.
        monkeypatch.setattr(
            "filigree.install.run_doctor",
            lambda: [CheckResult(name="config.json", passed=False, message="stub", fix_hint="run init")],
        )
        monkeypatch.setattr("filigree.cli_commands.admin.find_filigree_anchor", self._raise_foreign(tmp_path))

        result = cli_runner.invoke(cli, ["doctor", "--fix"])
        assert result.exit_code == 1
        assert "Refusing to latch" in (result.output or "")
        # Regression guard: the generic line must NOT appear after the fix.
        assert "Cannot fix: no .filigree/ directory found" not in (result.output or "")


class TestInstallStepFailureExitCode:
    """filigree-ca4e5d28dd: install must exit non-zero when any selected
    step failed, instead of always returning 0 with the "Next:" hint.
    """

    def test_install_exits_nonzero_when_step_fails(self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch) -> None:
        runner, _project = cli_in_project

        def _stub_failure(*_args: object, **_kwargs: object) -> tuple[bool, str]:
            return (False, "stub failure")

        # Pick an installer that's invoked unconditionally in install_all mode.
        monkeypatch.setattr("filigree.install.ensure_gitignore", _stub_failure)

        result = runner.invoke(cli, ["install", "--gitignore"])
        assert result.exit_code != 0
        assert "stub failure" in (result.output or "")
        # The "Next:" hint must be suppressed when any step failed.
        assert "Next: filigree create" not in (result.output or "")

    def test_install_happy_path_still_exits_zero(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _project = cli_in_project
        result = runner.invoke(cli, ["install", "--gitignore"])
        assert result.exit_code == 0
        assert "Next: filigree create" in (result.output or "")


class TestInstallStepExceptionReporting:
    def test_install_reports_step_exception_as_failure(
        self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, _project = cli_in_project

        def _raise_oserror(*_args: object, **_kwargs: object) -> tuple[bool, str]:
            raise OSError("disk full")

        monkeypatch.setattr("filigree.install.inject_instructions", _raise_oserror)

        result = runner.invoke(cli, ["install", "--claude-md"])

        assert result.exit_code == 1
        assert not isinstance(result.exception, OSError)
        assert "CLAUDE.md: disk full" in (result.output or "")
        assert "Some install steps failed" in (result.output or "")
        assert "Next: filigree create" not in (result.output or "")


class TestMetricsDaysValidation:
    """filigree-d9cf9d34b1: metrics --days must reject non-positive values
    with a clean click error, not a Python traceback from analytics.
    """

    def test_metrics_rejects_negative_days(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _project = cli_in_project
        result = runner.invoke(cli, ["metrics", "--days=-5"])
        # Click UsageError (exit 2) — pre-fix this leaked a ValueError from
        # analytics through to a Python traceback (exit 1).
        assert result.exit_code == 2
        assert "Invalid value for '--days'" in result.output

    def test_metrics_rejects_zero_days(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _project = cli_in_project
        result = runner.invoke(cli, ["metrics", "--days=0"])
        assert result.exit_code == 2
        assert "Invalid value for '--days'" in result.output

    def test_metrics_accepts_positive_days(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _project = cli_in_project
        result = runner.invoke(cli, ["metrics", "--days=30"])
        assert result.exit_code == 0


class TestFixMcpTokenReference:
    """doctor --fix embeds the literal *project* federation token in the .mcp.json
    header (deconfliction plumbing, not a secret) — the server-mode /mcp route is
    project-scoped, so the daemon validates against the project's own token, not
    the home store (weft-23574069a1)."""

    @pytest.fixture(autouse=True)
    def _isolate_server_token(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("filigree.server.SERVER_CONFIG_DIR", tmp_path / "_srvcfg")
        for _v in ("WEFT_FEDERATION_TOKEN", "FILIGREE_FEDERATION_API_TOKEN", "FILIGREE_API_TOKEN"):
            monkeypatch.delenv(_v, raising=False)

    def _write_mcp(self, root: Path, auth: str) -> None:
        (root / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "filigree": {
                            "type": "streamable-http",
                            "url": "http://localhost:8749/mcp/?project=x",
                            "headers": {"Authorization": auth},
                        }
                    }
                }
            )
        )

    def test_embeds_literal_project_token(self, tmp_path: Path) -> None:
        from filigree.cli_commands.admin import _fix_mcp_token_reference
        from filigree.core import resolve_store_dir

        self._write_mcp(tmp_path, "Bearer ${WEFT_FEDERATION_TOKEN}")
        ok, _msg = _fix_mcp_token_reference(tmp_path)
        assert ok is True
        token = (resolve_store_dir(tmp_path) / "federation_token").read_text().strip()
        auth = json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]["filigree"]["headers"]["Authorization"]
        assert auth == f"Bearer {token}"
        assert "${" not in auth
        # NOT the daemon home-store token (the pre-fix behaviour).
        home = tmp_path / "_srvcfg" / "federation_token"
        if home.exists():
            assert token != home.read_text().strip()

    def test_idempotent_when_already_literal(self, tmp_path: Path) -> None:
        from filigree.cli_commands.admin import _fix_mcp_token_reference
        from filigree.core import resolve_store_dir
        from filigree.federation_token import mint_token_file

        token = mint_token_file(resolve_store_dir(tmp_path))
        self._write_mcp(tmp_path, f"Bearer {token}")
        ok, msg = _fix_mcp_token_reference(tmp_path)
        assert ok is False
        assert "already" in msg.lower()


class TestRotateFederationToken:
    """`filigree rotate-federation-token` — the supported deconfliction-token
    rotation path that closes the tier-2 sibling lockout."""

    def test_rotates_store_token(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_runner: CliRunner) -> None:
        monkeypatch.chdir(tmp_path)
        cli_runner.invoke(cli, ["init"])
        store = tmp_path / ".weft" / "filigree"

        # First rotation creates the token file (init does not mint one).
        first = cli_runner.invoke(cli, ["rotate-federation-token", "--json"])
        assert first.exit_code == 0, first.output
        assert json.loads(first.output)["rotated"] is True
        before = (store / "federation_token").read_text().strip()
        assert before

        # Second rotation replaces it with a fresh secret.
        second = cli_runner.invoke(cli, ["rotate-federation-token", "--json"])
        assert second.exit_code == 0, second.output
        after = (store / "federation_token").read_text().strip()
        assert after  # a token is present
        assert after != before  # genuinely rotated

    def test_rotation_realigns_file_to_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_runner: CliRunner) -> None:
        monkeypatch.chdir(tmp_path)
        cli_runner.invoke(cli, ["init"])
        store = tmp_path / ".weft" / "filigree"
        monkeypatch.setenv("WEFT_FEDERATION_TOKEN", "env-pinned-tok")

        result = cli_runner.invoke(cli, ["rotate-federation-token", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["env_pinned"] is True
        # The stale file is realigned to what the daemon enforces (the env token).
        assert (store / "federation_token").read_text().strip() == "env-pinned-tok"

    def test_refuses_outside_project(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli_runner: CliRunner) -> None:
        monkeypatch.chdir(tmp_path)  # no init → not a project
        result = cli_runner.invoke(cli, ["rotate-federation-token", "--json"])
        assert result.exit_code == 1
        assert json.loads(result.output)["code"] == "NOT_INITIALIZED"
