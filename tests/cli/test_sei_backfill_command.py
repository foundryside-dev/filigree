"""CLI wiring for ``filigree sei-backfill``.

The backfill logic itself is exercised against a Clarion stub in
``tests/federation/test_sei_conformance_oracle.py``. This module covers the CLI
adapter: the verb is registered, and the clean precondition refusal
(``SeiBackfillError`` → exit 1 + a VALIDATION error envelope) is wired through
``get_db`` → ``run_sei_backfill`` → output, in both human and JSON modes. A
default local-mode project has no Clarion authority, which is exactly the
refusal path.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from filigree.cli import cli


def test_sei_backfill_refuses_in_local_mode_human(cli_in_project: tuple[CliRunner, Path]) -> None:
    runner, _root = cli_in_project
    result = runner.invoke(cli, ["sei-backfill"])
    assert result.exit_code == 1
    assert "Clarion" in result.output


def test_sei_backfill_refuses_in_local_mode_json(cli_in_project: tuple[CliRunner, Path]) -> None:
    runner, _root = cli_in_project
    result = runner.invoke(cli, ["sei-backfill", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["code"] == "VALIDATION"
    assert "Clarion" in payload["error"]


def test_sei_backfill_help_lists_execute_flag(cli_in_project: tuple[CliRunner, Path]) -> None:
    runner, _root = cli_in_project
    result = runner.invoke(cli, ["sei-backfill", "--help"])
    assert result.exit_code == 0
    assert "--execute" in result.output
