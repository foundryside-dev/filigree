"""CLI tests for issue CRUD commands (create, show, update, close, reopen, claim, comments, labels)."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from filigree.cli import cli
from filigree.core import DB_FILENAME, FILIGREE_DIR_NAME, read_config
from tests.cli.conftest import _extract_id


class TestInit:
    def test_init_creates_filigree_dir(self, tmp_path: Path, cli_runner: CliRunner) -> None:
        original = os.getcwd()
        os.chdir(str(tmp_path))
        try:
            result = cli_runner.invoke(cli, ["init"])
            assert result.exit_code == 0
            assert (tmp_path / FILIGREE_DIR_NAME).is_dir()
            assert (tmp_path / FILIGREE_DIR_NAME / DB_FILENAME).exists()
        finally:
            os.chdir(original)

    def test_init_with_prefix(self, tmp_path: Path, cli_runner: CliRunner) -> None:
        original = os.getcwd()
        os.chdir(str(tmp_path))
        try:
            result = cli_runner.invoke(cli, ["init", "--prefix", "myproj"])
            assert result.exit_code == 0
            config = read_config(tmp_path / FILIGREE_DIR_NAME)
            assert config["prefix"] == "myproj"
        finally:
            os.chdir(original)

    def test_init_already_exists(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["init"])
        assert result.exit_code == 0
        assert "already exists" in result.output


class TestCreate:
    def test_create_basic(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["create", "Fix the bug"])
        assert result.exit_code == 0
        assert "Created" in result.output
        assert "Fix the bug" in result.output

    def test_create_with_options(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(
            cli,
            [
                "create",
                "New feature",
                "--type",
                "feature",
                "-p",
                "1",
                "-d",
                "A description",
                "--notes",
                "Some notes",
                "-l",
                "backend",
                "-l",
                "urgent",
            ],
        )
        assert result.exit_code == 0
        assert "Created" in result.output

    def test_create_with_field(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["create", "With field", "-f", "severity=major"])
        assert result.exit_code == 0

    def test_create_invalid_field(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["create", "Bad field", "-f", "no_equals_sign"])
        assert result.exit_code == 1


class TestCliPublicIssueVocabulary:
    def test_create_json_uses_issue_id(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["create", "Public create", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["issue_id"]
        assert "id" not in data

    def test_show_json_uses_issue_id(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        created = runner.invoke(cli, ["create", "Public show"])
        issue_id = _extract_id(created.output)

        result = runner.invoke(cli, ["show", issue_id, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["issue_id"] == issue_id
        assert "id" not in data

    def test_list_json_uses_issue_id(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        created = runner.invoke(cli, ["create", "Public list"])
        issue_id = _extract_id(created.output)

        result = runner.invoke(cli, ["list", "--type", "task", "--json"])
        assert result.exit_code == 0
        items = json.loads(result.output)["items"]
        item = next(i for i in items if i["title"] == "Public list")
        assert item["issue_id"] == issue_id
        assert "id" not in item

    def test_parent_issue_id_cli_aliases_are_public_vocabulary(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        parent_id = _extract_id(runner.invoke(cli, ["create", "Parent"]).output)

        created = runner.invoke(cli, ["create", "Child", "--parent-issue-id", parent_id, "--json"])

        assert created.exit_code == 0
        child = json.loads(created.output)
        assert child["parent_issue_id"] == parent_id
        assert child["parent_id"] == parent_id

        listed = runner.invoke(cli, ["list", "--parent-issue-id", parent_id, "--json"])
        assert listed.exit_code == 0
        assert [item["issue_id"] for item in json.loads(listed.output)["items"]] == [child["issue_id"]]

        updated = runner.invoke(cli, ["update", child["issue_id"], "--parent-issue-id", "", "--json"])
        assert updated.exit_code == 0
        data = json.loads(updated.output)
        assert data["parent_issue_id"] is None
        assert data["parent_id"] is None

    def test_start_next_work_json_uses_issue_id(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        created = runner.invoke(cli, ["create", "Public start next", "--type", "task", "--priority", "0"])
        issue_id = _extract_id(created.output)

        result = runner.invoke(
            cli,
            ["start-next-work", "--assignee", "agent-1", "--type", "task", "--json"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["issue_id"] == issue_id
        assert "id" not in data


class TestShowAndList:
    def test_show_issue(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["create", "Show me"])
        issue_id = _extract_id(result.output)
        result = runner.invoke(cli, ["show", issue_id])
        assert result.exit_code == 0
        assert "Show me" in result.output

    def test_show_not_found(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["show", "test-nonexistent"])
        assert result.exit_code == 1

    def test_list_all(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        runner.invoke(cli, ["create", "Issue A"])
        runner.invoke(cli, ["create", "Issue B"])
        result = runner.invoke(cli, ["list"])
        assert result.exit_code == 0
        # 2 created + auto-seeded "Future" release = 3
        assert "3 issues" in result.output

    def test_list_filter_status(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        runner.invoke(cli, ["create", "Open one"])
        r = runner.invoke(cli, ["create", "Close one"])
        issue_id = _extract_id(r.output)
        runner.invoke(cli, ["close", issue_id])
        result = runner.invoke(cli, ["list", "--status", "open"])
        # 1 open task + auto-seeded "Future" release (planning = open category) = 2
        assert "2 issues" in result.output

    def test_show_with_files_json_has_files_key(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """--with-files --json includes a 'files' key (even if empty list)."""
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Files test issue"])
        issue_id = _extract_id(r.output)
        result = runner.invoke(cli, ["show", issue_id, "--with-files", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "files" in data
        assert isinstance(data["files"], list)

    def test_show_without_files_json_has_no_files_key(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Default show --json does NOT include a 'files' key."""
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "No files key issue"])
        issue_id = _extract_id(r.output)
        result = runner.invoke(cli, ["show", issue_id, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "files" not in data


class TestUpdateAndClose:
    def test_update_status(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Update me"])
        issue_id = _extract_id(r.output)
        result = runner.invoke(cli, ["update", issue_id, "--status", "in_progress"])
        assert result.exit_code == 0
        assert "in_progress" in result.output

    def test_update_json_surfaces_soft_transition_warning(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        created = runner.invoke(cli, ["create", "Warn me", "--type", "bug", "--json"])
        assert created.exit_code == 0
        issue_id = json.loads(created.output)["issue_id"]

        result = runner.invoke(cli, ["update", issue_id, "--status", "confirmed", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "confirmed"
        assert data["data_warnings"] == ["Missing recommended fields for 'confirmed': severity"]

    def test_update_invalid_transition_json_includes_valid_transitions(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        created = runner.invoke(cli, ["create", "Invalid transition hints", "--type", "bug", "--json"])
        assert created.exit_code == 0
        issue_id = json.loads(created.output)["issue_id"]

        result = runner.invoke(cli, ["update", issue_id, "--status", "fixing", "--json"])

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "INVALID_TRANSITION"
        assert {transition["to"] for transition in data["valid_transitions"]} == {"confirmed", "wont_fix", "not_a_bug"}
        assert data["hint"] == "Use get_valid_transitions to see allowed state changes"

    def test_update_claim_conflict_json_includes_details(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        created = runner.invoke(cli, ["create", "Claim-aware update", "--json"])
        assert created.exit_code == 0
        issue_id = json.loads(created.output)["issue_id"]
        runner.invoke(cli, ["claim", issue_id, "--assignee", "agent-holder"])

        result = runner.invoke(cli, ["--actor", "other-agent", "update", issue_id, "--priority", "0", "--json"])

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "CONFLICT"
        assert data["details"] == {"issue_id": issue_id, "observed": "agent-holder", "expected": "other-agent"}

    def test_update_not_found(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["update", "test-nonexistent", "--title", "nope"])
        assert result.exit_code == 1

    def test_update_design_sets_field(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Design shorthand"])
        issue_id = _extract_id(r.output)
        result = runner.invoke(cli, ["update", issue_id, "--design", "draft v1"])
        assert result.exit_code == 0

        show = runner.invoke(cli, ["show", issue_id, "--json"])
        data = json.loads(show.output)
        assert data["fields"]["design"] == "draft v1"

    def test_update_design_empty_string_clears_field(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """filigree-613e9f5f66: `--design=` must clear the field via shorthand, not silently no-op."""
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Design clear"])
        issue_id = _extract_id(r.output)
        runner.invoke(cli, ["update", issue_id, "--design", "initial"])

        # Clear via empty-string shorthand.
        result = runner.invoke(cli, ["update", issue_id, "--design", ""])
        assert result.exit_code == 0

        show = runner.invoke(cli, ["show", issue_id, "--json"])
        data = json.loads(show.output)
        assert data["fields"].get("design") == ""

    def test_close_issue(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Close me"])
        issue_id = _extract_id(r.output)
        result = runner.invoke(cli, ["close", issue_id])
        assert result.exit_code == 0
        assert "Closed" in result.output

    def test_close_with_reason(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Close with reason"])
        issue_id = _extract_id(r.output)
        result = runner.invoke(cli, ["close", issue_id, "--reason", "done"])
        assert result.exit_code == 0

    def test_close_not_found(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["close", "test-nonexistent"])
        assert result.exit_code == 1
        assert "Not found" in result.output

    def test_close_json_partial_failure_preserves_successes(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """When closing multiple issues with --json and one fails, successes must still appear."""
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Good one"])
        good_id = _extract_id(r.output)
        result = runner.invoke(cli, ["close", good_id, "test-nonexistent", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert len(data["succeeded"]) == 1
        assert data["succeeded"][0]["issue_id"] == good_id
        assert len(data["failed"]) == 1
        assert data["failed"][0]["id"] == "test-nonexistent"

    def test_close_json_all_success(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r1 = runner.invoke(cli, ["create", "A"])
        r2 = runner.invoke(cli, ["create", "B"])
        id1, id2 = _extract_id(r1.output), _extract_id(r2.output)
        result = runner.invoke(cli, ["close", id1, id2, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["succeeded"]) == 2
        assert data["failed"] == []

    def test_close_json_unblocked_only_newly_unblocked(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """`unblocked` must contain only issues that became ready from this close, not pre-existing ready issues."""
        runner, _ = cli_in_project
        already_ready = _extract_id(runner.invoke(cli, ["create", "Already ready"]).output)
        dep = _extract_id(runner.invoke(cli, ["create", "Dep"]).output)
        blocked = _extract_id(runner.invoke(cli, ["create", "Blocked"]).output)
        assert runner.invoke(cli, ["add-dep", blocked, dep]).exit_code == 0

        result = runner.invoke(cli, ["close", dep, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        unblocked_ids = {i["issue_id"] for i in data["newly_unblocked"]}
        assert blocked in unblocked_ids, "issue whose only dep closed must appear in newly_unblocked"
        assert already_ready not in unblocked_ids, f"pre-existing ready issue must NOT appear in newly_unblocked; got {unblocked_ids}"

    def test_close_claim_conflict_json_includes_details(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        created = runner.invoke(cli, ["create", "Claim-aware close", "--json"])
        assert created.exit_code == 0
        issue_id = json.loads(created.output)["issue_id"]
        runner.invoke(cli, ["claim", issue_id, "--assignee", "agent-holder"])

        result = runner.invoke(cli, ["--actor", "other-agent", "close", issue_id, "--json"])

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "CONFLICT"
        assert data["details"] == {"issue_id": issue_id, "observed": "agent-holder", "expected": "other-agent"}


class TestReopen:
    def test_reopen_issue(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Reopen me"])
        issue_id = _extract_id(r.output)
        runner.invoke(cli, ["close", issue_id])
        result = runner.invoke(cli, ["reopen", issue_id])
        assert result.exit_code == 0
        assert "Reopened" in result.output

    def test_reopen_bug_returns_to_last_non_done_status_and_clears_close_reason(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        created = runner.invoke(cli, ["create", "Bug reopen", "--type", "bug", "--field", "severity=major", "--json"])
        assert created.exit_code == 0
        issue_id = json.loads(created.output)["issue_id"]
        assert runner.invoke(cli, ["update", issue_id, "--status", "confirmed"]).exit_code == 0
        assert runner.invoke(cli, ["update", issue_id, "--status", "fixing", "--field", "root_cause=bad assumption"]).exit_code == 0
        assert (
            runner.invoke(cli, ["update", issue_id, "--status", "verifying", "--field", "fix_verification=regression added"]).exit_code == 0
        )
        assert runner.invoke(cli, ["close", issue_id, "--reason", "closed too early"]).exit_code == 0

        result = runner.invoke(cli, ["reopen", issue_id, "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["succeeded"][0]["status"] == "verifying"
        shown = runner.invoke(cli, ["show", issue_id, "--json"])
        fields = json.loads(shown.output)["fields"]
        assert fields["root_cause"] == "bad assumption"
        assert fields["fix_verification"] == "regression added"
        assert "close_reason" not in fields

    def test_reopen_not_found(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["reopen", "test-nonexistent"])
        assert result.exit_code == 1
        assert "Not found" in result.output

    def test_reopen_json_partial_failure_preserves_successes(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """When reopening multiple issues with --json and one fails, successes must still appear."""
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Reopen me"])
        good_id = _extract_id(r.output)
        runner.invoke(cli, ["close", good_id])
        result = runner.invoke(cli, ["reopen", good_id, "test-nonexistent", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert len(data["succeeded"]) == 1
        assert data["succeeded"][0]["issue_id"] == good_id
        assert len(data["failed"]) == 1

    def test_reopen_json_batch_ignores_closed_audit_assignee(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        held = runner.invoke(cli, ["create", "Held reopen"])
        free = runner.invoke(cli, ["create", "Free reopen"])
        held_id = _extract_id(held.output)
        free_id = _extract_id(free.output)
        assert runner.invoke(cli, ["claim", held_id, "--assignee", "agent-holder"]).exit_code == 0
        assert runner.invoke(cli, ["--actor", "agent-holder", "close", held_id]).exit_code == 0
        assert runner.invoke(cli, ["close", free_id]).exit_code == 0

        result = runner.invoke(cli, ["--actor", "other-agent", "reopen", held_id, free_id, "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert {item["issue_id"] for item in data["succeeded"]} == {held_id, free_id}
        assert data["failed"] == []

    def test_reopen_json_all_success(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r1 = runner.invoke(cli, ["create", "A"])
        r2 = runner.invoke(cli, ["create", "B"])
        id1, id2 = _extract_id(r1.output), _extract_id(r2.output)
        runner.invoke(cli, ["close", id1])
        runner.invoke(cli, ["close", id2])
        result = runner.invoke(cli, ["reopen", id1, id2, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["succeeded"]) == 2
        assert data["failed"] == []


class TestCommentsCli:
    def test_add_comment(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Commentable"])
        issue_id = _extract_id(r.output)
        result = runner.invoke(cli, ["add-comment", issue_id, "My comment"])
        assert result.exit_code == 0
        assert "Added comment" in result.output

    def test_add_comment_json_echoes_structured_comment(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Commentable"])
        issue_id = _extract_id(r.output)

        result = runner.invoke(cli, ["--actor", "cli-commenter", "add-comment", issue_id, "My comment", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["issue_id"] == issue_id
        assert data["comment"]["comment_id"] == data["comment_id"]
        assert data["comment"]["author"] == "cli-commenter"
        assert data["comment"]["text"] == "My comment"
        assert isinstance(data["comment"]["created_at"], str)

    def test_add_comment_json_get_comment_sqlite_error_returns_io(
        self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sqlite3

        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Commentable"])
        issue_id = _extract_id(r.output)

        def _raise(*_a: object, **_kw: object) -> None:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr("filigree.core.FiligreeDB.get_comment", _raise)
        result = runner.invoke(cli, ["add-comment", issue_id, "My comment", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "IO"
        assert "database is locked" in data["error"]

    def test_add_comment_claim_conflict_json_includes_details(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Claim-aware comment"])
        issue_id = _extract_id(r.output)
        runner.invoke(cli, ["claim", issue_id, "--assignee", "agent-holder"])

        result = runner.invoke(cli, ["--actor", "other-agent", "add-comment", issue_id, "note", "--json"])

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "CONFLICT"
        assert data["details"] == {"issue_id": issue_id, "observed": "agent-holder", "expected": "other-agent"}

    def test_list_comments(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Commentable"])
        issue_id = _extract_id(r.output)
        runner.invoke(cli, ["add-comment", issue_id, "First comment"])
        runner.invoke(cli, ["add-comment", issue_id, "Second comment"])
        result = runner.invoke(cli, ["get-comments", issue_id])
        assert result.exit_code == 0
        assert "First comment" in result.output
        assert "Second comment" in result.output

    def test_list_comments_json_uses_comment_id(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Commentable"])
        issue_id = _extract_id(r.output)
        runner.invoke(cli, ["add-comment", issue_id, "First comment"])

        result = runner.invoke(cli, ["get-comments", issue_id, "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["items"][0]["comment_id"]
        assert "id" not in data["items"][0]

    def test_comment_not_found(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["add-comment", "test-nonexistent", "text"])
        assert result.exit_code == 1

    def test_add_comment_refreshes_context_md(
        self,
        cli_in_project: tuple[CliRunner, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """filigree-8188eb6fe7: add-comment must refresh context.md like other CLI mutations."""
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Refresh me"])
        issue_id = _extract_id(r.output)

        calls: list[tuple[object, object]] = []

        def spy(db: object, path: object) -> None:
            calls.append((db, path))

        monkeypatch.setattr("filigree.cli_common.write_summary", spy)
        result = runner.invoke(cli, ["add-comment", issue_id, "needs refresh"])
        assert result.exit_code == 0
        assert calls, "add-comment must call refresh_summary (write_summary was never invoked)"

    def test_comments_empty(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "No comments"])
        issue_id = _extract_id(r.output)
        result = runner.invoke(cli, ["get-comments", issue_id])
        assert result.exit_code == 0
        assert "No comments" in result.output


class TestLabelCli:
    def test_label_add(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Label me"])
        issue_id = _extract_id(r.output)
        result = runner.invoke(cli, ["add-label", "urgent", issue_id])
        assert result.exit_code == 0
        assert "Added label" in result.output

    def test_label_remove(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Label me", "-l", "urgent"])
        issue_id = _extract_id(r.output)
        result = runner.invoke(cli, ["remove-label", issue_id, "urgent"])
        assert result.exit_code == 0
        assert "Removed label" in result.output

    def test_label_add_rejects_reserved_type_name(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Label me"])
        issue_id = _extract_id(r.output)
        result = runner.invoke(cli, ["add-label", "bug", issue_id])
        assert result.exit_code == 1
        assert "reserved as an issue type" in result.output

    def test_label_add_rejects_priority_like_label_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Label me"])
        issue_id = _extract_id(r.output)

        result = runner.invoke(cli, ["add-label", "P1", issue_id, "--json"])

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "VALIDATION"
        assert "priority field" in data["error"]

    def test_label_add_claim_conflict_json_includes_details(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Claim-aware label"])
        issue_id = _extract_id(r.output)
        runner.invoke(cli, ["claim", issue_id, "--assignee", "agent-holder"])

        result = runner.invoke(cli, ["--actor", "other-agent", "add-label", "needs-review", issue_id, "--json"])

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "CONFLICT"
        assert data["details"] == {"issue_id": issue_id, "observed": "agent-holder", "expected": "other-agent"}

    def test_label_remove_claim_conflict_json_includes_details(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Claim-aware remove label", "-l", "needs-review"])
        issue_id = _extract_id(r.output)
        runner.invoke(cli, ["claim", issue_id, "--assignee", "agent-holder"])

        result = runner.invoke(cli, ["--actor", "other-agent", "remove-label", issue_id, "needs-review", "--json"])

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "CONFLICT"
        assert data["details"] == {"issue_id": issue_id, "observed": "agent-holder", "expected": "other-agent"}

    def test_label_add_not_found(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["add-label", "bug", "test-nonexistent"])
        assert result.exit_code == 1

    def test_label_add_echoes_canonical_form(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Bug filigree-6870a1dcc0: add-label must echo the canonical (stripped) label, not raw argv."""
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Label echo"])
        issue_id = _extract_id(r.output)
        result = runner.invoke(cli, ["add-label", "  urgent  ", issue_id])
        assert result.exit_code == 0
        assert "'urgent'" in result.output, f"expected canonical 'urgent' in output, got: {result.output!r}"
        assert "  urgent  " not in result.output

    def test_label_remove_echoes_canonical_form(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Bug filigree-6870a1dcc0: remove-label must echo the canonical (stripped) label, not raw argv."""
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Label echo", "-l", "urgent"])
        issue_id = _extract_id(r.output)
        result = runner.invoke(cli, ["remove-label", issue_id, "  urgent  "])
        assert result.exit_code == 0
        assert "'urgent'" in result.output
        assert "  urgent  " not in result.output


class TestClaimCli:
    def test_claim_issue(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Claimable"])
        issue_id = _extract_id(r.output)
        result = runner.invoke(cli, ["claim", issue_id, "--assignee", "agent-1"])
        assert result.exit_code == 0
        assert "Claimed" in result.output
        assert "agent-1" in result.output

    def test_claim_already_claimed(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Claimable"])
        issue_id = _extract_id(r.output)
        runner.invoke(cli, ["claim", issue_id, "--assignee", "agent-1"])
        result = runner.invoke(cli, ["claim", issue_id, "--assignee", "agent-2"])
        assert result.exit_code == 1

    def test_claim_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Claimable JSON"])
        issue_id = _extract_id(r.output)
        result = runner.invoke(cli, ["claim", issue_id, "--assignee", "agent-1", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["assignee"] == "agent-1"
        assert data["claimed_at"] is not None
        assert data["last_heartbeat_at"] == data["claimed_at"]
        assert data["claim_expires_at"] is not None

    def test_claim_conflict_json_includes_details(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Claim conflict JSON"])
        issue_id = _extract_id(r.output)
        runner.invoke(cli, ["claim", issue_id, "--assignee", "agent-1"])

        result = runner.invoke(cli, ["claim", issue_id, "--assignee", "agent-2", "--json"])

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "CONFLICT"
        assert data["details"] == {"issue_id": issue_id, "observed": "agent-1", "expected": "agent-2"}

    def test_claim_not_found(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["claim", "test-nonexistent", "--assignee", "a"])
        assert result.exit_code == 1

    def test_claim_whitespace_assignee_json_is_validation(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Cross-surface parity: MCP pre-validates assignee to VALIDATION.

        CLI must match. Previously, a blank assignee fell through to db.claim_issue,
        raised "Assignee cannot be empty", and was miscoded as CONFLICT.
        """
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Claimable"])
        issue_id = _extract_id(r.output)
        result = runner.invoke(cli, ["claim", issue_id, "--assignee", "   ", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "VALIDATION"

    def test_heartbeat_work_json_refreshes_claim(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Heartbeat CLI"])
        issue_id = _extract_id(r.output)
        runner.invoke(cli, ["claim", issue_id, "--assignee", "agent-1"])
        old = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
        from filigree.cli_common import get_db

        with get_db() as db:
            db.conn.execute(
                "UPDATE issues SET last_heartbeat_at = ?, claim_expires_at = ? WHERE id = ?",
                (old, old, issue_id),
            )
            db.conn.commit()

        result = runner.invoke(cli, ["--actor", "agent-1", "heartbeat-work", issue_id, "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["issue_id"] == issue_id
        assert datetime.fromisoformat(data["last_heartbeat_at"]) > datetime.fromisoformat(old)

    def test_stale_claims_json_lists_legacy_assigned_work(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Stale CLI", "-p", "1"])
        issue_id = _extract_id(r.output)
        old = (datetime.now(UTC) - timedelta(days=5)).isoformat()
        from filigree.cli_common import get_db

        with get_db() as db:
            db.conn.execute(
                "UPDATE issues SET assignee = 'old-agent', updated_at = ? WHERE id = ?",
                (old, issue_id),
            )
            db.conn.commit()

        result = runner.invoke(cli, ["stale-claims", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert [item["issue_id"] for item in data["items"]] == [issue_id]

    def test_stale_claims_json_can_include_near_expiry_leases(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        soon_id = _extract_id(runner.invoke(cli, ["create", "Expires soon CLI", "-p", "0"]).output)
        later_id = _extract_id(runner.invoke(cli, ["create", "Expires later CLI", "-p", "0"]).output)
        expired_id = _extract_id(runner.invoke(cli, ["create", "Expired CLI", "-p", "0"]).output)
        runner.invoke(cli, ["claim", soon_id, "--assignee", "agent-soon"])
        runner.invoke(cli, ["claim", later_id, "--assignee", "agent-later"])
        runner.invoke(cli, ["claim", expired_id, "--assignee", "agent-expired"])
        now = datetime.now(UTC)
        soon_at = (now + timedelta(hours=1)).isoformat()
        later_at = (now + timedelta(hours=5)).isoformat()
        expired_at = (now - timedelta(hours=1)).isoformat()
        from filigree.cli_common import get_db

        with get_db() as db:
            db.conn.execute(
                "UPDATE issues SET claim_expires_at = ?, last_heartbeat_at = ? WHERE id = ?",
                (soon_at, soon_at, soon_id),
            )
            db.conn.execute(
                "UPDATE issues SET claim_expires_at = ?, last_heartbeat_at = ? WHERE id = ?",
                (later_at, later_at, later_id),
            )
            db.conn.execute(
                "UPDATE issues SET claim_expires_at = ?, last_heartbeat_at = ? WHERE id = ?",
                (expired_at, expired_at, expired_id),
            )
            db.conn.commit()

        default_result = runner.invoke(cli, ["stale-claims", "--json"])
        proactive_result = runner.invoke(cli, ["stale-claims", "--expires-within-hours", "2", "--json"])

        assert default_result.exit_code == 0
        assert proactive_result.exit_code == 0
        default_data = json.loads(default_result.output)
        proactive_data = json.loads(proactive_result.output)
        assert [item["issue_id"] for item in default_data["items"]] == [expired_id]
        assert [item["issue_id"] for item in proactive_data["items"]] == [soon_id, expired_id]

    def test_reclaim_issue_json_transfers_expected_holder(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Reclaim CLI"])
        issue_id = _extract_id(r.output)
        runner.invoke(cli, ["claim", issue_id, "--assignee", "agent-old"])

        result = runner.invoke(
            cli,
            [
                "--actor",
                "coordinator",
                "reclaim",
                issue_id,
                "--assignee",
                "agent-new",
                "--expected-assignee",
                "agent-old",
                "--reason",
                "missed heartbeat",
                "--json",
            ],
        )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["assignee"] == "agent-new"
        assert data["claimed_at"] is not None

    def test_release_reason_is_recorded(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Release reason"])
        issue_id = _extract_id(r.output)
        runner.invoke(cli, ["claim", issue_id, "--assignee", "agent-1"])

        result = runner.invoke(cli, ["--actor", "agent-1", "release", issue_id, "--reason", "handoff", "--json"])

        assert result.exit_code == 0
        from filigree.cli_common import get_db

        with get_db() as db:
            events = db.get_issue_events(issue_id, limit=10)
        released = [event for event in events if event["event_type"] == "released"]
        assert released[-1]["comment"] == "handoff"


class TestClaimNextCli:
    def test_claim_next_basic(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        runner.invoke(cli, ["create", "Ready task", "-p", "1"])
        result = runner.invoke(cli, ["claim-next", "--assignee", "agent-1"])
        assert result.exit_code == 0
        assert "Claimed" in result.output

    def test_claim_next_empty(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        # Claim the auto-seeded "Future" release first so the queue is truly empty
        runner.invoke(cli, ["claim-next", "--assignee", "drain"])
        result = runner.invoke(cli, ["claim-next", "--assignee", "agent-1"])
        assert result.exit_code == 0
        assert "No issues available" in result.output

    def test_claim_next_with_type_filter(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        runner.invoke(cli, ["create", "A task", "--type", "task"])
        runner.invoke(cli, ["create", "A bug", "--type", "bug"])
        result = runner.invoke(cli, ["claim-next", "--assignee", "a", "--type", "bug", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["type"] == "bug"

    def test_claim_next_json_empty(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        # Claim the auto-seeded "Future" release first so the queue is truly empty
        runner.invoke(cli, ["claim-next", "--assignee", "drain"])
        result = runner.invoke(cli, ["claim-next", "--assignee", "a", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "empty"

    def test_claim_next_json_empty_includes_reason(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """filigree-0e8dadbfcc: empty --json must mirror MCP ClaimNextEmptyResponse.

        types/api.py:266 declares ``reason`` alongside ``status``; the MCP
        handler emits ``"No ready issues matching filters"``. CLI parity.
        """
        runner, _ = cli_in_project
        runner.invoke(cli, ["claim-next", "--assignee", "drain"])
        result = runner.invoke(cli, ["claim-next", "--assignee", "a", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == {"status": "empty", "reason": "No ready issues matching filters"}

    def test_claim_next_json_success_includes_selection_reason(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """filigree-0e8dadbfcc: successful --json must mirror MCP ClaimNextResponse.

        types/api.py:140 declares ``selection_reason``; the MCP handler builds
        it via ``Issue.format_claim_next_reason``. CLI parity is required by
        Phase E §9 (envelope shape contract).
        """
        runner, _ = cli_in_project
        runner.invoke(cli, ["create", "P1 task", "-p", "1"])
        result = runner.invoke(cli, ["claim-next", "--assignee", "bot", "--type", "task", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["selection_reason"] == "Highest-priority P1, ready issue (no blockers)"

    def test_claim_next_json_success_selection_reason_includes_type_when_nondefault(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """The seeded ``Future`` release issue exercises the ``type=`` branch."""
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["claim-next", "--assignee", "bot", "--type", "release", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["type"] == "release"
        assert "type=release" in data["selection_reason"]

    def test_claim_next_whitespace_assignee_shows_clean_error(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        runner.invoke(cli, ["create", "A task"])
        result = runner.invoke(cli, ["claim-next", "--assignee", "   "])
        assert result.exit_code == 1
        assert "Traceback" not in (result.output or "")

    def test_claim_next_whitespace_assignee_json_is_validation(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Cross-surface parity: MCP returns VALIDATION for blank assignee.

        CLI must match — blank assignee is bad user input, not a race condition.
        """
        runner, _ = cli_in_project
        runner.invoke(cli, ["create", "A task"])
        result = runner.invoke(cli, ["claim-next", "--assignee", "   ", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "VALIDATION"


class TestReleaseCli:
    def test_release_issue(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Releasable"])
        issue_id = _extract_id(r.output)
        runner.invoke(cli, ["claim", issue_id, "--assignee", "agent-1"])
        result = runner.invoke(cli, ["release", issue_id])
        assert result.exit_code == 0
        assert "Released" in result.output

    def test_release_not_found(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["release", "test-nonexistent"])
        assert result.exit_code == 1

    def test_release_not_claimed(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Not claimed"])
        issue_id = _extract_id(r.output)
        result = runner.invoke(cli, ["release", issue_id])
        assert result.exit_code == 1

    def test_release_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "JSON release"])
        issue_id = _extract_id(r.output)
        runner.invoke(cli, ["claim", issue_id, "--assignee", "agent-1"])
        result = runner.invoke(cli, ["release", issue_id, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["issue_id"] == issue_id
        assert "id" not in data
        assert data["assignee"] == ""

    def test_release_if_held_unassigned_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "JSON release if held"])
        issue_id = _extract_id(r.output)

        result = runner.invoke(cli, ["--actor", "agent-1", "release", issue_id, "--if-held", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["issue_id"] == issue_id
        assert data["assignee"] == ""

    def test_release_if_held_expected_assignee_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "JSON release expected holder"])
        issue_id = _extract_id(r.output)
        runner.invoke(cli, ["claim", issue_id, "--assignee", "agent-1"])

        result = runner.invoke(
            cli,
            ["--actor", "coordinator", "release", issue_id, "--if-held", "--expected-assignee", "agent-1", "--json"],
        )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["assignee"] == ""

    def test_release_if_held_rejects_other_assignee_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "JSON release other holder"])
        issue_id = _extract_id(r.output)
        runner.invoke(cli, ["claim", issue_id, "--assignee", "agent-2"])

        result = runner.invoke(cli, ["--actor", "agent-1", "release", issue_id, "--if-held", "--json"])

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "CONFLICT"
        assert "agent-2" in data["error"]
        assert data["details"] == {"issue_id": issue_id, "observed": "agent-2", "expected": "agent-1"}

    def test_release_json_not_found(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["release", "test-nonexistent", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert "error" in data

    def test_release_my_claims_json_exits_nonzero_on_failures(
        self,
        cli_in_project: tuple[CliRunner, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "JSON bulk release failure"])
        issue_id = _extract_id(r.output)
        runner.invoke(cli, ["claim", issue_id, "--assignee", "agent-1"])

        def fail_release_claim(*_args: object, **_kwargs: object) -> object:
            raise ValueError("simulated release failure")

        monkeypatch.setattr("filigree.db_issues.IssuesMixin.release_claim", fail_release_claim)

        result = runner.invoke(cli, ["--actor", "agent-1", "release-my-claims", "--json"])

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["succeeded"] == []
        assert data["failed"][0]["id"] == issue_id
        assert "simulated release failure" in data["failed"][0]["error"]

    def test_release_my_claims_text_exits_nonzero_on_failures(
        self,
        cli_in_project: tuple[CliRunner, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "Text bulk release failure"])
        issue_id = _extract_id(r.output)
        runner.invoke(cli, ["claim", issue_id, "--assignee", "agent-1"])

        def fail_release_claim(*_args: object, **_kwargs: object) -> object:
            raise ValueError("simulated release failure")

        monkeypatch.setattr("filigree.db_issues.IssuesMixin.release_claim", fail_release_claim)

        result = runner.invoke(cli, ["--actor", "agent-1", "release-my-claims"])

        assert result.exit_code == 1
        assert "failure(s)" in result.output
        assert issue_id in result.output
        assert "simulated release failure" in result.output

    def test_release_my_claims_json_refreshes_context_after_release(
        self,
        cli_in_project: tuple[CliRunner, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "JSON bulk release refresh"])
        issue_id = _extract_id(r.output)
        runner.invoke(cli, ["claim", issue_id, "--assignee", "agent-1"])
        calls: list[object] = []

        def spy_refresh(db: object) -> None:
            calls.append(db)

        monkeypatch.setattr("filigree.cli_commands.issues.refresh_summary", spy_refresh)

        result = runner.invoke(cli, ["--actor", "agent-1", "release-my-claims", "--json"])

        assert result.exit_code == 0, result.output
        assert calls, "release-my-claims --json must refresh context.md after real releases"

    def test_release_my_claims_json_dry_run_does_not_refresh_context(
        self,
        cli_in_project: tuple[CliRunner, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runner, _ = cli_in_project
        r = runner.invoke(cli, ["create", "JSON bulk release dry-run"])
        issue_id = _extract_id(r.output)
        runner.invoke(cli, ["claim", issue_id, "--assignee", "agent-1"])
        calls: list[object] = []

        def spy_refresh(db: object) -> None:
            calls.append(db)

        monkeypatch.setattr("filigree.cli_commands.issues.refresh_summary", spy_refresh)

        result = runner.invoke(cli, ["--actor", "agent-1", "release-my-claims", "--dry-run", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["dry_run"] is True
        assert calls == []


class TestListLabelQuery:
    def test_list_label_prefix(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        runner.invoke(cli, ["create", "Issue A", "-l", "cluster:broad-except"])
        runner.invoke(cli, ["create", "Issue B", "-l", "effort:m"])
        result = runner.invoke(cli, ["list", "--label-prefix", "cluster:"])
        assert result.exit_code == 0
        assert "Issue A" in result.output

    def test_list_not_label(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        runner.invoke(cli, ["create", "Issue A", "-l", "wont-fix"])
        runner.invoke(cli, ["create", "Issue B", "-l", "needs-review"])
        result = runner.invoke(cli, ["list", "--not-label", "wont-fix"])
        assert result.exit_code == 0
        assert "Issue A" not in result.output

    def test_list_multiple_labels_and(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        runner.invoke(cli, ["create", "Issue A", "-l", "needs-review", "-l", "urgent"])
        runner.invoke(cli, ["create", "Issue B", "-l", "needs-review"])
        result = runner.invoke(cli, ["list", "-l", "needs-review", "-l", "urgent"])
        assert result.exit_code == 0
        assert "Issue A" in result.output

    def test_list_virtual_label_via_cli(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        runner.invoke(cli, ["create", "Fresh issue"])
        result = runner.invoke(cli, ["list", "-l", "age:fresh"])
        assert result.exit_code == 0
        assert "Fresh issue" in result.output

    def test_list_invalid_virtual_label_emits_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """`list --json` must emit a 2.0 envelope on invalid label, not a Click string.

        Regression for filigree-d87fd24b12.
        """
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["list", "-l", "age:bogus", "--json"])
        assert result.exit_code == 1, result.output
        data = json.loads(result.output)
        assert data["code"] == "VALIDATION", data
        assert "age" in data["error"].lower() or "bogus" in data["error"].lower()

    def test_list_malformed_label_prefix_emits_json_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """`list --json` must emit a 2.0 envelope on malformed --label-prefix.

        Regression for filigree-d87fd24b12.
        """
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["list", "--label-prefix", "missing-colon", "--json"])
        assert result.exit_code == 1, result.output
        data = json.loads(result.output)
        assert data["code"] == "VALIDATION", data
        assert "label_prefix" in data["error"].lower() or "colon" in data["error"].lower()
