"""CLI tests for the Legis closure-gate (B5).

The ``close`` command must consult the same gate as HTTP/MCP. The Legis
client is faked via ``filigree.governance.check_closure_gate``; an issue is
made governed by attaching a signed entity-association directly on the DB.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from filigree import governance, legis_client
from filigree.cli import cli
from filigree.core import FILIGREE_DIR_NAME, FiligreeDB
from filigree.legis_client import LegisGateResult, LegisGateStatus
from tests.cli.conftest import _extract_id


def _make_governed(project_root: Path, issue_id: str) -> None:
    db = FiligreeDB.from_filigree_dir(project_root / FILIGREE_DIR_NAME)
    db.add_entity_association(issue_id, "sei:gov", content_hash="h", actor="legis", signature="sig", signoff_seq=1)
    db.close()


def _patch_gate(monkeypatch: pytest.MonkeyPatch, result: LegisGateResult) -> list[str]:
    monkeypatch.setenv(legis_client.LEGIS_URL_ENV, "http://legis.test")
    calls: list[str] = []

    def _fake(issue_id: str) -> LegisGateResult:
        calls.append(issue_id)
        return result

    monkeypatch.setattr(governance, "check_closure_gate", _fake)
    return calls


def test_cli_close_governed_blocked(cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    runner, project = cli_in_project
    issue_id = _extract_id(runner.invoke(cli, ["create", "Governed"]).output)
    _make_governed(project, issue_id)
    _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.BLOCKED, reason="no verified binding"))
    result = runner.invoke(cli, ["close", issue_id])
    assert result.exit_code == 1
    assert "no verified binding" in result.output


def test_cli_close_governed_allowed(cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    runner, project = cli_in_project
    issue_id = _extract_id(runner.invoke(cli, ["create", "Governed"]).output)
    _make_governed(project, issue_id)
    _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.ALLOWED))
    result = runner.invoke(cli, ["close", issue_id])
    assert result.exit_code == 0
    assert "Closed" in result.output


def test_cli_close_ungoverned_does_not_call_gate(cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    runner, _ = cli_in_project
    issue_id = _extract_id(runner.invoke(cli, ["create", "Ungoverned"]).output)
    calls = _patch_gate(monkeypatch, LegisGateResult(LegisGateStatus.BLOCKED))
    result = runner.invoke(cli, ["close", issue_id])
    assert result.exit_code == 0
    assert calls == []
