"""CLI verb plumbing for the commit anchor (warpline seam, contract B).

``close``, ``claim``, and ``start-work`` accept an optional ``--commit`` option
that threads to the DB layer and persists as ``close_commit`` / ``claim_commit``.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

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
