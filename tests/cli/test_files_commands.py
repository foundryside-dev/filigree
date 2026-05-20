"""CLI tests for file tracking and finding triage commands.

MCP shape verification (verified against mcp_tools/files.py handlers):

File shapes:
- list-files items: EnrichedFileItem — PublicFileRecord + {summary, associations_count, observation_count}
  PublicFileRecord keys: file_id, path, language, file_type, content_hash, registry_backend,
        first_seen, updated_at, metadata, data_warnings
- get-file: FileDetail — {file, associations, recent_findings, summary, observation_count}
- get-file-timeline: ListResponse of TimelineEntry — {timeline_event_id, type, timestamp, data, typed source ID}
  NOTE: MCP returns raw PaginatedResult; CLI normalizes to ListResponse.
- get-issue-files: ListResponse of IssueFileAssociation
  NOTE: MCP returns raw list; CLI normalizes to ListResponse.
- add-file-association: {"status": "created"}
- register-file: FileRecordDict — {id, path, language, file_type, content_hash, registry_backend,
        first_seen, updated_at, metadata, data_warnings}
- delete-file-record: {status, file_id, deleted_findings, deleted_associations, deleted_file_events, unlinked_observations, actor}

Finding shapes:
- list-findings items: PublicScanFinding
  keys: finding_id, file_id, severity, status, scan_source, rule_id, message, suggestion,
        scan_run_id, line_start, line_end, issue_id, seen_count, created_by, updated_by, first_seen, updated_at,
        last_seen_at, metadata, data_warnings
- get-finding: ScanFindingDict (same)
- update-finding: ScanFindingDict (same)
- promote-finding: PublicIssue — {issue_id, title, status, status_category, priority,
                                  type, parent_id, assignee, claimed_at,
                                  last_heartbeat_at, claim_expires_at, created_at,
                                  updated_at, closed_at, description, notes, fields,
                                  labels, blocks, blocked_by, is_ready, children,
                                  data_warnings}
- dismiss-finding: ScanFindingDict (same as get-finding)
- batch-update-findings: BatchResponse[str] — {succeeded, failed} or error envelope
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

from filigree.cli import cli
from filigree.registry import RegistryUnavailableError, ResolvedFile
from tests._seeds import SeededProject

# ---------------------------------------------------------------------------
# Canonical MCP key-sets
# ---------------------------------------------------------------------------

_FILE_RECORD_KEYS = frozenset(
    {
        "file_id",
        "path",
        "language",
        "file_type",
        "content_hash",
        "registry_backend",
        "created_by",
        "updated_by",
        "first_seen",
        "updated_at",
        "metadata",
        "data_warnings",
    }
)

# EnrichedFileItem = FileRecordDict + extra fields
_ENRICHED_FILE_ITEM_KEYS = _FILE_RECORD_KEYS | frozenset({"summary", "associations_count", "observation_count"})

_FILE_DETAIL_KEYS = frozenset({"file", "associations", "recent_findings", "summary", "observation_count"})

_TIMELINE_ENTRY_BASE_KEYS = frozenset({"timeline_event_id", "type", "timestamp", "data"})
_TIMELINE_FINDING_ENTRY_KEYS = _TIMELINE_ENTRY_BASE_KEYS | frozenset({"finding_id"})
_TIMELINE_ASSOC_ENTRY_KEYS = _TIMELINE_ENTRY_BASE_KEYS | frozenset({"assoc_id"})
_TIMELINE_ISSUE_EVENT_KEYS = _TIMELINE_ENTRY_BASE_KEYS | frozenset({"event_id", "issue_id"})

_ISSUE_FILE_ASSOC_KEYS = frozenset({"assoc_id", "file_id", "issue_id", "assoc_type", "actor", "created_at", "file_path", "file_language"})

_SCAN_FINDING_KEYS = frozenset(
    {
        "finding_id",
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
        "created_by",
        "updated_by",
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
        # Both parent_id and parent_issue_id are emitted (filigree-cb980eee0d, P2.9)
        # to close the cross-tool naming inconsistency.
        "parent_issue_id",
        "assignee",
        "claimed_at",
        "last_heartbeat_at",
        "claim_expires_at",
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
            assert data["file"]["file_id"] == file_id
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
        file_id = json.loads(created.output)["file_id"]

        result = runner.invoke(cli, ["delete-file-record", file_id, "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["status"] == "deleted"
        assert data["file_id"] == file_id
        missing = runner.invoke(cli, ["get-file", file_id, "--json"])
        assert missing.exit_code != 0
        assert json.loads(missing.output)["code"] == "NOT_FOUND"

    def test_delete_file_record_allowed_under_clarion_mode(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, project = cli_in_project
        from filigree.cli_common import get_db

        with get_db() as db:
            file_record = db.register_file("src/delete_clarion_mode.py")

        conf_path = project / ".filigree.conf"
        conf = json.loads(conf_path.read_text())
        conf["registry_backend"] = "clarion"
        conf["clarion"] = {"base_url": "http://clarion.test", "allow_local_fallback": True}
        conf_path.write_text(json.dumps(conf))

        result = runner.invoke(cli, ["delete-file-record", file_record.id, "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["status"] == "deleted"
        assert data["file_id"] == file_record.id

    def test_delete_file_record_refuses_association_without_force(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        created = runner.invoke(cli, ["register-file", "src/linked.py", "--json"])
        file_id = json.loads(created.output)["file_id"]
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
            assert set(entry.keys()) == _TIMELINE_FINDING_ENTRY_KEYS, (
                f"Timeline entry key mismatch: {set(entry.keys()) ^ _TIMELINE_FINDING_ENTRY_KEYS}"
            )
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
        file_id = json.loads(created.output)["file_id"]
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
        with_issue_items = json.loads(with_issue_events.output)["items"]
        association_events = [e for e in with_issue_items if e["type"] == "association_created"]
        assert association_events
        assert set(association_events[0].keys()) == _TIMELINE_ASSOC_ENTRY_KEYS
        issue_events = [e for e in with_issue_items if e["type"] == "issue_event"]
        assert issue_events
        assert set(issue_events[0].keys()) == _TIMELINE_ISSUE_EVENT_KEYS
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

    def test_register_file_displaced_under_clarion_mode(
        self, cli_in_project: tuple[CliRunner, Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        runner, project = cli_in_project
        conf_path = project / ".filigree.conf"
        conf = json.loads(conf_path.read_text())
        conf["registry_backend"] = "clarion"
        # ``allow_local_fallback`` is set so the ADR-014 capability probe at
        # ``FiligreeDB.__init__`` downgrades the http://localhost:9111
        # unreachability to a WARN — the test's intent is to verify the
        # ``register-file`` CLI displaces on a Clarion-mode project, which
        # is a state check independent of whether Clarion is reachable.
        conf["clarion"] = {"base_url": "http://localhost:9111", "allow_local_fallback": True}
        conf_path.write_text(json.dumps(conf))

        with caplog.at_level(logging.WARNING, logger="filigree.cli_commands.files"):
            result = runner.invoke(cli, ["register-file", "src/newfile.py", "--language", "python", "--json"])

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "FILE_REGISTRY_DISPLACED"
        assert "http://localhost:9111/api/v1/files" in data["error"]
        assert "src/newfile.py" in data["error"]
        records = [record for record in caplog.records if record.message == "file_registry_displaced_registration_rejected"]
        assert records
        assert records[0].file_path == "src/newfile.py"
        assert records[0].registry_backend == "clarion"

    def test_register_file_infers_language_json(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, _ = cli_in_project
        py = runner.invoke(cli, ["register-file", "src/inferred.py", "--json"])
        md = runner.invoke(cli, ["register-file", "docs/inferred.md", "--json"])
        unknown = runner.invoke(cli, ["register-file", "tools/inferred.unknownext", "--json"])

        assert py.exit_code == 0, py.output
        assert md.exit_code == 0, md.output
        assert unknown.exit_code == 0, unknown.output
        assert json.loads(py.output)["language"] == "python"
        assert json.loads(md.output)["language"] == "markdown"
        assert json.loads(unknown.output)["language"] == ""

    def test_register_file_idempotent(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        """Registering same path twice returns the same file record."""
        runner, _ = cli_in_project
        result1 = runner.invoke(cli, ["register-file", "src/same.py", "--json"])
        assert result1.exit_code == 0, result1.output
        data1 = json.loads(result1.output)

        result2 = runner.invoke(cli, ["register-file", "src/same.py", "--json"])
        assert result2.exit_code == 0, result2.output
        data2 = json.loads(result2.output)

        assert data1["file_id"] == data2["file_id"]

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
# TestMigrateRegistryCommand
# ---------------------------------------------------------------------------


class TestMigrateRegistryCommand:
    def test_apply_registry_migration_opens_immediate_transaction(self) -> None:
        from filigree.cli_commands.files import _apply_registry_migration

        class FakeCursor:
            rowcount = 1

            def __init__(self, *, one: tuple[int] | None = None, many: list[tuple[object, ...]] | None = None) -> None:
                self.one = one
                self.many = many or []

            def fetchone(self) -> tuple[int] | None:
                return self.one

            def fetchall(self) -> list[tuple[object, ...]]:
                return self.many

        class FakeConnection:
            def __init__(self) -> None:
                self.statements: list[str] = []

            def execute(self, sql: str, params: tuple[object, ...] = ()) -> FakeCursor:
                self.statements.append(sql)
                if sql == "PRAGMA foreign_keys":
                    return FakeCursor(one=(1,))
                if sql == "PRAGMA foreign_key_check":
                    return FakeCursor(many=[])
                return FakeCursor()

            def commit(self) -> None:
                self.statements.append("COMMIT")

            def rollback(self) -> None:
                self.statements.append("ROLLBACK")

        class FakeDB:
            def __init__(self) -> None:
                self.conn = FakeConnection()

        db = FakeDB()

        _apply_registry_migration(db, [])

        assert "BEGIN IMMEDIATE" in db.conn.statements
        assert "BEGIN" not in db.conn.statements

    def test_migrate_registry_execute_manifest_write_failure_leaves_db_unchanged(
        self,
        cli_in_project: tuple[CliRunner, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runner, project = cli_in_project

        from filigree.cli_common import get_db

        with get_db() as db:
            file_record = db.register_file("src/missing_manifest.py")
            old_file_id = file_record.id

        new_file_id = "core:file:migrated@src/missing_manifest.py"

        class FakeClarionRegistry:
            def __init__(self, base_url: str, *, timeout_seconds: float = 5) -> None:
                self.base_url = base_url
                self.timeout_seconds = timeout_seconds

            def resolve_file(self, path: str, *, language: str = "", actor: str = "") -> ResolvedFile:
                return {
                    "file_id": new_file_id,
                    "content_hash": "sha256:migrated",
                    "canonical_path": path,
                    "language": language,
                    "registry_backend": "clarion",
                }

            def is_displaced(self) -> bool:
                return True

        monkeypatch.setattr("filigree.core.ClarionRegistry", FakeClarionRegistry)
        conf_path = project / ".filigree.conf"
        conf = json.loads(conf_path.read_text())
        conf["registry_backend"] = "clarion"
        conf["clarion"] = {"base_url": "http://clarion.test", "allow_local_fallback": True}
        conf_path.write_text(json.dumps(conf))

        manifest = project / "missing-parent" / "registry-migration.json"
        executed = runner.invoke(
            cli,
            ["migrate-registry", "--to", "clarion", "--execute", "--manifest", str(manifest), "--json"],
        )

        assert executed.exit_code == 1
        assert not manifest.exists()
        with get_db() as db:
            assert db.get_file(old_file_id).id == old_file_id
            with pytest.raises(KeyError):
                db.get_file(new_file_id)

    def test_migrate_registry_execute_removes_manifest_on_apply_failure(
        self,
        cli_in_project: tuple[CliRunner, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runner, project = cli_in_project

        from filigree.cli_common import get_db

        with get_db() as db:
            file_record = db.register_file("src/apply_failure.py")
            old_file_id = file_record.id

        new_file_id = "core:file:migrated@src/apply_failure.py"

        class FakeClarionRegistry:
            def __init__(self, base_url: str, *, timeout_seconds: float = 5) -> None:
                self.base_url = base_url
                self.timeout_seconds = timeout_seconds

            def resolve_file(self, path: str, *, language: str = "", actor: str = "") -> ResolvedFile:
                return {
                    "file_id": new_file_id,
                    "content_hash": "sha256:migrated",
                    "canonical_path": path,
                    "language": language,
                    "registry_backend": "clarion",
                }

            def is_displaced(self) -> bool:
                return True

        monkeypatch.setattr("filigree.core.ClarionRegistry", FakeClarionRegistry)
        conf_path = project / ".filigree.conf"
        conf = json.loads(conf_path.read_text())
        conf["registry_backend"] = "clarion"
        conf["clarion"] = {"base_url": "http://clarion.test", "allow_local_fallback": True}
        conf_path.write_text(json.dumps(conf))

        manifest = project / "registry-migration.json"
        observed = {"manifest_seen_by_apply": False}

        def failing_apply_registry_migration(db: object, entries: list[dict[str, object]], *, reverse: bool = False) -> None:
            observed["manifest_seen_by_apply"] = manifest.exists()
            raise sqlite3.IntegrityError("apply failed")

        monkeypatch.setattr("filigree.cli_commands.files._apply_registry_migration", failing_apply_registry_migration)

        executed = runner.invoke(
            cli,
            ["migrate-registry", "--to", "clarion", "--execute", "--manifest", str(manifest), "--json"],
        )

        assert executed.exit_code == 1
        assert observed["manifest_seen_by_apply"] is True
        assert not manifest.exists()
        with get_db() as db:
            assert db.get_file(old_file_id).id == old_file_id
            with pytest.raises(KeyError):
                db.get_file(new_file_id)

    def test_migrate_registry_execute_aborts_on_malformed_scan_run_file_ids(
        self,
        cli_in_project: tuple[CliRunner, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runner, project = cli_in_project

        from filigree.cli_common import get_db

        with get_db() as db:
            file_record = db.register_file("src/malformed_scan_run.py")
            old_file_id = file_record.id
            db.create_scan_run(
                scan_run_id="scan-run-malformed",
                scanner_name="ruff",
                scan_source="ruff",
                file_paths=["src/malformed_scan_run.py"],
                file_ids=[old_file_id],
            )
            db.conn.execute(
                "UPDATE scan_runs SET file_ids = ? WHERE id = ?",
                (json.dumps(old_file_id), "scan-run-malformed"),
            )
            db.conn.commit()

        new_file_id = "core:file:migrated@src/malformed_scan_run.py"

        class FakeClarionRegistry:
            def __init__(self, base_url: str, *, timeout_seconds: float = 5) -> None:
                self.base_url = base_url
                self.timeout_seconds = timeout_seconds

            def resolve_file(self, path: str, *, language: str = "", actor: str = "") -> ResolvedFile:
                return {
                    "file_id": new_file_id,
                    "content_hash": "sha256:migrated",
                    "canonical_path": path,
                    "language": language,
                    "registry_backend": "clarion",
                }

            def is_displaced(self) -> bool:
                return True

        monkeypatch.setattr("filigree.core.ClarionRegistry", FakeClarionRegistry)
        conf_path = project / ".filigree.conf"
        conf = json.loads(conf_path.read_text())
        conf["registry_backend"] = "clarion"
        conf["clarion"] = {"base_url": "http://clarion.test", "allow_local_fallback": True}
        conf_path.write_text(json.dumps(conf))

        dry_run = runner.invoke(cli, ["migrate-registry", "--to", "clarion", "--dry-run", "--json"])
        assert dry_run.exit_code == 0, dry_run.output
        dry_payload = json.loads(dry_run.output)
        assert dry_payload["unresolved"][0]["scan_run_id"] == "scan-run-malformed"
        assert "malformed file_ids" in dry_payload["unresolved"][0]["error"]

        manifest = project / "registry-migration.json"
        executed = runner.invoke(
            cli,
            ["migrate-registry", "--to", "clarion", "--execute", "--manifest", str(manifest), "--json"],
        )

        assert executed.exit_code == 1
        data = json.loads(executed.output)
        assert "1 scan_runs have malformed file_ids" in data["error"]
        assert not manifest.exists()
        with get_db() as db:
            assert db.get_file(old_file_id).id == old_file_id
            with pytest.raises(KeyError):
                db.get_file(new_file_id)

    def test_scan_run_file_id_rewrite_escapes_like_metacharacters(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        from filigree.cli_commands.files import _rewrite_scan_run_file_ids, _scan_run_file_id_rewrite_blockers
        from filigree.cli_common import get_db

        runner, _ = cli_in_project
        assert runner is not None
        now = "2026-01-01T00:00:00+00:00"
        old_file_id = "core:file:abc_def"
        unrelated_like_match = "core:file:abcXdef"
        new_file_id = "core:file:new_def"
        with get_db() as db:
            db.conn.execute(
                "INSERT INTO scan_runs (id, scanner_name, scan_source, file_ids, started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("scan-exact", "ruff", "ruff", json.dumps([old_file_id]), now, now),
            )
            db.conn.execute(
                "INSERT INTO scan_runs (id, scanner_name, scan_source, file_ids, started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("scan-wildcard-overmatch", "ruff", "ruff", f'["{unrelated_like_match}",', now, now),
            )
            db.conn.commit()

            assert _scan_run_file_id_rewrite_blockers(db.conn, old_file_id, "src/a.py") == []
            _rewrite_scan_run_file_ids(db.conn, old_file_id, new_file_id)

            exact = db.conn.execute("SELECT file_ids FROM scan_runs WHERE id = ?", ("scan-exact",)).fetchone()
            overmatch = db.conn.execute("SELECT file_ids FROM scan_runs WHERE id = ?", ("scan-wildcard-overmatch",)).fetchone()
            assert json.loads(exact["file_ids"]) == [new_file_id]
            assert overmatch["file_ids"] == f'["{unrelated_like_match}",'

    def test_migrate_registry_rollback_rejects_non_list_planned_manifest(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        from filigree.cli_commands.files import _registry_manifest_project_identity
        from filigree.cli_common import get_db

        runner, project = cli_in_project
        with get_db() as db:
            project_identity = _registry_manifest_project_identity(db)
        manifest = project / "bad-rollback-manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "version": 1,
                    "to": "clarion",
                    "project": project_identity,
                    "planned": {"old_file_id": "not-a-list"},
                }
            )
        )

        result = runner.invoke(cli, ["migrate-registry", "--rollback", str(manifest), "--json"])

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "VALIDATION"
        assert "planned" in data["error"]

    def test_migrate_registry_rollback_missing_manifest_is_io(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, project = cli_in_project
        missing_manifest = project / "missing-rollback-manifest.json"

        result = runner.invoke(cli, ["migrate-registry", "--rollback", str(missing_manifest), "--json"])

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "IO"
        assert "missing-rollback-manifest.json" in data["error"]

    def test_migrate_registry_execute_aborts_with_unresolved_files(
        self,
        cli_in_project: tuple[CliRunner, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runner, project = cli_in_project

        from filigree.cli_common import get_db

        with get_db() as db:
            file_record = db.register_file("src/unresolved.py")
            old_file_id = file_record.id

        class FailingClarionRegistry:
            def __init__(self, base_url: str, *, timeout_seconds: float = 5) -> None:
                self.base_url = base_url
                self.timeout_seconds = timeout_seconds

            def resolve_file(self, path: str, *, language: str = "", actor: str = "") -> ResolvedFile:
                raise RuntimeError(f"Clarion cannot resolve {path}")

            def is_displaced(self) -> bool:
                return True

        monkeypatch.setattr("filigree.core.ClarionRegistry", FailingClarionRegistry)
        conf_path = project / ".filigree.conf"
        conf = json.loads(conf_path.read_text())
        conf["registry_backend"] = "clarion"
        conf["clarion"] = {"base_url": "http://clarion.test", "allow_local_fallback": True}
        conf_path.write_text(json.dumps(conf))

        manifest = project / "registry-migration.json"
        executed = runner.invoke(
            cli,
            ["migrate-registry", "--to", "clarion", "--execute", "--manifest", str(manifest), "--json"],
        )

        assert executed.exit_code == 1
        data = json.loads(executed.output)
        assert "Cannot execute registry migration with 1 unresolved file(s)" in data["error"]
        assert not manifest.exists()
        with get_db() as db:
            assert db.get_file(old_file_id).id == old_file_id

    def test_migrate_registry_rejects_local_fallback_resolution_under_clarion_target(
        self,
        cli_in_project: tuple[CliRunner, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Migration to ``clarion`` must NOT accept a local-fallback resolution.

        Reproduces P1: when Clarion is unreachable and
        ``allow_local_fallback=true``, the wrapping
        ``_ClarionLocalFallbackRegistry`` silently returns a local
        ``ResolvedFile`` with empty ``content_hash`` and a local-prefix
        ``file_id``. Without the guard in ``_registry_migration_plan``, the
        plan would record those rows as ``new_registry_backend=clarion`` and
        rewrite issue/file associations to local IDs marked as Clarion-backed.
        The guard surfaces such rows as ``unresolved`` so operators can
        diagnose (and the migration aborts cleanly).
        """
        runner, project = cli_in_project

        from filigree.cli_common import get_db

        with get_db() as db:
            file_record = db.register_file("src/fallback_resolves.py")
            old_file_id = file_record.id

        class UnreachableClarionRegistry:
            def __init__(self, base_url: str, *, timeout_seconds: float = 5) -> None:
                self.base_url = base_url
                self.timeout_seconds = timeout_seconds

            def resolve_file(self, path: str, *, language: str = "", actor: str = "") -> ResolvedFile:
                raise RegistryUnavailableError(
                    "stubbed unreachable",
                    url=f"{self.base_url}/api/v1/files",
                    path=path,
                    cause_kind="network",
                )

            def is_displaced(self) -> bool:
                return True

        monkeypatch.setattr("filigree.core.ClarionRegistry", UnreachableClarionRegistry)
        conf_path = project / ".filigree.conf"
        conf = json.loads(conf_path.read_text())
        conf["registry_backend"] = "clarion"
        conf["clarion"] = {"base_url": "http://clarion.test", "allow_local_fallback": True}
        conf_path.write_text(json.dumps(conf))

        # Dry-run surfaces the diagnostic in the unresolved payload.
        dry_run = runner.invoke(
            cli,
            ["migrate-registry", "--to", "clarion", "--dry-run", "--json"],
        )
        assert dry_run.exit_code == 0, dry_run.output
        dry_data = json.loads(dry_run.output)
        unresolved_entries = [entry for entry in dry_data["unresolved"] if entry.get("file_id") == old_file_id]
        assert unresolved_entries, dry_data
        error_message = unresolved_entries[0]["error"]
        assert "allow_local_fallback" in error_message
        assert "registry_backend='local'" in error_message
        assert dry_data["planned"] == []

        # Execute aborts with a non-zero exit before mutating the database.
        manifest = project / "registry-migration.json"
        executed = runner.invoke(
            cli,
            ["migrate-registry", "--to", "clarion", "--execute", "--manifest", str(manifest), "--json"],
        )
        assert executed.exit_code == 1, executed.output
        data = json.loads(executed.output)
        assert "Cannot execute registry migration with 1 unresolved file(s)" in data["error"]
        assert not manifest.exists()
        with get_db() as db:
            assert db.get_file(old_file_id).id == old_file_id
            assert db.get_file(old_file_id).registry_backend == "local"

    def test_migrate_registry_execute_rolls_back_on_target_id_conflict(
        self,
        cli_in_project: tuple[CliRunner, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runner, project = cli_in_project

        from filigree.cli_common import get_db

        with get_db() as db:
            source = db.register_file("src/conflict_a.py")
            existing_target = db.register_file("src/conflict_b.py")

        class ConflictingClarionRegistry:
            def __init__(self, base_url: str, *, timeout_seconds: float = 5) -> None:
                self.base_url = base_url
                self.timeout_seconds = timeout_seconds

            def resolve_file(self, path: str, *, language: str = "", actor: str = "") -> ResolvedFile:
                file_id = existing_target.id if path == "src/conflict_a.py" else f"core:file:{path}"
                return {
                    "file_id": file_id,
                    "content_hash": f"sha256:{path}",
                    "canonical_path": path,
                    "language": language,
                    "registry_backend": "clarion",
                }

            def is_displaced(self) -> bool:
                return True

        monkeypatch.setattr("filigree.core.ClarionRegistry", ConflictingClarionRegistry)
        conf_path = project / ".filigree.conf"
        conf = json.loads(conf_path.read_text())
        conf["registry_backend"] = "clarion"
        conf["clarion"] = {"base_url": "http://clarion.test", "allow_local_fallback": True}
        conf_path.write_text(json.dumps(conf))

        manifest = project / "registry-migration.json"
        executed = runner.invoke(
            cli,
            ["migrate-registry", "--to", "clarion", "--execute", "--manifest", str(manifest), "--json"],
        )

        assert executed.exit_code == 1
        data = json.loads(executed.output)
        assert "target file_id already exists" in data["error"]
        assert not manifest.exists()
        with get_db() as db:
            assert db.get_file(source.id).path == "src/conflict_a.py"
            assert db.get_file(existing_target.id).path == "src/conflict_b.py"

    def test_migrate_registry_rollback_restores_transaction_after_fk_violation(
        self,
        cli_in_project: tuple[CliRunner, Path],
    ) -> None:
        runner, project = cli_in_project

        from filigree.cli_common import get_db

        old_file_id = "filigree-f-localrollback"
        new_file_id = "core:file:rollback@src/rollback_fk.py"
        now = "2026-01-01T00:00:00+00:00"
        with get_db() as db:
            db.conn.execute(
                "INSERT INTO file_records "
                "(id, path, language, content_hash, registry_backend, first_seen, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (new_file_id, "src/rollback_fk.py", "python", "sha256:rollback", "clarion", now, now),
            )
            db.conn.execute(
                "INSERT INTO scan_findings (id, file_id, scan_source, first_seen, updated_at) VALUES (?, ?, ?, ?, ?)",
                ("finding-ok", new_file_id, "ruff", now, now),
            )
            db.conn.commit()
            db.conn.execute("PRAGMA foreign_keys=OFF")
            db.conn.execute(
                "INSERT INTO scan_findings (id, file_id, scan_source, first_seen, updated_at) VALUES (?, ?, ?, ?, ?)",
                ("finding-orphan", "missing-file-id", "ruff", now, now),
            )
            db.conn.commit()
            db.conn.execute("PRAGMA foreign_keys=ON")
            project_identity = {
                "prefix": db.prefix,
                "project_root": str(db.project_root.resolve()) if db.project_root is not None else "",
                "db_path": str(db.db_path.resolve()),
            }

        manifest = project / "registry-migration.json"
        manifest.write_text(
            json.dumps(
                {
                    "version": 1,
                    "to": "clarion",
                    "project": project_identity,
                    "planned": [
                        {
                            "old_file_id": old_file_id,
                            "new_file_id": new_file_id,
                            "old_path": "src/rollback_fk.py",
                            "new_path": "src/rollback_fk.py",
                            "old_language": "python",
                            "new_language": "python",
                            "old_content_hash": "",
                            "new_content_hash": "sha256:rollback",
                            "old_registry_backend": "local",
                            "new_registry_backend": "clarion",
                        }
                    ],
                }
            )
        )

        rolled_back = runner.invoke(cli, ["migrate-registry", "--rollback", str(manifest), "--json"])

        assert rolled_back.exit_code == 1
        data = json.loads(rolled_back.output)
        assert data["code"] == "IO"
        assert "foreign-key violations" in data["error"]
        with get_db() as db:
            assert db.get_file(new_file_id).registry_backend == "clarion"
            with pytest.raises(KeyError):
                db.get_file(old_file_id)
            file_id = db.conn.execute("SELECT file_id FROM scan_findings WHERE id = ?", ("finding-ok",)).fetchone()[0]
            assert file_id == new_file_id

    def test_migrate_registry_execute_requires_matching_project_backend(
        self,
        cli_in_project: tuple[CliRunner, Path],
    ) -> None:
        runner, _ = cli_in_project

        from filigree.cli_common import get_db

        with get_db() as db:
            file_record = db.register_file("src/precondition.py")

        executed = runner.invoke(cli, ["migrate-registry", "--to", "clarion", "--execute", "--json"])

        assert executed.exit_code == 1
        data = json.loads(executed.output)
        assert "Project registry_backend is 'local'; set it to 'clarion' before migration" in data["error"]
        with get_db() as db:
            assert db.get_file(file_record.id).registry_backend == "local"

    def test_migrate_registry_rollback_rejects_wrong_project_manifest(self, cli_in_project: tuple[CliRunner, Path]) -> None:
        runner, project = cli_in_project
        manifest = project / "wrong-project-registry-migration.json"
        manifest.write_text(
            json.dumps(
                {
                    "version": 1,
                    "to": "clarion",
                    "project": {
                        "prefix": "other",
                        "project_root": str(project.resolve()),
                        "db_path": str((project / ".filigree" / "filigree.db").resolve()),
                    },
                    "planned": [],
                }
            )
        )

        rolled_back = runner.invoke(cli, ["migrate-registry", "--rollback", str(manifest), "--json"])

        assert rolled_back.exit_code == 1
        payload = json.loads(rolled_back.output)
        assert payload["code"] == "VALIDATION"
        assert "Rollback manifest project identity does not match" in payload["error"]

    def test_migrate_registry_default_manifest_path_is_project_root_absolute(
        self,
        cli_in_project: tuple[CliRunner, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runner, project = cli_in_project

        from filigree.cli_common import get_db

        with get_db() as db:
            db.register_file("src/default_manifest.py")

        class FakeClarionRegistry:
            def __init__(self, base_url: str, *, timeout_seconds: float = 5) -> None:
                self.base_url = base_url
                self.timeout_seconds = timeout_seconds

            def resolve_file(self, path: str, *, language: str = "", actor: str = "") -> ResolvedFile:
                return {
                    "file_id": f"core:file:migrated@{path}",
                    "content_hash": "sha256:migrated",
                    "canonical_path": path,
                    "language": language,
                    "registry_backend": "clarion",
                }

            def is_displaced(self) -> bool:
                return True

        monkeypatch.setattr("filigree.core.ClarionRegistry", FakeClarionRegistry)
        conf_path = project / ".filigree.conf"
        conf = json.loads(conf_path.read_text())
        conf["registry_backend"] = "clarion"
        conf["clarion"] = {"base_url": "http://clarion.test", "allow_local_fallback": True}
        conf_path.write_text(json.dumps(conf))
        subdir = project / "nested"
        subdir.mkdir()
        monkeypatch.chdir(subdir)

        executed = runner.invoke(cli, ["migrate-registry", "--to", "clarion", "--execute", "--json"])

        assert executed.exit_code == 0, executed.output
        payload = json.loads(executed.output)
        manifest_path = Path(payload["manifest_path"])
        assert manifest_path.is_absolute()
        assert manifest_path.parent == project.resolve()
        assert manifest_path.exists()

    def _seed_migrate_registry_project(
        self,
        cli_in_project: tuple[CliRunner, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> tuple[CliRunner, Path, str, str, Path]:
        runner, project = cli_in_project
        source_path = project / "src" / "migrate.py"
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text("print('migration')\n")

        from filigree.cli_common import get_db

        with get_db() as db:
            file_record = db.register_file("src/migrate.py", metadata={"owner": "before"})
            old_file_id = file_record.id
            issue = db.create_issue("Bug in migrated file")
            db.add_file_association(old_file_id, issue.id, "bug_in")
            db.create_observation("Active observation", file_path="src/migrate.py")
            linked_observation = db.create_observation("Linked observation", file_path="src/migrate.py")
            db.link_observation_to_issue(linked_observation["id"], issue.id)
            db.annotate_file("src/migrate.py", "Migration annotation", line_start=1, line_end=1)
            db.create_scan_run(
                scan_run_id="scan-run-migrate",
                scanner_name="ruff",
                scan_source="ruff",
                file_paths=["src/migrate.py"],
                file_ids=[old_file_id],
            )
            db.process_scan_results(
                scan_source="ruff",
                findings=[{"path": "src/migrate.py", "rule_id": "E501", "severity": "low", "message": "msg"}],
            )
            db.register_file("src/migrate.py", metadata={"owner": "after"})

        new_file_id = "core:file:migrated@src/migrate.py"

        class FakeClarionRegistry:
            def __init__(self, base_url: str, *, timeout_seconds: float = 5) -> None:
                self.base_url = base_url
                self.timeout_seconds = timeout_seconds

            def resolve_file(self, path: str, *, language: str = "", actor: str = "") -> ResolvedFile:
                return {
                    "file_id": new_file_id,
                    "content_hash": "sha256:migrated",
                    "canonical_path": path,
                    "language": language,
                    "registry_backend": "clarion",
                }

            def is_displaced(self) -> bool:
                return True

        monkeypatch.setattr("filigree.core.ClarionRegistry", FakeClarionRegistry)
        conf_path = project / ".filigree.conf"
        conf = json.loads(conf_path.read_text())
        conf["registry_backend"] = "clarion"
        conf["clarion"] = {"base_url": "http://clarion.test", "allow_local_fallback": True}
        conf_path.write_text(json.dumps(conf))

        return runner, project, old_file_id, new_file_id, project / "registry-migration.json"

    def _assert_migration_references(self, db: object, file_id: str) -> None:
        assert db.conn.execute("SELECT COUNT(*) FROM scan_findings WHERE file_id = ?", (file_id,)).fetchone()[0] == 1
        assert db.conn.execute("SELECT COUNT(*) FROM file_associations WHERE file_id = ?", (file_id,)).fetchone()[0] == 1
        assert db.conn.execute("SELECT COUNT(*) FROM file_events WHERE file_id = ?", (file_id,)).fetchone()[0] >= 1
        assert db.conn.execute("SELECT COUNT(*) FROM observations WHERE file_id = ?", (file_id,)).fetchone()[0] == 1
        assert db.conn.execute("SELECT COUNT(*) FROM observation_links WHERE file_id = ?", (file_id,)).fetchone()[0] == 1
        assert db.conn.execute("SELECT COUNT(*) FROM annotations WHERE file_id = ?", (file_id,)).fetchone()[0] == 1
        scan_run = db.get_scan_run("scan-run-migrate")
        assert scan_run["file_ids"] == [file_id]

    def test_migrate_registry_dry_run_plans_without_rewriting(
        self,
        cli_in_project: tuple[CliRunner, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runner, _project, old_file_id, new_file_id, _manifest = self._seed_migrate_registry_project(cli_in_project, monkeypatch)
        from filigree.cli_common import get_db

        dry_run = runner.invoke(cli, ["migrate-registry", "--to", "clarion", "--dry-run", "--json"])
        assert dry_run.exit_code == 0, dry_run.output
        dry_payload = json.loads(dry_run.output)
        assert dry_payload["mode"] == "dry-run"
        assert dry_payload["planned"][0]["old_file_id"] == old_file_id
        assert dry_payload["planned"][0]["new_file_id"] == new_file_id
        with get_db() as db:
            assert db.get_file(old_file_id).id == old_file_id

    def test_migrate_registry_execute_rewrites_file_identity_and_manifest(
        self,
        cli_in_project: tuple[CliRunner, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runner, project, old_file_id, new_file_id, manifest = self._seed_migrate_registry_project(cli_in_project, monkeypatch)
        from filigree.cli_common import get_db

        executed = runner.invoke(
            cli,
            ["migrate-registry", "--to", "clarion", "--execute", "--manifest", str(manifest), "--json"],
        )
        assert executed.exit_code == 0, executed.output
        execute_payload = json.loads(executed.output)
        assert execute_payload["mode"] == "execute"
        assert execute_payload["manifest_path"] == str(manifest)
        assert execute_payload["project"]["prefix"]
        assert execute_payload["project"]["project_root"] == str(project.resolve())
        assert manifest.exists()
        manifest_payload = json.loads(manifest.read_text())
        assert manifest_payload["project"] == execute_payload["project"]

        with get_db() as db:
            assert db.get_file(new_file_id).registry_backend == "clarion"
            with pytest.raises(KeyError):
                db.get_file(old_file_id)
            self._assert_migration_references(db, new_file_id)

    def test_migrate_registry_rollback_restores_file_identity_references(
        self,
        cli_in_project: tuple[CliRunner, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runner, _project, old_file_id, new_file_id, manifest = self._seed_migrate_registry_project(cli_in_project, monkeypatch)
        from filigree.cli_common import get_db

        executed = runner.invoke(
            cli,
            ["migrate-registry", "--to", "clarion", "--execute", "--manifest", str(manifest), "--json"],
        )
        assert executed.exit_code == 0, executed.output

        rolled_back = runner.invoke(cli, ["migrate-registry", "--rollback", str(manifest), "--json"])
        assert rolled_back.exit_code == 0, rolled_back.output
        rollback_payload = json.loads(rolled_back.output)
        assert rollback_payload["mode"] == "rollback"

        with get_db() as db:
            assert db.get_file(old_file_id).registry_backend == "local"
            with pytest.raises(KeyError):
                db.get_file(new_file_id)
            self._assert_migration_references(db, old_file_id)


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
            assert data["finding_id"] == finding_id
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

    def test_update_finding_refreshes_context_md(
        self,
        initialized_project_with_finding: SeededProject,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_finding.path))
        try:
            calls: list[tuple[object, object]] = []

            def spy(db: object, path: object) -> None:
                calls.append((db, path))

            monkeypatch.setattr("filigree.cli_common.write_summary", spy)
            finding_id = initialized_project_with_finding.finding_id
            result = runner.invoke(cli, ["update-finding", finding_id, "--status", "fixed", "--json"])
            assert result.exit_code == 0, result.output
            assert calls, "update-finding must refresh context.md after a successful mutation"
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

    def test_promote_finding_refreshes_context_md(
        self,
        initialized_project_with_finding: SeededProject,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_finding.path))
        try:
            calls: list[tuple[object, object]] = []

            def spy(db: object, path: object) -> None:
                calls.append((db, path))

            monkeypatch.setattr("filigree.cli_common.write_summary", spy)
            finding_id = initialized_project_with_finding.finding_id
            result = runner.invoke(cli, ["promote-finding", finding_id, "--json"])
            assert result.exit_code == 0, result.output
            assert calls, "promote-finding must refresh context.md after creating an issue"
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

    def test_dismiss_finding_accepts_status_and_reason(self, initialized_project_with_finding: SeededProject) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_finding.path))
        try:
            finding_id = initialized_project_with_finding.finding_id
            result = runner.invoke(cli, ["dismiss-finding", finding_id, "--status", "fixed", "--reason", "verified fixed", "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["status"] == "fixed"
            assert data["metadata"]["dismiss_reason"] == "verified fixed"
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

    def test_dismiss_finding_refreshes_context_md(
        self,
        initialized_project_with_finding: SeededProject,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_finding.path))
        try:
            calls: list[tuple[object, object]] = []

            def spy(db: object, path: object) -> None:
                calls.append((db, path))

            monkeypatch.setattr("filigree.cli_common.write_summary", spy)
            finding_id = initialized_project_with_finding.finding_id
            result = runner.invoke(cli, ["dismiss-finding", finding_id, "--json"])
            assert result.exit_code == 0, result.output
            assert calls, "dismiss-finding must refresh context.md after a successful mutation"
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

    def test_batch_update_refreshes_context_md_after_success(
        self,
        initialized_project_with_many_findings: SeededProject,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_many_findings.path))
        try:
            calls: list[tuple[object, object]] = []

            def spy(db: object, path: object) -> None:
                calls.append((db, path))

            monkeypatch.setattr("filigree.cli_common.write_summary", spy)
            ids = initialized_project_with_many_findings.finding_ids
            result = runner.invoke(cli, ["batch-update-findings", *ids, "--status", "fixed", "--json"])
            assert result.exit_code == 0, result.output
            assert calls, "batch-update-findings must refresh context.md after successful updates"
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

    def test_global_actor_is_recorded_on_finding_update(self, initialized_project_with_finding: SeededProject) -> None:
        runner = CliRunner()
        original = os.getcwd()
        os.chdir(str(initialized_project_with_finding.path))
        try:
            finding_id = initialized_project_with_finding.finding_id
            result = runner.invoke(cli, ["--actor", "triager-1", "update-finding", finding_id, "--status", "acknowledged", "--json"])
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["finding_id"] == finding_id
            assert data["updated_by"] == "triager-1"
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
