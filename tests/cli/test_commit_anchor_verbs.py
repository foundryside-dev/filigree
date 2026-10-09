"""CLI verb plumbing for the commit anchor (warpline seam, contract B).

``close``, ``claim``, and ``start-work`` accept an optional ``--commit`` option
that threads to the DB layer and persists as ``close_commit`` / ``claim_commit``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from filigree import commit_reachability
from filigree.cli import cli
from filigree.cli_common import get_db
from tests.cli.conftest import _extract_id


def _anchors(issue_id: str) -> tuple[str | None, str | None]:
    with get_db() as db:
        row = db.conn.execute("SELECT claim_commit, close_commit FROM issues WHERE id = ?", (issue_id,)).fetchone()
    return row["claim_commit"], row["close_commit"]


class TestCommitAnchorCLI:
    def test_close_commit_option_persists(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        issue_id = _extract_id(runner.invoke(cli, ["create", "Close w/ commit"]).output)
        result = runner.invoke(cli, ["close", issue_id, "--reason", "done", "--commit", "main@abc123"])
        assert result.exit_code == 0, result.output
        _, close_commit = _anchors(issue_id)
        assert close_commit == "main@abc123"

    def test_close_without_commit_leaves_null(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        issue_id = _extract_id(runner.invoke(cli, ["create", "Close no commit"]).output)
        result = runner.invoke(cli, ["close", issue_id, "--reason", "done"])
        assert result.exit_code == 0, result.output
        _, close_commit = _anchors(issue_id)
        assert close_commit is None

    def test_claim_commit_option_persists(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        issue_id = _extract_id(runner.invoke(cli, ["create", "Claim w/ commit"]).output)
        result = runner.invoke(cli, ["claim", issue_id, "--assignee", "alice", "--commit", "main@c0ffee"])
        assert result.exit_code == 0, result.output
        claim_commit, _ = _anchors(issue_id)
        assert claim_commit == "main@c0ffee"

    def test_start_work_commit_option_persists(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        issue_id = _extract_id(runner.invoke(cli, ["create", "Start w/ commit"]).output)
        result = runner.invoke(cli, ["start-work", issue_id, "--assignee", "alice", "--commit", "main@1234abcd"])
        assert result.exit_code == 0, result.output
        claim_commit, _ = _anchors(issue_id)
        assert claim_commit == "main@1234abcd"


class TestCloseReachabilityWarningCLI:
    """Task 0.6: ``close`` surfaces the commit-reachability warning (never blocks)."""

    @staticmethod
    def _stub_unreachable(monkeypatch: pytest.MonkeyPatch) -> str:
        result = commit_reachability.ReachabilityCheck(reachable=False, sha="abc1234", ref="origin/main")
        monkeypatch.setattr(commit_reachability, "check_commit_reachable", lambda *_a, **_k: result)
        assert result.warning is not None
        return result.warning

    def test_close_json_carries_warning(self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch) -> None:
        runner, _ = cli_in_project
        warning = self._stub_unreachable(monkeypatch)
        issue_id = _extract_id(runner.invoke(cli, ["create", "Close unreachable"]).output)
        result = runner.invoke(cli, ["close", issue_id, "--commit", "side@abc1234", "--json"])
        assert result.exit_code == 0, result.output
        [item] = json.loads(result.output)["succeeded"]
        assert item["warnings"] == [warning]
        assert item["status"] == "closed"

    def test_close_text_prints_warning_to_stderr(self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch) -> None:
        runner, _ = cli_in_project
        warning = self._stub_unreachable(monkeypatch)
        issue_id = _extract_id(runner.invoke(cli, ["create", "Close unreachable"]).output)
        result = runner.invoke(cli, ["close", issue_id, "--commit", "side@abc1234"])
        assert result.exit_code == 0, result.output
        assert f"Warning: {warning}" in result.stderr

    def test_close_json_omits_warnings_when_none(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        issue_id = _extract_id(runner.invoke(cli, ["create", "Close plain"]).output)
        result = runner.invoke(cli, ["close", issue_id, "--json"])
        assert result.exit_code == 0, result.output
        [item] = json.loads(result.output)["succeeded"]
        assert "warnings" not in item


class TestIntegrationRefConfig:
    """Task 0.6: ``integration_ref`` is settable like ``population`` (Task 0.1)."""

    def test_config_set_integration_ref(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        from filigree.core import find_filigree_anchor, read_integration_ref

        runner, project = cli_in_project
        result = runner.invoke(cli, ["config", "set", "integration_ref", "release/3.3.0"])
        assert result.exit_code == 0, result.output
        assert read_integration_ref(find_filigree_anchor(project).store_dir) == "release/3.3.0"

    def test_config_set_rejects_unsafe_integration_ref(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        from filigree.core import find_filigree_anchor, read_integration_ref

        runner, project = cli_in_project
        result = runner.invoke(cli, ["config", "set", "integration_ref", "a..b"])
        assert result.exit_code != 0
        assert read_integration_ref(find_filigree_anchor(project).store_dir) == "main"
