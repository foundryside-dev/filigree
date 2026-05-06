"""CLI tests for file tracking and finding triage commands.

MCP shape verification (verified against mcp_tools/files.py handlers):

File shapes:
- list-files items: EnrichedFileItem — FileRecordDict + {summary, associations_count, observation_count}
  FileRecordDict keys: id, path, language, file_type, first_seen, updated_at, metadata, data_warnings
- get-file: FileDetail — {file, associations, recent_findings, summary, observation_count}
- get-file-timeline: ListResponse of TimelineEntry — {id, type, timestamp, source_id, data}
  NOTE: MCP returns raw PaginatedResult; CLI normalizes to ListResponse.
- get-issue-files: ListResponse of IssueFileAssociation
  NOTE: MCP returns raw list; CLI normalizes to ListResponse.
- add-file-association: {"status": "created"}
- register-file: FileRecordDict — {id, path, language, file_type, first_seen, updated_at, metadata, data_warnings}
- delete-file-record: {status, file_id, deleted_findings, deleted_associations, deleted_file_events, unlinked_observations}

Finding shapes:
- list-findings items: ScanFindingDict
  keys: id, file_id, severity, status, scan_source, rule_id, message, suggestion,
        scan_run_id, line_start, line_end, issue_id, seen_count, first_seen, updated_at,
        last_seen_at, metadata, data_warnings
- get-finding: ScanFindingDict (same)
- update-finding: ScanFindingDict (same)
- promote-finding: PublicIssue — {issue_id, title, status, status_category, priority,
                                  type, parent_id, assignee, created_at, updated_at,
                                  closed_at, description, notes, fields, labels,
                                  blocks, blocked_by, is_ready, children, data_warnings}
- dismiss-finding: ScanFindingDict (same as get-finding)
- batch-update-findings: BatchResponse[str] — {succeeded, failed} or error envelope
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from filigree.cli import cli
from tests._seeds import SeededProject

# ---------------------------------------------------------------------------
# Canonical MCP key-sets
# ---------------------------------------------------------------------------

_FILE_RECORD_KEYS = frozenset({"id", "path", "language", "file_type", "first_seen", "updated_at", "metadata", "data_warnings"})

# EnrichedFileItem = FileRecordDict + extra fields
_ENRICHED_FILE_ITEM_KEYS = _FILE_RECORD_KEYS | frozenset({"summary", "associations_count", "observation_count"})

_FILE_DETAIL_KEYS = frozenset({"file", "associations", "recent_findings", "summary", "observation_count"})

_TIMELINE_ENTRY_KEYS = frozenset({"id", "type", "timestamp", "source_id", "data"})

_ISSUE_FILE_ASSOC_KEYS = frozenset({"id", "file_id", "issue_id", "assoc_type", "created_at", "file_path", "file_language"})

_SCAN_FINDING_KEYS = frozenset(
    {
        "id",
        "file_id",
        "severity",
        "status",
        "scan_source",
        "rule_id",
        "message",
        "suggestion",
        "scan_run_id",
        "line_start",
        "line_end",
        "issue_id",
        "seen_count",
        "first_seen",
        "updated_at",
        "last_seen_at",
        "metadata",
        "data_warnings",
    }
)

_OBSERVATION_KEYS = frozenset(
    {
        "id",
        "summary",
        "detail",
        "file_id",
        "file_path",
        "line",
        "source_issue_id",
        "priority",
        "actor",
        "created_at",
        "expires_at",
    }
)

_PUBLIC_ISSUE_KEYS = frozenset(
    {
        "issue_id",
        "title",
        "status",
        "status_category",
        "priority",
        "type",
        "parent_id",
        "assignee",
        "created_at",
        "updated_at",
        "closed_at",
        "description",
        "notes",
        "fields",
        "labels",
        "blocks",
        "blocked_by",
        "is_ready",
        "children",
        "data_warnings",
    }
)

_LIST_ENVELOPE_KEYS_NO_MORE = frozenset({"items", "has_more"})
_LIST_ENVELOPE_KEYS_HAS_MORE = frozenset({"items", "has_more", "next_offset"})
_BATCH_KEYS = frozenset({"succeeded", "failed"})


# ---------------------------------------------------------------------------
# TestListFilesCommand
# ---------------------------------------------------------------------------


class TestListFilesCommand:
    def test_list_empty_returns_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["list-files", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["items"] == []
        assert data["has_more"] is False
        assert "next_offset" not in data

    def test_list_populated_item_shape(self, initialized_project_with_file: SeededProject) -> None:
        """Each item must have the exact EnrichedFileItem key-set."""
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_file.path))
        try:
            result = runner.invoke(cli, ["list-files", "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert len(data["items"]) == 1
            item = data["items"][0]
            assert set(item.keys()) == _ENRICHED_FILE_ITEM_KEYS, f"Item key mismatch: {set(item.keys()) ^ _ENRICHED_FILE_ITEM_KEYS}"
        finally:
            os.chdir(original)

    def test_list_pagination_overfetch(self, initialized_project_with_many_findings: SeededProject) -> None:
        """With limit=1 and 1+ files seeded, verify has_more boundary detection."""
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_many_findings.path))
        try:
            # Only 1 file was seeded; limit=1 should give has_more=False
            result = runner.invoke(cli, ["list-files", "--limit", "1", "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert len(data["items"]) == 1
            # has_more False because DB knows exact total
            assert data["has_more"] is False
            assert set(data.keys()) == _LIST_ENVELOPE_KEYS_NO_MORE
        finally:
            os.chdir(original)

    def test_list_plain_text_empty(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["list-files"])
        assert result.exit_code == 0
        assert "No files" in result.output

    def test_list_plain_text_populated(self, initialized_project_with_file: SeededProject) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_file.path))
        try:
            result = runner.invoke(cli, ["list-files"])
            assert result.exit_code == 0
            assert "src/foo.py" in result.output
        finally:
            os.chdir(original)

    def test_list_filter_by_language(self, initialized_project_with_file: SeededProject) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_file.path))
        try:
            # Filter by seeded language
            result = runner.invoke(cli, ["list-files", "--language", "python", "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert len(data["items"]) == 1

            # Filter by non-matching language
            result2 = runner.invoke(cli, ["list-files", "--language", "rust", "--json"])
            assert result2.exit_code == 0, result2.output
            data2 = json.loads(result2.output)
            assert data2["items"] == []
        finally:
            os.chdir(original)


# ---------------------------------------------------------------------------
# TestGetFileCommand
# ---------------------------------------------------------------------------


class TestGetFileCommand:
    def test_get_file_happy_path_json(self, initialized_project_with_file: SeededProject) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_file.path))
        try:
            file_id = initialized_project_with_file.file_id
            result = runner.invoke(cli, ["get-file", file_id, "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert set(data.keys()) == _FILE_DETAIL_KEYS, f"Shape mismatch: {set(data.keys()) ^ _FILE_DETAIL_KEYS}"
            assert data["file"]["id"] == file_id
        finally:
            os.chdir(original)

    def test_get_file_not_found_json_error_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["get-file", "file-nonexistent", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert "error" in data
        assert data["code"] == "NOT_FOUND"

    def test_get_file_plain_text(self, initialized_project_with_file: SeededProject) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_file.path))
        try:
            file_id = initialized_project_with_file.file_id
            result = runner.invoke(cli, ["get-file", file_id])
            assert result.exit_code == 0
            assert file_id in result.output
        finally:
            os.chdir(original)

    def test_get_file_detail_nested_file_keys(self, initialized_project_with_file: SeededProject) -> None:
        """The nested 'file' dict must have the FileRecordDict key-set."""
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_file.path))
        try:
            file_id = initialized_project_with_file.file_id
            result = runner.invoke(cli, ["get-file", file_id, "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert set(data["file"].keys()) == _FILE_RECORD_KEYS, (
                f"Nested 'file' key mismatch: {set(data['file'].keys()) ^ _FILE_RECORD_KEYS}"
            )
        finally:
            os.chdir(original)


class TestDeleteFileRecordCommand:
    def test_delete_file_record_happy_path_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        created = runner.invoke(cli, ["register-file", "src/delete_me.py", "--json"])
        assert created.exit_code == 0, created.output
        file_id = json.loads(created.output)["id"]

        result = runner.invoke(cli, ["delete-file-record", file_id, "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["status"] == "deleted"
        assert data["file_id"] == file_id
        missing = runner.invoke(cli, ["get-file", file_id, "--json"])
        assert missing.exit_code != 0
        assert json.loads(missing.output)["code"] == "NOT_FOUND"

    def test_delete_file_record_refuses_association_without_force(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        created = runner.invoke(cli, ["register-file", "src/linked.py", "--json"])
        file_id = json.loads(created.output)["id"]
        issue = runner.invoke(cli, ["create", "Linked file issue", "--json"])
        issue_id = json.loads(issue.output)["issue_id"]
        runner.invoke(cli, ["add-file-association", file_id, issue_id, "mentioned_in"])

        blocked = runner.invoke(cli, ["delete-file-record", file_id, "--json"])
        forced = runner.invoke(cli, ["delete-file-record", file_id, "--force", "--json"])

        assert blocked.exit_code != 0
        assert json.loads(blocked.output)["code"] == "CONFLICT"
        assert forced.exit_code == 0, forced.output
        assert json.loads(forced.output)["deleted_associations"] == 1


# ---------------------------------------------------------------------------
# TestGetFileTimelineCommand
# ---------------------------------------------------------------------------


class TestGetFileTimelineCommand:
    def test_get_file_timeline_empty_json(self, initialized_project_with_file: SeededProject) -> None:
        """A new file with no events returns an empty ListResponse."""
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_file.path))
        try:
            file_id = initialized_project_with_file.file_id
            result = runner.invoke(cli, ["get-file-timeline", file_id, "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert "items" in data
            assert "has_more" in data
            assert data["has_more"] is False
            assert "next_offset" not in data
        finally:
            os.chdir(original)

    def test_get_file_timeline_with_finding_events(self, initialized_project_with_finding: SeededProject) -> None:
        """After seeding a finding, timeline should contain entries."""
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_finding.path))
        try:
            file_id = initialized_project_with_finding.file_id
            result = runner.invoke(cli, ["get-file-timeline", file_id, "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert len(data["items"]) >= 1
            entry = data["items"][0]
            assert set(entry.keys()) == _TIMELINE_ENTRY_KEYS, f"Timeline entry key mismatch: {set(entry.keys()) ^ _TIMELINE_ENTRY_KEYS}"
        finally:
            os.chdir(original)

    def test_get_file_timeline_not_found_json_error(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["get-file-timeline", "file-nonexistent", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert "error" in data
        assert data["code"] == "NOT_FOUND"

    def test_get_file_timeline_envelope_is_list_response(self, initialized_project_with_file: SeededProject) -> None:
        """Envelope must be ListResponse regardless of DB's PaginatedResult."""
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_file.path))
        try:
            file_id = initialized_project_with_file.file_id
            result = runner.invoke(cli, ["get-file-timeline", file_id, "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            # Must have ListResponse keys, not PaginatedResult keys (results, total, etc.)
            assert "items" in data
            assert "has_more" in data
            assert "results" not in data
            assert "total" not in data
        finally:
            os.chdir(original)

    def test_get_file_timeline_can_include_issue_events(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        created = runner.invoke(cli, ["register-file", "src/timeline_issue.py", "--json"])
        file_id = json.loads(created.output)["id"]
        issue = runner.invoke(cli, ["create", "CLI issue timeline", "--json"])
        issue_id = json.loads(issue.output)["issue_id"]
        runner.invoke(cli, ["add-file-association", file_id, issue_id, "mentioned_in"])
        runner.invoke(cli, ["update", issue_id, "--status", "in_progress"])

        default = runner.invoke(cli, ["get-file-timeline", file_id, "--json"])
        with_issue_events = runner.invoke(cli, ["get-file-timeline", file_id, "--include-issue-events", "--json"])
        issue_only = runner.invoke(cli, ["get-file-timeline", file_id, "--event-type", "issue_event", "--json"])

        assert default.exit_code == 0, default.output
        assert with_issue_events.exit_code == 0, with_issue_events.output
        assert issue_only.exit_code == 0, issue_only.output
        assert all(e["type"] != "issue_event" for e in json.loads(default.output)["items"])
        issue_events = [e for e in json.loads(with_issue_events.output)["items"] if e["type"] == "issue_event"]
        assert issue_events
        assert issue_events[0]["data"]["issue_id"] == issue_id
        assert issue_events[0]["data"]["event_type"] == "status_changed"
        assert all(e["type"] == "issue_event" for e in json.loads(issue_only.output)["items"])


# ---------------------------------------------------------------------------
# TestGetIssueFilesCommand
# ---------------------------------------------------------------------------


class TestGetIssueFilesCommand:
    def test_get_issue_files_empty_json(self, initialized_project_with_bug: SeededProject) -> None:
        """An issue with no file associations returns empty ListResponse."""
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_bug.path))
        try:
            issue_id = initialized_project_with_bug.bug_id
            result = runner.invoke(cli, ["get-issue-files", issue_id, "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["items"] == []
            assert data["has_more"] is False
            assert "next_offset" not in data
        finally:
            os.chdir(original)

    def test_get_issue_files_not_found_json_error(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["get-issue-files", "test-nonexistent", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert "error" in data
        assert data["code"] == "NOT_FOUND"

    def test_get_issue_files_with_association_item_shape(self, initialized_project_with_file: SeededProject) -> None:
        """After adding a file association, item shape must be exact."""
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_file.path))
        try:
            file_id = initialized_project_with_file.file_id
            # Create an issue, then associate the file
            create_result = runner.invoke(cli, ["create", "Test issue for file assoc"])
            assert create_result.exit_code == 0, create_result.output
            issue_id = create_result.output.split(":")[0].replace("Created ", "").strip()

            assoc_result = runner.invoke(cli, ["add-file-association", file_id, issue_id, "bug_in", "--json"])
            assert assoc_result.exit_code == 0, assoc_result.output

            result = runner.invoke(cli, ["get-issue-files", issue_id, "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert len(data["items"]) == 1
            item = data["items"][0]
            assert set(item.keys()) == _ISSUE_FILE_ASSOC_KEYS, f"Item key mismatch: {set(item.keys()) ^ _ISSUE_FILE_ASSOC_KEYS}"
        finally:
            os.chdir(original)


# ---------------------------------------------------------------------------
# TestAddFileAssociationCommand
# ---------------------------------------------------------------------------


class TestAddFileAssociationCommand:
    def test_add_file_association_happy_path_json(self, initialized_project_with_file: SeededProject) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_file.path))
        try:
            file_id = initialized_project_with_file.file_id
            create_result = runner.invoke(cli, ["create", "Associated issue"])
            assert create_result.exit_code == 0, create_result.output
            issue_id = create_result.output.split(":")[0].replace("Created ", "").strip()

            result = runner.invoke(cli, ["add-file-association", file_id, issue_id, "bug_in", "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data == {"status": "created"}
        finally:
            os.chdir(original)

    def test_add_file_association_file_not_found(self, initialized_project_with_bug: SeededProject) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_bug.path))
        try:
            issue_id = initialized_project_with_bug.bug_id
            result = runner.invoke(cli, ["add-file-association", "file-bad", issue_id, "bug_in", "--json"])
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert "error" in data
            assert data["code"] == "NOT_FOUND"
        finally:
            os.chdir(original)

    def test_add_file_association_issue_not_found(self, initialized_project_with_file: SeededProject) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_file.path))
        try:
            file_id = initialized_project_with_file.file_id
            result = runner.invoke(cli, ["add-file-association", file_id, "test-bad", "bug_in", "--json"])
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert "error" in data
            assert data["code"] == "NOT_FOUND"
        finally:
            os.chdir(original)

    def test_add_file_association_plain_text(self, initialized_project_with_file: SeededProject) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_file.path))
        try:
            file_id = initialized_project_with_file.file_id
            create_result = runner.invoke(cli, ["create", "Issue for plain text assoc"])
            assert create_result.exit_code == 0, create_result.output
            issue_id = create_result.output.split(":")[0].replace("Created ", "").strip()

            result = runner.invoke(cli, ["add-file-association", file_id, issue_id, "bug_in"])
            assert result.exit_code == 0
            assert "Associated" in result.output
        finally:
            os.chdir(original)


# ---------------------------------------------------------------------------
# TestRegisterFileCommand
# ---------------------------------------------------------------------------


class TestRegisterFileCommand:
    def test_register_file_happy_path_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["register-file", "src/newfile.py", "--language", "python", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert set(data.keys()) == _FILE_RECORD_KEYS, f"Shape mismatch: {set(data.keys()) ^ _FILE_RECORD_KEYS}"
        assert data["path"] == "src/newfile.py"
        assert data["language"] == "python"

    def test_register_file_idempotent(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Registering same path twice returns the same file record."""
        runner, _ = cli_in_project
        result1 = runner.invoke(cli, ["register-file", "src/same.py", "--json"])
        assert result1.exit_code == 0, result1.output
        data1 = json.loads(result1.output)

        result2 = runner.invoke(cli, ["register-file", "src/same.py", "--json"])
        assert result2.exit_code == 0, result2.output
        data2 = json.loads(result2.output)

        assert data1["id"] == data2["id"]

    def test_register_file_plain_text(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["register-file", "src/plain.py"])
        assert result.exit_code == 0
        assert "Registered" in result.output

    def test_register_file_with_metadata_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(
            cli,
            ["register-file", "src/meta.py", "--metadata", '{"owner": "team-a"}', "--json"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["metadata"] == {"owner": "team-a"}

    def test_register_file_bad_metadata_exits_1(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["register-file", "src/bad.py", "--metadata", "not-json", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert "error" in data
        assert data["code"] == "VALIDATION"

    def test_register_file_absolute_path_rejected(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Absolute paths must be rejected with VALIDATION error, matching MCP contract."""
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["register-file", "/etc/passwd", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert "error" in data
        assert data["code"] == "VALIDATION"

    def test_register_file_traversal_rejected(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Path traversal (../../escape) must be rejected with VALIDATION error, matching MCP contract."""
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["register-file", "../../escape", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert "error" in data
        assert data["code"] == "VALIDATION"


# ---------------------------------------------------------------------------
# TestListFindingsCommand
# ---------------------------------------------------------------------------


class TestListFindingsCommand:
    def test_list_empty_returns_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["list-findings", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["items"] == []
        assert data["has_more"] is False
        assert "next_offset" not in data

    def test_list_populated_item_shape(self, initialized_project_with_finding: SeededProject) -> None:
        """Each finding item must have the exact ScanFindingDict key-set."""
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_finding.path))
        try:
            result = runner.invoke(cli, ["list-findings", "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert len(data["items"]) >= 1
            item = data["items"][0]
            assert set(item.keys()) == _SCAN_FINDING_KEYS, f"Item key mismatch: {set(item.keys()) ^ _SCAN_FINDING_KEYS}"
        finally:
            os.chdir(original)

    def test_list_pagination_boundary(self, initialized_project_with_many_findings: SeededProject) -> None:
        """With limit=2 and 3 findings seeded, has_more=True and next_offset present."""
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_many_findings.path))
        try:
            result = runner.invoke(cli, ["list-findings", "--limit", "2", "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["has_more"] is True
            assert len(data["items"]) == 2
            assert "next_offset" in data
            assert data["next_offset"] == 2
            assert set(data.keys()) == _LIST_ENVELOPE_KEYS_HAS_MORE
        finally:
            os.chdir(original)

    def test_list_pagination_second_page(self, initialized_project_with_many_findings: SeededProject) -> None:
        """Second page with offset=2, limit=2 against 3 findings returns last 1, has_more=False."""
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_many_findings.path))
        try:
            result = runner.invoke(cli, ["list-findings", "--limit", "2", "--offset", "2", "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert len(data["items"]) == 1
            assert data["has_more"] is False
            assert "next_offset" not in data
        finally:
            os.chdir(original)

    def test_list_filter_by_severity(self, initialized_project_with_finding: SeededProject) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_finding.path))
        try:
            result = runner.invoke(cli, ["list-findings", "--severity", "high", "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert len(data["items"]) == 1
            assert data["items"][0]["severity"] == "high"
        finally:
            os.chdir(original)

    def test_list_plain_text_empty(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["list-findings"])
        assert result.exit_code == 0
        assert "No findings" in result.output


# ---------------------------------------------------------------------------
# TestGetFindingCommand
# ---------------------------------------------------------------------------


class TestGetFindingCommand:
    def test_get_finding_happy_path_json(self, initialized_project_with_finding: SeededProject) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_finding.path))
        try:
            finding_id = initialized_project_with_finding.finding_id
            result = runner.invoke(cli, ["get-finding", finding_id, "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert set(data.keys()) == _SCAN_FINDING_KEYS, f"Shape mismatch: {set(data.keys()) ^ _SCAN_FINDING_KEYS}"
            assert data["id"] == finding_id
        finally:
            os.chdir(original)

    def test_get_finding_not_found_json_error_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["get-finding", "finding-nonexistent", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert "error" in data
        assert data["code"] == "NOT_FOUND"

    def test_get_finding_plain_text(self, initialized_project_with_finding: SeededProject) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_finding.path))
        try:
            finding_id = initialized_project_with_finding.finding_id
            result = runner.invoke(cli, ["get-finding", finding_id])
            assert result.exit_code == 0
            assert finding_id in result.output
        finally:
            os.chdir(original)


# ---------------------------------------------------------------------------
# TestUpdateFindingCommand
# ---------------------------------------------------------------------------


class TestUpdateFindingCommand:
    def test_update_finding_status_json(self, initialized_project_with_finding: SeededProject) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_finding.path))
        try:
            finding_id = initialized_project_with_finding.finding_id
            result = runner.invoke(cli, ["update-finding", finding_id, "--status", "fixed", "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert set(data.keys()) == _SCAN_FINDING_KEYS, f"Shape mismatch: {set(data.keys()) ^ _SCAN_FINDING_KEYS}"
            assert data["status"] == "fixed"
        finally:
            os.chdir(original)

    def test_update_finding_not_found_json_error(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["update-finding", "finding-bad", "--status", "fixed", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert "error" in data
        assert data["code"] == "NOT_FOUND"

    def test_update_finding_no_options_exits_1(self, initialized_project_with_finding: SeededProject) -> None:
        """Providing neither --status nor --issue-id must fail."""
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_finding.path))
        try:
            finding_id = initialized_project_with_finding.finding_id
            result = runner.invoke(cli, ["update-finding", finding_id, "--json"])
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert "error" in data
        finally:
            os.chdir(original)

    def test_update_finding_plain_text(self, initialized_project_with_finding: SeededProject) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_finding.path))
        try:
            finding_id = initialized_project_with_finding.finding_id
            result = runner.invoke(cli, ["update-finding", finding_id, "--status", "acknowledged"])
            assert result.exit_code == 0
            assert finding_id in result.output
        finally:
            os.chdir(original)


# ---------------------------------------------------------------------------
# TestPromoteFindingCommand
# ---------------------------------------------------------------------------


class TestPromoteFindingCommand:
    def test_promote_finding_happy_path_json(self, initialized_project_with_finding: SeededProject) -> None:
        """promote-finding returns a PublicIssue and links the source finding."""
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_finding.path))
        try:
            finding_id = initialized_project_with_finding.finding_id
            result = runner.invoke(cli, ["promote-finding", finding_id, "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert set(data.keys()) == _PUBLIC_ISSUE_KEYS, f"Shape mismatch: {set(data.keys()) ^ _PUBLIC_ISSUE_KEYS}"
            assert "id" not in data
            assert data["issue_id"]
            assert "Test finding" in data["title"] or data["title"]
            assert "from-finding" in data["labels"]
        finally:
            os.chdir(original)

    def test_promote_finding_not_found_json_error(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["promote-finding", "finding-bad", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert "error" in data
        assert data["code"] == "NOT_FOUND"

    def test_promote_finding_plain_text(self, initialized_project_with_finding: SeededProject) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_finding.path))
        try:
            finding_id = initialized_project_with_finding.finding_id
            result = runner.invoke(cli, ["promote-finding", finding_id])
            assert result.exit_code == 0
            assert "Promoted" in result.output
            assert "issue" in result.output
        finally:
            os.chdir(original)

    def test_promote_finding_with_priority(self, initialized_project_with_finding: SeededProject) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_finding.path))
        try:
            finding_id = initialized_project_with_finding.finding_id
            result = runner.invoke(cli, ["promote-finding", finding_id, "--priority", "1", "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["priority"] == 1
        finally:
            os.chdir(original)


# ---------------------------------------------------------------------------
# TestDismissFindingCommand
# ---------------------------------------------------------------------------


class TestDismissFindingCommand:
    def test_dismiss_finding_happy_path_json(self, initialized_project_with_finding: SeededProject) -> None:
        """dismiss-finding returns the updated ScanFindingDict."""
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_finding.path))
        try:
            finding_id = initialized_project_with_finding.finding_id
            result = runner.invoke(cli, ["dismiss-finding", finding_id, "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert set(data.keys()) == _SCAN_FINDING_KEYS, f"Shape mismatch: {set(data.keys()) ^ _SCAN_FINDING_KEYS}"
            assert data["status"] == "false_positive"
        finally:
            os.chdir(original)

    def test_dismiss_finding_not_found_json_error(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["dismiss-finding", "finding-bad", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert "error" in data
        assert data["code"] == "NOT_FOUND"

    def test_dismiss_finding_with_reason(self, initialized_project_with_finding: SeededProject) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_finding.path))
        try:
            finding_id = initialized_project_with_finding.finding_id
            result = runner.invoke(cli, ["dismiss-finding", finding_id, "--reason", "false alarm", "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["status"] == "false_positive"
        finally:
            os.chdir(original)

    def test_dismiss_finding_plain_text(self, initialized_project_with_finding: SeededProject) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_finding.path))
        try:
            finding_id = initialized_project_with_finding.finding_id
            result = runner.invoke(cli, ["dismiss-finding", finding_id])
            assert result.exit_code == 0
            assert "Dismissed" in result.output
        finally:
            os.chdir(original)


# ---------------------------------------------------------------------------
# TestBatchUpdateFindingsCommand
# ---------------------------------------------------------------------------


class TestBatchUpdateFindingsCommand:
    def test_batch_update_all_valid_json(self, initialized_project_with_many_findings: SeededProject) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_many_findings.path))
        try:
            ids = initialized_project_with_many_findings.finding_ids
            result = runner.invoke(cli, ["batch-update-findings", *ids, "--status", "fixed", "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert set(data.keys()) == _BATCH_KEYS, f"Shape mismatch: {set(data.keys()) ^ _BATCH_KEYS}"
            assert len(data["succeeded"]) == len(ids)
            assert data["failed"] == []
        finally:
            os.chdir(original)

    def test_batch_update_mixed_valid_invalid(self, initialized_project_with_finding: SeededProject) -> None:
        """Mixed valid + invalid IDs produce succeeded + failed lists."""
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_finding.path))
        try:
            finding_id = initialized_project_with_finding.finding_id
            result = runner.invoke(
                cli,
                ["batch-update-findings", finding_id, "finding-nonexistent", "--status", "fixed", "--json"],
            )
            # Partial failure exits 1
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert set(data.keys()) == _BATCH_KEYS
            assert finding_id in data["succeeded"]
            assert len(data["failed"]) == 1
            assert data["failed"][0]["id"] == "finding-nonexistent"
            assert data["failed"][0]["code"] == "NOT_FOUND"
        finally:
            os.chdir(original)

    def test_batch_update_all_invalid_returns_error_envelope(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """When all updates fail, returns ErrorResponse (not BatchResponse)."""
        runner, _ = cli_in_project
        result = runner.invoke(
            cli,
            ["batch-update-findings", "finding-bad-1", "finding-bad-2", "--status", "fixed", "--json"],
        )
        assert result.exit_code == 1
        data = json.loads(result.output)
        # Mirror MCP: all-failed → ErrorResponse with "error" key
        assert "error" in data
        assert "code" in data

    def test_batch_update_missing_status_exits_with_usage_error(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """--status is required; missing it should fail."""
        runner, _ = cli_in_project
        result = runner.invoke(cli, ["batch-update-findings", "finding-1"])
        assert result.exit_code != 0

    def test_batch_update_plain_text(self, initialized_project_with_finding: SeededProject) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_finding.path))
        try:
            finding_id = initialized_project_with_finding.finding_id
            result = runner.invoke(cli, ["batch-update-findings", finding_id, "--status", "fixed"])
            assert result.exit_code == 0
            assert "Updated" in result.output
        finally:
            os.chdir(original)


# ---------------------------------------------------------------------------
# Bug-fix regressions: actor propagation and error-code classification.
# ---------------------------------------------------------------------------


class TestPromoteFindingHonoursGlobalActor:
    """`filigree --actor X promote-finding ID` must record actor X.

    Regression: the command previously declared a local ``--actor`` defaulting
    to ``"cli"`` and skipped ``@click.pass_context``, so the validated group
    actor in ``ctx.obj["actor"]`` was silently dropped. (filigree-cb82dc6b37)
    """

    def test_global_actor_is_recorded_on_issue_event(self, initialized_project_with_finding: SeededProject) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_finding.path))
        try:
            finding_id = initialized_project_with_finding.finding_id
            result = runner.invoke(cli, ["--actor", "bot-1", "promote-finding", finding_id, "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            events_result = runner.invoke(cli, ["events", data["issue_id"], "--json"])
            assert events_result.exit_code == 0, events_result.output
            events = json.loads(events_result.output)["items"]
            created_event = next(event for event in events if event["event_type"] == "created")
            assert created_event["actor"] == "bot-1", f"global --actor was dropped; created.actor={created_event['actor']!r}"
        finally:
            os.chdir(original)

    def test_local_actor_overrides_global(self, initialized_project_with_finding: SeededProject) -> None:
        """Command-local ``--actor`` still wins when explicitly supplied."""
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_finding.path))
        try:
            finding_id = initialized_project_with_finding.finding_id
            result = runner.invoke(
                cli,
                [
                    "--actor",
                    "bot-1",
                    "promote-finding",
                    finding_id,
                    "--actor",
                    "bot-2",
                    "--json",
                ],
            )
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            events_result = runner.invoke(cli, ["events", data["issue_id"], "--json"])
            assert events_result.exit_code == 0, events_result.output
            events = json.loads(events_result.output)["items"]
            created_event = next(event for event in events if event["event_type"] == "created")
            assert created_event["actor"] == "bot-2"
        finally:
            os.chdir(original)

    def test_local_actor_is_sanitized(self, initialized_project_with_finding: SeededProject) -> None:
        """A local ``--actor`` with invalid characters must fail validation,
        not be silently written to the audit trail."""
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_finding.path))
        try:
            finding_id = initialized_project_with_finding.finding_id
            result = runner.invoke(
                cli,
                ["promote-finding", finding_id, "--actor", "  ", "--json"],
            )
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["code"] == "VALIDATION"
        finally:
            os.chdir(original)


class TestSqliteErrorClassification:
    """Read commands must classify ``sqlite3.Error`` as ``ErrorCode.IO``,
    not ``VALIDATION``. (filigree-ef5db29b89)"""

    def test_list_files_sqlite_error_is_io(self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch) -> None:
        runner, _ = cli_in_project

        def _raise(*_a: object, **_kw: object) -> None:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr("filigree.core.FiligreeDB.list_files_paginated", _raise)
        result = runner.invoke(cli, ["list-files", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "IO", f"expected IO, got {data['code']}"
        assert "database is locked" in data["error"]

    def test_get_file_timeline_sqlite_error_is_io(self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch) -> None:
        runner, _ = cli_in_project

        def _raise(*_a: object, **_kw: object) -> None:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr("filigree.core.FiligreeDB.get_file_timeline", _raise)
        result = runner.invoke(cli, ["get-file-timeline", "file-anything", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "IO"
        assert "database is locked" in data["error"]

    def test_list_findings_sqlite_error_is_io(self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch) -> None:
        runner, _ = cli_in_project

        def _raise(*_a: object, **_kw: object) -> None:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr("filigree.core.FiligreeDB.list_findings_global", _raise)
        result = runner.invoke(cli, ["list-findings", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "IO"
        assert "database is locked" in data["error"]

    def test_batch_update_findings_all_io_failures_envelope_is_io(
        self,
        initialized_project_with_many_findings: SeededProject,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Regression for filigree-c2aeba2946: when every per-item failure in
        # batch-update-findings is sqlite3.Error (code=IO), the all-failed
        # JSON envelope must surface IO so callers can retry, not VALIDATION.
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_many_findings.path))
        try:
            ids = initialized_project_with_many_findings.finding_ids

            def _raise(*_a: object, **_kw: object) -> None:
                raise sqlite3.OperationalError("database is locked")

            monkeypatch.setattr("filigree.core.FiligreeDB.update_finding", _raise)
            result = runner.invoke(cli, ["batch-update-findings", *ids, "--status", "fixed", "--json"])
            assert result.exit_code == 1, result.output
            data = json.loads(result.output)
            assert "error" in data
            assert "code" in data
            assert data["code"] == "IO", f"expected IO, got {data['code']}"
        finally:
            os.chdir(original)


class TestAssociationLookupErrorEnvelope:
    """Existence-check DB calls in association commands must surface
    ``sqlite3.Error`` as the IO envelope, not as an uncaught traceback.
    (filigree-c7f94428c4)"""

    def test_get_issue_files_sqlite_error_is_io(self, initialized_project_with_bug: SeededProject, monkeypatch: pytest.MonkeyPatch) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_bug.path))
        try:

            def _raise(*_a: object, **_kw: object) -> None:
                raise sqlite3.OperationalError("database is locked")

            monkeypatch.setattr("filigree.core.FiligreeDB.get_issue_files", _raise)
            issue_id = initialized_project_with_bug.bug_id
            result = runner.invoke(cli, ["get-issue-files", issue_id, "--json"])
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["code"] == "IO"
            assert "database is locked" in data["error"]
        finally:
            os.chdir(original)

    def test_add_file_association_get_file_sqlite_error_is_io(
        self, cli_in_project: tuple[CliRunner, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner, _ = cli_in_project

        def _raise(*_a: object, **_kw: object) -> None:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr("filigree.core.FiligreeDB.get_file", _raise)
        result = runner.invoke(cli, ["add-file-association", "file-x", "issue-x", "bug_in", "--json"])
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "IO"
        assert "database is locked" in data["error"]

    def test_add_file_association_get_issue_sqlite_error_is_io(
        self, initialized_project_with_file: SeededProject, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_file.path))
        try:
            file_id = initialized_project_with_file.file_id

            def _raise(*_a: object, **_kw: object) -> None:
                raise sqlite3.OperationalError("database is locked")

            monkeypatch.setattr("filigree.core.FiligreeDB.get_issue", _raise)
            result = runner.invoke(cli, ["add-file-association", file_id, "issue-x", "bug_in", "--json"])
            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["code"] == "IO"
            assert "database is locked" in data["error"]
        finally:
            os.chdir(original)
