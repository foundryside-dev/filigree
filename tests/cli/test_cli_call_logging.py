"""Call-outcome logging on the CLI surface (Task 0.1)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from filigree.cli import cli
from tests.conftest import JsonLogCapture


@pytest.fixture
def project_cwd(initialized_project: Path) -> Path:
    os.chdir(initialized_project)
    return initialized_project


def test_cli_command_logged_with_exit_code(cli_runner: CliRunner, project_cwd: Path, caplog_json: JsonLogCapture) -> None:
    cli_runner.invoke(cli, ["show", "nope", "--json"])
    rec = caplog_json.last(event="call", surface="cli")
    assert rec["name"] == "show"
    assert rec["outcome"] == "error"
    assert rec["code"] == "NOT_FOUND"


def test_cli_success_logged_ok(cli_runner: CliRunner, project_cwd: Path, caplog_json: JsonLogCapture) -> None:
    result = cli_runner.invoke(cli, ["ready", "--json"])
    assert result.exit_code == 0
    rec = caplog_json.last(event="call", surface="cli")
    assert rec["name"] == "ready"
    assert rec["outcome"] == "ok"
    assert rec["code"] is None
    assert isinstance(rec["duration_ms"], float)


def test_cli_usage_error_logged_as_validation(cli_runner: CliRunner, project_cwd: Path, caplog_json: JsonLogCapture) -> None:
    cli_runner.invoke(cli, ["show", "--json"])  # missing required ISSUE_ID
    rec = caplog_json.last(event="call", surface="cli")
    assert rec["name"] == "show"
    assert rec["outcome"] == "validation"
    assert rec["code"] == "VALIDATION"


def test_cli_nested_group_command_path(cli_runner: CliRunner, project_cwd: Path, caplog_json: JsonLogCapture) -> None:
    cli_runner.invoke(cli, ["finding", "clean-stale", "--help"])
    rec = caplog_json.last(event="call", surface="cli")
    assert rec["name"] == "finding clean-stale"


def test_cli_writes_call_record_to_project_log(cli_runner: CliRunner, project_cwd: Path) -> None:
    import json

    from filigree.core import find_filigree_anchor

    cli_runner.invoke(cli, ["show", "nope", "--json"])
    log_path = find_filigree_anchor(project_cwd).store_dir / "filigree.log"
    records = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    assert any(r.get("event") == "call" and r.get("surface") == "cli" and r.get("name") == "show" for r in records)


def test_cli_population_recorded(cli_runner: CliRunner, project_cwd: Path, caplog_json: JsonLogCapture) -> None:
    cli_runner.invoke(cli, ["ready", "--json"])
    assert caplog_json.last(event="call", surface="cli")["population"] == "product-use"
