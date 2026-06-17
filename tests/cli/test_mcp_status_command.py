"""CLI tests for the ``mcp-status`` command.

``mcp-status`` is the CLI counterpart of the MCP ``get_mcp_status`` tool. Both
surfaces flow through ``filigree.mcp_server.get_mcp_status_payload``; these
tests assert the CLI emits the same payload shape so the two surfaces cannot
silently diverge.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from pathlib import Path

import pytest
from click.testing import CliRunner

from filigree.cli import cli

# Keys the MCP ``get_mcp_status`` payload guarantees in its healthy (``ok``)
# branch. The CLI ``--json`` output must carry exactly these.
EXPECTED_KEYS = {
    "status",
    "db_initialized",
    "schema_compatible",
    "installed_schema_version",
    "database_schema_version",
    "code",
    "error",
    "guidance",
    "project_root",
    "filigree_dir",
    "runtime",
    "actor_verification",
}


@pytest.fixture(autouse=True)
def _restore_mcp_globals() -> Generator[None, None, None]:
    """Snapshot/restore mcp_server module globals.

    The command calls ``_attempt_startup`` which mutates module-level globals
    (``db``, ``_filigree_dir``, the degraded-mode sentinels). Under in-process
    ``CliRunner`` invocation that state would leak into sibling tests; restore
    it after each test.
    """
    from filigree import mcp_server

    names = ("db", "_filigree_dir", "_project_root", "_schema_mismatch", "_registry_startup_error", "_db_open_error")
    saved = {name: getattr(mcp_server, name) for name in names}
    try:
        yield
    finally:
        # The command opens a DB onto ``mcp_server.db`` (process-lifetime in
        # production; closed when the CLI process exits). Under in-process
        # CliRunner the handle would be dropped unclosed when we restore the
        # globals below, leaking the connection — finalized mid-session it trips
        # the suite's ``error::ResourceWarning`` filter. Close it first.
        opened = getattr(mcp_server, "db", None)
        if opened is not None and opened is not saved["db"]:
            opened.close()
        for name, value in saved.items():
            setattr(mcp_server, name, value)


class TestMcpStatusCommand:
    def test_human_output_runs(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["mcp-status"])
        assert result.exit_code == 0
        assert "Status: ok" in result.output
        assert "Schema compatible: True" in result.output
        assert "Runtime:" in result.output

    def test_json_output_has_expected_keys(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, project = cli_in_project
        result = runner.invoke(cli, ["mcp-status", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert set(data.keys()) == EXPECTED_KEYS
        assert data["status"] == "ok"
        assert data["db_initialized"] is True
        assert data["schema_compatible"] is True
        assert data["project_root"] == str(project)
        # runtime diagnostics block carries install-context introspection
        assert "install_context" in data["runtime"]
        assert "python_executable" in data["runtime"]

    def test_human_output_names_project_root_and_store(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, project = cli_in_project
        result = runner.invoke(cli, ["mcp-status"])
        assert result.exit_code == 0
        assert f"Project root: {project}" in result.output
        assert "Store dir:" in result.output

    def test_cli_json_matches_mcp_payload(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """The CLI --json output must equal what the MCP tool emits.

        Compares against the actual MCP handler (_handle_get_mcp_status), not
        just get_mcp_status_payload(), so the test stays honest even if the
        handler ever stops being a pure pass-through. Guards the CLI wrapper
        against dropping, renaming, or re-serializing keys.
        """
        import asyncio

        runner, _ = cli_in_project
        result = runner.invoke(cli, ["mcp-status", "--json"])
        assert result.exit_code == 0
        cli_payload = json.loads(result.output)

        # Invoke the real MCP tool handler the same way the MCP server does and
        # parse its serialized TextContent back to a dict.
        from filigree.mcp_tools.workflow import _handle_get_mcp_status

        mcp_text = asyncio.run(_handle_get_mcp_status({}))[0].text
        mcp_payload = json.loads(mcp_text)
        assert cli_payload == mcp_payload
