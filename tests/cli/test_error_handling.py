"""CLI error handling regression tests.

Covers: filigree-8a7e6a (reopen exit code), filigree-537425 (create --json field error),
filigree-25daf4e886 (remove-label ValueError), filigree-565ff86495 (remove-dep WrongProjectError),
filigree-62c5b61f68 (refresh_summary OSError), filigree-3c4196854b (get_db corrupt conf),
2a.11 ErrorCode alignment (--json errors emit uppercase code values).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from filigree.cli import cli
from tests.cli.conftest import _extract_id


class TestReopenExitCode:
    """CLI reopen must exit non-zero on error."""

    def test_reopen_nonexistent_exits_nonzero(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["reopen", "test-nonexistent"])
        assert result.exit_code != 0, f"Expected non-zero exit code, got {result.exit_code}"
        assert "Not found" in result.output

    def test_reopen_nonexistent_json_exits_nonzero(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["reopen", "test-nonexistent", "--json"])
        assert result.exit_code != 0
        data = json.loads(result.output)
        assert "failed" in data
        assert len(data["failed"]) == 1

    def test_reopen_open_issue_exits_nonzero(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Reopening an already-open issue should fail (ValueError path)."""
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Not closed"])
        issue_id = _extract_id(r.output)
        result = runner.invoke(cli, ["reopen", issue_id])
        assert result.exit_code != 0


class TestCreateJsonFieldError:
    """create --json must emit JSON errors for bad field format."""

    def test_create_bad_field_json_output(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["create", "Bad field", "-f", "no_equals_sign", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert "error" in data
        assert "Invalid field format" in data["error"]

    def test_create_bad_field_text_output(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Without --json, error goes to stderr as text."""
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["create", "Bad field", "-f", "no_equals_sign"])
        assert result.exit_code == 1
        assert "Invalid field format" in result.output


class TestRemoveLabelValueError:
    """filigree-25daf4e886: remove-label must handle ValueError from label validation."""

    def test_reserved_namespace_text(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Target"])
        issue_id = _extract_id(r.output)
        result = runner.invoke(cli, ["remove-label", issue_id, "area:foo"])
        assert result.exit_code == 1
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "area:" in result.output

    def test_reserved_namespace_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Target"])
        issue_id = _extract_id(r.output)
        result = runner.invoke(cli, ["remove-label", issue_id, "area:foo", "--json"])
        assert result.exit_code == 1
        assert result.exception is None or isinstance(result.exception, SystemExit)
        data = json.loads(result.output)
        assert "error" in data


class TestRemoveDepWrongProjectError:
    """filigree-565ff86495: remove-dep must handle WrongProjectError (ValueError)."""

    def test_foreign_prefix_text(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Target"])
        issue_id = _extract_id(r.output)
        # Foreign prefix triggers WrongProjectError from _check_id_prefix
        result = runner.invoke(cli, ["remove-dep", issue_id, "foreign-abc1234567"])
        assert result.exit_code == 1
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "wrong project" in result.output.lower() or "foreign" in result.output.lower() or "error" in result.output.lower()

    def test_foreign_prefix_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Target"])
        issue_id = _extract_id(r.output)
        result = runner.invoke(cli, ["remove-dep", issue_id, "foreign-abc1234567", "--json"])
        assert result.exit_code == 1
        assert result.exception is None or isinstance(result.exception, SystemExit)
        data = json.loads(result.output)
        assert "error" in data

    def test_same_prefix_missing_source_json_is_not_found(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Target"])
        issue_id = _extract_id(r.output)
        result = runner.invoke(cli, ["remove-dep", "test-0000000000", issue_id, "--json"])
        assert result.exit_code == 1
        assert result.exception is None or isinstance(result.exception, SystemExit)
        data = json.loads(result.output)
        assert data["code"] == "NOT_FOUND", data


class TestMetaWrongProjectError:
    """filigree-f8861115a9: add-comment / add-label / remove-label must surface
    WrongProjectError as VALIDATION, not NOT_FOUND. The read-side precheck
    (db.get_issue) intentionally ignores prefix, so foreign-prefix IDs were
    being misreported as missing. Same-project missing IDs must still emit
    NOT_FOUND.
    """

    def test_add_comment_foreign_prefix_json_is_validation(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["add-comment", "foreign-abc1234567", "hello", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "VALIDATION", data
        assert "project" in data["error"].lower()

    def test_add_comment_same_prefix_missing_still_not_found(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["add-comment", "test-0000000000", "hello", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "NOT_FOUND", data

    def test_add_label_foreign_prefix_json_is_validation(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["add-label", "needs-review", "foreign-abc1234567", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "VALIDATION", data
        assert "project" in data["error"].lower()

    def test_add_label_same_prefix_missing_still_not_found(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["add-label", "needs-review", "test-0000000000", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "NOT_FOUND", data

    def test_remove_label_foreign_prefix_json_is_validation(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["remove-label", "foreign-abc1234567", "needs-review", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "VALIDATION", data
        assert "project" in data["error"].lower()

    def test_remove_label_same_prefix_missing_still_not_found(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["remove-label", "test-0000000000", "needs-review", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "NOT_FOUND", data


class TestRefreshSummaryOSError:
    """filigree-62c5b61f68: refresh_summary must not fail successful mutations on OSError."""

    def test_oserror_does_not_fail_successful_create(
        self,
        cli_in_project: tuple[CliRunner, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A disk/permission error during summary write must not fail the CLI."""
        runner, _ = cli_in_project

        def boom(db: object, path: object) -> None:
            raise OSError("simulated disk full")

        monkeypatch.setattr("filigree.cli_common.write_summary", boom)
        result = runner.invoke(cli, ["create", "After-failure create", "--json"])
        assert result.exit_code == 0, f"Expected success, got {result.exit_code}: {result.output}"
        # The mutation result JSON must be on stdout — parse without stripping tracebacks
        data = json.loads(result.output)
        assert "issue_id" in data
        assert "id" not in data


class TestGetDbCorruptConf:
    """filigree-3c4196854b: get_db() must surface corrupt conf as a clean error."""

    def test_corrupt_conf_produces_clean_error(self, tmp_path: Path, cli_runner: CliRunner) -> None:
        """A .filigree.conf that is not a JSON object must not crash raw."""
        original_cwd = os.getcwd()
        os.chdir(str(tmp_path))
        try:
            (tmp_path / ".filigree.conf").write_text('"not an object"')
            result = cli_runner.invoke(cli, ["stats"])
            assert result.exit_code != 0
            # Must be a clean click error, not an unhandled ValueError traceback
            assert result.exception is None or isinstance(result.exception, SystemExit), (
                f"Unhandled exception leaked: {type(result.exception).__name__}: {result.exception}"
            )
            assert "must be a JSON object" in result.output or "Error" in result.output
        finally:
            os.chdir(original_cwd)

    def test_conf_missing_keys_produces_clean_error(self, tmp_path: Path, cli_runner: CliRunner) -> None:
        """A .filigree.conf missing required keys must not crash raw."""
        original_cwd = os.getcwd()
        os.chdir(str(tmp_path))
        try:
            (tmp_path / ".filigree.conf").write_text("{}")
            result = cli_runner.invoke(cli, ["stats"])
            assert result.exit_code != 0
            assert result.exception is None or isinstance(result.exception, SystemExit), (
                f"Unhandled exception leaked: {type(result.exception).__name__}: {result.exception}"
            )
        finally:
            os.chdir(original_cwd)


class TestErrorCodeAlignment:
    """2a.11: --json error output must include 'code' with uppercase ErrorCode values."""

    def test_show_nonexistent_json_has_code(self, initialized_project: Path) -> None:
        """show <nonexistent> --json must emit code=NOT_FOUND."""
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            # Use correct prefix (test-) but nonexistent suffix to reach KeyError path
            result = runner.invoke(cli, ["show", "test-0000000000", "--json"])
        finally:
            os.chdir(original)
        assert result.exit_code != 0
        payload = json.loads(result.output)
        assert payload["code"] == "NOT_FOUND"
        assert isinstance(payload["error"], str)

    def test_update_nonexistent_json_has_code(self, initialized_project: Path) -> None:
        """update <nonexistent> --json must emit code=NOT_FOUND."""
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["update", "test-0000000000", "--status", "open", "--json"])
        finally:
            os.chdir(original)
        assert result.exit_code != 0
        payload = json.loads(result.output)
        assert payload["code"] == "NOT_FOUND"

    def test_create_bad_field_json_has_code(self, initialized_project: Path) -> None:
        """create with invalid --field format --json must emit code=VALIDATION."""
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["create", "Title", "-f", "no_equals_sign", "--json"])
        finally:
            os.chdir(original)
        assert result.exit_code != 0
        payload = json.loads(result.output)
        assert payload["code"] == "VALIDATION"

    def test_add_comment_nonexistent_json_has_code(self, initialized_project: Path) -> None:
        """add-comment <nonexistent> --json must emit code=NOT_FOUND."""
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["add-comment", "test-0000000000", "hello", "--json"])
        finally:
            os.chdir(original)
        assert result.exit_code != 0
        payload = json.loads(result.output)
        assert payload["code"] == "NOT_FOUND"

    def test_claim_nonexistent_json_has_code(self, initialized_project: Path) -> None:
        """claim <nonexistent> --json must emit code=NOT_FOUND."""
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project))
        try:
            result = runner.invoke(cli, ["claim", "test-0000000000", "--assignee", "agent1", "--json"])
        finally:
            os.chdir(original)
        assert result.exit_code != 0
        payload = json.loads(result.output)
        assert payload["code"] == "NOT_FOUND"
