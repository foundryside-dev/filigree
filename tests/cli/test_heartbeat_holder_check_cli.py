"""CLI ``heartbeat-work`` holder check (final review I5): ``--override`` is the only bypass."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from filigree.cli import cli
from filigree.cli_common import get_db
from tests.core.test_heartbeat_holder_check import _events


class TestCli:
    def _claim(self, runner: CliRunner) -> str:
        with get_db() as db:
            issue = db.create_issue("Held CLI", type="task")
            db.claim_issue(issue.id, assignee="alice", actor="alice")
            return issue.id

    def test_non_holder_naming_the_holder_conflicts(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        issue_id = self._claim(runner)

        result = runner.invoke(cli, ["--actor", "bob", "heartbeat-work", issue_id, "--expected-assignee", "alice", "--json"])

        assert result.exit_code == 1
        assert json.loads(result.stdout)["code"] == "CONFLICT"

    def test_override_records_override_event(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        issue_id = self._claim(runner)

        result = runner.invoke(cli, ["--actor", "coordinator", "heartbeat-work", issue_id, "--override", "--json"])

        assert result.exit_code == 0, result.output
        with get_db() as db:
            assert _events(db, issue_id) == [("heartbeat_by_override", "coordinator")]
