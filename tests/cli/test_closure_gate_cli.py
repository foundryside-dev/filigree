"""CLI tests for the Legis closure-gate (B5).

The ``close`` command must consult the same gate as HTTP/MCP. Legis is retired
(M-7): a governed close with fresh bindings proceeds and records a
``governance_warning`` event; ``governance_on`` makes any Legis network use fail
the test. A drifted sign-off still fails closed as STALE. An issue is made
governed by attaching a signed entity-association directly on the DB.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from filigree.cli import cli
from filigree.core import FiligreeDB
from tests._fakes.legis_retired import ARCHIVED_WARNING, governance_on
from tests.cli.conftest import _extract_id


def _make_governed(project_root: Path, issue_id: str) -> None:
    db = FiligreeDB.from_project(project_root)
    db.add_entity_association(issue_id, "sei:gov", content_hash="h", actor="legis", signature="sig", signoff_seq=1)
    db.close()


def _make_stale(project_root: Path, issue_id: str) -> None:
    """Drift the sign-off: a signatureless re-attach advances content past the signed snapshot."""
    db = FiligreeDB.from_project(project_root)
    db.add_entity_association(issue_id, "sei:gov", content_hash="h-drifted", actor="agent")
    db.close()


def _warning_events(project_root: Path, issue_id: str) -> list[str]:
    db = FiligreeDB.from_project(project_root)
    try:
        return [e["new_value"] or "" for e in db.get_issue_events(issue_id) if e["event_type"] == "governance_warning"]
    finally:
        db.close()


def test_cli_close_governed_stale(cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    runner, project = cli_in_project
    issue_id = _extract_id(runner.invoke(cli, ["create", "Governed"]).output)
    _make_governed(project, issue_id)
    _make_stale(project, issue_id)
    governance_on(monkeypatch)
    result = runner.invoke(cli, ["close", issue_id])
    assert result.exit_code == 1
    assert "drifted" in result.output


def test_cli_close_governed_proceeds_with_warning_event(cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    runner, project = cli_in_project
    issue_id = _extract_id(runner.invoke(cli, ["create", "Governed"]).output)
    _make_governed(project, issue_id)
    governance_on(monkeypatch)
    result = runner.invoke(cli, ["close", issue_id])
    assert result.exit_code == 0
    assert "Closed" in result.output
    assert _warning_events(project, issue_id) == [ARCHIVED_WARNING]


def test_cli_close_json_carries_archived_warning(cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    # Task 0.6: the archived-provider warning rides on ``close --json`` items.
    runner, project = cli_in_project
    issue_id = _extract_id(runner.invoke(cli, ["create", "Governed"]).output)
    _make_governed(project, issue_id)
    governance_on(monkeypatch)
    result = runner.invoke(cli, ["close", issue_id, "--json"])
    assert result.exit_code == 0, result.output
    [item] = json.loads(result.output)["succeeded"]
    assert item["warnings"] == [ARCHIVED_WARNING]


def test_cli_close_ungoverned_does_not_call_gate(cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    runner, project = cli_in_project
    issue_id = _extract_id(runner.invoke(cli, ["create", "Ungoverned"]).output)
    governance_on(monkeypatch)
    result = runner.invoke(cli, ["close", issue_id])
    assert result.exit_code == 0
    assert _warning_events(project, issue_id) == []


# --- C1: the `update` command can also close (open→closed) and must gate -----


def _status_of(project: Path, issue_id: str) -> str:
    db = FiligreeDB.from_project(project)
    status = db.get_issue(issue_id).status
    db.close()
    return status


def test_cli_update_to_done_governed_stale(cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    runner, project = cli_in_project
    issue_id = _extract_id(runner.invoke(cli, ["create", "Governed"]).output)
    _make_governed(project, issue_id)
    _make_stale(project, issue_id)
    governance_on(monkeypatch)
    result = runner.invoke(cli, ["update", issue_id, "--status", "closed"])
    assert result.exit_code == 1
    assert "drifted" in result.output
    assert _status_of(project, issue_id) != "closed"


def test_cli_update_to_done_governed_proceeds_with_warning_event(
    cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    runner, project = cli_in_project
    issue_id = _extract_id(runner.invoke(cli, ["create", "Governed"]).output)
    _make_governed(project, issue_id)
    governance_on(monkeypatch)
    result = runner.invoke(cli, ["update", issue_id, "--status", "closed"])
    assert result.exit_code == 0, result.output
    assert _status_of(project, issue_id) == "closed"
    assert _warning_events(project, issue_id) == [ARCHIVED_WARNING]


def test_cli_update_to_non_done_does_not_call_gate(cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    runner, project = cli_in_project
    issue_id = _extract_id(runner.invoke(cli, ["create", "Governed"]).output)
    _make_governed(project, issue_id)
    governance_on(monkeypatch)
    result = runner.invoke(cli, ["update", issue_id, "--status", "in_progress"])
    assert result.exit_code == 0, result.output
    assert _warning_events(project, issue_id) == []  # non-closing status change is never gated


def test_cli_update_to_done_ungoverned_does_not_call_gate(cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    runner, project = cli_in_project
    issue_id = _extract_id(runner.invoke(cli, ["create", "Ungoverned"]).output)
    governance_on(monkeypatch)
    result = runner.invoke(cli, ["update", issue_id, "--status", "closed"])
    assert result.exit_code == 0, result.output
    assert _warning_events(project, issue_id) == []
