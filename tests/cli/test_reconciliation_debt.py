"""CLI tests for the reconciliation-debt list surface (B2 / Design A, Task 4).

Reconciliation debt is recorded when a governed finding→issue auto-close is
deferred by the Legis gate. This verb makes that debt actionable.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from filigree.cli import cli
from filigree.core import FiligreeDB
from filigree.finding_issue_cascade import record_reconciliation_debt_comment
from tests.cli.conftest import _extract_id


def _record_debt(project: Path, issue_id: str, text: str) -> None:
    db = FiligreeDB.from_project(project)
    record_reconciliation_debt_comment(db.conn, issue_id, text)
    db.close()


def test_reconciliation_debt_lists_issue_with_debt(cli_in_project: tuple[CliRunner, Path]) -> None:
    runner, project = cli_in_project
    issue_id = _extract_id(runner.invoke(cli, ["create", "Blocked"]).output)
    _record_debt(project, issue_id, "blocked by Legis")

    result = runner.invoke(cli, ["reconciliation-debt", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    ids = {item["issue_id"] for item in payload["items"]}
    assert issue_id in ids
    row = next(item for item in payload["items"] if item["issue_id"] == issue_id)
    assert row["debt_count"] == 1


def test_reconciliation_debt_empty(cli_in_project: tuple[CliRunner, Path]) -> None:
    runner, _ = cli_in_project
    result = runner.invoke(cli, ["reconciliation-debt"])
    assert result.exit_code == 0, result.output
    assert "No reconciliation debt" in result.output
