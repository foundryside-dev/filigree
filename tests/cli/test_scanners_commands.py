"""CLI tests for scanner lifecycle commands.

MCP shape verification (verified against mcp_tools/scanners.py handlers):

- list-scanners: ListResponse[T] — {items: [...], has_more: bool}
  Item keys: name, description, file_types (from ScannerConfig.to_dict())
- trigger-scan success: {status, scanner, file_path, file_id, scan_run_id, pid, log_path, message}
- trigger-scan-batch success: {status, scanner, file_count, processes_spawned, batch_id, scan_run_ids, per_file}
- get-scan-status: ScanRunStatusDict — {id, status, scanner_name, ..., process_alive, log_tail}
- preview-scan: {scanner, file_path, command, command_string, valid, validation_error}
- report-finding: {status, findings_created, findings_updated, file_created,
  observations_created, observations_failed, observation_ids, [finding_id], [observation_id], [warnings]}

Subprocess mocking: patch "filigree.scanner_runtime.subprocess.Popen" (same path as MCP tests).
trigger-scan and trigger-scan-batch are mocked at the subprocess level.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from filigree.cli import cli
from filigree.cli_common import get_db
from filigree.core import FiligreeDB, write_config
from tests._seeds import SeededProject

# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------


def _write_scanner_toml(project_path: Path, name: str = "test-scanner") -> None:
    """Write a scanner TOML into <project>/.filigree/scanners/."""
    scanners_dir = project_path / ".filigree" / "scanners"
    scanners_dir.mkdir(parents=True, exist_ok=True)
    (scanners_dir / f"{name}.toml").write_text(
        f'[scanner]\nname = "{name}"\ndescription = "Test scanner"\n'
        'command = "echo"\n'
        'args = ["scan", "{file}", "--api-url", "{api_url}", "--scan-run-id", "{scan_run_id}"]\n'
        'file_types = ["py"]\n'
    )


def _make_target_file(project_path: Path, name: str = "target.py") -> Path:
    """Create a target file and return its absolute path."""
    target = project_path / name
    target.write_text("x = 1\n")
    return target


class _FakeProc:
    """Minimal subprocess.Popen stand-in."""

    def __init__(self, pid: int = 12345) -> None:
        self.pid = pid
        self.killed = False

    def poll(self) -> None:
        return None

    def kill(self) -> None:
        self.killed = True


@pytest.fixture
def project_with_scanner(initialized_project: Path) -> SeededProject:
    """A project with a test-scanner TOML and a target.py file."""
    _write_scanner_toml(initialized_project)
    _make_target_file(initialized_project, "target.py")
    return SeededProject(path=initialized_project)


# ---------------------------------------------------------------------------
# TestListScannersCommand
# ---------------------------------------------------------------------------


class TestListScannersCommand:
    def test_list_empty_returns_envelope(self, initialized_project: Path) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["list-scanners", "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["items"] == []
            assert data["has_more"] is False
        finally:
            os.chdir(original)

    def test_list_scanner_present_item_shape(self, initialized_project: Path) -> None:
        """Items must include ScannerConfig.to_dict() scanner metadata."""
        _write_scanner_toml(initialized_project)
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["list-scanners", "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert len(data["items"]) == 1
            item = data["items"][0]
            assert item["name"] == "test-scanner"
            assert "description" in item
            assert "file_types" in item
            # Exact key set matches ScannerConfig.to_dict()
            assert set(item.keys()) == {
                "bundled_match",
                "bundled_name",
                "description",
                "estimated_cost",
                "execution_mode",
                "accepts_prompt",
                "applicable_prompts",
                "file_types",
                "language_focus",
                "managed",
                "may_send_contents",
                "name",
                "prompt_pack_aware",
                "preview_recommended",
                "prompt_packs_endpoint",
                "prompt_pack_scope",
                "prompt_pack_scope_summary",
                "requires_approval",
                "requires_dashboard",
                "risk_summary",
                "safe_preview_only",
                "sandbox_class",
                "sandbox_summary",
            }
            assert item["accepts_prompt"] is False
            assert item["prompt_pack_aware"] is False
            assert item["applicable_prompts"] == []
            assert item["prompt_packs_endpoint"] == "list_prompt_packs"
            assert item["bundled_name"] is False
            assert item["bundled_match"] is False
            assert item["managed"] is False
            assert item["language_focus"] == []
        finally:
            os.chdir(original)

    def test_list_plain_text_empty(self, initialized_project: Path) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["list-scanners"])
            assert result.exit_code == 0
            assert "No scanners" in result.output
            assert "filigree scanner available" in result.output
        finally:
            os.chdir(original)

    def test_list_plain_text_populated(self, initialized_project: Path) -> None:
        _write_scanner_toml(initialized_project)
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["list-scanners"])
            assert result.exit_code == 0
            assert "test-scanner" in result.output
        finally:
            os.chdir(original)


class TestScannerManagementCommand:
    def test_scanner_group_help_shows_bootstrap_flow(self, initialized_project: Path) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["scanner"])
            assert result.exit_code == 0, result.output
            assert "available -> enable -> trigger" in result.output
            assert "list-scanners" in result.output
            assert "trigger-scan" in result.output
        finally:
            os.chdir(original)

    def test_scanner_group_aliases_flat_scanner_commands(self, project_with_scanner: SeededProject) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(project_with_scanner.path))
        try:
            listed = runner.invoke(cli, ["scanner", "list", "--json"])
            assert listed.exit_code == 0, listed.output
            assert json.loads(listed.output)["items"][0]["name"] == "test-scanner"

            previewed = runner.invoke(cli, ["scanner", "preview", "test-scanner", "target.py", "--json"])
            assert previewed.exit_code == 0, previewed.output
            assert json.loads(previewed.output)["scanner"] == "test-scanner"

            with patch("filigree.scanner_runtime.subprocess.Popen", return_value=_FakeProc(12345)):
                triggered = runner.invoke(cli, ["scanner", "trigger", "test-scanner", "target.py", "--json"])
            assert triggered.exit_code == 0, triggered.output
            assert json.loads(triggered.output)["status"] == "triggered"
        finally:
            os.chdir(original)

    def test_scanner_available_lists_bundled_scanners(self, initialized_project: Path) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["scanner", "available", "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            names = {item["name"] for item in data["items"]}
            assert {"codex", "claude"} <= names
            codex = next(item for item in data["items"] if item["name"] == "codex")
            assert codex["enabled"] is False
            assert codex["command"] == "filigree-scanner-codex"
            assert "command_available" in codex
            assert "command_path" in codex
            assert codex["language_focus"] == ["python"]
            assert "python-engineering" in codex["applicable_prompts"]
            assert "pytorch" in codex["applicable_prompts"]
            assert "rust" not in codex["applicable_prompts"]
        finally:
            os.chdir(original)

    def test_scanner_available_reports_cli_prereq_status(self, initialized_project: Path) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            with patch("filigree.cli_commands.scanners.shutil.which", return_value=None):
                result = runner.invoke(cli, ["scanner", "available", "--json"])
            assert result.exit_code == 0, result.output
            codex = next(item for item in json.loads(result.output)["items"] if item["name"] == "codex")
            assert codex["command_available"] is False
            assert codex["command_path"] is None
        finally:
            os.chdir(original)

    def test_scanner_enable_warns_when_runner_command_missing(self, initialized_project: Path) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            with patch("filigree.cli_commands.scanners.shutil.which", return_value=None):
                result = runner.invoke(cli, ["scanner", "enable", "codex", "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["status"] == "enabled"
            assert data["command_available"] is False
            assert "uv tool install --upgrade filigree" in data["warnings"][0]
        finally:
            os.chdir(original)

    def test_scanner_enable_writes_installed_entrypoint_toml(self, initialized_project: Path) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["scanner", "enable", "codex", "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["status"] == "enabled"
            assert data["scanner"] == "codex"
            scanner_toml = initialized_project / ".filigree" / "scanners" / "codex.toml"
            content = scanner_toml.read_text()
            assert "# Generated by 'filigree scanner enable codex'." in content
            assert "scanner disable codex" in content
            assert 'command = "filigree-scanner-codex"' in content
            assert '"--prompt", "{prompt}"' in content
            assert "scripts/codex_bug_hunt.py" not in content

            listed = runner.invoke(cli, ["list-scanners", "--json"])
            assert listed.exit_code == 0, listed.output
            items = json.loads(listed.output)["items"]
            assert [item["name"] for item in items] == ["codex"]
        finally:
            os.chdir(original)

    def test_scanner_enable_human_output_deemphasizes_toml_path(self, initialized_project: Path) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["scanner", "enable", "codex"])
            assert result.exit_code == 0, result.output
            assert "Enabled scanner codex (managed)." in result.output
            assert "filigree scanner disable codex" in result.output
            assert ".filigree/scanners/codex.toml" not in result.output
        finally:
            os.chdir(original)

    def test_enabled_bundled_scanner_advertises_prompt_and_sandbox(self, initialized_project: Path) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            enable = runner.invoke(cli, ["scanner", "enable", "codex", "--json"])
            assert enable.exit_code == 0, enable.output

            result = runner.invoke(cli, ["list-scanners", "--json"])
            assert result.exit_code == 0, result.output
            item = json.loads(result.output)["items"][0]
            assert item["accepts_prompt"] is True
            assert item["prompt_pack_aware"] is True
            assert item["prompt_packs_endpoint"] == "list_prompt_packs"
            assert "security" in item["applicable_prompts"]
            assert "python-engineering" in item["applicable_prompts"]
            assert "pytorch" in item["applicable_prompts"]
            assert "rust" not in item["applicable_prompts"]
            assert "terraform" not in item["applicable_prompts"]
            assert "read-only" in item["sandbox_summary"]
            assert item["sandbox_class"] == "tool-sandboxed"
            assert item["managed"] is True
            assert item["bundled_name"] is True
            assert item["bundled_match"] is True
            assert item["language_focus"] == ["python"]
        finally:
            os.chdir(original)

    def test_scanner_disable_removes_enabled_bundled_scanner(self, initialized_project: Path) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            enable = runner.invoke(cli, ["scanner", "enable", "codex", "--json"])
            assert enable.exit_code == 0, enable.output

            result = runner.invoke(cli, ["scanner", "disable", "codex", "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["status"] == "disabled"
            assert not (initialized_project / ".filigree" / "scanners" / "codex.toml").exists()
        finally:
            os.chdir(original)

    def test_scanner_disable_refuses_custom_scanner_without_force(self, initialized_project: Path) -> None:
        custom = initialized_project / ".filigree" / "scanners" / "codex.toml"
        custom.write_text(
            '[scanner]\n'
            'name = "codex"\n'
            'description = "Custom scanner"\n'
            'command = "python custom.py"\n'
            'args = []\n'
            'file_types = ["py"]\n'
        )
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["scanner", "disable", "codex", "--json"])
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["code"] == "CONFLICT"
            assert "--force" in data["error"]
            assert data["details"]["conflict_kind"] == "custom"
            assert custom.exists()
        finally:
            os.chdir(original)

    def test_scanner_enable_reports_likely_stale_bundled_config(self, initialized_project: Path) -> None:
        stale = initialized_project / ".filigree" / "scanners" / "codex.toml"
        stale.write_text(
            '[scanner]\n'
            'name = "codex"\n'
            'description = "Per-file bug hunt using Codex CLI"\n'
            'command = "filigree-scanner-codex"\n'
            'args = ["--root", "{project_root}", "--file", "{file}"]\n'
            'file_types = ["py"]\n'
        )
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["scanner", "enable", "codex", "--json"])
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["code"] == "CONFLICT"
            assert "does not match current bundled definition" in data["error"]
            assert "--force" in data["error"]
            assert data["details"]["conflict_kind"] == "stale_bundled"
        finally:
            os.chdir(original)

    def test_scanner_disable_removes_non_bundled_custom_scanner_without_force(self, initialized_project: Path) -> None:
        custom = initialized_project / ".filigree" / "scanners" / "custom.toml"
        custom.write_text(
            '[scanner]\n'
            'name = "custom"\n'
            'description = "Custom scanner"\n'
            'command = "python custom.py"\n'
            'args = []\n'
            'file_types = ["py"]\n'
        )
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["scanner", "disable", "custom", "--json"])
            assert result.exit_code == 0, result.output
            assert json.loads(result.output)["status"] == "disabled"
            assert not custom.exists()
        finally:
            os.chdir(original)

    def test_scanner_prompts_lists_bundled_prompt_packs(self, initialized_project: Path) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["scanner", "prompts", "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            names = {item["name"] for item in data["items"]}
            assert {"security", "pytorch", "quality-engineering", "major-refactor", "css", "javascript", "typescript"} <= names
            assert all(item["when_to_use"].startswith("Use when ") for item in data["items"])
            assert all(item["audience"] == "agent" for item in data["items"])
            assert all(item["prompt_pack_scope"] == "advisory" for item in data["items"])
            assert all(item["expected_relative_cost"] in {"low", "medium", "high"} for item in data["items"])
            assert all(item["instructions"] for item in data["items"])
            bug_hunt = next(item for item in data["items"] if item["name"] == "bug-hunt")
            assert bug_hunt["language"] == "any"
            assert bug_hunt["expected_relative_cost"] == "low"
            python = next(item for item in data["items"] if item["name"] == "python-engineering")
            assert python["language"] == "python"
            rust = next(item for item in data["items"] if item["name"] == "rust")
            assert rust["language"] == "rust"
            major = next(item for item in data["items"] if item["name"] == "major-refactor")
            assert major["expected_relative_cost"] == "high"
            assert "when_to_use" in major
            assert major["components"] == [
                "solution-architecture",
                "systems-thinking",
                "python-engineering",
                "quality-engineering",
            ]
            comprehensive = next(item for item in data["items"] if item["name"] == "comprehensive")
            assert "security" in comprehensive["components"]
            assert comprehensive["components"] != major["components"]
        finally:
            os.chdir(original)

    def test_scanner_prompts_human_output_includes_when_to_use(self, initialized_project: Path) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["scanner", "prompts"])
            assert result.exit_code == 0, result.output
            assert "Use when you want a broad pass" in result.output
            assert "Prompt packs are advisory" in result.output
            assert "Some packs are language-specific" in result.output
            assert "applicable_prompts" in result.output
        finally:
            os.chdir(original)

    def test_scanner_prompts_filters_by_language(self, initialized_project: Path) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["scanner", "prompts", "--language", "python", "--json"])
            assert result.exit_code == 0, result.output
            names = {item["name"] for item in json.loads(result.output)["items"]}
            assert {"bug-hunt", "security", "python-engineering", "pytorch"} <= names
            assert "rust" not in names
            assert "terraform" not in names
            assert "react" not in names
        finally:
            os.chdir(original)

    def test_list_available_scanners_alias(self, initialized_project: Path) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["list-available-scanners", "--json"])
            assert result.exit_code == 0, result.output
            names = {item["name"] for item in json.loads(result.output)["items"]}
            assert {"codex", "claude"} <= names
        finally:
            os.chdir(original)


# ---------------------------------------------------------------------------
# TestPreviewScanCommand
# ---------------------------------------------------------------------------


class TestPreviewScanCommand:
    def test_preview_scan_happy_path(self, project_with_scanner: SeededProject) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(project_with_scanner.path))
        try:
            result = runner.invoke(cli, ["preview-scan", "test-scanner", "target.py", "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["scanner"] == "test-scanner"
            assert data["file_path"] == "target.py"
            assert isinstance(data["command"], list)
            assert "target.py" in data["command_string"]
            assert data["valid"] is True
            assert data["validation_error"] is None
            assert data["api_url_source"] in {"fallback_default", "ephemeral_port", "server_config"}
            assert data["execution_mode"] == "external_process"
            assert data["may_send_contents"] is True
            assert data["requires_dashboard"] is True
            assert data["estimated_cost"] == "unknown"
            assert data["safe_preview_only"] is True
            assert data["preview_recommended"] is True
            assert data["requires_approval"] is True
            assert data["sandbox_class"] == "custom"
            assert "External scanner process" in data["risk_summary"]
        finally:
            os.chdir(original)

    def test_preview_scan_accepts_prompt_pack(self, initialized_project: Path) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            enabled = runner.invoke(cli, ["scanner", "enable", "codex", "--json"])
            assert enabled.exit_code == 0, enabled.output
            _make_target_file(initialized_project, "target.py")

            result = runner.invoke(cli, ["preview-scan", "codex", "target.py", "--prompt", "security", "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert "--prompt security" in data["command_string"]
        finally:
            os.chdir(original)

    def test_preview_scan_rejects_unknown_prompt_pack(self, initialized_project: Path) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            enabled = runner.invoke(cli, ["scanner", "enable", "codex", "--json"])
            assert enabled.exit_code == 0, enabled.output
            _make_target_file(initialized_project, "target.py")

            result = runner.invoke(cli, ["preview-scan", "codex", "target.py", "--prompt", "not-a-pack", "--json"])
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["code"] == "VALIDATION"
            assert "Unknown prompt pack" in data["error"]
        finally:
            os.chdir(original)

    def test_preview_scan_rejects_prompt_pack_when_scanner_template_cannot_accept_it(
        self,
        project_with_scanner: SeededProject,
    ) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(project_with_scanner.path))
        try:
            result = runner.invoke(cli, ["preview-scan", "test-scanner", "target.py", "--prompt", "security", "--json"])
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["code"] == "VALIDATION"
            assert "does not accept prompt packs" in data["error"]
        finally:
            os.chdir(original)

    def test_preview_scan_uses_ethereal_port_file_for_default_api_url(self, project_with_scanner: SeededProject) -> None:
        (project_with_scanner.path / ".filigree" / "ephemeral.port").write_text("9229\n")
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(project_with_scanner.path))
        try:
            result = runner.invoke(cli, ["preview-scan", "test-scanner", "target.py", "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert "http://localhost:9229" in data["command"]
            assert data["api_url"] == "http://localhost:9229"
            assert data["api_url_source"] == "ephemeral_port"
            assert "http://localhost:8377" not in data["command"]
        finally:
            os.chdir(original)

    def test_preview_scan_uses_server_config_port_in_server_mode(
        self,
        project_with_scanner: SeededProject,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from filigree.server import ServerConfig

        write_config(project_with_scanner.path / ".filigree", {"prefix": "test", "version": 1, "mode": "server"})
        monkeypatch.setattr("filigree.server.read_server_config", lambda: ServerConfig(port=9230))

        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(project_with_scanner.path))
        try:
            result = runner.invoke(cli, ["preview-scan", "test-scanner", "target.py", "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert "http://localhost:9230" in data["command"]
            assert data["api_url"] == "http://localhost:9230"
            assert data["api_url_source"] == "server_config"
        finally:
            os.chdir(original)

    def test_preview_scan_not_found(self, initialized_project: Path) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["preview-scan", "nonexistent", "foo.py", "--json"])
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["code"] == "NOT_FOUND"
        finally:
            os.chdir(original)

    def test_preview_scan_known_bundled_not_enabled_points_to_enable_flow(self, initialized_project: Path) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["preview-scan", "codex", "foo.py", "--json"])
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["code"] == "NOT_FOUND"
            assert data["details"]["bundled"] is True
            assert data["details"]["enable_with"] == "enable_scanner"
            assert data["details"]["cli_enable_command"] == "filigree scanner enable codex"
            assert "filigree scanner available" in data["details"]["hint"]
        finally:
            os.chdir(original)

    def test_preview_scan_path_traversal(self, initialized_project: Path) -> None:
        _write_scanner_toml(initialized_project)
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["preview-scan", "test-scanner", "../../etc/passwd", "--json"])
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["code"] == "VALIDATION"
        finally:
            os.chdir(original)

    def test_preview_scan_plain_text(self, project_with_scanner: SeededProject) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(project_with_scanner.path))
        try:
            result = runner.invoke(cli, ["preview-scan", "test-scanner", "target.py"])
            assert result.exit_code == 0
            assert "test-scanner" in result.output
            assert "target.py" in result.output
        finally:
            os.chdir(original)


# ---------------------------------------------------------------------------
# TestGetScanStatusCommand
# ---------------------------------------------------------------------------


class TestGetScanStatusCommand:
    def test_get_scan_status_happy_path(self, initialized_project: Path) -> None:
        from filigree.cli_common import get_db

        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            with get_db() as db:
                db.create_scan_run(
                    scan_run_id="test-run-cli-1",
                    scanner_name="scanner",
                    scan_source="scanner",
                    file_paths=["src/a.py"],
                    file_ids=["fid-1"],
                )
            runner = CliRunner()
            result = runner.invoke(cli, ["get-scan-status", "test-run-cli-1", "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["id"] == "test-run-cli-1"
            assert data["status"] == "pending"
            assert "process_alive" in data
            assert "log_tail" in data
        finally:
            os.chdir(original)

    def test_get_scan_status_not_found(self, initialized_project: Path) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["get-scan-status", "nonexistent-run", "--json"])
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["code"] == "NOT_FOUND"
        finally:
            os.chdir(original)

    def test_get_scan_status_empty_id_rejected(self, initialized_project: Path) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["get-scan-status", "   ", "--json"])
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["code"] == "VALIDATION"
        finally:
            os.chdir(original)

    def test_get_scan_status_plain_text(self, initialized_project: Path) -> None:
        from filigree.cli_common import get_db

        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            with get_db() as db:
                db.create_scan_run(
                    scan_run_id="test-run-cli-plain",
                    scanner_name="scanner",
                    scan_source="scanner",
                    file_paths=["src/a.py"],
                    file_ids=["fid-2"],
                )
            runner = CliRunner()
            result = runner.invoke(cli, ["get-scan-status", "test-run-cli-plain"])
            assert result.exit_code == 0
            assert "test-run-cli-plain" in result.output
        finally:
            os.chdir(original)


# ---------------------------------------------------------------------------
# TestReportFindingCommand
# ---------------------------------------------------------------------------


_REPORT_FINDING_JSON = json.dumps(
    {
        "path": "src/foo.py",
        "rule_id": "test-rule",
        "message": "This is a test finding",
        "severity": "high",
    }
)

_REPORT_FINDING_KEYS = frozenset({"status", "findings_created", "findings_updated", "file_created"})


class TestReportFindingCommand:
    def test_report_finding_stdin(self, initialized_project: Path) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            # Pass --response-detail=full to keep the legacy batch-stats keys
            # (F3 — review-h switched the CLI default to 'slim' too).
            result = runner.invoke(
                cli,
                ["report-finding", "--json", "--response-detail", "full"],
                input=_REPORT_FINDING_JSON,
            )
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert _REPORT_FINDING_KEYS.issubset(set(data.keys()))
            assert data["findings_created"] == 1
            assert data["file_created"] is True
            assert "finding_id" in data
            assert data["observations_created"] == 0
            assert "observation_id" not in data
            assert data["observation_ids"] == []
        finally:
            os.chdir(original)

    def test_report_finding_slim_default_drops_batch_stats(self, initialized_project: Path) -> None:
        """CLI default is now slim — batch stats absent without --response-detail=full."""
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["report-finding", "--json"], input=_REPORT_FINDING_JSON)
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            # Slim keeps status / finding_id only; paired observations are opt-in.
            assert data["status"] == "created"
            assert "finding_id" in data
            assert "observation_id" not in data
            # And drops the batch ingest stats.
            for noisy in (
                "findings_created",
                "findings_updated",
                "file_created",
                "observations_created",
                "observations_failed",
                "observation_ids",
            ):
                assert noisy not in data, f"slim CLI response unexpectedly carries {noisy!r}"
        finally:
            os.chdir(original)

    def test_report_finding_file_option(self, initialized_project: Path) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        finding_file = initialized_project / "finding.json"
        finding_file.write_text(_REPORT_FINDING_JSON)
        try:
            result = runner.invoke(cli, ["report-finding", "--file", "finding.json", "--json", "--response-detail", "full"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert _REPORT_FINDING_KEYS.issubset(set(data.keys()))
            assert data["findings_created"] == 1
        finally:
            os.chdir(original)

    def test_report_finding_create_observation_opt_in(self, initialized_project: Path) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(
                cli,
                ["report-finding", "--json", "--response-detail", "full", "--create-observation"],
                input=_REPORT_FINDING_JSON,
            )
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["observations_created"] == 1
            assert isinstance(data["observation_id"], str)
            assert data["observation_ids"] == [data["observation_id"]]
        finally:
            os.chdir(original)

    def test_report_finding_stdin_and_file_same_result(self, initialized_project: Path) -> None:
        """stdin and --file paths should both succeed and produce the required key set."""
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        # Use distinct findings to avoid dedup (which suppresses finding_id on update)
        finding_a = json.dumps({"path": "src/a.py", "rule_id": "rule-a", "message": "A"})
        finding_b = json.dumps({"path": "src/b.py", "rule_id": "rule-b", "message": "B"})
        finding_file = initialized_project / "finding_b.json"
        finding_file.write_text(finding_b)
        try:
            stdin_result = runner.invoke(
                cli,
                ["report-finding", "--json", "--response-detail", "full"],
                input=finding_a,
            )
            file_result = runner.invoke(
                cli,
                ["report-finding", "--file", "finding_b.json", "--json", "--response-detail", "full"],
            )
            assert stdin_result.exit_code == 0, stdin_result.output
            assert file_result.exit_code == 0, file_result.output
            stdin_data = json.loads(stdin_result.output)
            file_data = json.loads(file_result.output)
            # Both should have the required keys
            assert _REPORT_FINDING_KEYS.issubset(set(stdin_data.keys()))
            assert _REPORT_FINDING_KEYS.issubset(set(file_data.keys()))
            assert stdin_data["findings_created"] == 1
            assert file_data["findings_created"] == 1
        finally:
            os.chdir(original)

    def test_report_finding_invalid_json(self, initialized_project: Path) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["report-finding", "--json"], input="not valid json{")
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["code"] == "VALIDATION"
        finally:
            os.chdir(original)

    def test_report_finding_missing_required_fields(self, initialized_project: Path) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["report-finding", "--json"], input='{"path": "foo.py"}')
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["code"] == "VALIDATION"
        finally:
            os.chdir(original)

    def test_report_finding_invalid_severity(self, initialized_project: Path) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        bad_json = json.dumps({"path": "foo.py", "rule_id": "r", "message": "m", "severity": "SUPER_CRITICAL"})
        try:
            result = runner.invoke(cli, ["report-finding", "--json"], input=bad_json)
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["code"] == "VALIDATION"
        finally:
            os.chdir(original)

    def test_report_finding_accepts_file_path_alias(self, initialized_project: Path) -> None:
        """The CLI should accept file_path as an alias for path."""
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        alias_json = json.dumps({"file_path": "src/bar.py", "rule_id": "r", "message": "m"})
        try:
            result = runner.invoke(cli, ["report-finding", "--json", "--response-detail", "full"], input=alias_json)
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["findings_created"] == 1
        finally:
            os.chdir(original)

    def test_report_finding_plain_text(self, initialized_project: Path) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["report-finding"], input=_REPORT_FINDING_JSON)
            assert result.exit_code == 0
            assert "src/foo.py" in result.output
        finally:
            os.chdir(original)


# ---------------------------------------------------------------------------
# TestTriggerScanCommand
# ---------------------------------------------------------------------------

# NOTE: trigger-scan and trigger-scan-batch spawn subprocesses. We mock
# filigree.scanner_runtime.subprocess.Popen to avoid actually running a process.
# Validation/error-path tests do not need mocking.


class TestTriggerScanCommand:
    def test_trigger_scan_success(self, project_with_scanner: SeededProject) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(project_with_scanner.path))
        try:
            with patch("filigree.scanner_runtime.subprocess.Popen", return_value=_FakeProc(12345)):
                result = runner.invoke(cli, ["trigger-scan", "test-scanner", "target.py", "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["status"] == "triggered"
            assert data["scanner"] == "test-scanner"
            assert data["file_path"] == "target.py"
            assert "file_id" in data
            assert "scan_run_id" in data
            assert data["pid"] == 12345
            assert "log_path" in data
            assert "message" in data
            assert data["api_url_source"] in {"fallback_default", "ephemeral_port", "server_config"}
            assert data["api_url"].startswith("http://localhost:")
            assert data["prompt_pack_scope"] == "advisory"
            assert data["preview_recommended"] is True
            assert data["sandbox_class"] == "custom"
            assert "repository files" in data["risk_summary"]
        finally:
            os.chdir(original)

    def test_trigger_scan_uses_ethereal_port_file_for_default_api_url(self, project_with_scanner: SeededProject) -> None:
        (project_with_scanner.path / ".filigree" / "ephemeral.port").write_text("9229\n")
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(project_with_scanner.path))
        try:
            with patch("filigree.scanner_runtime.subprocess.Popen", return_value=_FakeProc(12345)) as popen:
                result = runner.invoke(cli, ["trigger-scan", "test-scanner", "target.py", "--json"])
            assert result.exit_code == 0, result.output
            assert "http://localhost:9229" in popen.call_args.args[0]
            assert "http://localhost:8377" not in popen.call_args.args[0]
            assert json.loads(result.output)["api_url_source"] == "ephemeral_port"
        finally:
            os.chdir(original)

    def test_trigger_scan_explicit_api_url_overrides_port_file(self, project_with_scanner: SeededProject) -> None:
        (project_with_scanner.path / ".filigree" / "ephemeral.port").write_text("9229\n")
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(project_with_scanner.path))
        try:
            with patch("filigree.scanner_runtime.subprocess.Popen", return_value=_FakeProc(12345)) as popen:
                result = runner.invoke(
                    cli,
                    ["trigger-scan", "test-scanner", "target.py", "--api-url", "http://localhost:9999", "--json"],
                )
            assert result.exit_code == 0, result.output
            assert "http://localhost:9999" in popen.call_args.args[0]
            assert "http://localhost:9229" not in popen.call_args.args[0]
            assert json.loads(result.output)["api_url_source"] == "explicit"
        finally:
            os.chdir(original)

    def test_trigger_scan_rejects_prompt_pack_when_scanner_template_cannot_accept_it(
        self,
        project_with_scanner: SeededProject,
    ) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(project_with_scanner.path))
        try:
            result = runner.invoke(cli, ["trigger-scan", "test-scanner", "target.py", "--prompt", "security", "--json"])
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["code"] == "VALIDATION"
            assert "does not accept prompt packs" in data["error"]
        finally:
            os.chdir(original)

    def test_trigger_scan_scanner_not_found(self, initialized_project: Path) -> None:
        _make_target_file(initialized_project, "target.py")
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["trigger-scan", "nonexistent", "target.py", "--json"])
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["code"] == "NOT_FOUND"
        finally:
            os.chdir(original)

    def test_trigger_scan_file_not_found(self, initialized_project: Path) -> None:
        _write_scanner_toml(initialized_project)
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["trigger-scan", "test-scanner", "no_such_file.py", "--json"])
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["code"] in ("NOT_FOUND", "VALIDATION")
        finally:
            os.chdir(original)

    def test_trigger_scan_path_traversal(self, initialized_project: Path) -> None:
        _write_scanner_toml(initialized_project)
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["trigger-scan", "test-scanner", "../../etc/passwd", "--json"])
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["code"] == "VALIDATION"
        finally:
            os.chdir(original)

    def test_trigger_scan_invalid_api_url(self, project_with_scanner: SeededProject) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(project_with_scanner.path))
        try:
            result = runner.invoke(
                cli,
                ["trigger-scan", "test-scanner", "target.py", "--api-url", "https://evil.example.com", "--json"],
            )
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["code"] == "INVALID_API_URL"
        finally:
            os.chdir(original)

    def test_trigger_scan_spawn_failure(self, project_with_scanner: SeededProject) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(project_with_scanner.path))
        try:
            with patch("filigree.scanner_runtime.subprocess.Popen", side_effect=OSError("mock spawn fail")):
                result = runner.invoke(cli, ["trigger-scan", "test-scanner", "target.py", "--json"])
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["code"] == "IO"
        finally:
            os.chdir(original)

    def test_trigger_scan_backfill_failure_marks_reserved_run_failed(self, project_with_scanner: SeededProject) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(project_with_scanner.path))
        proc = _FakeProc(12346)
        try:
            with (
                patch("filigree.scanner_runtime.subprocess.Popen", return_value=proc),
                patch.object(FiligreeDB, "set_scan_run_spawn_info", side_effect=sqlite3.OperationalError("DB broken")),
            ):
                result = runner.invoke(cli, ["trigger-scan", "test-scanner", "target.py", "--json"])
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["code"] == "IO"
            assert proc.killed is True
            with get_db() as db:
                row = db.conn.execute("SELECT id, status, error_message FROM scan_runs").fetchone()
            assert row is not None
            assert row["status"] == "failed"
            assert "DB tracking failed" in row["error_message"]
        finally:
            os.chdir(original)


# ---------------------------------------------------------------------------
# TestTriggerScanBatchCommand
# ---------------------------------------------------------------------------


class TestTriggerScanBatchCommand:
    def test_batch_scan_success(self, project_with_scanner: SeededProject) -> None:
        _make_target_file(project_with_scanner.path, "target2.py")
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(project_with_scanner.path))
        try:
            with patch(
                "filigree.scanner_runtime.subprocess.Popen",
                side_effect=[_FakeProc(100), _FakeProc(101)],
            ):
                result = runner.invoke(
                    cli,
                    ["trigger-scan-batch", "test-scanner", "target.py", "target2.py", "--json"],
                )
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["status"] == "triggered"
            assert data["scanner"] == "test-scanner"
            assert data["file_count"] == 2
            assert data["processes_spawned"] == 2
            assert "batch_id" in data
            assert len(data["scan_run_ids"]) == 2
            assert len(set(data["scan_run_ids"])) == 2  # unique per file
            assert len(data["per_file"]) == 2
            assert data["api_url_source"] in {"fallback_default", "ephemeral_port", "server_config"}
            assert data["prompt_pack_scope"] == "advisory"
            assert data["sandbox_class"] == "custom"
        finally:
            os.chdir(original)

    def test_batch_scan_all_spawn_failure(self, project_with_scanner: SeededProject) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(project_with_scanner.path))
        try:
            with patch("filigree.scanner_runtime.subprocess.Popen", side_effect=OSError("mock fail")):
                result = runner.invoke(
                    cli,
                    ["trigger-scan-batch", "test-scanner", "target.py", "--json"],
                )
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["code"] == "IO"
        finally:
            os.chdir(original)

    def test_batch_scan_scanner_not_found(self, initialized_project: Path) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(
                cli,
                ["trigger-scan-batch", "nonexistent", "foo.py", "--json"],
            )
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["code"] == "NOT_FOUND"
        finally:
            os.chdir(original)

    def test_batch_scan_skips_invalid_paths(self, project_with_scanner: SeededProject) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(project_with_scanner.path))
        try:
            with patch("filigree.scanner_runtime.subprocess.Popen", return_value=_FakeProc(100)):
                result = runner.invoke(
                    cli,
                    ["trigger-scan-batch", "test-scanner", "target.py", "nonexistent.py", "../../etc/passwd", "--json"],
                )
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["status"] == "triggered"
            assert data["file_count"] == 1
            assert len(data["skipped"]) == 2
        finally:
            os.chdir(original)

    def test_batch_scan_invalid_api_url(self, project_with_scanner: SeededProject) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(project_with_scanner.path))
        try:
            result = runner.invoke(
                cli,
                ["trigger-scan-batch", "test-scanner", "target.py", "--api-url", "https://evil.example.com", "--json"],
            )
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["code"] == "INVALID_API_URL"
        finally:
            os.chdir(original)

    def test_batch_scan_backfill_failure_marks_reserved_run_failed(self, project_with_scanner: SeededProject) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(project_with_scanner.path))
        proc = _FakeProc(102)
        try:
            with (
                patch("filigree.scanner_runtime.subprocess.Popen", return_value=proc),
                patch.object(FiligreeDB, "set_scan_run_spawn_info", side_effect=sqlite3.OperationalError("DB broken")),
            ):
                result = runner.invoke(cli, ["trigger-scan-batch", "test-scanner", "target.py", "--json"])
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["code"] == "IO"
            assert proc.killed is True
            with get_db() as db:
                row = db.conn.execute("SELECT id, status, error_message FROM scan_runs").fetchone()
            assert row is not None
            assert row["status"] == "failed"
            assert "DB tracking failed" in row["error_message"]
        finally:
            os.chdir(original)


# ---------------------------------------------------------------------------
# TestForeignDatabaseDiagnostic — bug fix: scanner CLI must surface the rich
# ForeignDatabaseError message, not collapse it into a generic
# "Project directory not initialized" line. Mirrors the regression contract
# in tests/test_doctor.py::test_foreign_database_is_reported_with_specific_message.
# ---------------------------------------------------------------------------


class TestForeignDatabaseDiagnostic:
    @staticmethod
    def _make_foreign_layout(tmp_path: Path) -> Path:
        """Outer project has .filigree/, inner is a separate git repo with no anchor."""
        outer = tmp_path / "outer"
        outer.mkdir()
        (outer / ".git").mkdir()
        (outer / ".filigree.conf").write_text("[filigree]\n", encoding="utf-8")
        (outer / ".filigree").mkdir()
        inner = outer / "inner-repo"
        inner.mkdir()
        (inner / ".git").mkdir()
        return inner

    def _assert_foreign_message(self, output: str) -> None:
        data = json.loads(output)
        assert data["code"] == "NOT_INITIALIZED"
        # The whole point: the rich diagnostic survives, not the old generic line.
        assert "Refusing to latch" in data["error"]
        assert "filigree init" in data["error"]
        assert data["error"] != "Project directory not initialized"

    @pytest.mark.parametrize(
        "argv",
        [
            ["list-scanners", "--json"],
            ["preview-scan", "any-scanner", "any-file.py", "--json"],
            ["trigger-scan", "any-scanner", "any-file.py", "--json"],
            ["trigger-scan-batch", "any-scanner", "any-file.py", "--json"],
            # report-finding --file used to read/validate the file before
            # resolving the project, masking ForeignDatabaseError as VALIDATION.
            ["report-finding", "--file", "missing.json", "--json"],
        ],
    )
    def test_foreign_database_message_survives(self, tmp_path: Path, argv: list[str]) -> None:
        inner = self._make_foreign_layout(tmp_path)
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(inner))
        try:
            result = runner.invoke(cli, argv)
            assert result.exit_code == 1, result.output
            self._assert_foreign_message(result.output)
        finally:
            os.chdir(original)

    def test_trigger_scan_batch_empty_filepaths_returns_validation_envelope(self, initialized_project: Path) -> None:
        """`trigger-scan-batch <scanner> --json` (no file paths) must emit a
        structured VALIDATION envelope, matching the MCP batch handler's
        contract. Click's variadic ``required=True`` previously preempted the
        in-callback empty-list guard with raw usage text on stderr.
        """
        _write_scanner_toml(initialized_project)
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["trigger-scan-batch", "test-scanner", "--json"])
            assert result.exit_code == 1, result.output
            data = json.loads(result.output)
            assert data["code"] == "VALIDATION"
            assert "non-empty" in data["error"]
        finally:
            os.chdir(original)


# ---------------------------------------------------------------------------
# TestReportFindingTypeValidation — bug fix: malformed JSON field types must
# yield ErrorCode.VALIDATION envelopes, never raw TypeError or ErrorCode.IO.
# ---------------------------------------------------------------------------


class TestReportFindingTypeValidation:
    @staticmethod
    def _invoke(initialized_project: Path, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["report-finding", "--json"], input=json.dumps(payload))
            return result.exit_code, json.loads(result.output)
        finally:
            os.chdir(original)

    def test_severity_unhashable_does_not_crash(self, initialized_project: Path) -> None:
        """`"severity": []` used to raise TypeError from `not in VALID_SEVERITIES`."""
        code, data = self._invoke(
            initialized_project,
            {"path": "foo.py", "rule_id": "r", "message": "m", "severity": []},
        )
        assert code == 1
        assert data["code"] == "VALIDATION"
        assert "severity" in str(data["error"])

    def test_path_must_be_string(self, initialized_project: Path) -> None:
        """A truthy non-string path used to slip past the falsy guard and surface as IO."""
        code, data = self._invoke(
            initialized_project,
            {"path": [1, 2], "rule_id": "r", "message": "m"},
        )
        assert code == 1
        assert data["code"] == "VALIDATION"
        assert "path" in str(data["error"])

    def test_rule_id_must_be_string(self, initialized_project: Path) -> None:
        code, data = self._invoke(
            initialized_project,
            {"path": "foo.py", "rule_id": 42, "message": "m"},
        )
        assert code == 1
        assert data["code"] == "VALIDATION"
        assert "rule_id" in str(data["error"])

    def test_message_must_be_string(self, initialized_project: Path) -> None:
        code, data = self._invoke(
            initialized_project,
            {"path": "foo.py", "rule_id": "r", "message": {"nested": "no"}},
        )
        assert code == 1
        assert data["code"] == "VALIDATION"
        assert "message" in str(data["error"])

    def test_line_start_non_int_validation(self, initialized_project: Path) -> None:
        """line_start used to reach the DB layer and surface as ErrorCode.IO."""
        code, data = self._invoke(
            initialized_project,
            {"path": "foo.py", "rule_id": "r", "message": "m", "line_start": "ten"},
        )
        assert code == 1
        assert data["code"] == "VALIDATION"
        assert "line_start" in str(data["error"])

    def test_line_end_non_int_validation(self, initialized_project: Path) -> None:
        code, data = self._invoke(
            initialized_project,
            {"path": "foo.py", "rule_id": "r", "message": "m", "line_end": 1.5},
        )
        assert code == 1
        assert data["code"] == "VALIDATION"
        assert "line_end" in str(data["error"])
