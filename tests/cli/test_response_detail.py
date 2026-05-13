"""CLI-side tests for ``--detail`` opt-in on batch commands.

Closes the gap where the agent guidance promised ``--detail=full`` on
every batch CLI command but the flag was missing. The matching MCP side
lives in ``tests/mcp/test_response_detail.py``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from filigree.cli import cli
from filigree.cli_common import get_db
from tests._seeds import seed_file, seed_finding, seed_observations

_SLIM_KEYS = {"issue_id", "title", "status", "priority", "type"}
_FULL_ONLY_KEYS = {"description", "labels", "blocks", "blocked_by", "is_ready", "fields"}


def _create_issue(runner: CliRunner) -> str:
    result = runner.invoke(cli, ["create", "T", "--type=task"])
    assert result.exit_code == 0, result.output
    return result.output.split(":", 1)[0].replace("Created ", "").strip()


# ---------------------------------------------------------------------------
# batch-update / batch-close
# ---------------------------------------------------------------------------


class TestCliIssueBatchDetail:
    def test_batch_update_slim_default_keys(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        issue_id = _create_issue(runner)
        result = runner.invoke(cli, ["batch-update", issue_id, "--priority", "0", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert set(data["succeeded"][0].keys()) == _SLIM_KEYS

    def test_batch_update_full_carries_extra_keys(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        issue_id = _create_issue(runner)
        result = runner.invoke(
            cli,
            ["batch-update", issue_id, "--priority", "0", "--detail", "full", "--json"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert set(data["succeeded"][0].keys()) >= _FULL_ONLY_KEYS

    def test_batch_close_slim_default_keys(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        issue_id = _create_issue(runner)
        result = runner.invoke(cli, ["batch-close", issue_id, "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert set(data["succeeded"][0].keys()) == _SLIM_KEYS

    def test_batch_close_full_carries_extra_keys(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        issue_id = _create_issue(runner)
        result = runner.invoke(cli, ["batch-close", issue_id, "--detail", "full", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert set(data["succeeded"][0].keys()) >= _FULL_ONLY_KEYS


# ---------------------------------------------------------------------------
# batch-add-label / batch-add-comment
# ---------------------------------------------------------------------------


class TestCliMetaBatchDetail:
    def test_batch_add_label_slim_default_emits_id_strings(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        issue_id = _create_issue(runner)
        result = runner.invoke(cli, ["batch-add-label", "x", issue_id, "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["succeeded"] == [issue_id]

    def test_batch_add_label_full_emits_full_records(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        issue_id = _create_issue(runner)
        result = runner.invoke(cli, ["batch-add-label", "x", issue_id, "--detail", "full", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        item = data["succeeded"][0]
        assert isinstance(item, dict)
        assert set(item.keys()) >= _FULL_ONLY_KEYS
        assert "x" in item["labels"]

    def test_batch_remove_label_slim_default_emits_id_strings(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        issue_id = _create_issue(runner)
        runner.invoke(cli, ["add-label", "x", issue_id])
        result = runner.invoke(cli, ["batch-remove-label", "x", issue_id, "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["succeeded"] == [issue_id]

    def test_batch_remove_label_full_emits_full_records(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        issue_id = _create_issue(runner)
        runner.invoke(cli, ["add-label", "x", issue_id])
        result = runner.invoke(cli, ["batch-remove-label", "x", issue_id, "--detail", "full", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        item = data["succeeded"][0]
        assert isinstance(item, dict)
        assert set(item.keys()) >= _FULL_ONLY_KEYS
        assert "x" not in item["labels"]

    def test_batch_add_comment_slim_default_emits_id_strings(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        issue_id = _create_issue(runner)
        result = runner.invoke(cli, ["batch-add-comment", "hi", issue_id, "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["succeeded"] == [issue_id]

    def test_batch_add_comment_full_emits_full_records(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        issue_id = _create_issue(runner)
        result = runner.invoke(cli, ["batch-add-comment", "hi", issue_id, "--detail", "full", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        item = data["succeeded"][0]
        assert isinstance(item, dict)
        assert set(item.keys()) >= _FULL_ONLY_KEYS


# ---------------------------------------------------------------------------
# batch-update-findings / batch-dismiss-observations
# ---------------------------------------------------------------------------


class TestCliFindingsAndObservationsBatchDetail:
    def test_batch_update_findings_slim_default(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, project = cli_in_project
        original = os.getcwd()
        os.chdir(str(project))
        try:
            with get_db() as db:
                file_id = seed_file(db, path="src/cli_findings.py")
                finding_id = seed_finding(db, file_id=file_id)
        finally:
            os.chdir(original)
        result = runner.invoke(
            cli,
            ["batch-update-findings", finding_id, "--status", "fixed", "--json"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["succeeded"] == [finding_id]

    def test_batch_update_findings_full(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, project = cli_in_project
        original = os.getcwd()
        os.chdir(str(project))
        try:
            with get_db() as db:
                file_id = seed_file(db, path="src/cli_findings_full.py")
                finding_id = seed_finding(db, file_id=file_id)
        finally:
            os.chdir(original)
        result = runner.invoke(
            cli,
            [
                "batch-update-findings",
                finding_id,
                "--status",
                "fixed",
                "--detail",
                "full",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        item = data["succeeded"][0]
        assert isinstance(item, dict)
        assert "finding_id" in item
        assert "id" not in item
        assert item["status"] == "fixed"

    def test_batch_dismiss_observations_slim_default(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, project = cli_in_project
        original = os.getcwd()
        os.chdir(str(project))
        try:
            with get_db() as db:
                obs_ids = seed_observations(db, count=2)
        finally:
            os.chdir(original)
        result = runner.invoke(cli, ["batch-dismiss-observations", *obs_ids, "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert set(data["succeeded"]) == set(obs_ids)

    def test_batch_dismiss_observations_full(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, project = cli_in_project
        original = os.getcwd()
        os.chdir(str(project))
        try:
            with get_db() as db:
                obs_ids = seed_observations(db, count=2)
        finally:
            os.chdir(original)
        result = runner.invoke(
            cli,
            ["batch-dismiss-observations", *obs_ids, "--detail", "full", "--json"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert len(data["succeeded"]) == 2
        for item in data["succeeded"]:
            assert isinstance(item, dict)
            assert {"observation_id", "summary"} <= set(item.keys())
            assert "id" not in item

    def test_batch_promote_observations_slim_default(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, project = cli_in_project
        original = os.getcwd()
        os.chdir(str(project))
        try:
            with get_db() as db:
                obs_ids = seed_observations(db, count=2)
        finally:
            os.chdir(original)
        result = runner.invoke(cli, ["batch-promote-observations", *obs_ids, "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert len(data["succeeded"]) == 2
        for item in data["succeeded"]:
            assert set(item.keys()) == _SLIM_KEYS

    def test_batch_promote_observations_full(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, project = cli_in_project
        original = os.getcwd()
        os.chdir(str(project))
        try:
            with get_db() as db:
                obs_ids = seed_observations(db, count=2)
        finally:
            os.chdir(original)
        result = runner.invoke(
            cli,
            ["batch-promote-observations", *obs_ids, "--detail", "full", "--json"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert len(data["succeeded"]) == 2
        for item in data["succeeded"]:
            assert isinstance(item, dict)
            assert set(item.keys()) >= _FULL_ONLY_KEYS


# ---------------------------------------------------------------------------
# Bad value
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        ("batch-update", "--priority", "0"),
        ("batch-close",),
        ("batch-add-label", "x"),
        ("batch-add-comment", "hi"),
    ],
)
def test_invalid_detail_value_is_usage_error(
    cli_in_project: tuple[CliRunner, Path],
    command: tuple[str, ...],
) -> None:
    """click.Choice rejects unknown values with exit 2 — matches MCP's VALIDATION code."""
    runner, _ = cli_in_project
    issue_id = _create_issue(runner)
    args = [command[0], issue_id, *command[1:], "--detail", "medium", "--json"]
    # batch-add-label and batch-add-comment take the value/text positionally
    # before issue_ids — re-shuffle for those.
    if command[0] in {"batch-add-label", "batch-add-comment"}:
        args = [command[0], command[1], issue_id, "--detail", "medium", "--json"]
    result = runner.invoke(cli, args)
    assert result.exit_code == 2
    assert "medium" in result.output or "Invalid value" in result.output
