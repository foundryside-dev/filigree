"""MCP server contract tests — test all 20 tools via call_tool()."""

from __future__ import annotations

import asyncio
import builtins
import json
import logging
import sqlite3
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from filigree.core import DB_FILENAME, FILIGREE_DIR_NAME, SUMMARY_FILENAME, FiligreeDB, write_config
from filigree.db_schema import CURRENT_SCHEMA_VERSION
from filigree.mcp_server import (  # type: ignore[attr-defined]
    _MAX_LIST_RESULTS,
    _safe_path,
    _text,
    call_tool,
    create_mcp_app,
    get_workflow_prompt,
    list_resources,
    list_tools,
    read_context,
)
from filigree.types.api import ErrorCode
from tests.mcp._helpers import _parse


class TestCreateAndGet:
    async def test_create_issue(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("create_issue", {"title": "MCP test issue"})
        data = _parse(result)
        assert data["title"] == "MCP test issue"
        assert data["issue_id"].startswith("mcp-")

    async def test_create_issue_full(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool(
            "create_issue",
            {
                "title": "Full MCP issue",
                "type": "bug",
                "priority": 1,
                "description": "A bug",
                "notes": "Notes here",
                "fields": {"severity": "critical"},
                "labels": ["urgent"],
            },
        )
        data = _parse(result)
        assert data["type"] == "bug"
        assert data["priority"] == 1
        assert data["fields"]["severity"] == "critical"
        assert "urgent" in data["labels"]

    async def test_get_issue(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Get me")
        result = await call_tool("get_issue", {"issue_id": issue.id})
        data = _parse(result)
        assert data["title"] == "Get me"

    async def test_get_issue_not_found(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("get_issue", {"issue_id": "mcp-nonexistent"})
        data = _parse(result)
        assert data["code"] == ErrorCode.NOT_FOUND

    async def test_get_issue_file_lookup_failure_propagates(self, mcp_db: FiligreeDB) -> None:
        """filigree-c6c7842661: get_issue must not convert sqlite3.Error into files=[]."""
        import sqlite3

        issue = mcp_db.create_issue("File lookup will fail")

        def _raise(issue_id: str) -> list[Any]:
            raise sqlite3.OperationalError("no such table: file_associations")

        with patch.object(mcp_db, "get_issue_files", side_effect=_raise), pytest.raises(sqlite3.OperationalError):
            # include_files=True is required post-Phase D4: the default flipped to False
            await call_tool("get_issue", {"issue_id": issue.id, "include_files": True})

    async def test_get_issue_without_files_skips_lookup(self, mcp_db: FiligreeDB) -> None:
        """include_files=False must not call get_issue_files at all."""
        issue = mcp_db.create_issue("Skip file lookup")
        with patch.object(mcp_db, "get_issue_files") as mocked:
            result = await call_tool("get_issue", {"issue_id": issue.id, "include_files": False})
        mocked.assert_not_called()
        data = _parse(result)
        assert "files" not in data

    async def test_get_issue_include_files_uses_assoc_id(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Issue with file")
        file_record = mcp_db.register_file("src/linked.py", language="python")
        mcp_db.add_file_association(file_record.id, issue.id, "mentioned_in")

        result = await call_tool("get_issue", {"issue_id": issue.id, "include_files": True})

        data = _parse(result)
        assert data["files"][0]["assoc_id"]
        assert "id" not in data["files"][0]
        assert data["files"][0]["file_id"] == file_record.id


class TestRefreshSummaryBestEffort:
    async def test_mutation_succeeds_when_summary_refresh_fails(self, mcp_db: FiligreeDB) -> None:
        """If _refresh_summary() raises, the mutation result should still be returned."""
        with patch("filigree.mcp_server.write_summary", side_effect=OSError("disk full")):
            result = await call_tool("create_issue", {"title": "Should succeed"})
        data = _parse(result)
        # Mutation must succeed — the issue was created in the DB
        assert "issue_id" in data
        assert data["title"] == "Should succeed"
        # Verify it's actually in the DB
        issue = mcp_db.get_issue(data["issue_id"])
        assert issue.title == "Should succeed"


class TestListAndSearch:
    async def test_list_issues(self, mcp_db: FiligreeDB) -> None:
        mcp_db.create_issue("List A")
        mcp_db.create_issue("List B")
        result = await call_tool("list_issues", {})
        data = _parse(result)
        # +1 for the auto-seeded "Future" release singleton
        assert len(data["items"]) == 3
        assert data["has_more"] is False

    async def test_list_issues_filter(self, mcp_db: FiligreeDB) -> None:
        mcp_db.create_issue("Open one")
        b = mcp_db.create_issue("Close one")
        mcp_db.close_issue(b.id)
        result = await call_tool("list_issues", {"status": "open"})
        data = _parse(result)
        # +1 for the auto-seeded "Future" release (status "planning" is in
        # the "open" category, so it matches the status="open" filter)
        assert len(data["items"]) == 2

    async def test_list_issues_sort_by_updated_at_desc(self, mcp_db: FiligreeDB) -> None:
        older = mcp_db.create_issue("Older task", type="task", priority=2)
        newer = mcp_db.create_issue("Newer task", type="task", priority=2)
        mcp_db.conn.execute(
            "UPDATE issues SET updated_at = ? WHERE id = ?",
            ("2026-01-01T00:00:00+00:00", older.id),
        )
        mcp_db.conn.execute(
            "UPDATE issues SET updated_at = ? WHERE id = ?",
            ("2026-02-01T00:00:00+00:00", newer.id),
        )
        mcp_db.conn.commit()

        result = await call_tool("list_issues", {"type": "task", "sort_by": "updated_at", "direction": "desc", "limit": 2})
        data = _parse(result)

        assert [item["issue_id"] for item in data["items"]] == [newer.id, older.id]

    async def test_list_issues_rejects_invalid_sort_by(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("list_issues", {"sort_by": "title"})
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION

    async def test_search(self, mcp_db: FiligreeDB) -> None:
        mcp_db.create_issue("Authentication bug")
        mcp_db.create_issue("Something else")
        result = await call_tool("search_issues", {"query": "auth"})
        data = _parse(result)
        assert len(data["items"]) == 1
        assert data["items"][0]["title"] == "Authentication bug"
        assert data["has_more"] is False


class TestPublicIssueVocabulary:
    async def test_create_issue_uses_issue_id(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("create_issue", {"title": "MCP public"})
        data = _parse(result)
        assert data["issue_id"].startswith("mcp-")
        assert "id" not in data

    async def test_get_issue_uses_issue_id(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("MCP get public")
        result = await call_tool("get_issue", {"issue_id": issue.id})
        data = _parse(result)
        assert data["issue_id"] == issue.id
        assert "id" not in data

    async def test_list_issues_uses_issue_id(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("MCP list public")
        result = await call_tool("list_issues", {"type": "task"})
        data = _parse(result)
        item = next(i for i in data["items"] if i["title"] == issue.title)
        assert item["issue_id"] == issue.id
        assert "id" not in item

    async def test_start_next_work_uses_issue_id(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("MCP start next public", priority=0)
        result = await call_tool("start_next_work", {"assignee": "bot", "type": "task"})
        data = _parse(result)
        assert data["issue_id"] == issue.id
        assert "id" not in data

    async def test_undo_last_nested_issue_uses_issue_id(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("MCP undo public")
        await call_tool("update_issue", {"issue_id": issue.id, "title": "Changed title"})

        result = await call_tool("undo_last", {"issue_id": issue.id})

        data = _parse(result)
        assert data["undone"] is True
        assert data["issue_id"] == issue.id
        assert "issue" not in data
        assert "id" not in data
        assert data["title"] == "MCP undo public"


class TestToolDescriptions:
    async def test_ready_tool_descriptions_name_open_category(self) -> None:
        tools = {tool.name: tool for tool in await list_tools()}

        get_ready_description = tools["work_ready"].description or ""
        assert "open category" in get_ready_description
        assert "include_context" in get_ready_description
        assert "(open, no blockers)" not in get_ready_description

        get_stats_description = tools["stats_get"].description or ""
        assert "status_name_counts" in get_stats_description
        assert "status_category_counts" in get_stats_description

        for tool_name in ("work_claim_next", "work_start_next"):
            description = tools[tool_name].description or ""
            assert "open-category ready issue" in description

    async def test_requirement_type_descriptions_name_optional_pack(self) -> None:
        tools = {tool.name: tool for tool in await list_tools()}

        for tool_name in ("issue_create", "observation_promote", "observation_batch_promote"):
            schema = tools[tool_name].inputSchema
            assert isinstance(schema, dict)
            properties = schema.get("properties", {})
            assert isinstance(properties, dict)
            type_property = properties["type"]
            assert isinstance(type_property, dict)
            description = str(type_property.get("description", "")).lower()
            assert "requirement" in description
            assert "requirements pack" in description


class TestListPagination:
    """Pagination cap and has_more for list_issues / search_issues."""

    async def test_list_issues_capped(self, mcp_db: FiligreeDB) -> None:
        """Default cap limits results to _MAX_LIST_RESULTS."""
        for i in range(_MAX_LIST_RESULTS + 5):
            mcp_db.create_issue(f"Issue {i}")
        result = await call_tool("list_issues", {})
        data = _parse(result)
        assert len(data["items"]) == _MAX_LIST_RESULTS
        assert data["has_more"] is True
        assert data["next_offset"] == _MAX_LIST_RESULTS

    async def test_list_issues_no_limit(self, mcp_db: FiligreeDB) -> None:
        """no_limit=true bypasses the cap."""
        for i in range(_MAX_LIST_RESULTS + 5):
            mcp_db.create_issue(f"Issue {i}")
        result = await call_tool("list_issues", {"no_limit": True})
        data = _parse(result)
        # +1 for the auto-seeded "Future" release singleton
        assert len(data["items"]) == _MAX_LIST_RESULTS + 5 + 1
        assert data["has_more"] is False

    async def test_list_issues_no_limit_with_explicit_limit_has_more(self, mcp_db: FiligreeDB) -> None:
        """no_limit=true with explicit limit should compute has_more correctly."""
        for i in range(10):
            mcp_db.create_issue(f"Issue {i}")
        result = await call_tool("list_issues", {"no_limit": True, "limit": 5})
        data = _parse(result)
        assert len(data["items"]) == 5
        assert data["has_more"] is True
        assert data["next_offset"] == 5

    async def test_list_issues_offset(self, mcp_db: FiligreeDB) -> None:
        """Offset works with the capped limit."""
        for i in range(_MAX_LIST_RESULTS + 10):
            mcp_db.create_issue(f"Issue {i}")
        result = await call_tool("list_issues", {"offset": _MAX_LIST_RESULTS})
        data = _parse(result)
        # +1 for the auto-seeded "Future" release singleton
        assert len(data["items"]) == 11
        assert data["has_more"] is False
        assert "next_offset" not in data

    async def test_list_issues_requested_limit_below_cap(self, mcp_db: FiligreeDB) -> None:
        """Explicit limit below _MAX_LIST_RESULTS is respected."""
        for i in range(10):
            mcp_db.create_issue(f"Issue {i}")
        result = await call_tool("list_issues", {"limit": 3})
        data = _parse(result)
        assert len(data["items"]) == 3
        assert data["has_more"] is True
        assert data["next_offset"] == 3

    async def test_search_issues_capped(self, mcp_db: FiligreeDB) -> None:
        """search_issues respects the same cap."""
        for i in range(_MAX_LIST_RESULTS + 5):
            mcp_db.create_issue(f"Bug {i}")
        result = await call_tool("search_issues", {"query": "Bug"})
        data = _parse(result)
        assert len(data["items"]) == _MAX_LIST_RESULTS
        assert data["has_more"] is True

    async def test_search_issues_no_limit(self, mcp_db: FiligreeDB) -> None:
        """search_issues with no_limit=true bypasses the cap."""
        for i in range(_MAX_LIST_RESULTS + 5):
            mcp_db.create_issue(f"Bug {i}")
        result = await call_tool("search_issues", {"query": "Bug", "no_limit": True})
        data = _parse(result)
        assert len(data["items"]) == _MAX_LIST_RESULTS + 5
        assert data["has_more"] is False

    async def test_search_issues_no_limit_with_explicit_limit_has_more(self, mcp_db: FiligreeDB) -> None:
        """search_issues no_limit=true with explicit limit computes has_more correctly."""
        for i in range(10):
            mcp_db.create_issue(f"Bug {i}")
        result = await call_tool("search_issues", {"query": "Bug", "no_limit": True, "limit": 5})
        data = _parse(result)
        assert len(data["items"]) == 5
        assert data["has_more"] is True


class TestUpdateAndClose:
    async def test_update_issue(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Update me")
        result = await call_tool("update_issue", {"issue_id": issue.id, "status": "in_progress"})
        data = _parse(result)
        assert data["status"] == "in_progress"

    async def test_update_issue_defaults_expected_assignee_to_actor_for_held_issue(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Claim-aware update")
        mcp_db.claim_issue(issue.id, assignee="agent-holder")

        result = await call_tool("update_issue", {"issue_id": issue.id, "priority": 0, "actor": "other-agent"})

        data = _parse(result)
        assert data["code"] == ErrorCode.CONFLICT
        assert "assigned to 'agent-holder'" in data["error"]
        assert "expected 'other-agent'" in data["error"]
        assert data["details"] == {"issue_id": issue.id, "observed": "agent-holder", "expected": "other-agent"}

    async def test_update_issue_force_overwrite_corrupt_fields(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("MCP corrupt fields")
        mcp_db.conn.execute("UPDATE issues SET fields = ? WHERE id = ?", (b"\xff\xfe", issue.id))
        mcp_db.conn.commit()

        result = await call_tool(
            "update_issue",
            {
                "issue_id": issue.id,
                "fields": {"restored": True},
                "force_overwrite_corrupt": True,
            },
        )

        data = _parse(result)
        assert data["fields"] == {"restored": True}

    async def test_update_issue_explicit_expected_assignee_overrides_actor_default(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Claim-aware update")
        mcp_db.claim_issue(issue.id, assignee="agent-holder")

        result = await call_tool(
            "update_issue",
            {
                "issue_id": issue.id,
                "priority": 0,
                "actor": "coordinator",
                "expected_assignee": "agent-holder",
            },
        )

        data = _parse(result)
        assert data["priority"] == 0

    async def test_update_issue_surfaces_soft_transition_warning_once(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Warn me", type="bug")

        result = await call_tool("update_issue", {"issue_id": issue.id, "status": "confirmed"})

        data = _parse(result)
        assert data["status"] == "confirmed"
        assert data["data_warnings"] == ["Missing recommended fields for 'confirmed': severity"]
        warning_events = [event for event in mcp_db.get_issue_events(issue.id) if event["event_type"] == "transition_warning"]
        assert [event["comment"] for event in warning_events] == data["data_warnings"]

    async def test_update_not_found(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("update_issue", {"issue_id": "mcp-nonexistent", "title": "nope"})
        data = _parse(result)
        assert data["code"] == ErrorCode.NOT_FOUND

    async def test_close_issue(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Close me")
        result = await call_tool("close_issue", {"issue_id": issue.id, "reason": "done"})
        data = _parse(result)
        assert data["status"] == "closed"

    async def test_close_invalid_transition_includes_valid_transitions(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Invalid close", type="bug")
        result = await call_tool("close_issue", {"issue_id": issue.id, "reason": "cleanup"})
        data = _parse(result)
        assert data["code"] == ErrorCode.INVALID_TRANSITION
        assert {t["to"] for t in data["valid_transitions"]} == {"confirmed", "wont_fix", "not_a_bug"}

    async def test_close_not_found(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("close_issue", {"issue_id": "mcp-nonexistent"})
        data = _parse(result)
        assert data["code"] == ErrorCode.NOT_FOUND


class TestDeleteIssue:
    async def test_delete_issue_tool_registered(self, mcp_db: FiligreeDB) -> None:
        tools = {tool.name: tool for tool in await list_tools()}
        assert "issue_delete" in tools
        schema = tools["issue_delete"].inputSchema
        assert set(schema["properties"]) == {"issue_id", "force", "actor"}
        assert schema["required"] == ["issue_id"]
        assert "IRREVERSIBLE" in tools["issue_delete"].description
        assert "undo_last cannot reverse" in tools["issue_delete"].description

    async def test_delete_success_envelope(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Delete me")
        mcp_db.close_issue(issue.id)
        result = await call_tool("delete_issue", {"issue_id": issue.id, "actor": "alice"})
        data = _parse(result)
        assert data["status"] == "deleted"
        assert data["issue_id"] == issue.id
        assert data["actor"] == "alice"
        assert "deleted_events" in data
        # Tombstone exists.
        row = mcp_db.conn.execute("SELECT * FROM deleted_issues WHERE issue_id = ?", (issue.id,)).fetchone()
        assert row is not None

    async def test_delete_not_found(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("delete_issue", {"issue_id": "mcp-nonexistent"})
        data = _parse(result)
        assert data["code"] == ErrorCode.NOT_FOUND

    async def test_delete_non_terminal_conflict(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Still open")
        result = await call_tool("delete_issue", {"issue_id": issue.id})
        data = _parse(result)
        assert data["code"] == ErrorCode.CONFLICT

    async def test_delete_has_children_conflict(self, mcp_db: FiligreeDB) -> None:
        parent = mcp_db.create_issue("Parent")
        mcp_db.create_issue("Child", parent_id=parent.id)
        mcp_db.close_issue(parent.id)
        result = await call_tool("delete_issue", {"issue_id": parent.id})
        data = _parse(result)
        assert data["code"] == ErrorCode.CONFLICT

    async def test_delete_force_orphans_children(self, mcp_db: FiligreeDB) -> None:
        parent = mcp_db.create_issue("Parent")
        child = mcp_db.create_issue("Child", parent_id=parent.id)
        mcp_db.close_issue(parent.id)
        result = await call_tool("delete_issue", {"issue_id": parent.id, "force": True})
        data = _parse(result)
        assert data["status"] == "deleted"
        assert data["orphaned_children"] == 1
        assert mcp_db.get_issue(child.id).parent_id is None

    async def test_delete_force_bool_validation(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("x")
        mcp_db.close_issue(issue.id)
        result = await call_tool("delete_issue", {"issue_id": issue.id, "force": "yes"})
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION


class TestDependencies:
    async def test_add_dependency(self, mcp_db: FiligreeDB) -> None:
        a = mcp_db.create_issue("Blocked")
        b = mcp_db.create_issue("Blocker")
        result = await call_tool("add_dependency", {"from_issue_id": a.id, "to_issue_id": b.id})
        data = _parse(result)
        assert data["issue_id"] == a.id
        assert data["dependency_result"] == "added"
        assert data["dependency"] == {"from_issue_id": a.id, "to_issue_id": b.id}
        assert b.id in data["blocked_by"]
        assert "from_id" not in data
        assert "to_id" not in data

    async def test_add_dependency_cycle(self, mcp_db: FiligreeDB) -> None:
        a = mcp_db.create_issue("A")
        b = mcp_db.create_issue("B")
        mcp_db.add_dependency(a.id, b.id)
        result = await call_tool("add_dependency", {"from_issue_id": b.id, "to_issue_id": a.id})
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION

    async def test_remove_dependency(self, mcp_db: FiligreeDB) -> None:
        a = mcp_db.create_issue("A")
        b = mcp_db.create_issue("B")
        mcp_db.add_dependency(a.id, b.id)
        result = await call_tool("remove_dependency", {"from_issue_id": a.id, "to_issue_id": b.id})
        data = _parse(result)
        assert data["issue_id"] == a.id
        assert data["dependency_result"] == "removed"
        assert data["dependency"] == {"from_issue_id": a.id, "to_issue_id": b.id}
        assert b.id not in data["blocked_by"]


class TestReadyAndBlocked:
    async def test_get_ready(self, mcp_db: FiligreeDB) -> None:
        mcp_db.create_issue("Ready one")
        result = await call_tool("get_ready", {})
        data = _parse(result)
        # +1 for the auto-seeded "Future" release singleton (status "planning"
        # is in the "open" category, so it counts as ready)
        assert len(data["items"]) == 2
        titles = {d["title"] for d in data["items"]}
        assert "Ready one" in titles

    async def test_get_ready_excludes_assigned_issue(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Claimed ready one")
        mcp_db.claim_issue(issue.id, assignee="agent-1")

        result = await call_tool("get_ready", {})

        data = _parse(result)
        ids = {d["issue_id"] for d in data["items"]}
        assert issue.id not in ids

    async def test_get_ready_shape(self, mcp_db: FiligreeDB) -> None:
        """get_ready returns the slim shape plus the startability flag.

        ``startable`` is always present (filigree-406e6b7ee0); ``next_action``
        is omitted for a single-hop-startable item to keep the shape slim.
        """
        task = mcp_db.create_issue("Ready", type="task")
        result = await call_tool("get_ready", {})
        data = _parse(result)
        item = next(i for i in data["items"] if i["issue_id"] == task.id)
        expected_keys = {"issue_id", "title", "status", "priority", "type", "startable"}
        assert set(item.keys()) == expected_keys
        assert item["startable"] is True

    async def test_get_ready_marks_triage_bug_not_startable(self, mcp_db: FiligreeDB) -> None:
        """filigree-406e6b7ee0: a triage bug is ready but not single-hop startable.

        It carries ``startable: false`` and ``next_action`` pointing at the
        intermediate status the agent must move through first.
        """
        bug = mcp_db.create_issue("Triage bug", type="bug")
        result = await call_tool("get_ready", {})
        data = _parse(result)
        item = next(i for i in data["items"] if i["issue_id"] == bug.id)
        assert item["startable"] is False
        assert item["next_action"] == "confirmed"

    async def test_get_ready_include_context_adds_parent_context(self, mcp_db: FiligreeDB) -> None:
        parent = mcp_db.create_issue("Parent epic", type="epic")
        child = mcp_db.create_issue("Child task", parent_id=parent.id)

        result = await call_tool("get_ready", {"include_context": True})

        data = _parse(result)
        item = next(i for i in data["items"] if i["issue_id"] == child.id)
        assert item["parent_issue_id"] == parent.id
        assert item["parent_title"] == "Parent epic"

    async def test_get_blocked(self, mcp_db: FiligreeDB) -> None:
        a = mcp_db.create_issue("Blocked")
        b = mcp_db.create_issue("Blocker")
        mcp_db.add_dependency(a.id, b.id)
        result = await call_tool("get_blocked", {})
        data = _parse(result)
        assert len(data["items"]) == 1
        assert data["items"][0]["issue_id"] == a.id
        assert b.id in data["items"][0]["blocked_by"]

    async def test_get_blocked_shape(self, mcp_db: FiligreeDB) -> None:
        """get_blocked must return BlockedIssue shape (SlimIssue + blocked_by)."""
        a = mcp_db.create_issue("Blocked")
        b = mcp_db.create_issue("Blocker")
        mcp_db.add_dependency(a.id, b.id)
        result = await call_tool("get_blocked", {})
        data = _parse(result)
        expected_keys = {"issue_id", "title", "status", "priority", "type", "blocked_by"}
        assert set(data["items"][0].keys()) == expected_keys

    async def test_get_blocked_include_blockers_hydrates_blocker_context(self, mcp_db: FiligreeDB) -> None:
        a = mcp_db.create_issue("Blocked")
        b = mcp_db.create_issue("Blocker", priority=1, type="bug")
        mcp_db.add_dependency(a.id, b.id)

        result = await call_tool("get_blocked", {"include_blockers": True})

        data = _parse(result)
        item = data["items"][0]
        assert item["issue_id"] == a.id
        assert item["blocked_by"] == [b.id]
        assert item["blockers"] == [
            {
                "issue_id": b.id,
                "title": "Blocker",
                "status": "triage",
                "priority": 1,
                "type": "bug",
            }
        ]

    async def test_get_blocked_sqlite_error_returns_io(self, mcp_db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise() -> object:
            raise sqlite3.OperationalError("blocked read failed")

        monkeypatch.setattr(mcp_db, "get_blocked", _raise)

        data = _parse(await call_tool("get_blocked", {}))

        assert data["code"] == ErrorCode.IO
        assert "blocked read failed" in data["error"]

    async def test_get_blocked_include_blockers_sqlite_error_returns_io(self, mcp_db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
        a = mcp_db.create_issue("Blocked")
        b = mcp_db.create_issue("Blocker")
        mcp_db.add_dependency(a.id, b.id)

        def _raise(issue_id: str) -> object:
            raise sqlite3.OperationalError(f"blocker hydrate failed for {issue_id}")

        monkeypatch.setattr(mcp_db, "get_issue", _raise)

        data = _parse(await call_tool("get_blocked", {"include_blockers": True}))

        assert data["code"] == ErrorCode.IO
        assert "blocker hydrate failed" in data["error"]


class TestPlan:
    async def test_get_plan(self, mcp_db: FiligreeDB) -> None:
        ms = mcp_db.create_issue("Milestone", type="milestone")
        p = mcp_db.create_issue("Phase 1", type="phase", parent_id=ms.id)
        s = mcp_db.create_issue("Step 1", type="step", parent_id=p.id)
        # step requires walking pending → in_progress → completed
        # (filigree-cb980eee0d, validate-transitions policy).
        mcp_db.update_issue(s.id, status="in_progress")
        mcp_db.close_issue(s.id)
        result = await call_tool("get_plan", {"milestone_id": ms.id})
        data = _parse(result)
        assert data["total_steps"] == 1
        assert data["completed_steps"] == 1
        assert data["milestone"]["issue_id"] == ms.id
        assert "fields" not in data["milestone"]
        assert "id" not in data["milestone"]
        assert data["phases"][0]["phase"]["issue_id"] == p.id
        assert "fields" not in data["phases"][0]["phase"]
        assert "id" not in data["phases"][0]["phase"]
        assert data["phases"][0]["steps"][0]["issue_id"] == s.id
        assert "fields" not in data["phases"][0]["steps"][0]
        assert "id" not in data["phases"][0]["steps"][0]

    async def test_get_plan_full_response_detail(self, mcp_db: FiligreeDB) -> None:
        ms = mcp_db.create_issue("Full Milestone", type="milestone", fields={"scope_summary": "full"})
        p = mcp_db.create_issue("Full Phase", type="phase", parent_id=ms.id, fields={"sequence": 1})
        s = mcp_db.create_issue("Full Step", type="step", parent_id=p.id, fields={"sequence": 1})

        result = await call_tool("get_plan", {"milestone_id": ms.id, "response_detail": "full"})
        data = _parse(result)

        assert data["milestone"]["fields"]["scope_summary"] == "full"
        assert data["phases"][0]["phase"]["fields"]["sequence"] == 1
        assert data["phases"][0]["steps"][0]["fields"]["sequence"] == 1
        assert data["phases"][0]["steps"][0]["issue_id"] == s.id

    async def test_get_plan_not_found(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("get_plan", {"milestone_id": "mcp-nonexistent"})
        data = _parse(result)
        assert data["code"] == ErrorCode.NOT_FOUND

    async def test_get_plan_rejects_non_milestone_root(self, mcp_db: FiligreeDB) -> None:
        phase = mcp_db.create_issue("Wrong root", type="phase")
        result = await call_tool("get_plan", {"milestone_id": phase.id})
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION
        assert "not a milestone" in data["error"]


class TestComments:
    async def test_add_comment(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Commentable")
        result = await call_tool("add_comment", {"issue_id": issue.id, "text": "A comment"})
        data = _parse(result)
        assert data["issue_id"] == issue.id
        assert data["status"] == "open"
        assert "comment_id" in data

    async def test_add_comment_defaults_expected_assignee_to_actor_for_held_issue(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Claim-aware comment")
        mcp_db.claim_issue(issue.id, assignee="agent-holder")

        result = await call_tool("add_comment", {"issue_id": issue.id, "text": "note", "actor": "other-agent"})

        data = _parse(result)
        assert data["code"] == ErrorCode.CONFLICT
        assert data["details"] == {"issue_id": issue.id, "observed": "agent-holder", "expected": "other-agent"}

    async def test_get_comments(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("With comments")
        mcp_db.add_comment(issue.id, "First", author="alice")
        mcp_db.add_comment(issue.id, "Second", author="bob")
        result = await call_tool("get_comments", {"issue_id": issue.id})
        data = _parse(result)
        assert len(data["items"]) == 2
        assert data["items"][0]["comment_id"]
        assert "id" not in data["items"][0]
        assert data["items"][0]["text"] == "First"


class TestTemplateAndSummary:
    async def test_get_template(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("get_template", {"type": "bug"})
        data = _parse(result)
        assert data["type"] == "bug"
        assert data["pack"] == "core"
        assert "fields_schema" in data

    async def test_get_template_is_canonical_for_type_info(self, mcp_db: FiligreeDB) -> None:
        template = _parse(await call_tool("get_template", {"type": "task"}))
        type_info = _parse(await call_tool("get_type_info", {"type": "task"}))

        assert type_info == template

    async def test_template_tool_descriptions_explain_alias_contract(self, mcp_db: FiligreeDB) -> None:
        tools = {tool.name: tool for tool in await list_tools()}

        assert "canonical full workflow definition" in tools["template_get"].description
        assert "compatibility alias" in tools["type_get"].description

    async def test_get_template_unknown(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("get_template", {"type": "nonexistent"})
        data = _parse(result)
        assert data["code"] == ErrorCode.NOT_FOUND

    async def test_get_summary(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("get_summary", {})
        text = _parse(result)
        assert "Project Pulse" in text

    async def test_session_context_tool_is_available(self, mcp_db: FiligreeDB) -> None:
        tools = await list_tools()
        assert "session_context_get" in {tool.name for tool in tools}

    async def test_session_context_returns_startup_snapshot(self, mcp_db: FiligreeDB) -> None:
        mcp_db.create_issue("Ready via MCP session context")

        result = await call_tool("session_context", {})

        text = _parse(result)
        assert text.startswith("=== Filigree Project Snapshot ===")
        assert "READY TO WORK" in text
        assert "Ready via MCP session context" in text
        assert "STATS:" in text
        assert "Project Pulse" not in text

    async def test_get_stats(self, mcp_db: FiligreeDB) -> None:
        mcp_db.create_issue("A")
        result = await call_tool("get_stats", {})
        data = _parse(result)
        assert "by_status" in data
        assert data["by_status"]["open"] == 1

    async def test_get_stats_exposes_unambiguous_status_count_names(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("A")
        mcp_db.update_issue(issue.id, status="in_progress")

        result = await call_tool("get_stats", {})

        data = _parse(result)
        # status_name_counts / status_category_counts are DEPRECATED
        # (filigree-17694d2db8) duplicates of by_status / by_category, kept as
        # wire-compatibility aliases per ADR-009 §7. Pin them on the MCP wire
        # surface: present and identical until the next major.
        assert data["status_name_counts"] == data["by_status"]
        assert data["status_category_counts"] == data["by_category"]
        assert data["status_name_counts"]["in_progress"] == 1
        assert data["status_category_counts"]["wip"] == 1


class TestLabels:
    async def test_add_label(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Labelable")
        result = await call_tool("add_label", {"issue_id": issue.id, "label": "urgent"})
        data = _parse(result)
        assert data["issue_id"] == issue.id
        assert data["status"] == "open"
        assert data["label_result"] == "added"
        assert data["label"] == "urgent"
        assert "urgent" in data["labels"]

    async def test_add_label_defaults_expected_assignee_to_actor_for_held_issue(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Claim-aware label")
        mcp_db.claim_issue(issue.id, assignee="agent-holder")

        result = await call_tool("add_label", {"issue_id": issue.id, "label": "needs-review", "actor": "other-agent"})

        data = _parse(result)
        assert data["code"] == ErrorCode.CONFLICT
        assert "assigned to 'agent-holder'" in data["error"]
        assert "expected 'other-agent'" in data["error"]
        assert data["details"] == {"issue_id": issue.id, "observed": "agent-holder", "expected": "other-agent"}

    async def test_remove_label_defaults_expected_assignee_to_actor_for_held_issue(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Claim-aware remove label")
        mcp_db.add_label(issue.id, "needs-review")
        mcp_db.claim_issue(issue.id, assignee="agent-holder")

        result = await call_tool("remove_label", {"issue_id": issue.id, "label": "needs-review", "actor": "other-agent"})

        data = _parse(result)
        assert data["code"] == ErrorCode.CONFLICT
        assert data["details"] == {"issue_id": issue.id, "observed": "agent-holder", "expected": "other-agent"}

    async def test_add_label_rejects_reserved_type_name(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Labelable")
        result = await call_tool("add_label", {"issue_id": issue.id, "label": "bug"})
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION
        assert "reserved as an issue type" in data["error"]

    async def test_add_label_rejects_priority_like_label(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Labelable")
        result = await call_tool("add_label", {"issue_id": issue.id, "label": "priority:1"})
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION
        assert "priority field" in data["error"]

    async def test_remove_label(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Labelable", labels=["defect", "urgent"])
        result = await call_tool("remove_label", {"issue_id": issue.id, "label": "defect"})
        data = _parse(result)
        assert data["issue_id"] == issue.id
        assert data["status"] == "open"
        assert data["label_result"] == "removed"
        assert "defect" not in data["labels"]
        assert "urgent" in data["labels"]

    async def test_add_label_returns_canonical_label(self, mcp_db: FiligreeDB) -> None:
        """Bug filigree-6870a1dcc0: MCP must return canonical (stripped) label, not raw arg."""
        issue = mcp_db.create_issue("Labelable")
        result = await call_tool("add_label", {"issue_id": issue.id, "label": "  urgent  "})
        data = _parse(result)
        assert data["label_result"] == "added"
        assert data["label"] == "urgent", f"expected canonical 'urgent', got {data['label']!r}"

    async def test_remove_label_returns_canonical_label(self, mcp_db: FiligreeDB) -> None:
        """Bug filigree-6870a1dcc0: MCP must return canonical (stripped) label, not raw arg."""
        issue = mcp_db.create_issue("Labelable", labels=["urgent"])
        result = await call_tool("remove_label", {"issue_id": issue.id, "label": "  urgent  "})
        data = _parse(result)
        assert data["label_result"] == "removed"
        assert data["label"] == "urgent"


class TestCreatePlan:
    async def test_create_plan_sqlite_error_returns_io(self, mcp_db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(*args: Any, **kwargs: Any) -> object:
            raise sqlite3.OperationalError("plan write failed")

        monkeypatch.setattr(mcp_db, "create_plan", _raise)

        data = _parse(await call_tool("create_plan", {"milestone": {"title": "M"}, "phases": [{"title": "P"}]}))

        assert data["code"] == ErrorCode.IO
        assert "plan write failed" in data["error"]

    async def test_create_plan_basic(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool(
            "create_plan",
            {
                "milestone": {"title": "v1.0 Release"},
                "phases": [
                    {
                        "title": "Phase 1",
                        "steps": [
                            {"title": "Step 1.1"},
                            {"title": "Step 1.2", "deps": [0]},
                        ],
                    },
                    {
                        "title": "Phase 2",
                        "steps": [
                            {"title": "Step 2.1", "deps": ["0.1"]},
                        ],
                    },
                ],
            },
        )
        data = _parse(result)
        assert data["milestone"]["title"] == "v1.0 Release"
        assert data["milestone"]["issue_id"]
        assert "id" not in data["milestone"]
        assert data["total_steps"] == 3
        assert len(data["phases"]) == 2
        assert data["phases"][0]["total"] == 2
        assert data["phases"][1]["total"] == 1
        assert data["phases"][0]["phase"]["issue_id"]
        assert "id" not in data["phases"][0]["phase"]
        assert data["phases"][0]["steps"][0]["issue_id"]
        assert "id" not in data["phases"][0]["steps"][0]

    async def test_create_plan_empty_phases(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool(
            "create_plan",
            {
                "milestone": {"title": "Empty milestone"},
                "phases": [{"title": "Empty phase"}],
            },
        )
        data = _parse(result)
        assert data["total_steps"] == 0
        assert len(data["phases"]) == 1

    async def test_create_plan_with_descriptions(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool(
            "create_plan",
            {
                "milestone": {"title": "Described plan", "description": "A plan with descriptions", "priority": 1},
                "phases": [
                    {
                        "title": "Phase A",
                        "description": "First phase",
                        "steps": [{"title": "Step A.1", "description": "Do something", "priority": 0}],
                    },
                ],
            },
        )
        data = _parse(result)
        assert data["milestone"]["priority"] == 1
        assert data["phases"][0]["steps"][0]["priority"] == 0

    async def test_create_plan_labels_propagate_with_level_overrides(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool(
            "create_plan",
            {
                "milestone": {"title": "Labelled plan", "labels": ["release:v1", "scratch"]},
                "phases": [
                    {
                        "title": "Phase A",
                        "labels": ["phase:build"],
                        "steps": [
                            {"title": "Step A.1", "labels": ["step:one"]},
                            {"title": "Step A.2"},
                        ],
                    }
                ],
            },
        )
        data = _parse(result)
        assert set(data["milestone"]["labels"]) == {"release:v1", "scratch"}
        assert set(data["phases"][0]["phase"]["labels"]) == {"release:v1", "scratch", "phase:build"}
        assert set(data["phases"][0]["steps"][0]["labels"]) == {"release:v1", "scratch", "phase:build", "step:one"}
        assert set(data["phases"][0]["steps"][1]["labels"]) == {"release:v1", "scratch", "phase:build"}

    async def test_create_plan_from_file_tool_is_available(self, mcp_db: FiligreeDB) -> None:
        tools = await list_tools()
        assert "plan_create_from_file" in {tool.name for tool in tools}

    async def test_create_plan_from_file_reads_project_relative_json(self, mcp_db: FiligreeDB) -> None:
        plan_path = mcp_db.db_path.parent.parent / "plan.json"
        plan_path.write_text(
            json.dumps(
                {
                    "milestone": {"title": "File-backed plan", "labels": ["release:file"]},
                    "phases": [
                        {
                            "title": "Phase from file",
                            "steps": [
                                {"title": "Write fixture"},
                                {"title": "Import fixture", "deps": [0]},
                            ],
                        }
                    ],
                }
            )
        )

        result = await call_tool("create_plan_from_file", {"file_path": "plan.json", "actor": "planner"})

        data = _parse(result)
        assert data["milestone"]["title"] == "File-backed plan"
        assert data["milestone"]["issue_id"]
        assert data["total_steps"] == 2
        assert data["phases"][0]["phase"]["title"] == "Phase from file"
        assert data["phases"][0]["steps"][1]["title"] == "Import fixture"
        assert set(data["phases"][0]["steps"][1]["labels"]) == {"release:file"}

    async def test_create_plan_from_file_rejects_path_traversal(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("create_plan_from_file", {"file_path": "../outside.json"})
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION
        assert "escapes project directory" in data["error"]

    async def test_create_plan_from_file_missing_file_path_rejected(self, mcp_db: FiligreeDB) -> None:
        data = _parse(await call_tool("create_plan_from_file", {}))

        assert data["code"] == ErrorCode.VALIDATION
        assert data["error"] == "file_path is required"

    async def test_create_plan_from_file_non_string_file_path_rejected(self, mcp_db: FiligreeDB) -> None:
        data = _parse(await call_tool("create_plan_from_file", {"file_path": ["plan.json"]}))

        assert data["code"] == ErrorCode.VALIDATION
        assert data["error"] == "file_path must be a string"

    async def test_label_subtree_labels_root_and_descendants(self, mcp_db: FiligreeDB) -> None:
        created = await call_tool(
            "create_plan",
            {
                "milestone": {"title": "Subtree"},
                "phases": [{"title": "Phase", "steps": [{"title": "Step"}]}],
            },
        )
        plan = _parse(created)
        milestone_id = plan["milestone"]["issue_id"]
        phase_id = plan["phases"][0]["phase"]["issue_id"]
        step_id = plan["phases"][0]["steps"][0]["issue_id"]

        result = await call_tool("label_subtree", {"parent_id": milestone_id, "label": "scratch"})
        data = _parse(result)

        assert set(data["succeeded"]) == {milestone_id, phase_id, step_id}
        assert data["failed"] == []
        assert "scratch" in mcp_db.get_issue(milestone_id).labels
        assert "scratch" in mcp_db.get_issue(phase_id).labels
        assert "scratch" in mcp_db.get_issue(step_id).labels

    async def test_add_plan_step_creates_step_with_plan_context(self, mcp_db: FiligreeDB) -> None:
        created = await call_tool(
            "create_plan",
            {
                "milestone": {"title": "Editable plan", "labels": ["release:v1"]},
                "phases": [
                    {
                        "title": "Phase",
                        "labels": ["phase:build"],
                        "steps": [{"title": "Existing step"}],
                    }
                ],
            },
        )
        plan = _parse(created)
        phase_id = plan["phases"][0]["phase"]["issue_id"]
        existing_step_id = plan["phases"][0]["steps"][0]["issue_id"]

        result = await call_tool(
            "add_plan_step",
            {
                "phase_id": phase_id,
                "title": "Inserted step",
                "description": "Added after initial planning",
                "priority": 1,
                "labels": ["step:inserted"],
                "deps": [existing_step_id],
                "actor": "planner",
            },
        )

        data = _parse(result)
        assert data["type"] == "step"
        assert data["parent_id"] == phase_id
        assert data["priority"] == 1
        assert data["description"] == "Added after initial planning"
        assert set(data["labels"]) == {"release:v1", "phase:build", "step:inserted"}
        assert data["blocked_by"] == [existing_step_id]

        updated = _parse(await call_tool("get_plan", {"milestone_id": plan["milestone"]["issue_id"]}))
        assert updated["total_steps"] == 2
        assert {step["title"] for step in updated["phases"][0]["steps"]} == {"Existing step", "Inserted step"}

    async def test_retarget_plan_dependency_swaps_blocker(self, mcp_db: FiligreeDB) -> None:
        created = await call_tool(
            "create_plan",
            {
                "milestone": {"title": "Retarget plan"},
                "phases": [
                    {
                        "title": "Phase",
                        "steps": [
                            {"title": "Old blocker"},
                            {"title": "Blocked step", "deps": [0]},
                            {"title": "New blocker"},
                        ],
                    }
                ],
            },
        )
        steps = _parse(created)["phases"][0]["steps"]
        old_blocker_id = steps[0]["issue_id"]
        blocked_step_id = steps[1]["issue_id"]
        new_blocker_id = steps[2]["issue_id"]

        result = await call_tool(
            "retarget_plan_dependency",
            {
                "step_id": blocked_step_id,
                "old_depends_on_id": old_blocker_id,
                "new_depends_on_id": new_blocker_id,
                "actor": "planner",
            },
        )

        data = _parse(result)
        assert data["dependency_result"] == "retargeted"
        assert data["dependency"] == {
            "from_issue_id": blocked_step_id,
            "old_to_issue_id": old_blocker_id,
            "to_issue_id": new_blocker_id,
        }
        assert data["blocked_by"] == [new_blocker_id]
        assert mcp_db.get_issue(blocked_step_id).blocked_by == [new_blocker_id]

    async def test_move_plan_step_moves_between_phases(self, mcp_db: FiligreeDB) -> None:
        created = await call_tool(
            "create_plan",
            {
                "milestone": {"title": "Move plan"},
                "phases": [
                    {"title": "Phase A", "steps": [{"title": "Move me"}]},
                    {"title": "Phase B", "steps": []},
                ],
            },
        )
        plan = _parse(created)
        milestone_id = plan["milestone"]["issue_id"]
        step_id = plan["phases"][0]["steps"][0]["issue_id"]
        target_phase_id = plan["phases"][1]["phase"]["issue_id"]

        result = await call_tool(
            "move_plan_step",
            {"step_id": step_id, "phase_id": target_phase_id, "actor": "planner"},
        )

        data = _parse(result)
        assert data["move_result"] == "moved"
        assert data["parent_id"] == target_phase_id

        updated = _parse(await call_tool("get_plan", {"milestone_id": milestone_id}))
        assert updated["phases"][0]["steps"] == []
        assert [step["issue_id"] for step in updated["phases"][1]["steps"]] == [step_id]

    async def test_move_plan_step_warns_when_dependencies_carry_forward(self, mcp_db: FiligreeDB) -> None:
        created = await call_tool(
            "create_plan",
            {
                "milestone": {"title": "Move warning plan"},
                "phases": [
                    {"title": "Phase A", "steps": [{"title": "Blocker"}, {"title": "Move me", "deps": [0]}]},
                    {"title": "Phase B", "steps": []},
                ],
            },
        )
        plan = _parse(created)
        step_id = plan["phases"][0]["steps"][1]["issue_id"]
        target_phase_id = plan["phases"][1]["phase"]["issue_id"]

        result = await call_tool("move_plan_step", {"step_id": step_id, "phase_id": target_phase_id, "actor": "planner"})

        data = _parse(result)
        assert data["move_result"] == "moved"
        assert data["warnings"]
        assert "dependencies carried forward" in data["warnings"][0]

    async def test_label_plan_tree_requires_milestone_root(self, mcp_db: FiligreeDB) -> None:
        phase = mcp_db.create_issue("Loose phase", type="phase")

        result = await call_tool("label_plan_tree", {"milestone_id": phase.id, "label": "release:v1"})

        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION
        assert "milestone" in data["error"]

    async def test_label_plan_tree_missing_milestone_id_rejected(self, mcp_db: FiligreeDB) -> None:
        data = _parse(await call_tool("label_plan_tree", {"label": "release:v1"}))

        assert data["code"] == ErrorCode.VALIDATION
        assert data["error"] == "milestone_id is required"

    async def test_label_plan_tree_non_string_fields_rejected(self, mcp_db: FiligreeDB) -> None:
        milestone = mcp_db.create_issue("Milestone", type="milestone")

        bad_milestone = _parse(await call_tool("label_plan_tree", {"milestone_id": ["bad"], "label": "release:v1"}))
        bad_label = _parse(await call_tool("label_plan_tree", {"milestone_id": milestone.id, "label": ["bad"]}))

        assert bad_milestone["code"] == ErrorCode.VALIDATION
        assert bad_milestone["error"] == "milestone_id must be a string"
        assert bad_label["code"] == ErrorCode.VALIDATION
        assert bad_label["error"] == "label must be a string"

    async def test_label_plan_tree_full_refresh_sqlite_error_returns_io(self, mcp_db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
        milestone = mcp_db.create_issue("Milestone", type="milestone")
        original_get_issue = mcp_db.get_issue

        def _label_subtree(*args: Any, **kwargs: Any) -> tuple[list[dict[str, str]], list[object]]:
            return ([{"id": "filigree-refresh-target"}], [])

        def _get_issue(issue_id: str) -> object:
            if issue_id == milestone.id:
                return original_get_issue(issue_id)
            raise sqlite3.OperationalError(f"label refresh failed for {issue_id}")

        monkeypatch.setattr(mcp_db, "label_subtree", _label_subtree)
        monkeypatch.setattr(mcp_db, "get_issue", _get_issue)

        data = _parse(
            await call_tool(
                "label_plan_tree",
                {"milestone_id": milestone.id, "label": "release:v1", "response_detail": "full"},
            )
        )

        assert data["code"] == ErrorCode.IO
        assert "label refresh failed" in data["error"]


class TestBatchClose:
    async def test_batch_close(self, mcp_db: FiligreeDB) -> None:
        a = mcp_db.create_issue("Close A")
        b = mcp_db.create_issue("Close B")
        result = await call_tool("batch_close", {"issue_ids": [a.id, b.id], "reason": "done"})
        data = _parse(result)
        assert len(data["succeeded"]) == 2
        succeeded_ids = {s["issue_id"] for s in data["succeeded"]}
        assert a.id in succeeded_ids
        assert b.id in succeeded_ids
        assert data["failed"] == []

    async def test_batch_close_not_found(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("batch_close", {"issue_ids": ["mcp-nonexistent"]})
        data = _parse(result)
        assert data["succeeded"] == []
        assert len(data["failed"]) == 1
        assert data["failed"][0]["code"] == "NOT_FOUND"


class TestBatchUpdate:
    async def test_batch_update_status(self, mcp_db: FiligreeDB) -> None:
        a = mcp_db.create_issue("Update A")
        b = mcp_db.create_issue("Update B")
        result = await call_tool("batch_update", {"issue_ids": [a.id, b.id], "status": "in_progress"})
        data = _parse(result)
        assert len(data["succeeded"]) == 2
        assert a.id in {s["issue_id"] for s in data["succeeded"]}
        assert mcp_db.get_issue(a.id).status == "in_progress"
        assert mcp_db.get_issue(b.id).status == "in_progress"

    async def test_batch_update_priority(self, mcp_db: FiligreeDB) -> None:
        a = mcp_db.create_issue("Priority A")
        b = mcp_db.create_issue("Priority B")
        result = await call_tool("batch_update", {"issue_ids": [a.id, b.id], "priority": 0})
        data = _parse(result)
        assert len(data["succeeded"]) == 2
        assert mcp_db.get_issue(a.id).priority == 0

    async def test_batch_update_not_found(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("batch_update", {"issue_ids": ["mcp-nonexistent"], "status": "closed"})
        data = _parse(result)
        assert data["succeeded"] == []
        assert len(data["failed"]) == 1
        assert data["failed"][0]["code"] == "NOT_FOUND"


class TestBatchAddLabel:
    async def test_batch_add_label(self, mcp_db: FiligreeDB) -> None:
        a = mcp_db.create_issue("Label A")
        b = mcp_db.create_issue("Label B")
        result = await call_tool("batch_add_label", {"issue_ids": [a.id, b.id], "label": "security"})
        data = _parse(result)
        assert len(data["succeeded"]) == 2
        assert a.id in data["succeeded"]
        assert b.id in data["succeeded"]
        assert data["failed"] == []

    async def test_batch_add_label_partial_failure(self, mcp_db: FiligreeDB) -> None:
        a = mcp_db.create_issue("Label A")
        result = await call_tool("batch_add_label", {"issue_ids": [a.id, "mcp-nonexistent"], "label": "security"})
        data = _parse(result)
        assert len(data["succeeded"]) == 1
        assert a.id in data["succeeded"]
        assert len(data["failed"]) == 1
        assert data["failed"][0]["code"] == "NOT_FOUND"

    async def test_batch_add_label_validation_error(self, mcp_db: FiligreeDB) -> None:
        a = mcp_db.create_issue("Label A")
        result = await call_tool("batch_add_label", {"issue_ids": [a.id], "label": "bug"})
        data = _parse(result)
        assert data["succeeded"] == []
        assert len(data["failed"]) == 1
        assert data["failed"][0]["code"] == "VALIDATION"

    async def test_batch_add_label_rejects_priority_like_label(self, mcp_db: FiligreeDB) -> None:
        a = mcp_db.create_issue("Label A")
        result = await call_tool("batch_add_label", {"issue_ids": [a.id], "label": "P1"})
        data = _parse(result)
        assert data["succeeded"] == []
        assert len(data["failed"]) == 1
        assert data["failed"][0]["code"] == "VALIDATION"
        assert "priority field" in data["failed"][0]["error"]


class TestBatchRemoveLabel:
    async def test_batch_remove_label(self, mcp_db: FiligreeDB) -> None:
        a = mcp_db.create_issue("Label A", labels=["security"])
        b = mcp_db.create_issue("Label B", labels=["security"])
        result = await call_tool("batch_remove_label", {"issue_ids": [a.id, b.id], "label": "security"})
        data = _parse(result)
        assert len(data["succeeded"]) == 2
        assert a.id in data["succeeded"]
        assert b.id in data["succeeded"]
        assert data["failed"] == []
        assert "security" not in mcp_db.get_issue(a.id).labels
        assert "security" not in mcp_db.get_issue(b.id).labels

    async def test_batch_remove_label_partial_failure(self, mcp_db: FiligreeDB) -> None:
        a = mcp_db.create_issue("Label A", labels=["security"])
        result = await call_tool("batch_remove_label", {"issue_ids": [a.id, "mcp-nonexistent"], "label": "security"})
        data = _parse(result)
        assert len(data["succeeded"]) == 1
        assert a.id in data["succeeded"]
        assert len(data["failed"]) == 1
        assert data["failed"][0]["code"] == "NOT_FOUND"

    async def test_batch_remove_label_validation_error(self, mcp_db: FiligreeDB) -> None:
        a = mcp_db.create_issue("Label A")
        result = await call_tool("batch_remove_label", {"issue_ids": [a.id], "label": "bug"})
        data = _parse(result)
        assert data["succeeded"] == []
        assert len(data["failed"]) == 1
        assert data["failed"][0]["code"] == "VALIDATION"


class TestBatchAddComment:
    async def test_batch_add_comment(self, mcp_db: FiligreeDB) -> None:
        a = mcp_db.create_issue("Comment A")
        b = mcp_db.create_issue("Comment B")
        result = await call_tool("batch_add_comment", {"issue_ids": [a.id, b.id], "text": "triage complete"})
        data = _parse(result)
        assert len(data["succeeded"]) == 2
        assert a.id in data["succeeded"]
        assert b.id in data["succeeded"]
        assert data["failed"] == []

    async def test_batch_add_comment_partial_failure(self, mcp_db: FiligreeDB) -> None:
        a = mcp_db.create_issue("Comment A")
        result = await call_tool("batch_add_comment", {"issue_ids": [a.id, "mcp-nonexistent"], "text": "triage complete"})
        data = _parse(result)
        assert len(data["succeeded"]) == 1
        assert a.id in data["succeeded"]
        assert len(data["failed"]) == 1
        assert data["failed"][0]["code"] == "NOT_FOUND"

    async def test_batch_add_comment_validation_error(self, mcp_db: FiligreeDB) -> None:
        a = mcp_db.create_issue("Comment A")
        result = await call_tool("batch_add_comment", {"issue_ids": [a.id], "text": "   "})
        data = _parse(result)
        assert data["succeeded"] == []
        assert len(data["failed"]) == 1
        assert data["failed"][0]["code"] == "VALIDATION"


class TestStartWork:
    """MCP wrapper coverage for the D6 composed operations."""

    async def test_start_work_via_mcp(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("mcp-d6-start", type="task")
        result = await call_tool("start_work", {"issue_id": issue.id, "assignee": "bob"})
        data = _parse(result)
        assert data["assignee"] == "bob"
        assert data["status"] == "in_progress"

    async def test_start_work_explicit_target(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("mcp-d6-explicit", type="task")
        result = await call_tool(
            "start_work",
            {"issue_id": issue.id, "assignee": "bob", "target_status": "in_progress"},
        )
        data = _parse(result)
        assert data["status"] == "in_progress"

    async def test_start_work_confirmed_bug_defaults_to_reachable_fixing(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("mcp-d6-confirmed-bug", type="bug", fields={"severity": "major"})
        mcp_db.update_issue(issue.id, status="confirmed")

        result = await call_tool("start_work", {"issue_id": issue.id, "assignee": "bob"})

        data = _parse(result)
        assert data["assignee"] == "bob"
        assert data["status"] == "fixing"

    async def test_start_work_fresh_bug_reports_no_reachable_wip(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("mcp-d6-fresh-bug", type="bug")

        result = await call_tool("start_work", {"issue_id": issue.id, "assignee": "bob"})

        data = _parse(result)
        assert data["code"] == ErrorCode.INVALID_TRANSITION
        # filigree-406e6b7ee0: actionable hint naming the intermediate hop.
        assert "triage" in data["error"]
        assert "not directly startable" in data["error"]
        assert "confirmed" in data["error"]
        current = mcp_db.get_issue(issue.id)
        assert current.assignee == ""
        assert current.status == "triage"

    async def test_start_work_advance_walks_triage_bug_to_wip(self, mcp_db: FiligreeDB) -> None:
        """filigree-406e6b7ee0 Part 2: advance=true walks the soft path to wip."""
        issue = mcp_db.create_issue("mcp-advance-bug", type="bug")

        result = await call_tool("start_work", {"issue_id": issue.id, "assignee": "bob", "advance": True})

        data = _parse(result)
        assert data["status"] == "fixing"
        assert data["assignee"] == "bob"

    async def test_start_work_blank_assignee_rejected(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("mcp-d6-blank", type="task")
        result = await call_tool("start_work", {"issue_id": issue.id, "assignee": ""})
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION

    async def test_start_work_not_found(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("start_work", {"issue_id": "mcp-nonexistent", "assignee": "bob"})
        data = _parse(result)
        assert data["code"] == ErrorCode.NOT_FOUND

    async def test_start_work_returns_conflict_for_already_claimed_issue(self, mcp_db: FiligreeDB) -> None:
        """MCP start_work must surface a CONFLICT envelope on an ownership race.

        Mirrors the ``claim_issue`` MCP test pattern: a claim already held by
        another assignee raises ``ClaimConflictError`` deep in the DB layer;
        the tool must emit ``ErrorCode.CONFLICT`` with structured details so
        MCP consumers can branch correctly. Falling through to the generic
        ``ValueError`` handler would mis-tag the race as VALIDATION.
        """
        issue = mcp_db.create_issue("mcp-start-work-race", type="task")
        mcp_db.claim_issue(issue.id, assignee="agent-holder")

        result = await call_tool("start_work", {"issue_id": issue.id, "assignee": "agent-challenger"})

        data = _parse(result)
        assert data["code"] == ErrorCode.CONFLICT
        assert data["details"] == {
            "issue_id": issue.id,
            "observed": "agent-holder",
            "expected": "agent-challenger",
        }

    async def test_start_next_work_picks_highest_priority(self, mcp_db: FiligreeDB) -> None:
        mcp_db.create_issue("mcp-d6-next-low", type="task", priority=4)
        high = mcp_db.create_issue("mcp-d6-next-high", type="task", priority=0)
        result = await call_tool("start_next_work", {"assignee": "carol"})
        data = _parse(result)
        assert data["issue_id"] == high.id
        assert data["assignee"] == "carol"
        assert data["status"] == "in_progress"

    async def test_start_next_work_no_match_returns_empty(self, mcp_db: FiligreeDB) -> None:
        mcp_db.create_issue("mcp-d6-only-task", type="task", priority=2)
        result = await call_tool("start_next_work", {"assignee": "dan", "type": "nonexistent_type"})
        data = _parse(result)
        assert data["status"] == "empty"

    async def test_start_next_work_invalid_target_status_is_invalid_transition(self, mcp_db: FiligreeDB) -> None:
        mcp_db.create_issue("mcp-d6-invalid-target", type="task", priority=2)

        result = await call_tool(
            "start_next_work",
            {"assignee": "erin", "type": "task", "target_status": "not-a-status"},
        )

        data = _parse(result)
        assert data["code"] == ErrorCode.INVALID_TRANSITION


class TestClaimIssue:
    async def test_claim_success(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Claimable")
        result = await call_tool("claim_issue", {"issue_id": issue.id, "assignee": "agent-1"})
        data = _parse(result)
        assert data["status"] == "open"  # status unchanged — claim only sets assignee
        assert data["assignee"] == "agent-1"
        assert data["claimed_at"] is not None
        assert data["last_heartbeat_at"] == data["claimed_at"]
        assert data["claim_expires_at"] is not None

    async def test_claim_conflict(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Claimable")
        mcp_db.claim_issue(issue.id, assignee="agent-1")
        result = await call_tool("claim_issue", {"issue_id": issue.id, "assignee": "agent-2"})
        data = _parse(result)
        assert data["code"] == ErrorCode.CONFLICT
        assert data["details"] == {"issue_id": issue.id, "observed": "agent-1", "expected": "agent-2"}

    async def test_claim_released_wip_issue_for_handoff(self, mcp_db: FiligreeDB) -> None:
        """release_claim auto-reverts wip→open (filigree-cb980eee0d, P1.3),
        so the released issue is in 'open' and the handoff agent picks up
        from the open state.
        """
        issue = mcp_db.create_issue("MCP handoff", type="task")
        await call_tool("start_work", {"issue_id": issue.id, "assignee": "agent-alpha"})
        released = _parse(await call_tool("release_claim", {"issue_id": issue.id, "actor": "agent-alpha"}))
        assert released["status"] == "open"
        assert released["assignee"] == ""

        result = await call_tool("claim_issue", {"issue_id": issue.id, "assignee": "agent-bravo"})

        data = _parse(result)
        assert data["status"] == "open"
        assert data["assignee"] == "agent-bravo"

    async def test_claim_closed_issue_is_validation(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Closed claim", type="task")
        mcp_db.close_issue(issue.id, reason="done")

        result = await call_tool("claim_issue", {"issue_id": issue.id, "assignee": "agent-1"})

        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION
        assert "expected open-category state" in data["error"]

    async def test_claim_not_found(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("claim_issue", {"issue_id": "mcp-nonexistent", "assignee": "agent-1"})
        data = _parse(result)
        assert data["code"] == ErrorCode.NOT_FOUND


class TestClaimLeaseTools:
    async def test_release_claim_records_reason(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Release reason MCP")
        mcp_db.claim_issue(issue.id, assignee="agent-1")

        result = await call_tool("release_claim", {"issue_id": issue.id, "actor": "agent-1", "reason": "handoff"})

        data = _parse(result)
        assert data["assignee"] == ""
        events = mcp_db.get_issue_events(issue.id, limit=10)
        released = [event for event in events if event["event_type"] == "released"]
        assert released[-1]["comment"] == "handoff"

    async def test_heartbeat_work_refreshes_claim(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Heartbeat MCP")
        mcp_db.claim_issue(issue.id, assignee="agent-1")
        old = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
        mcp_db.conn.execute(
            "UPDATE issues SET last_heartbeat_at = ?, claim_expires_at = ? WHERE id = ?",
            (old, old, issue.id),
        )
        mcp_db.conn.commit()

        result = await call_tool("heartbeat_work", {"issue_id": issue.id, "actor": "agent-1"})

        data = _parse(result)
        assert data["issue_id"] == issue.id
        assert datetime.fromisoformat(data["last_heartbeat_at"]) > datetime.fromisoformat(old)
        assert datetime.fromisoformat(data["claim_expires_at"]) > datetime.fromisoformat(data["last_heartbeat_at"])

    async def test_get_stale_claims_includes_legacy_assigned_work(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Stale MCP", priority=1)
        old = (datetime.now(UTC) - timedelta(days=5)).isoformat()
        mcp_db.conn.execute(
            "UPDATE issues SET assignee = 'old-agent', updated_at = ? WHERE id = ?",
            (old, issue.id),
        )
        mcp_db.conn.commit()

        result = await call_tool("get_stale_claims", {})

        data = _parse(result)
        assert [item["issue_id"] for item in data["items"]] == [issue.id]

    async def test_get_stale_claims_can_include_near_expiry_leases(self, mcp_db: FiligreeDB) -> None:
        soon = mcp_db.create_issue("Expires soon MCP", priority=0)
        later = mcp_db.create_issue("Expires later MCP", priority=0)
        expired = mcp_db.create_issue("Expired MCP", priority=0)
        mcp_db.claim_issue(soon.id, assignee="agent-soon")
        mcp_db.claim_issue(later.id, assignee="agent-later")
        mcp_db.claim_issue(expired.id, assignee="agent-expired")
        now = datetime.now(UTC)
        soon_at = (now + timedelta(hours=1)).isoformat()
        later_at = (now + timedelta(hours=5)).isoformat()
        expired_at = (now - timedelta(hours=1)).isoformat()
        mcp_db.conn.execute(
            "UPDATE issues SET claim_expires_at = ?, last_heartbeat_at = ? WHERE id = ?",
            (soon_at, soon_at, soon.id),
        )
        mcp_db.conn.execute(
            "UPDATE issues SET claim_expires_at = ?, last_heartbeat_at = ? WHERE id = ?",
            (later_at, later_at, later.id),
        )
        mcp_db.conn.execute(
            "UPDATE issues SET claim_expires_at = ?, last_heartbeat_at = ? WHERE id = ?",
            (expired_at, expired_at, expired.id),
        )
        mcp_db.conn.commit()

        default_result = await call_tool("get_stale_claims", {})
        proactive_result = await call_tool("get_stale_claims", {"expires_within_hours": 2})

        default_data = _parse(default_result)
        proactive_data = _parse(proactive_result)
        assert [item["issue_id"] for item in default_data["items"]] == [expired.id]
        assert [item["issue_id"] for item in proactive_data["items"]] == [soon.id, expired.id]

    async def test_reclaim_issue_transfers_expected_holder(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Reclaim MCP")
        mcp_db.claim_issue(issue.id, assignee="agent-old")

        result = await call_tool(
            "reclaim_issue",
            {
                "issue_id": issue.id,
                "assignee": "agent-new",
                "expected_assignee": "agent-old",
                "reason": "missed heartbeat",
                "actor": "coordinator",
            },
        )

        data = _parse(result)
        assert data["assignee"] == "agent-new"
        assert data["claimed_at"] is not None

    async def test_reclaim_issue_rejects_unexpected_holder(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Reclaim MCP conflict")
        mcp_db.claim_issue(issue.id, assignee="agent-current")

        result = await call_tool(
            "reclaim_issue",
            {
                "issue_id": issue.id,
                "assignee": "agent-new",
                "expected_assignee": "agent-old",
                "reason": "missed heartbeat",
                "actor": "coordinator",
            },
        )

        data = _parse(result)
        assert data["code"] == ErrorCode.CONFLICT
        assert data["details"] == {"issue_id": issue.id, "observed": "agent-current", "expected": "agent-old"}


class TestGetChanges:
    async def test_get_changes(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Track me")
        mcp_db.update_issue(issue.id, status="in_progress")
        result = await call_tool("get_changes", {"since": "2000-01-01T00:00:00+00:00"})
        data = _parse(result)
        assert len(data["items"]) >= 2  # created + status_changed
        assert data["items"][0]["event_id"]
        assert "id" not in data["items"][0]
        assert any(e["event_type"] == "status_changed" for e in data["items"])

    async def test_get_issue_events_uses_event_id(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Issue events")
        mcp_db.update_issue(issue.id, status="in_progress")

        result = await call_tool("get_issue_events", {"issue_id": issue.id})

        data = _parse(result)
        assert len(data["items"]) >= 1
        assert data["items"][0]["event_id"]
        assert "id" not in data["items"][0]

    @pytest.mark.parametrize("limit", ["5", 0, -1, True])
    async def test_get_issue_events_invalid_limit_returns_validation(self, mcp_db: FiligreeDB, limit: object) -> None:
        issue = mcp_db.create_issue("Issue events invalid limit")

        result = await call_tool("get_issue_events", {"issue_id": issue.id, "limit": limit})

        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION

    async def test_get_changes_empty(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("get_changes", {"since": "2099-01-01T00:00:00+00:00"})
        data = _parse(result)
        assert data["items"] == []
        assert data["has_more"] is False

    async def test_get_changes_with_limit(self, mcp_db: FiligreeDB) -> None:
        for i in range(5):
            mcp_db.create_issue(f"Issue {i}")
        result = await call_tool("get_changes", {"since": "2000-01-01T00:00:00+00:00", "limit": 2})
        data = _parse(result)
        assert len(data["items"]) == 2

    async def test_get_changes_filters_by_actor_and_event_type(self, mcp_db: FiligreeDB) -> None:
        mcp_db.create_issue("Alpha event", actor="agent-alpha")
        mcp_db.create_issue("Beta event", actor="agent-beta")

        result = await call_tool(
            "get_changes",
            {"since": "2000-01-01T00:00:00+00:00", "actor": "agent-alpha", "type": "created"},
        )

        data = _parse(result)
        assert [item["actor"] for item in data["items"]] == ["agent-alpha"]
        assert {item["event_type"] for item in data["items"]} == {"created"}
        assert data["next_since"] == data["items"][-1]["created_at"]

    async def test_get_changes_filters_by_issue_id(self, mcp_db: FiligreeDB) -> None:
        target = mcp_db.create_issue("Target changes")
        other = mcp_db.create_issue("Other changes")
        mcp_db.update_issue(target.id, status="in_progress", actor="agent-alpha")
        mcp_db.update_issue(other.id, status="in_progress", actor="agent-beta")

        result = await call_tool("get_changes", {"since": "2000-01-01T00:00:00+00:00", "issue_id": target.id})

        data = _parse(result)
        assert data["items"]
        assert {item["issue_id"] for item in data["items"]} == {target.id}

    async def test_get_changes_filters_by_label_before_pagination(self, mcp_db: FiligreeDB) -> None:
        target = mcp_db.create_issue("Labelled changes", labels=["cluster:focus"])
        mcp_db.update_issue(target.id, status="in_progress", actor="agent-alpha")
        for i in range(5):
            mcp_db.create_issue(f"Noise {i}")

        result = await call_tool(
            "get_changes",
            {"since": "2000-01-01T00:00:00+00:00", "label": "cluster:focus", "limit": 1},
        )

        data = _parse(result)
        assert len(data["items"]) == 1
        assert data["items"][0]["issue_id"] == target.id
        assert data["has_more"] is True
        assert data["next_since"] == data["items"][0]["created_at"]

    async def test_get_changes_can_resume_inside_same_timestamp_group(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("MCP same timestamp cursor")
        tied_timestamp = "2026-01-01T00:00:00+00:00"
        cursor = mcp_db.conn.execute(
            "INSERT INTO events (issue_id, event_type, actor, created_at) VALUES (?, ?, ?, ?)",
            (issue.id, "title_changed", "import", tied_timestamp),
        ).lastrowid
        expected = mcp_db.conn.execute(
            "INSERT INTO events (issue_id, event_type, actor, created_at) VALUES (?, ?, ?, ?)",
            (issue.id, "status_changed", "import", tied_timestamp),
        ).lastrowid
        mcp_db.conn.commit()

        result = await call_tool(
            "get_changes",
            {"since": tied_timestamp, "after_event_id": cursor, "limit": 1, "include_heartbeats": True},
        )

        data = _parse(result)
        assert [item["event_id"] for item in data["items"]] == [expected]
        assert data["next_since"] == tied_timestamp
        assert data["next_event_id"] == expected


class TestActorIdentity:
    async def test_update_with_actor(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Actor test")
        await call_tool("update_issue", {"issue_id": issue.id, "status": "in_progress", "actor": "agent-alpha"})
        events = mcp_db.get_recent_events(limit=5)
        status_event = next(e for e in events if e["event_type"] == "status_changed")
        assert status_event["actor"] == "agent-alpha"

    async def test_close_with_actor(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Actor close")
        await call_tool("close_issue", {"issue_id": issue.id, "actor": "agent-beta"})
        events = mcp_db.get_recent_events(limit=5)
        close_event = next(e for e in events if e["event_type"] == "status_changed" and e["new_value"] == "closed")
        assert close_event["actor"] == "agent-beta"

    async def test_default_actor_is_mcp(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Default actor")
        await call_tool("update_issue", {"issue_id": issue.id, "status": "in_progress"})
        events = mcp_db.get_recent_events(limit=5)
        status_event = next(e for e in events if e["event_type"] == "status_changed")
        assert status_event["actor"] == "mcp"


class TestResource:
    async def test_get_schema_documents_entity_id_prefixes(self, mcp_db: FiligreeDB) -> None:
        tools = {tool.name for tool in await list_tools()}
        assert "schema_get" in tools

        result = await call_tool("get_schema", {})

        data = _parse(result)
        assert data["project_prefix"] == "mcp"
        prefixes = data["entity_id_prefixes"]
        assert prefixes["issue"]["prefix"] == "mcp-"
        assert prefixes["issue"]["primary_key"] == "issue_id"
        # accepted_by_tools emits the served (namespaced) names, matching list_tools().
        assert "issue_get" in prefixes["issue"]["accepted_by_tools"]
        assert prefixes["observation"]["prefix"] == "mcp-obs-"
        assert prefixes["observation"]["primary_key"] == "observation_id"
        assert "observation_promote" in prefixes["observation"]["accepted_by_tools"]
        assert "observation_batch_promote" in prefixes["observation"]["accepted_by_tools"]
        assert prefixes["scan_finding"]["prefix"] == "mcp-sf-"
        assert prefixes["scan_finding"]["primary_key"] == "finding_id"
        assert "finding_promote" in prefixes["scan_finding"]["accepted_by_tools"]
        assert prefixes["file_record"]["prefix"] == "mcp-f-"
        assert prefixes["file_record"]["primary_key"] == "file_id"
        assert "file_get" in prefixes["file_record"]["accepted_by_tools"]
        assert "file_delete" in prefixes["file_record"]["accepted_by_tools"]

    async def test_get_schema_accepted_tools_are_derived_from_live_registry(self, mcp_db: FiligreeDB) -> None:
        field_map = {
            "issue": {
                "issue_id",
                "issue_ids",
                "from_issue_id",
                "to_issue_id",
                "parent_issue_id",
                "milestone_id",
                "phase_id",
                "step_id",
                "old_depends_on_id",
                "new_depends_on_id",
                "source_issue_id",
            },
            "observation": {"observation_id", "observation_ids"},
            "scan_finding": {"finding_id", "finding_ids", "source_finding_id"},
            "file_record": {"file_id", "file_ids"},
            "annotation": {"annotation_id", "annotation_ids"},
        }
        expected: dict[str, list[str]] = {entity: [] for entity in field_map}
        # Both surfaces now emit the served (namespaced) names: list_tools()
        # serves them, and get_schema's accepted_by_tools is translated through
        # RENAME_MAP at the emission point. So compare new names directly.
        for tool in await list_tools():
            props = tool.inputSchema.get("properties", {}) if isinstance(tool.inputSchema, dict) else {}
            prop_names = set(props) if isinstance(props, dict) else set()
            for entity, fields in field_map.items():
                if prop_names & fields:
                    expected[entity].append(tool.name)

        data = _parse(await call_tool("get_schema", {}))
        actual = {entity: schema["accepted_by_tools"] for entity, schema in data["entity_id_prefixes"].items()}

        assert actual == {entity: sorted(set(tools)) for entity, tools in expected.items()}

    async def test_get_mcp_status_reports_compatible_database(self, mcp_db: FiligreeDB) -> None:
        tools = {tool.name for tool in await list_tools()}
        assert "mcp_status_get" in tools

        result = await call_tool("get_mcp_status", {})

        data = _parse(result)
        assert data["status"] == "ok"
        assert data["db_initialized"] is True
        assert data["schema_compatible"] is True
        assert data["database_schema_version"] == data["installed_schema_version"]
        assert data["code"] is None

    async def test_get_mcp_status_survives_schema_mismatch(self, mcp_db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
        import filigree.mcp_server as mcp_mod
        from filigree.types.api import SchemaVersionMismatchError

        monkeypatch.setattr(mcp_mod, "db", None)
        monkeypatch.setattr(mcp_mod, "_schema_mismatch", SchemaVersionMismatchError(installed=8, database=9))
        monkeypatch.setattr(mcp_mod, "_db_open_error", None)

        status = _parse(await call_tool("get_mcp_status", {}))
        blocked = _parse(await call_tool("get_schema", {}))

        assert status["status"] == "schema_mismatch"
        assert status["db_initialized"] is False
        assert status["schema_compatible"] is False
        assert status["installed_schema_version"] == 8
        assert status["database_schema_version"] == 9
        assert status["code"] == ErrorCode.SCHEMA_MISMATCH
        assert "upgrade" in status["guidance"].lower()
        assert blocked["code"] == ErrorCode.SCHEMA_MISMATCH

    async def test_runtime_schema_drift_gates_writes(self, mcp_db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
        """Senior-user MCP review run e P2.5: detect post-init forward drift.

        Init-time gate only trips when ``_schema_mismatch`` is set. A
        long-running MCP can have its DB forward-migrated by a sibling
        process — init succeeded with version N, but ``user_version``
        now reports N+1. The runtime gate re-checks every call and
        fail-closes when the DB is strictly newer than the installed
        binary; ``get_mcp_status`` stays available for diagnosis.
        """
        import filigree.mcp_server as mcp_mod

        # Ensure init-time global is clean — we're testing the runtime path.
        monkeypatch.setattr(mcp_mod, "_schema_mismatch", None)
        monkeypatch.setattr(mcp_mod, "db", mcp_db)
        # Spoof a forward-drifted DB version (current+1).
        installed = CURRENT_SCHEMA_VERSION
        monkeypatch.setattr(mcp_db, "get_schema_version", lambda: installed + 1)

        # Diagnostic stays available — it must not be gated even under drift.
        status = _parse(await call_tool("get_mcp_status", {}))
        assert status["status"] == "schema_mismatch"
        assert status["database_schema_version"] == installed + 1

        # Writes are gated.
        blocked = _parse(await call_tool("create_issue", {"title": "should be blocked"}))
        assert blocked["code"] == ErrorCode.SCHEMA_MISMATCH

    async def test_runtime_schema_drift_uses_request_scoped_db(self, mcp_db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
        """Server-mode HTTP has no module-global DB; the runtime drift gate
        must inspect the same request-scoped DB that handlers will use.
        """
        import filigree.mcp_server as mcp_mod

        installed = CURRENT_SCHEMA_VERSION
        monkeypatch.setattr(mcp_mod, "_schema_mismatch", None)
        monkeypatch.setattr(mcp_mod, "db", None)
        monkeypatch.setattr(mcp_db, "get_schema_version", lambda: installed + 1)

        token = mcp_mod._request_db.set(mcp_db)
        try:
            blocked = _parse(await call_tool("list_issues", {}))
        finally:
            mcp_mod._request_db.reset(token)

        assert blocked["code"] == ErrorCode.SCHEMA_MISMATCH
        assert f"v{installed + 1}" in blocked["error"]

    async def test_list_resources(self, mcp_db: FiligreeDB) -> None:
        resources = await list_resources()
        assert len(resources) == 1
        assert resources[0].name == "Project Pulse"
        assert str(resources[0].uri) == "filigree://context"

    async def test_read_context_resource(self, mcp_db: FiligreeDB) -> None:
        mcp_db.create_issue("Resource test")
        content = await read_context("filigree://context")
        assert "Project Pulse" in content
        assert "Resource test" in content

    async def test_read_context_resource_reports_schema_mismatch(self, mcp_db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
        import filigree.mcp_server as mcp_mod
        from filigree.types.api import SchemaVersionMismatchError

        monkeypatch.setattr(mcp_mod, "db", None)
        monkeypatch.setattr(mcp_mod, "_schema_mismatch", SchemaVersionMismatchError(installed=8, database=9))
        monkeypatch.setattr(mcp_mod, "_db_open_error", None)

        content = await read_context("filigree://context")
        data = json.loads(content)

        assert data["code"] == ErrorCode.SCHEMA_MISMATCH
        assert "upgrade" in data["error"].lower()

    async def test_read_unknown_resource(self, mcp_db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="Unknown resource"):
            await read_context("filigree://nonexistent")


class TestPrompt:
    async def test_get_workflow_prompt(self, mcp_db: FiligreeDB) -> None:
        result = await get_workflow_prompt("filigree-workflow", None)
        assert result.description is not None
        assert "workflow" in result.description.lower()
        assert len(result.messages) >= 1
        # First message should contain the workflow text
        assert "Filigree Workflow" in result.messages[0].content.text

    async def test_workflow_prompt_includes_context(self, mcp_db: FiligreeDB) -> None:
        mcp_db.create_issue("Prompt context test")
        result = await get_workflow_prompt("filigree-workflow", None)
        assert len(result.messages) == 2
        # Second message should be the project summary
        assert "Project Pulse" in result.messages[1].content.text

    async def test_workflow_prompt_excludes_context(self, mcp_db: FiligreeDB) -> None:
        result = await get_workflow_prompt("filigree-workflow", {"include_context": "false"})
        assert len(result.messages) == 1

    async def test_workflow_prompt_recommends_start_work_flow(self, mcp_db: FiligreeDB) -> None:
        result = await get_workflow_prompt("filigree-workflow", {"include_context": "false"})
        text = result.messages[0].content.text
        assert "Use `work_start` or `work_start_next`" in text
        assert "`work_claim` / `work_claim_next` — claim-only" in text
        assert "Use `work_claim` or `work_claim_next` to atomically claim a task" not in text


class TestProactiveContext:
    async def test_close_returns_newly_unblocked(self, mcp_db: FiligreeDB) -> None:
        blocker = mcp_db.create_issue("Blocker")
        blocked = mcp_db.create_issue("Blocked task")
        mcp_db.add_dependency(blocked.id, blocker.id)
        result = await call_tool("close_issue", {"issue_id": blocker.id})
        data = _parse(result)
        assert "newly_unblocked" in data
        assert len(data["newly_unblocked"]) == 1
        assert data["newly_unblocked"][0]["issue_id"] == blocked.id
        # Wire format: SlimIssue 5-key contract (Phase D3 vocabulary)
        assert set(data["newly_unblocked"][0].keys()) == {"issue_id", "title", "status", "priority", "type"}

    async def test_close_no_unblocked_items(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Standalone")
        result = await call_tool("close_issue", {"issue_id": issue.id})
        data = _parse(result)
        assert "newly_unblocked" not in data

    async def test_batch_close_returns_newly_unblocked(self, mcp_db: FiligreeDB) -> None:
        b1 = mcp_db.create_issue("Blocker 1")
        b2 = mcp_db.create_issue("Blocker 2")
        blocked = mcp_db.create_issue("Doubly blocked")
        mcp_db.add_dependency(blocked.id, b1.id)
        mcp_db.add_dependency(blocked.id, b2.id)
        result = await call_tool("batch_close", {"issue_ids": [b1.id, b2.id]})
        data = _parse(result)
        assert "newly_unblocked" in data
        assert any(item["issue_id"] == blocked.id for item in data["newly_unblocked"])


class TestMetrics:
    async def test_get_metrics(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Metric test")
        mcp_db.update_issue(issue.id, status="in_progress")
        mcp_db.close_issue(issue.id)
        result = await call_tool("get_metrics", {})
        data = _parse(result)
        assert "throughput" in data
        assert "avg_cycle_time_hours" in data
        assert data["throughput"] >= 1
        assert data["period_days"] == 30

    async def test_get_metrics_with_days(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("get_metrics", {"days": 7})
        data = _parse(result)
        assert data["period_days"] == 7

    @pytest.mark.parametrize("days", ["7", 0, -1, True])
    async def test_get_metrics_invalid_days_returns_validation(self, mcp_db: FiligreeDB, days: object) -> None:
        result = await call_tool("get_metrics", {"days": days})
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION


class TestCriticalPathMCP:
    async def test_get_critical_path(self, mcp_db: FiligreeDB) -> None:
        a = mcp_db.create_issue("A")
        b = mcp_db.create_issue("B")
        mcp_db.add_dependency(a.id, b.id)
        result = await call_tool("get_critical_path", {})
        data = _parse(result)
        assert data["length"] == 2
        assert len(data["path"]) == 2
        assert data["path"][0]["issue_id"]
        assert "id" not in data["path"][0]

    async def test_get_critical_path_empty(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("get_critical_path", {})
        data = _parse(result)
        assert data["length"] == 0
        assert data["path"] == []
        assert data["note"] == "no open dependency chains"


class TestUnknownTool:
    async def test_unknown_tool(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("nonexistent_tool", {})
        data = _parse(result)
        assert data["code"] == ErrorCode.NOT_FOUND


class TestTextHelper:
    def test_text_string(self) -> None:
        result = _text("hello")
        assert result[0].text == "hello"

    def test_text_dict(self) -> None:
        result = _text({"key": "value"})
        assert '"key"' in result[0].text


class TestWorkflowTemplateTools:
    """Tests for Batch 1 — MCP read tools for workflow templates."""

    async def test_list_types(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("list_types", {})
        data = _parse(result)
        items = data["items"]
        assert isinstance(items, list)
        assert len(items) >= 2  # At least task and bug from core pack
        type_names = [t["type"] for t in items]
        assert "task" in type_names
        assert "bug" in type_names
        # Each type has required fields
        for t in items:
            assert "display_name" in t
            assert "pack" in t
            assert "states" in t
            assert "initial_state" in t

    async def test_list_types_sorted(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("list_types", {})
        data = _parse(result)
        type_names = [t["type"] for t in data["items"]]
        assert type_names == sorted(type_names)

    async def test_get_type_info_task(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("get_type_info", {"type": "task"})
        data = _parse(result)
        assert data["type"] == "task"
        assert data["display_name"] == "Task"
        assert len(data["states"]) >= 3  # open, in_progress, closed at minimum
        assert len(data["transitions"]) >= 2
        assert "fields_schema" in data

    async def test_get_type_info_not_found(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("get_type_info", {"type": "nonexistent"})
        data = _parse(result)
        assert data["code"] == ErrorCode.NOT_FOUND

    async def test_get_type_info_fields_have_options(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("get_type_info", {"type": "bug"})
        data = _parse(result)
        severity_field = next((f for f in data["fields_schema"] if f["name"] == "severity"), None)
        assert severity_field is not None
        assert "options" in severity_field

    async def test_list_packs(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("list_packs", {})
        data = _parse(result)
        items = data["items"]
        assert isinstance(items, list)
        assert len(items) >= 2  # core + planning
        pack_names = [p["pack"] for p in items]
        assert "core" in pack_names
        for p in items:
            assert "version" in p
            assert "types" in p
            assert "requires_packs" in p

    async def test_get_valid_transitions(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Transition test", type="task")
        result = await call_tool("get_valid_transitions", {"issue_id": issue.id})
        data = _parse(result)
        assert set(data) == {"items", "has_more"}
        assert data["has_more"] is False
        assert len(data["items"]) >= 1
        for t in data["items"]:
            assert "to" in t
            assert "category" in t
            assert "ready" in t
            assert "missing_fields" in t
            # H1 regression: missing_fields must always be list[str], never list[dict]
            assert isinstance(t["missing_fields"], list)
            for mf in t["missing_fields"]:
                assert isinstance(mf, str), f"missing_fields element should be str, got {type(mf)}"

    async def test_get_valid_transitions_enforcement_always_str(self, mcp_db: FiligreeDB) -> None:
        """enforcement field must always be str, never None — even when source is None."""
        from dataclasses import replace
        from unittest.mock import patch

        issue = mcp_db.create_issue("Enforcement coercion test", type="task")
        real_transitions = mcp_db.get_valid_transitions(issue.id)
        # Inject a None enforcement to simulate the edge case
        patched = [replace(t, enforcement=None) for t in real_transitions]
        with patch.object(mcp_db, "get_valid_transitions", return_value=patched):
            result = await call_tool("get_valid_transitions", {"issue_id": issue.id})
        data = _parse(result)
        for t in data["items"]:
            assert isinstance(t["enforcement"], str), f"enforcement should be str, got {type(t['enforcement'])}: {t['enforcement']}"

    async def test_get_valid_transitions_not_found(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("get_valid_transitions", {"issue_id": "mcp-nonexistent"})
        data = _parse(result)
        assert data["code"] == ErrorCode.NOT_FOUND

    async def test_validate_issue(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Validate test", type="task")
        result = await call_tool("validate_issue", {"issue_id": issue.id})
        data = _parse(result)
        assert "valid" in data
        assert "warnings" in data
        assert "errors" in data
        assert data["valid"] is True

    async def test_validate_issue_not_found(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("validate_issue", {"issue_id": "mcp-nonexistent"})
        data = _parse(result)
        assert data["code"] == ErrorCode.NOT_FOUND

    async def test_get_workflow_guide(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("get_workflow_guide", {"pack": "core"})
        data = _parse(result)
        assert data["pack"] == "core"
        # Guide content may or may not be present depending on the pack data
        assert "guide" in data

    async def test_get_workflow_guide_not_found(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("get_workflow_guide", {"pack": "nonexistent"})
        data = _parse(result)
        assert data["code"] == ErrorCode.NOT_FOUND

    async def test_explain_status(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("explain_status", {"type": "task", "status": "open"})
        data = _parse(result)
        assert data["status"] == "open"
        assert data["category"] == "open"
        assert data["type"] == "task"
        assert "inbound_transitions" in data
        assert "outbound_transitions" in data
        assert "required_fields" in data

    async def test_explain_status_unknown_type(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("explain_status", {"type": "nonexistent", "status": "open"})
        data = _parse(result)
        assert data["code"] == ErrorCode.NOT_FOUND

    async def test_explain_status_unknown_status(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("explain_status", {"type": "task", "status": "nonexistent"})
        data = _parse(result)
        assert data["code"] == ErrorCode.NOT_FOUND

    async def test_reload_templates(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("reload_templates", {})
        data = _parse(result)
        assert data["status"] == "ok"
        # Templates should still work after reload
        result2 = await call_tool("list_types", {})
        data2 = _parse(result2)
        assert len(data2["items"]) >= 2

    async def test_reload_templates_corrupt_config_returns_structured_error(self, mcp_db: FiligreeDB) -> None:
        """Bug filigree-5c9f9aa7c2: corrupt config.json must not crash the MCP tool."""
        # Clear the explicit override so reload_templates re-reads config.json.
        mcp_db._enabled_packs_override = None
        config_path = mcp_db.db_path.parent / "config.json"
        config_path.write_text("{not valid json")

        result = await call_tool("reload_templates", {})
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION
        assert "config.json" in data["error"]

    async def test_reload_templates_refreshes_context_md(self, mcp_db: FiligreeDB) -> None:
        """Bug filigree-33e7bf9947: reload_templates must refresh context.md so
        template-derived sections reflect the new registry."""
        summary_path = mcp_db.db_path.parent / SUMMARY_FILENAME
        # Create a marker we know _refresh_summary will overwrite.
        summary_path.write_text("STALE-MARKER-BEFORE-RELOAD")

        result = await call_tool("reload_templates", {})
        data = _parse(result)
        assert data["status"] == "ok"
        assert summary_path.read_text() != "STALE-MARKER-BEFORE-RELOAD"


class TestMCPMutationEnhancements:
    """Tests for Batch 2 — enhanced error handling and new features."""

    async def test_get_issue_with_transitions(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Test transitions", type="task")
        result = await call_tool("get_issue", {"issue_id": issue.id, "include_transitions": True})
        data = _parse(result)
        assert data["title"] == "Test transitions"
        assert "valid_transitions" in data
        assert isinstance(data["valid_transitions"], list)
        assert len(data["valid_transitions"]) >= 1
        # H1 regression: missing_fields shape must be list[str] in both MCP paths
        for t in data["valid_transitions"]:
            for mf in t.get("missing_fields", []):
                assert isinstance(mf, str), f"missing_fields element should be str, got {type(mf)}"

    async def test_get_issue_without_transitions(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("No transitions")
        result = await call_tool("get_issue", {"issue_id": issue.id})
        data = _parse(result)
        assert "valid_transitions" not in data

    async def test_list_issues_status_category(self, mcp_db: FiligreeDB) -> None:
        a = mcp_db.create_issue("Open one")
        b = mcp_db.create_issue("WIP one")
        mcp_db.update_issue(b.id, status="in_progress")
        result = await call_tool("list_issues", {"status_category": "open"})
        data = _parse(result)
        issues = data["items"]
        # Should return open issues (not in_progress ones)
        statuses = {d["status"] for d in issues}
        assert "in_progress" not in statuses
        assert a.id in [d["issue_id"] for d in issues]

    async def test_update_issue_error_includes_transitions(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Error test", type="bug")
        # Try an invalid status
        result = await call_tool("update_issue", {"issue_id": issue.id, "status": "nonexistent_state"})
        data = _parse(result)
        assert data["code"] == ErrorCode.INVALID_TRANSITION
        assert "valid_transitions" in data
        assert "hint" in data

    async def test_update_closed_issue_hint_points_to_reopen(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Closed update hint", type="task")
        mcp_db.close_issue(issue.id, reason="done")

        result = await call_tool("update_issue", {"issue_id": issue.id, "status": "in_progress"})

        data = _parse(result)
        assert data["code"] == ErrorCode.INVALID_TRANSITION
        assert data["valid_transitions"] == []
        assert data["reopen_available"] is True
        assert "issue_reopen" in data["hint"]

    async def test_transition_error_survives_enrichment_backend_failure(self, mcp_db: FiligreeDB) -> None:
        """filigree-55c5347992: enrichment lookup failure must not mask invalid_transition."""
        import sqlite3

        from filigree.mcp_tools.common import _build_transition_error

        issue = mcp_db.create_issue("Crash enrichment", type="bug")
        with patch.object(
            mcp_db,
            "get_valid_transitions",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            data = _build_transition_error(mcp_db, issue.id, "bad state")
        assert data["code"] == ErrorCode.INVALID_TRANSITION
        assert data["error"] == "bad state"
        # Enrichment fields are absent — lookup failed, but original error is preserved.
        assert "valid_transitions" not in data
        assert "hint" not in data

    async def test_claim_next_success(self, mcp_db: FiligreeDB) -> None:
        mcp_db.create_issue("Ready task", type="task", priority=1)
        result = await call_tool("claim_next", {"assignee": "agent-1"})
        data = _parse(result)
        assert data["assignee"] == "agent-1"
        assert data["title"] == "Ready task"
        assert "selection_reason" in data
        assert isinstance(data["selection_reason"], str)

    async def test_claim_next_empty(self, mcp_db: FiligreeDB) -> None:
        # The auto-seeded "Future" release is the only ready issue, so
        # claim it first, then the next claim_next should be empty.
        first = await call_tool("claim_next", {"assignee": "agent-0"})
        first_data = _parse(first)
        assert first_data["title"] == "Future"

        result = await call_tool("claim_next", {"assignee": "agent-1"})
        data = _parse(result)
        assert data["status"] == "empty"

    async def test_claim_next_with_type_filter(self, mcp_db: FiligreeDB) -> None:
        mcp_db.create_issue("A task", type="task")
        mcp_db.create_issue("A bug", type="bug")
        result = await call_tool("claim_next", {"assignee": "agent-1", "type": "bug"})
        data = _parse(result)
        assert data["type"] == "bug"

    async def test_claim_next_with_priority_filter(self, mcp_db: FiligreeDB) -> None:
        mcp_db.create_issue("Low priority", type="task", priority=4)
        mcp_db.create_issue("High priority", type="task", priority=0)
        result = await call_tool("claim_next", {"assignee": "agent-1", "priority_max": 2})
        data = _parse(result)
        assert data["priority"] <= 2

    async def test_batch_close_partial_failure(self, mcp_db: FiligreeDB) -> None:
        a = mcp_db.create_issue("Closeable")
        result = await call_tool("batch_close", {"issue_ids": [a.id, "mcp-nonexistent"]})
        data = _parse(result)
        assert len(data["succeeded"]) == 1
        assert a.id in {s["issue_id"] for s in data["succeeded"]}
        assert len(data["failed"]) == 1
        assert data["failed"][0]["id"] == "mcp-nonexistent"

    async def test_batch_update_partial_failure(self, mcp_db: FiligreeDB) -> None:
        a = mcp_db.create_issue("Updatable")
        result = await call_tool("batch_update", {"issue_ids": [a.id, "mcp-nonexistent"], "priority": 0})
        data = _parse(result)
        assert len(data["succeeded"]) == 1
        assert a.id in {s["issue_id"] for s in data["succeeded"]}
        assert len(data["failed"]) == 1


class TestCoreReloadTemplates:
    """Test core.py reload_templates method."""

    def test_reload_clears_registry(self, mcp_db: FiligreeDB) -> None:
        # Access templates to load them
        _ = mcp_db.templates.list_types()
        assert mcp_db._template_registry is not None
        mcp_db.reload_templates()
        assert mcp_db._template_registry is None
        # Should reload on next access
        types = mcp_db.templates.list_types()
        assert len(types) >= 2

    def test_reload_updates_enabled_packs_from_config(self, mcp_db: FiligreeDB) -> None:
        """Bug filigree-8fc86587f1: reload_templates must update enabled_packs
        from config.json, not keep stale constructor value."""
        import json

        # Initial state: default packs
        _ = mcp_db.templates.list_types()
        original_packs = mcp_db.enabled_packs[:]

        # Write new config with incident pack added
        config_path = mcp_db.db_path.parent / "config.json"
        config = json.loads(config_path.read_text())
        config["enabled_packs"] = ["core", "planning", "release", "incident"]
        config_path.write_text(json.dumps(config))

        # Reload templates
        mcp_db.reload_templates()
        _ = mcp_db.templates.list_types()

        # enabled_packs should reflect the updated config
        assert "incident" in mcp_db.enabled_packs
        assert mcp_db.enabled_packs != original_packs

    def test_reload_with_explicit_override_keeps_override(self, tmp_path: Path) -> None:
        """When enabled_packs was explicitly set in constructor, reload should
        still honour the override (not fall back to config.json)."""
        filigree_dir = tmp_path / FILIGREE_DIR_NAME
        filigree_dir.mkdir()
        write_config(filigree_dir, {"prefix": "test", "version": 1, "enabled_packs": ["core", "planning", "release"]})
        d = FiligreeDB(filigree_dir / DB_FILENAME, prefix="test", enabled_packs=["core"])
        d.initialize()
        try:
            _ = d.templates.list_types()
            assert d.enabled_packs == ["core"]
            d.reload_templates()
            _ = d.templates.list_types()
            # Should still be the explicit override, not config.json
            assert d.enabled_packs == ["core"]
        finally:
            d.close()


class TestDynamicWorkflowPrompt:
    """Test the dynamic workflow prompt builder."""

    async def test_workflow_prompt_includes_types(self, mcp_db: FiligreeDB) -> None:
        result = await get_workflow_prompt("filigree-workflow", {"include_context": "false"})
        assert len(result.messages) >= 1
        text = result.messages[0].content.text
        assert "task" in text.lower()
        assert "Registered Types" in text or "Key tools" in text

    async def test_build_workflow_text_error_logs_at_error_level(self, mcp_db: FiligreeDB) -> None:
        """Bug filigree-964d67: template registry failures must log at error, not warning."""
        import logging

        import filigree.mcp_server as mcp_mod

        original = mcp_mod._get_db

        def _boom() -> None:
            raise RuntimeError("template registry broken")

        mcp_mod._get_db = _boom  # type: ignore[assignment]
        try:
            logger = logging.getLogger("filigree.mcp_server")
            with pytest.MonkeyPatch.context() as mp:
                logged_errors: list[str] = []
                logged_warnings: list[str] = []
                mp.setattr(logger, "error", lambda msg, *a, **kw: logged_errors.append(msg))
                mp.setattr(logger, "warning", lambda msg, *a, **kw: logged_warnings.append(msg))
                from filigree.mcp_server import _build_workflow_text

                text = _build_workflow_text()
            assert "Failed to load workflow types" in text
            assert len(logged_errors) == 1
            assert logged_warnings == []
        finally:
            mcp_mod._get_db = original

    async def test_workflow_prompt_fallback_without_db(self) -> None:
        import filigree.mcp_server as mcp_mod

        original_db = mcp_mod.db
        mcp_mod.db = None
        try:
            result = await get_workflow_prompt("filigree-workflow", {"include_context": "false"})
            text = result.messages[0].content.text
            assert "Filigree Workflow" in text
        finally:
            mcp_mod.db = original_db


class TestPromptRuntimeErrorNarrowing:
    """Bug filigree-0458c5: get_workflow_prompt should only silence 'not initialized' RuntimeError."""

    async def test_unexpected_runtime_error_is_logged(self, mcp_db: FiligreeDB) -> None:
        """Non-initialization RuntimeErrors must be logged at error level, not swallowed."""
        import logging

        import filigree.mcp_server as mcp_mod

        original = mcp_mod.generate_summary  # type: ignore[attr-defined]

        def _boom(_db: object) -> str:
            raise RuntimeError("maximum recursion depth exceeded")

        mcp_mod.generate_summary = _boom  # type: ignore[attr-defined, assignment]
        try:
            logger = logging.getLogger("filigree.mcp_server")
            with pytest.MonkeyPatch.context() as mp:
                logged_errors: list[str] = []
                mp.setattr(logger, "error", lambda msg, *a, **kw: logged_errors.append(msg))
                result = await get_workflow_prompt("filigree-workflow", {"include_context": "true"})
            assert len(result.messages) >= 1  # prompt still returned
            assert any("Unexpected" in e for e in logged_errors)
        finally:
            mcp_mod.generate_summary = original  # type: ignore[attr-defined]

    async def test_not_initialized_error_is_silenced(self, mcp_db: FiligreeDB) -> None:
        """'not initialized' RuntimeError should be silently ignored (expected at startup)."""
        import logging

        import filigree.mcp_server as mcp_mod

        original = mcp_mod.generate_summary  # type: ignore[attr-defined]

        def _not_init(_db: object) -> str:
            raise RuntimeError("DB not initialized")

        mcp_mod.generate_summary = _not_init  # type: ignore[attr-defined, assignment]
        try:
            logger = logging.getLogger("filigree.mcp_server")
            with pytest.MonkeyPatch.context() as mp:
                logged_errors: list[str] = []
                mp.setattr(logger, "error", lambda msg, *a, **kw: logged_errors.append(msg))
                result = await get_workflow_prompt("filigree-workflow", {"include_context": "true"})
            assert len(result.messages) == 1  # no context appended
            assert logged_errors == []  # no error logged
        finally:
            mcp_mod.generate_summary = original  # type: ignore[attr-defined]


class TestInstructionsUpdate:
    """The injected prompt is intentionally lean — command catalogues are
    discoverable via `--help` and MCP tool schemas. What the prompt itself
    must pre-load is the action loop, behavioural policy, and project enums.
    These tests pin the load-bearing pieces."""

    def test_instructions_include_atomic_workflow(self) -> None:
        from filigree.install import FILIGREE_INSTRUCTIONS

        assert "start-next-work" in FILIGREE_INSTRUCTIONS
        assert "start-work" in FILIGREE_INSTRUCTIONS
        assert "session-context" in FILIGREE_INSTRUCTIONS

    def test_instructions_include_observation_policy(self) -> None:
        from filigree.install import FILIGREE_INSTRUCTIONS

        lower = FILIGREE_INSTRUCTIONS.lower()
        assert "incidental" in lower
        assert "current task" in lower

    def test_instructions_include_priority_scale(self) -> None:
        from filigree.install import FILIGREE_INSTRUCTIONS

        for level in ("P0", "P1", "P2", "P3", "P4"):
            assert level in FILIGREE_INSTRUCTIONS

    def test_instructions_include_invalid_transition_recovery(self) -> None:
        """An agent hitting INVALID_TRANSITION needs to know how to recover."""
        from filigree.install import FILIGREE_INSTRUCTIONS

        assert "INVALID_TRANSITION" in FILIGREE_INSTRUCTIONS
        # MCP tool namespaced old->new (ADR-016 §7): get_valid_transitions is
        # now served as workflow_transition_list.
        assert "workflow_transition_list" in FILIGREE_INSTRUCTIONS


class TestSafePath:
    """Tests for the _safe_path() path traversal guard."""

    def test_rejects_absolute_path(self, mcp_db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="Absolute paths not allowed"):
            _safe_path("/etc/passwd")

    def test_rejects_dotdot_escape(self, mcp_db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="Path escapes project directory"):
            _safe_path("../../etc/passwd")

    def test_rejects_dotdot_in_middle(self, mcp_db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="Path escapes project directory"):
            _safe_path("subdir/../../etc/passwd")

    def test_allows_valid_relative_path(self, mcp_db: FiligreeDB) -> None:
        result = _safe_path("backup.jsonl")
        assert result.name == "backup.jsonl"

    def test_allows_subdirectory_path(self, mcp_db: FiligreeDB) -> None:
        result = _safe_path("backups/export.jsonl")
        assert result.name == "export.jsonl"
        assert "backups" in str(result)

    def test_rejects_another_absolute_path(self, mcp_db: FiligreeDB) -> None:
        """Absolute paths on any platform should be rejected."""
        with pytest.raises(ValueError, match="Absolute paths not allowed"):
            _safe_path("/var/data/evil.jsonl")

    def test_project_not_initialized(self) -> None:
        """_safe_path fails gracefully when _filigree_dir is None."""
        import filigree.mcp_server as mcp_mod

        original = mcp_mod._filigree_dir
        mcp_mod._filigree_dir = None
        try:
            with pytest.raises(ValueError, match="Project directory not initialized"):
                _safe_path("test.jsonl")
        finally:
            mcp_mod._filigree_dir = original


class TestExportImportPathTraversal:
    """Tests that export_jsonl and import_jsonl MCP tools reject unsafe paths."""

    async def test_export_rejects_absolute_path(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("export_jsonl", {"output_path": "/var/data/evil.jsonl"})
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION
        assert "Absolute paths not allowed" in data["error"]

    async def test_export_rejects_path_traversal(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("export_jsonl", {"output_path": "../../evil.jsonl"})
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION
        assert "escapes project directory" in data["error"]

    async def test_import_rejects_absolute_path(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("import_jsonl", {"input_path": "/etc/passwd"})
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION
        assert "Absolute paths not allowed" in data["error"]

    async def test_import_rejects_path_traversal(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("import_jsonl", {"input_path": "../../../etc/passwd"})
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION
        assert "escapes project directory" in data["error"]

    async def test_export_allows_valid_relative_path(self, mcp_db: FiligreeDB) -> None:
        # Create an issue so there's data to export
        mcp_db.create_issue("Export test")
        result = await call_tool("export_jsonl", {"output_path": "test-export.jsonl"})
        data = _parse(result)
        assert data["status"] == "ok"
        assert data["records"] >= 1

    async def test_export_io_error_returns_structured_error(self, mcp_db: FiligreeDB) -> None:
        """export_jsonl to a nonexistent directory must return error, not crash."""
        result = await call_tool("export_jsonl", {"output_path": "nonexistent-dir/out.jsonl"})
        data = _parse(result)
        assert "error" in data
        assert data["code"] == ErrorCode.IO

    async def test_import_malformed_jsonl_skips_corrupt_lines(self, mcp_db: FiligreeDB) -> None:
        """import_jsonl with malformed JSONL should skip corrupt lines, not crash."""
        project_root = mcp_db.db_path.parent.parent
        bad_file = project_root / "bad.jsonl"
        bad_file.write_text("this is not valid json\n")
        result = await call_tool("import_jsonl", {"input_path": "bad.jsonl"})
        data = _parse(result)
        assert data["status"] == "ok"
        assert data["records"] == 0
        assert data["skipped_types"]["<corrupt_json>"] == 1

    async def test_import_jsonl_unexpected_exception_propagates(self, mcp_db: FiligreeDB) -> None:
        """Unexpected exceptions (not ValueError/OSError/sqlite3.Error) must propagate."""
        project_root = mcp_db.db_path.parent.parent
        valid_file = project_root / "valid.jsonl"
        valid_file.write_text("")  # empty is fine for path validation

        with (
            patch.object(mcp_db, "import_jsonl", side_effect=RuntimeError("unexpected internal error")),
            pytest.raises(RuntimeError, match="unexpected internal error"),
        ):
            await call_tool("import_jsonl", {"input_path": "valid.jsonl"})

    async def test_import_jsonl_expected_exceptions_handled_gracefully(self, mcp_db: FiligreeDB) -> None:
        """ValueError, OSError, sqlite3.Error are caught and coded by class.

        ValueError is a user-correctable validation failure (malformed record,
        unknown type, foreign-prefix id) and must surface VALIDATION, mirroring
        _handle_export_jsonl; only OSError/sqlite3.Error are transient IO.
        """
        import sqlite3

        from filigree.core import WrongProjectError

        project_root = mcp_db.db_path.parent.parent
        valid_file = project_root / "valid.jsonl"
        valid_file.write_text("")

        cases = [
            (ValueError("bad record"), ErrorCode.VALIDATION),
            (WrongProjectError("issue belongs to another project"), ErrorCode.VALIDATION),
            (OSError("disk gone"), ErrorCode.IO),
            (sqlite3.OperationalError("db locked"), ErrorCode.IO),
        ]
        for exc, expected_code in cases:
            with patch.object(mcp_db, "import_jsonl", side_effect=exc):
                result = await call_tool("import_jsonl", {"input_path": "valid.jsonl"})
                data = _parse(result)
                assert "error" in data, f"{type(exc).__name__} must be caught gracefully"
                assert data["code"] == expected_code, f"{type(exc).__name__} → {expected_code}"


class TestRefreshSummaryLogging:
    """Bug filigree-c13236: _refresh_summary must log even when _logger is None."""

    async def test_refresh_summary_logs_when_logger_is_none(self, mcp_db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
        """When _logger is None, fallback to logging.getLogger(__name__)."""
        import filigree.mcp_server as mcp_mod

        monkeypatch.setattr(mcp_mod, "_logger", None)

        with (
            patch.object(mcp_mod, "write_summary", side_effect=OSError("disk full")),
            patch("logging.getLogger") as mock_get_logger,
        ):
            mock_fallback = MagicMock()
            mock_get_logger.return_value = mock_fallback
            mcp_mod._refresh_summary()
            mock_fallback.warning.assert_called_once()


class TestRefreshSummaryErrorEscalation:
    """Bug filigree-e45c0d: _refresh_summary must log at error for non-OSError exceptions."""

    async def test_db_error_logged_at_error_level(self, mcp_db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
        import sqlite3

        import filigree.mcp_server as mcp_mod

        mock_logger = MagicMock()
        monkeypatch.setattr(mcp_mod, "_logger", mock_logger)

        with patch.object(mcp_mod, "write_summary", side_effect=sqlite3.DatabaseError("database disk image is malformed")):
            mcp_mod._refresh_summary()
            mock_logger.error.assert_called_once()
            assert "malformed" in str(mock_logger.error.call_args) or "context.md" in str(mock_logger.error.call_args)

    async def test_os_error_still_logged_at_warning(self, mcp_db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
        import filigree.mcp_server as mcp_mod

        mock_logger = MagicMock()
        monkeypatch.setattr(mcp_mod, "_logger", mock_logger)

        with patch.object(mcp_mod, "write_summary", side_effect=OSError("disk full")):
            mcp_mod._refresh_summary()
            mock_logger.warning.assert_called_once()
            mock_logger.error.assert_not_called()


class TestMCPTransactionSafety:
    """MCP-level safety net: no dirty transactions survive after failed tool calls."""

    async def test_failed_create_no_dirty_transaction(self, mcp_db: FiligreeDB) -> None:
        """create_issue with invalid deps returns error AND leaves no dirty txn."""
        result = await call_tool(
            "create_issue",
            {"title": "Should fail", "deps": ["nonexistent-dep-id"]},
        )
        data = _parse(result)
        assert "error" in data

        assert not mcp_db.conn.in_transaction, (
            "Dirty transaction left after failed create_issue — next successful commit would flush orphaned writes"
        )

    async def test_failed_update_no_dirty_transaction(self, mcp_db: FiligreeDB) -> None:
        """update_issue with invalid priority returns error AND leaves no dirty txn."""
        create_result = await call_tool("create_issue", {"title": "Valid issue"})
        issue_id = _parse(create_result)["issue_id"]

        result = await call_tool(
            "update_issue",
            {"issue_id": issue_id, "title": "New title", "priority": 99},
        )
        data = _parse(result)
        assert "error" in data

        assert not mcp_db.conn.in_transaction, (
            "Dirty transaction left after failed update_issue — next successful commit would flush orphaned events"
        )

    async def test_unhandled_error_rolls_back_dirty_transaction(self, mcp_db: FiligreeDB) -> None:
        """Safety net: unhandled exception from handler rolls back any dirty txn.

        Simulates a core function that writes to the DB then raises without
        rolling back — the MCP call_tool() safety net must clean up.
        """
        import filigree.mcp_server as mcp_mod

        async def _bad_handler(arguments: dict[str, Any]) -> list[Any]:
            # Simulate a buggy mutation: INSERT a row, then crash
            tracker = mcp_mod._get_db()
            tracker.conn.execute(
                "INSERT INTO issues (id, title, type, status, priority, description, "
                "notes, assignee, parent_id, fields, created_at, updated_at) "
                "VALUES ('orphan-1', 'Orphan', 'task', 'open', 2, '', '', '', NULL, "
                "'{}', '2026-01-01', '2026-01-01')"
            )
            msg = "Simulated unprotected crash"
            raise RuntimeError(msg)

        original = mcp_mod._all_handlers.get("create_issue")
        mcp_mod._all_handlers["create_issue"] = _bad_handler
        try:
            with pytest.raises(RuntimeError):
                await call_tool("create_issue", {"title": "Irrelevant"})
        finally:
            if original is not None:
                mcp_mod._all_handlers["create_issue"] = original

        # The safety net in call_tool() should have rolled back the dirty txn
        assert not mcp_db.conn.in_transaction, "MCP safety net failed — dirty transaction survived after unhandled exception"

        # The orphan row should NOT be visible after rollback
        orphan = mcp_db.conn.execute("SELECT id FROM issues WHERE id = 'orphan-1'").fetchone()
        assert orphan is None, "Orphan issue row survived — rollback did not happen"


class TestFileTools:
    """Tests for MCP file registration, association, and retrieval tools."""

    async def test_list_tools_includes_file_tools(self, mcp_db: FiligreeDB) -> None:
        tools = await list_tools()
        names = {t.name for t in tools}
        assert {
            "file_list",
            "file_get",
            "file_timeline_get",
            "issue_file_list",
            "file_association_add",
            "file_register",
            "file_delete",
        }.issubset(names)

    async def test_create_issue_tool_docs_call_out_labels_at_creation(self, mcp_db: FiligreeDB) -> None:
        tools = await list_tools()
        create_tool = next(t for t in tools if t.name == "issue_create")
        assert "labels" in create_tool.description.lower()
        labels_schema = (create_tool.inputSchema or {}).get("properties", {}).get("labels", {})
        assert "creation" in (labels_schema.get("description") or "").lower()

    async def test_register_file_and_get_file_round_trip(self, mcp_db: FiligreeDB) -> None:
        created = _parse(await call_tool("register_file", {"path": "src/example.py", "language": "python"}))
        assert "error" not in created
        assert created["path"] == "src/example.py"
        assert created["file_id"]
        assert "id" not in created

        detail = _parse(await call_tool("get_file", {"file_id": created["file_id"]}))
        assert detail["file"]["file_id"] == created["file_id"]
        assert "id" not in detail["file"]
        assert detail["file"]["path"] == "src/example.py"

    async def test_register_file_displaced_under_loomweave_mode(
        self, loomweave_mcp_db: FiligreeDB, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="filigree.mcp_tools.files"):
            data = _parse(await call_tool("register_file", {"path": "src/example.py", "language": "python"}))

        assert data["code"] == ErrorCode.FILE_REGISTRY_DISPLACED
        assert "http://localhost:9111/api/v1/files" in data["error"]
        assert "src/example.py" in data["error"]
        records = [record for record in caplog.records if record.message == "file_registry_displaced_registration_rejected"]
        assert records
        record_data = records[0].__dict__
        assert record_data["tool"] == "mcp"
        assert record_data["file_path"] == "src/example.py"
        assert loomweave_mcp_db.registry.is_displaced() is True

    async def test_register_file_displacement_uses_registry_protocol(self, mcp_db: FiligreeDB) -> None:
        # This deliberately constructs an invalid hybrid state that FiligreeDB.__init__
        # rejects. The point is to pin the handler to registry.is_displaced(), not the
        # cached backend label, if future tests monkeypatch the registry protocol.
        mcp_db.registry_backend = "loomweave"
        mcp_db.loomweave_config = {"base_url": "http://localhost:9111"}

        data = _parse(await call_tool("register_file", {"path": "src/protocol.py", "language": "python"}))

        assert "error" not in data
        assert data["path"] == "src/protocol.py"

    async def test_register_file_infers_language_without_hint(self, mcp_db: FiligreeDB) -> None:
        py = _parse(await call_tool("register_file", {"path": "src/inferred.py"}))
        md = _parse(await call_tool("register_file", {"path": "docs/inferred.md"}))
        unknown = _parse(await call_tool("register_file", {"path": "tools/inferred.unknownext"}))

        assert py["language"] == "python"
        assert md["language"] == "markdown"
        assert unknown["language"] == ""

    async def test_delete_file_record_removes_unreferenced_file(self, mcp_db: FiligreeDB) -> None:
        created = _parse(await call_tool("register_file", {"path": "src/delete_me.py"}))

        deleted = _parse(await call_tool("delete_file_record", {"file_id": created["file_id"]}))

        assert deleted["status"] == "deleted"
        assert deleted["file_id"] == created["file_id"]
        assert deleted["deleted_findings"] == 0
        missing = _parse(await call_tool("get_file", {"file_id": created["file_id"]}))
        assert missing["code"] == ErrorCode.NOT_FOUND

    async def test_delete_file_record_refuses_open_findings_without_force(self, mcp_db: FiligreeDB) -> None:
        scan = mcp_db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "src/delete_open_finding.py", "rule_id": "R1", "message": "Open"}],
        )
        file_record = mcp_db.get_file_by_path("src/delete_open_finding.py")
        assert file_record is not None

        result = _parse(await call_tool("delete_file_record", {"file_id": file_record.id}))

        assert result["code"] == ErrorCode.CONFLICT
        assert "open finding" in result["error"]
        assert mcp_db.get_finding(scan["new_finding_ids"][0])["file_id"] == file_record.id

    async def test_delete_file_record_force_cascades_dependents(self, mcp_db: FiligreeDB) -> None:
        scan = mcp_db.process_scan_results(
            scan_source="ruff",
            findings=[{"path": "src/delete_force.py", "rule_id": "R1", "message": "Open"}],
        )
        file_record = mcp_db.get_file_by_path("src/delete_force.py")
        assert file_record is not None
        issue = mcp_db.create_issue("Force delete linked issue")
        mcp_db.add_file_association(file_record.id, issue.id, "mentioned_in")

        result = _parse(await call_tool("delete_file_record", {"file_id": file_record.id, "force": True}))

        assert result["status"] == "deleted"
        assert result["deleted_findings"] == 1
        assert result["deleted_associations"] == 1
        with pytest.raises(KeyError):
            mcp_db.get_finding(scan["new_finding_ids"][0])

    async def test_get_file_recent_findings_use_finding_id(self, mcp_db: FiligreeDB) -> None:
        created = _parse(await call_tool("register_file", {"path": "src/with_finding.py", "language": "python"}))
        scan = mcp_db.process_scan_results(
            scan_source="contract-test",
            findings=[
                {
                    "path": "src/with_finding.py",
                    "rule_id": "R001",
                    "severity": "medium",
                    "message": "shape check",
                }
            ],
        )

        detail = _parse(await call_tool("get_file", {"file_id": created["file_id"]}))

        assert detail["recent_findings"][0]["finding_id"] == scan["new_finding_ids"][0]
        assert "id" not in detail["recent_findings"][0]

    async def test_register_file_is_idempotent_by_path(self, mcp_db: FiligreeDB) -> None:
        first = _parse(await call_tool("register_file", {"path": "src/idempotent.py"}))
        second = _parse(await call_tool("register_file", {"path": "./src/idempotent.py"}))
        assert first["file_id"] == second["file_id"]
        assert "id" not in first
        assert "id" not in second
        assert second["path"] == "src/idempotent.py"

    async def test_register_file_path_traversal_rejected(self, mcp_db: FiligreeDB) -> None:
        result = _parse(await call_tool("register_file", {"path": "../../etc/passwd"}))
        assert result["code"] == ErrorCode.VALIDATION

    async def test_register_file_project_root_dot_rejected(self, mcp_db: FiligreeDB) -> None:
        """Bug filigree-78903e4ff7: path='.' resolves to project root and must return a validation error, not crash."""
        result = _parse(await call_tool("register_file", {"path": "."}))
        assert result.get("code") == ErrorCode.VALIDATION

    async def test_list_files_with_filters(self, mcp_db: FiligreeDB) -> None:
        await call_tool("register_file", {"path": "src/a.py", "language": "python"})
        await call_tool("register_file", {"path": "docs/readme.md", "language": "markdown"})
        result = _parse(await call_tool("list_files", {"path_prefix": "src/", "limit": 10}))
        assert len(result["items"]) == 1
        assert result["items"][0]["path"] == "src/a.py"
        assert result["items"][0]["file_id"]
        assert "id" not in result["items"][0]
        assert result["has_more"] is False

    async def test_list_files_invalid_sort_rejected(self, mcp_db: FiligreeDB) -> None:
        result = _parse(await call_tool("list_files", {"sort": "bad_sort"}))
        assert result["code"] == ErrorCode.VALIDATION

    async def test_list_files_huge_offset_rejected_before_sqlite(self, mcp_db: FiligreeDB) -> None:
        result = _parse(await call_tool("list_files", {"offset": 9_223_372_036_854_775_808}))

        assert result["code"] == ErrorCode.VALIDATION
        assert "offset must be <=" in result["error"]

    async def test_list_files_huge_min_findings_rejected_before_sqlite(self, mcp_db: FiligreeDB) -> None:
        result = _parse(await call_tool("list_files", {"min_findings": 9_223_372_036_854_775_808}))

        assert result["code"] == ErrorCode.VALIDATION
        assert "min_findings must be <=" in result["error"]

    async def test_list_files_sqlite_error_returns_io(self, mcp_db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(*args: Any, **kwargs: Any) -> None:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(mcp_db, "list_files_paginated", _raise)

        result = _parse(await call_tool("list_files", {}))

        assert result["code"] == ErrorCode.IO
        assert "Database error" in result["error"]

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("language", {"bad": "type"}),
            ("path_prefix", {"bad": "type"}),
            ("scan_source", {"bad": "type"}),
        ],
    )
    async def test_list_files_optional_string_filters_reject_non_strings(
        self, mcp_db: FiligreeDB, field: str, value: dict[str, str]
    ) -> None:
        result = _parse(await call_tool("list_files", {field: value}))
        assert result["code"] == ErrorCode.VALIDATION
        assert result["error"] == f"{field} must be a string"

    async def test_add_file_association_and_get_issue_files(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Issue for file association")
        file_data = _parse(await call_tool("register_file", {"path": "src/assoc.py"}))

        created = _parse(
            await call_tool(
                "add_file_association",
                {
                    "file_id": file_data["file_id"],
                    "issue_id": issue.id,
                    "assoc_type": "task_for",
                },
            )
        )
        assert created["status"] == "created"

        files = _parse(await call_tool("get_issue_files", {"issue_id": issue.id}))
        assert "items" in files
        assert files["has_more"] is False
        assert len(files["items"]) == 1
        assert files["items"][0]["file_id"] == file_data["file_id"]
        assert files["items"][0]["assoc_id"]
        assert "id" not in files["items"][0]
        assert files["items"][0]["assoc_type"] == "task_for"

        detail = _parse(await call_tool("get_file", {"file_id": file_data["file_id"]}))
        assert detail["associations"][0]["assoc_id"]
        assert "id" not in detail["associations"][0]

    async def test_add_file_association_invalid_assoc_type(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Issue for invalid assoc")
        file_data = _parse(await call_tool("register_file", {"path": "src/invalid_assoc.py"}))
        result = _parse(
            await call_tool(
                "add_file_association",
                {
                    "file_id": file_data["file_id"],
                    "issue_id": issue.id,
                    "assoc_type": "invalid_assoc",
                },
            )
        )
        assert result["code"] == ErrorCode.VALIDATION

    async def test_add_file_association_file_not_found(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Issue for missing file")
        result = _parse(
            await call_tool(
                "add_file_association",
                {
                    "file_id": "missing-file-id",
                    "issue_id": issue.id,
                    "assoc_type": "task_for",
                },
            )
        )
        assert result["code"] == ErrorCode.NOT_FOUND

    async def test_get_issue_files_not_found(self, mcp_db: FiligreeDB) -> None:
        result = _parse(await call_tool("get_issue_files", {"issue_id": "mcp-nonexistent"}))
        assert result["code"] == ErrorCode.NOT_FOUND

    async def test_get_file_timeline_for_association_event(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Issue for timeline")
        file_data = _parse(await call_tool("register_file", {"path": "src/timeline.py"}))
        _parse(
            await call_tool(
                "add_file_association",
                {
                    "file_id": file_data["file_id"],
                    "issue_id": issue.id,
                    "assoc_type": "mentioned_in",
                },
            )
        )

        timeline = _parse(
            await call_tool(
                "get_file_timeline",
                {"file_id": file_data["file_id"], "event_type": "association"},
            )
        )
        assert "items" in timeline
        assert timeline["has_more"] is False
        assert len(timeline["items"]) >= 1
        association_event = next(e for e in timeline["items"] if e["type"] == "association_created")
        assert association_event["timeline_event_id"]
        assert association_event["assoc_id"]
        assert "id" not in association_event
        assert "source_id" not in association_event

    async def test_get_file_timeline_can_include_associated_issue_events(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("MCP issue timeline")
        file_data = _parse(await call_tool("register_file", {"path": "src/timeline_issue.py"}))
        _parse(
            await call_tool(
                "add_file_association",
                {
                    "file_id": file_data["file_id"],
                    "issue_id": issue.id,
                    "assoc_type": "mentioned_in",
                },
            )
        )
        await call_tool("update_issue", {"issue_id": issue.id, "status": "in_progress"})

        default_timeline = _parse(await call_tool("get_file_timeline", {"file_id": file_data["file_id"]}))
        with_issue_events = _parse(await call_tool("get_file_timeline", {"file_id": file_data["file_id"], "include_issue_events": True}))
        issue_only = _parse(await call_tool("get_file_timeline", {"file_id": file_data["file_id"], "event_type": "issue_event"}))

        assert all(e["type"] != "issue_event" for e in default_timeline["items"])
        issue_events = [e for e in with_issue_events["items"] if e["type"] == "issue_event"]
        assert issue_events
        assert issue_events[0]["issue_id"] == issue.id
        assert issue_events[0]["data"]["issue_title"] == "MCP issue timeline"
        assert issue_events[0]["data"]["event_type"] == "status_changed"
        assert issue_only["items"]
        assert all(e["type"] == "issue_event" for e in issue_only["items"])

    async def test_get_file_timeline_for_finding_event_uses_finding_id(self, mcp_db: FiligreeDB) -> None:
        file_data = _parse(await call_tool("register_file", {"path": "src/timeline_finding.py"}))
        scan = mcp_db.process_scan_results(
            scan_source="timeline-test",
            findings=[
                {
                    "path": "src/timeline_finding.py",
                    "rule_id": "R001",
                    "severity": "medium",
                    "message": "timeline shape",
                }
            ],
        )

        timeline = _parse(
            await call_tool(
                "get_file_timeline",
                {"file_id": file_data["file_id"], "event_type": "finding"},
            )
        )

        finding_event = next(e for e in timeline["items"] if e["type"] == "finding_created")
        assert finding_event["timeline_event_id"]
        assert finding_event["finding_id"] == scan["new_finding_ids"][0]
        assert "id" not in finding_event
        assert "source_id" not in finding_event

    async def test_get_file_timeline_invalid_event_type(self, mcp_db: FiligreeDB) -> None:
        file_data = _parse(await call_tool("register_file", {"path": "src/timeline_invalid.py"}))
        result = _parse(
            await call_tool(
                "get_file_timeline",
                {"file_id": file_data["file_id"], "event_type": "bogus"},
            )
        )
        assert result["code"] == ErrorCode.VALIDATION

    async def test_get_file_timeline_sqlite_error_returns_io(self, mcp_db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
        file_data = _parse(await call_tool("register_file", {"path": "src/timeline_io_error.py"}))

        def _raise_sqlite_error(self: FiligreeDB, *args: object, **kwargs: object) -> object:
            raise sqlite3.OperationalError("timeline storage failed")

        monkeypatch.setattr(FiligreeDB, "get_file_timeline", _raise_sqlite_error)

        result = _parse(await call_tool("get_file_timeline", {"file_id": file_data["file_id"]}))

        assert result["code"] == ErrorCode.IO
        assert "timeline storage failed" in result["error"]


@pytest.mark.slow
class TestScannerTools:
    """Tests for list_scanners and trigger_scan MCP tools."""

    def _write_scanner_toml(self, mcp_db: FiligreeDB, name: str = "test-scanner") -> None:
        """Helper: write a scanner TOML into the test .filigree/scanners/ dir."""
        import filigree.mcp_server as mcp_mod

        assert mcp_mod._filigree_dir is not None
        scanners_dir = mcp_mod._filigree_dir / "scanners"
        scanners_dir.mkdir(exist_ok=True)
        (scanners_dir / f"{name}.toml").write_text(
            f'[scanner]\nname = "{name}"\ndescription = "Test scanner"\n'
            # Use 'echo' as the command — exists on all systems, exits immediately
            f'command = "echo"\nargs = ["scan", "{{file}}", "--scan-run-id", "{{scan_run_id}}"]\nfile_types = ["py"]\n'
        )

    async def test_list_scanners_empty(self, mcp_db: FiligreeDB) -> None:
        result = _parse(await call_tool("list_scanners", {}))
        assert result["items"] == []
        assert result["has_more"] is False

    async def test_list_scanners_with_registry(self, mcp_db: FiligreeDB) -> None:
        self._write_scanner_toml(mcp_db)
        result = _parse(await call_tool("list_scanners", {}))
        assert len(result["items"]) == 1
        scanner = result["items"][0]
        assert scanner["name"] == "test-scanner"
        assert scanner["execution_mode"] == "external_process"
        assert scanner["may_send_contents"] is True
        assert scanner["requires_dashboard"] is True
        assert scanner["estimated_cost"] == "unknown"
        assert scanner["safe_preview_only"] is True
        assert scanner["requires_approval"] is True

    async def test_trigger_scan_scanner_not_found(self, mcp_db: FiligreeDB) -> None:
        result = _parse(
            await call_tool(
                "trigger_scan",
                {
                    "approve_execution": True,
                    "scanner": "nonexistent",
                    "file_path": "src/foo.py",
                },
            )
        )
        assert "error" in result
        assert "not found" in result["error"].lower()

    async def test_trigger_scan_malformed_scanner_returns_not_found(self, mcp_db: FiligreeDB) -> None:
        import filigree.mcp_server as mcp_mod

        assert mcp_mod._filigree_dir is not None
        scanners_dir = mcp_mod._filigree_dir / "scanners"
        scanners_dir.mkdir(exist_ok=True)
        (scanners_dir / "bad.toml").write_text(
            '[scanner]\nname = "bad"\ndescription = "bad scanner"\ncommand = "echo"\nargs = "not-a-list"\nfile_types = ["py"]\n'
        )

        project_root = mcp_mod._filigree_dir.parent
        target = project_root / "bad_target.py"
        try:
            target.write_text("x = 1\n")
            result = _parse(
                await call_tool(
                    "trigger_scan",
                    {
                        "approve_execution": True,
                        "scanner": "bad",
                        "file_path": "bad_target.py",
                    },
                )
            )
            assert result["code"] == ErrorCode.NOT_FOUND
            # Extras are in details under the 2.0 envelope shape.
            assert "bad" not in result.get("details", {}).get("available_scanners", [])
        finally:
            target.unlink(missing_ok=True)
            (scanners_dir / "bad.toml").unlink(missing_ok=True)

    async def test_trigger_scan_path_traversal_rejected(self, mcp_db: FiligreeDB) -> None:
        self._write_scanner_toml(mcp_db)
        result = _parse(
            await call_tool(
                "trigger_scan",
                {
                    "approve_execution": True,
                    "scanner": "test-scanner",
                    "file_path": "../../etc/passwd",
                },
            )
        )
        assert "error" in result
        assert result["code"] == ErrorCode.VALIDATION

    async def test_trigger_scan_scanner_name_traversal_rejected(self, mcp_db: FiligreeDB) -> None:
        result = _parse(
            await call_tool(
                "trigger_scan",
                {
                    "approve_execution": True,
                    "scanner": "../../../etc/crontab",
                    "file_path": "src/foo.py",
                },
            )
        )
        assert "error" in result
        assert "not found" in result["error"].lower()

    async def test_trigger_scan_non_localhost_api_url_rejected(self, mcp_db: FiligreeDB) -> None:
        """Security: non-localhost api_url must be rejected to prevent result exfiltration."""
        self._write_scanner_toml(mcp_db)
        result = _parse(
            await call_tool(
                "trigger_scan",
                {
                    "approve_execution": True,
                    "scanner": "test-scanner",
                    "file_path": "src/foo.py",
                    "api_url": "https://evil.example.com:8377",
                },
            )
        )
        assert result["code"] == ErrorCode.INVALID_API_URL
        assert "non-localhost" in result["error"].lower() or "not allowed" in result["error"].lower()

    async def test_trigger_scan_file_not_found(self, mcp_db: FiligreeDB) -> None:
        self._write_scanner_toml(mcp_db)
        result = _parse(
            await call_tool(
                "trigger_scan",
                {
                    "approve_execution": True,
                    "scanner": "test-scanner",
                    "file_path": "nonexistent/file.py",
                },
            )
        )
        assert "error" in result
        assert "not found" in result["error"].lower() or "not exist" in result["error"].lower()

    async def test_trigger_scan_success(self, mcp_db: FiligreeDB) -> None:
        import filigree.mcp_server as mcp_mod

        assert mcp_mod._filigree_dir is not None
        project_root = mcp_mod._filigree_dir.parent
        target = project_root / "test_target.py"
        try:
            target.write_text("x = 1\n")
            self._write_scanner_toml(mcp_db)
            result = _parse(
                await call_tool(
                    "trigger_scan",
                    {
                        "approve_execution": True,
                        "scanner": "test-scanner",
                        "file_path": "test_target.py",
                    },
                )
            )
            assert "error" not in result
            assert result["scanner"] == "test-scanner"
            assert result["file_path"] == "test_target.py"
            assert "file_id" in result
            assert "scan_run_id" in result
            assert result["file_id"] != ""

            # Verify the file was registered in file_records
            f = mcp_db.get_file_by_path("test_target.py")
            assert f is not None
        finally:
            target.unlink(missing_ok=True)

    async def test_trigger_scan_uses_canonical_path_in_scanner_command(self, mcp_db: FiligreeDB) -> None:
        import filigree.mcp_server as mcp_mod

        class _Proc:
            pid = 12345

            @staticmethod
            def poll() -> None:
                return None

        assert mcp_mod._filigree_dir is not None
        project_root = mcp_mod._filigree_dir.parent
        target = project_root / "canonical_target.py"
        try:
            target.write_text("x = 1\n")
            self._write_scanner_toml(mcp_db)
            with patch("filigree.scanner_runtime.subprocess.Popen", return_value=_Proc()) as popen:
                result = _parse(
                    await call_tool(
                        "trigger_scan",
                        {
                            "approve_execution": True,
                            "scanner": "test-scanner",
                            "file_path": "./canonical_target.py",
                        },
                    )
                )
            assert result.get("status") == "triggered"
            cmd = popen.call_args.args[0]
            assert "canonical_target.py" in cmd
            assert "./canonical_target.py" not in cmd
        finally:
            target.unlink(missing_ok=True)

    async def test_trigger_scan_registers_file_idempotent(self, mcp_db: FiligreeDB) -> None:
        import filigree.mcp_server as mcp_mod

        assert mcp_mod._filigree_dir is not None
        project_root = mcp_mod._filigree_dir.parent
        target = project_root / "existing.py"
        try:
            target.write_text("y = 2\n")
            existing = mcp_db.register_file("existing.py", language="python")
            self._write_scanner_toml(mcp_db)
            result = _parse(
                await call_tool(
                    "trigger_scan",
                    {
                        "approve_execution": True,
                        "scanner": "test-scanner",
                        "file_path": "existing.py",
                    },
                )
            )
            assert result["file_id"] == existing.id
        finally:
            target.unlink(missing_ok=True)

    async def test_trigger_scan_rate_limited(self, mcp_db: FiligreeDB) -> None:
        import filigree.mcp_server as mcp_mod

        assert mcp_mod._filigree_dir is not None
        project_root = mcp_mod._filigree_dir.parent
        target = project_root / "rate_test.py"
        try:
            target.write_text("z = 3\n")
            self._write_scanner_toml(mcp_db)
            # First call succeeds
            result1 = _parse(
                await call_tool(
                    "trigger_scan",
                    {
                        "approve_execution": True,
                        "scanner": "test-scanner",
                        "file_path": "rate_test.py",
                    },
                )
            )
            assert result1.get("status") == "triggered"

            # Immediate second call should be rate-limited: CONFLICT
            # (cooldown conflict), with the blocking run id in details.
            result2 = _parse(
                await call_tool(
                    "trigger_scan",
                    {
                        "approve_execution": True,
                        "scanner": "test-scanner",
                        "file_path": "rate_test.py",
                    },
                )
            )
            assert result2["code"] == ErrorCode.CONFLICT
            assert result2["details"]["blocking_run_id"] == result1["scan_run_id"]
        finally:
            target.unlink(missing_ok=True)
            # Clear cooldown state for test isolation

    async def test_trigger_scan_file_type_mismatch_warning(self, mcp_db: FiligreeDB) -> None:
        """Scanning a .txt file with a py-only scanner should succeed with a warning."""
        import filigree.mcp_server as mcp_mod

        assert mcp_mod._filigree_dir is not None
        project_root = mcp_mod._filigree_dir.parent
        target = project_root / "readme.txt"
        try:
            target.write_text("hello\n")
            self._write_scanner_toml(mcp_db)  # file_types = ["py"]
            result = _parse(
                await call_tool(
                    "trigger_scan",
                    {
                        "approve_execution": True,
                        "scanner": "test-scanner",
                        "file_path": "readme.txt",
                    },
                )
            )
            assert result.get("status") == "triggered"
            assert "warnings" in result
            assert any("txt" in w for w in result["warnings"])
        finally:
            target.unlink(missing_ok=True)

    async def test_trigger_scan_allows_templated_executable_path(self, mcp_db: FiligreeDB) -> None:
        import filigree.mcp_server as mcp_mod

        assert mcp_mod._filigree_dir is not None
        project_root = mcp_mod._filigree_dir.parent
        target = project_root / "templated_exec_target.py"
        scanner_exec = project_root / "scanner_exec.sh"
        scanners_dir = mcp_mod._filigree_dir / "scanners"
        scanners_dir.mkdir(exist_ok=True)

        try:
            target.write_text("x = 1\n")
            scanner_exec.write_text("#!/usr/bin/env bash\nexit 0\n")
            scanner_exec.chmod(0o755)
            (scanners_dir / "templated-scanner.toml").write_text(
                '[scanner]\nname = "templated-scanner"\ndescription = "Templated executable"\n'
                'command = "{project_root}/scanner_exec.sh"\n'
                'args = ["{file}", "--scan-run-id", "{scan_run_id}"]\nfile_types = ["py"]\n'
            )

            result = _parse(
                await call_tool(
                    "trigger_scan",
                    {
                        "approve_execution": True,
                        "scanner": "templated-scanner",
                        "file_path": "templated_exec_target.py",
                    },
                )
            )
            assert result.get("status") == "triggered"
        finally:
            target.unlink(missing_ok=True)
            scanner_exec.unlink(missing_ok=True)
            (scanners_dir / "templated-scanner.toml").unlink(missing_ok=True)

    async def test_trigger_scan_allows_project_relative_executable_path(self, mcp_db: FiligreeDB) -> None:
        import filigree.mcp_server as mcp_mod

        assert mcp_mod._filigree_dir is not None
        project_root = mcp_mod._filigree_dir.parent
        target = project_root / "relative_exec_target.py"
        scanner_exec = project_root / "scanner_exec.sh"
        scanners_dir = mcp_mod._filigree_dir / "scanners"
        scanners_dir.mkdir(exist_ok=True)

        try:
            target.write_text("x = 1\n")
            scanner_exec.write_text("#!/usr/bin/env bash\nexit 0\n")
            scanner_exec.chmod(0o755)
            (scanners_dir / "relative-scanner.toml").write_text(
                '[scanner]\nname = "relative-scanner"\ndescription = "Relative executable"\n'
                'command = "./scanner_exec.sh"\n'
                'args = ["{file}", "--scan-run-id", "{scan_run_id}"]\nfile_types = ["py"]\n'
            )

            result = _parse(
                await call_tool(
                    "trigger_scan",
                    {
                        "approve_execution": True,
                        "scanner": "relative-scanner",
                        "file_path": "relative_exec_target.py",
                    },
                )
            )
            assert result.get("status") == "triggered"
        finally:
            target.unlink(missing_ok=True)
            scanner_exec.unlink(missing_ok=True)
            (scanners_dir / "relative-scanner.toml").unlink(missing_ok=True)

    async def test_trigger_scan_response_includes_log_path(self, mcp_db: FiligreeDB) -> None:
        """trigger_scan must include log_path in response and create the log file."""
        import filigree.mcp_server as mcp_mod

        assert mcp_mod._filigree_dir is not None
        project_root = mcp_mod._filigree_dir.parent
        target = project_root / "log_target.py"
        try:
            target.write_text("x = 1\n")
            self._write_scanner_toml(mcp_db)
            result = _parse(
                await call_tool(
                    "trigger_scan",
                    {"approve_execution": True, "scanner": "test-scanner", "file_path": "log_target.py"},
                )
            )
            assert "error" not in result
            assert "log_path" in result, "Response must include log_path for diagnostics"
            assert result["log_path"].endswith(".log")

            # Verify the log file was actually created on disk
            scan_log = mcp_mod._filigree_dir / "scans"
            assert scan_log.is_dir(), ".filigree/scans/ directory must exist"
            log_files = list(scan_log.glob("*.log"))
            assert len(log_files) >= 1, "At least one scan log file must be created"
        finally:
            target.unlink(missing_ok=True)

    async def test_trigger_scan_closes_log_fd_after_popen(self, mcp_db: FiligreeDB) -> None:
        """Bug filigree-dfe017: scan_log_fd must be closed after Popen to prevent fd leak."""
        import filigree.mcp_server as mcp_mod

        class _Proc:
            pid = 12345

            @staticmethod
            def poll() -> None:
                return None

        assert mcp_mod._filigree_dir is not None
        project_root = mcp_mod._filigree_dir.parent
        target = project_root / "fd_leak_target.py"
        try:
            target.write_text("x = 1\n")
            self._write_scanner_toml(mcp_db)
            mock_fd = MagicMock()
            mock_fd.__enter__ = MagicMock(return_value=mock_fd)
            mock_fd.__exit__ = MagicMock(return_value=False)
            original_open = builtins.open

            def _spy_open(path: Any, *args: Any, **kwargs: Any) -> Any:
                if str(path).endswith(".log"):
                    return mock_fd
                return original_open(path, *args, **kwargs)

            with (
                patch("filigree.scanner_runtime.subprocess.Popen", return_value=_Proc()),
                patch("builtins.open", side_effect=_spy_open),
            ):
                result = _parse(
                    await call_tool(
                        "trigger_scan",
                        {"approve_execution": True, "scanner": "test-scanner", "file_path": "fd_leak_target.py"},
                    )
                )
            assert "error" not in result, f"trigger_scan failed: {result}"
            mock_fd.close.assert_called_once()  # scan_log_fd must be closed after Popen succeeds
        finally:
            target.unlink(missing_ok=True)

    async def test_trigger_scan_cooldown_is_scoped_per_project(self, tmp_path: Path) -> None:
        import filigree.mcp_server as mcp_mod

        original_db = mcp_mod.db
        original_dir = mcp_mod._filigree_dir

        def _make_project(name: str, prefix: str) -> tuple[FiligreeDB, Path]:
            project_root = tmp_path / name
            filigree_dir = project_root / FILIGREE_DIR_NAME
            filigree_dir.mkdir(parents=True)
            write_config(filigree_dir, {"prefix": prefix, "version": 1})
            (filigree_dir / SUMMARY_FILENAME).write_text("# test\n")
            db = FiligreeDB(filigree_dir / DB_FILENAME, prefix=prefix)
            db.initialize()

            scanners_dir = filigree_dir / "scanners"
            scanners_dir.mkdir(exist_ok=True)
            (scanners_dir / "test-scanner.toml").write_text(
                '[scanner]\nname = "test-scanner"\ndescription = "Test scanner"\n'
                'command = "echo"\nargs = ["scan", "{file}", "--scan-run-id", "{scan_run_id}"]\nfile_types = ["py"]\n'
            )
            (project_root / "shared.py").write_text("x = 1\n")
            return db, filigree_dir

        db_a, dir_a = _make_project("proj-a", "alpha")
        db_b, dir_b = _make_project("proj-b", "bravo")

        try:
            mcp_mod.db = db_a
            mcp_mod._filigree_dir = dir_a
            result_a = _parse(
                await call_tool(
                    "trigger_scan",
                    {
                        "approve_execution": True,
                        "scanner": "test-scanner",
                        "file_path": "shared.py",
                    },
                )
            )
            assert result_a.get("status") == "triggered"

            mcp_mod.db = db_b
            mcp_mod._filigree_dir = dir_b
            result_b = _parse(
                await call_tool(
                    "trigger_scan",
                    {
                        "approve_execution": True,
                        "scanner": "test-scanner",
                        "file_path": "shared.py",
                    },
                )
            )
            assert result_b.get("status") == "triggered"
        finally:
            mcp_mod.db = original_db
            mcp_mod._filigree_dir = original_dir
            db_a.close()
            db_b.close()


class TestHttpMcpRequestContext:
    """Regression tests for per-request DB and project directory isolation."""

    @staticmethod
    def _make_project(tmp_path: Path, name: str, prefix: str) -> tuple[FiligreeDB, Path]:
        project_root = tmp_path / name
        filigree_dir = project_root / FILIGREE_DIR_NAME
        filigree_dir.mkdir(parents=True)
        write_config(filigree_dir, {"prefix": prefix, "version": 1})
        (filigree_dir / SUMMARY_FILENAME).write_text("# test\n")
        db = FiligreeDB(filigree_dir / DB_FILENAME, prefix=prefix)
        db.initialize()
        return db, filigree_dir

    async def test_create_mcp_app_uses_request_scoped_db_and_dir(self, tmp_path: Path) -> None:
        import filigree.mcp_server as mcp_mod

        db_global, dir_global = self._make_project(tmp_path, "global", "global")
        db_a, dir_a = self._make_project(tmp_path, "proj-a", "alpha")
        db_b, dir_b = self._make_project(tmp_path, "proj-b", "bravo")

        original_db = mcp_mod.db
        original_dir = mcp_mod._filigree_dir
        mcp_mod.db = db_global
        mcp_mod._filigree_dir = dir_global

        selected_key: ContextVar[str] = ContextVar("selected_key", default="a")
        mapping = {"a": db_a, "b": db_b}

        started_a = asyncio.Event()
        started_b = asyncio.Event()
        observed: dict[str, Any] = {}

        class FakeSessionManager:
            def __init__(self, app: Any, json_response: bool, stateless: bool) -> None:
                self.app = app
                self.json_response = json_response
                self.stateless = stateless

            @asynccontextmanager
            async def run(self) -> Any:
                yield

            async def handle_request(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
                key = selected_key.get()
                current_db = mcp_mod._get_db()
                current_dir = mcp_mod._get_filigree_dir()
                safe_parent = mcp_mod._safe_path("nested/file.py").parent

                observed[f"{key}_before"] = (current_db, current_dir, safe_parent)
                if key == "a":
                    started_a.set()
                    await started_b.wait()
                else:
                    await started_a.wait()
                    started_b.set()

                await asyncio.sleep(0)
                observed[f"{key}_after"] = (
                    mcp_mod._get_db(),
                    mcp_mod._get_filigree_dir(),
                    mcp_mod._safe_path("nested/file.py").parent,
                )

        def _resolver() -> FiligreeDB:
            return mapping[selected_key.get()]

        async def _receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def _send(_message: dict[str, Any]) -> None:
            return None

        try:
            with patch("mcp.server.streamable_http_manager.StreamableHTTPSessionManager", FakeSessionManager):
                handler, _lifespan = create_mcp_app(db_resolver=_resolver)

                async def _request(key: str) -> None:
                    token = selected_key.set(key)
                    try:
                        await handler({"type": "http", "path": f"/{key}"}, _receive, _send)  # type: ignore[arg-type]
                    finally:
                        selected_key.reset(token)

                await asyncio.gather(_request("a"), _request("b"))

            assert observed["a_before"][0] is db_a
            assert observed["a_after"][0] is db_a
            assert observed["b_before"][0] is db_b
            assert observed["b_after"][0] is db_b

            assert observed["a_before"][1] == dir_a
            assert observed["a_after"][1] == dir_a
            assert observed["b_before"][1] == dir_b
            assert observed["b_after"][1] == dir_b

            assert observed["a_before"][2] == dir_a.parent / "nested"
            assert observed["a_after"][2] == dir_a.parent / "nested"
            assert observed["b_before"][2] == dir_b.parent / "nested"
            assert observed["b_after"][2] == dir_b.parent / "nested"

            # HTTP request handling must not overwrite shared module globals.
            assert mcp_mod.db is db_global
            assert mcp_mod._filigree_dir == dir_global
            assert mcp_mod._get_db() is db_global
            assert mcp_mod._get_filigree_dir() == dir_global
        finally:
            mcp_mod.db = original_db
            mcp_mod._filigree_dir = original_dir
            db_global.close()
            db_a.close()
            db_b.close()

    async def test_create_mcp_app_resolver_schema_mismatch_returns_structured_envelope(self) -> None:
        from filigree.types.api import SchemaVersionMismatchError

        def _resolver() -> FiligreeDB:
            raise SchemaVersionMismatchError(installed=8, database=9)

        handler, _lifespan = create_mcp_app(db_resolver=_resolver)
        messages: list[dict[str, Any]] = []

        async def _receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def _send(message: dict[str, Any]) -> None:
            messages.append(message)

        await handler({"type": "http", "path": "/mcp"}, _receive, _send)  # type: ignore[arg-type]

        start = next(m for m in messages if m["type"] == "http.response.start")
        body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
        data = json.loads(body)
        assert start["status"] == 503
        assert data["code"] == ErrorCode.SCHEMA_MISMATCH
        assert "upgrade" in data["error"].lower()

    async def test_create_mcp_app_resolver_registry_version_mismatch_returns_structured_envelope(self) -> None:
        from filigree.registry import EXPECTED_LOOMWEAVE_API_VERSION, RegistryVersionMismatchError

        def _resolver() -> FiligreeDB:
            raise RegistryVersionMismatchError(
                "Loomweave advertised an incompatible registry API",
                url="http://loomweave.test/api/v1/_capabilities",
                expected=EXPECTED_LOOMWEAVE_API_VERSION,
                advertised=EXPECTED_LOOMWEAVE_API_VERSION + 1,
            )

        handler, _lifespan = create_mcp_app(db_resolver=_resolver)
        messages: list[dict[str, Any]] = []

        async def _receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def _send(message: dict[str, Any]) -> None:
            messages.append(message)

        await handler({"type": "http", "path": "/mcp"}, _receive, _send)  # type: ignore[arg-type]

        start = next(m for m in messages if m["type"] == "http.response.start")
        body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
        data = json.loads(body)
        assert start["status"] == 503
        assert data["code"] == ErrorCode.CLARION_REGISTRY_VERSION_MISMATCH
        assert data["details"] == {
            "cause": "clarion_registry_version_mismatch",
            "url": "http://loomweave.test/api/v1/_capabilities",
            "expected": EXPECTED_LOOMWEAVE_API_VERSION,
            "advertised": EXPECTED_LOOMWEAVE_API_VERSION + 1,
        }

    async def test_create_mcp_app_resolver_config_error_returns_structured_envelope(self) -> None:
        def _resolver() -> FiligreeDB:
            raise ValueError("bad project config")

        handler, _lifespan = create_mcp_app(db_resolver=_resolver)
        messages: list[dict[str, Any]] = []

        async def _receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def _send(message: dict[str, Any]) -> None:
            messages.append(message)

        await handler({"type": "http", "path": "/mcp"}, _receive, _send)  # type: ignore[arg-type]

        start = next(m for m in messages if m["type"] == "http.response.start")
        body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
        data = json.loads(body)
        assert start["status"] == 400
        assert data == {"error": "bad project config", "code": ErrorCode.VALIDATION}


# ---------------------------------------------------------------------------
# Bug: filigree-ebb27d — list_issues drops status_category filter
# ---------------------------------------------------------------------------


class TestListIssuesStatusCategoryEmpty:
    """Bug fix: filigree-ebb27d — status_category must not be silently dropped."""

    async def test_unrecognized_category_returns_empty(self, mcp_db: FiligreeDB) -> None:
        """Passing a non-existent status_category should return empty, not all issues."""
        mcp_db.create_issue("Should not appear")
        result = await call_tool("list_issues", {"status_category": "nonexistent"})
        data = _parse(result)
        assert data["items"] == [], f"Expected empty results for unknown category, got {len(data['items'])} issues"

    async def test_valid_category_still_filters(self, mcp_db: FiligreeDB) -> None:
        """A valid category with matching issues should still work correctly."""
        mcp_db.create_issue("Open issue")
        b = mcp_db.create_issue("WIP issue")
        mcp_db.update_issue(b.id, status="in_progress")
        result = await call_tool("list_issues", {"status_category": "wip"})
        data = _parse(result)
        ids = [i["issue_id"] for i in data["items"]]
        assert b.id in ids


# ---------------------------------------------------------------------------
# Bug: filigree-7f5ee1 — add_file_association misclassifies not_found
# ---------------------------------------------------------------------------


class TestAddFileAssociationIssueNotFound:
    """Bug fix: filigree-7f5ee1 — missing issue should return not_found, not validation_error."""

    async def test_nonexistent_issue_returns_not_found(self, mcp_db: FiligreeDB) -> None:
        """add_file_association with a non-existent issue_id should return code=not_found."""
        file_data = _parse(await call_tool("register_file", {"path": "src/test.py"}))
        result = _parse(
            await call_tool(
                "add_file_association",
                {
                    "file_id": file_data["file_id"],
                    "issue_id": "mcp-nonexistent",
                    "assoc_type": "task_for",
                },
            )
        )
        assert result["code"] == ErrorCode.NOT_FOUND, f"Expected 'NOT_FOUND', got '{result['code']}'"

    async def test_invalid_assoc_type_still_validation_error(self, mcp_db: FiligreeDB) -> None:
        """Bad assoc_type should still be validation_error (not affected by fix)."""
        issue = mcp_db.create_issue("Real issue")
        file_data = _parse(await call_tool("register_file", {"path": "src/valid.py"}))
        result = _parse(
            await call_tool(
                "add_file_association",
                {
                    "file_id": file_data["file_id"],
                    "issue_id": issue.id,
                    "assoc_type": "invalid_type",
                },
            )
        )
        assert result["code"] == ErrorCode.VALIDATION

    async def test_get_file_sqlite_error_returns_io(self, mcp_db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise(*_args: object, **_kwargs: object) -> None:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr("filigree.core.FiligreeDB.get_file", _raise)
        result = _parse(
            await call_tool(
                "add_file_association",
                {
                    "file_id": "file-anything",
                    "issue_id": "issue-anything",
                    "assoc_type": "task_for",
                },
            )
        )

        assert result["code"] == ErrorCode.IO
        assert "database is locked" in result["error"]

    async def test_get_issue_sqlite_error_returns_io(self, mcp_db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
        file_data = _parse(await call_tool("register_file", {"path": "src/io-assoc.py"}))

        def _raise(*_args: object, **_kwargs: object) -> None:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr("filigree.core.FiligreeDB.get_issue", _raise)
        result = _parse(
            await call_tool(
                "add_file_association",
                {
                    "file_id": file_data["file_id"],
                    "issue_id": "issue-anything",
                    "assoc_type": "task_for",
                },
            )
        )

        assert result["code"] == ErrorCode.IO
        assert "database is locked" in result["error"]

    async def test_add_file_association_sqlite_error_returns_io(self, mcp_db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
        issue = mcp_db.create_issue("Real issue")
        file_data = _parse(await call_tool("register_file", {"path": "src/io-assoc-write.py"}))

        def _raise(*_args: object, **_kwargs: object) -> None:
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr("filigree.core.FiligreeDB.add_file_association", _raise)
        result = _parse(
            await call_tool(
                "add_file_association",
                {
                    "file_id": file_data["file_id"],
                    "issue_id": issue.id,
                    "assoc_type": "task_for",
                },
            )
        )

        assert result["code"] == ErrorCode.IO
        assert "database is locked" in result["error"]


# ---------------------------------------------------------------------------
# DB-persisted scan cooldown tests (replaces in-memory filigree-5bee22 tests)
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestTriggerScanCooldownDB:
    """Cooldown is now DB-persisted via scan_runs table."""

    async def test_db_cooldown_blocks_retrigger(self, mcp_db: FiligreeDB) -> None:
        """DB-persisted cooldown blocks re-triggering the same scanner+file."""
        import filigree.mcp_server as mcp_mod

        assert mcp_mod._filigree_dir is not None
        project_root = mcp_mod._filigree_dir.parent
        target = project_root / "cooldown_test.py"
        scanners_dir = mcp_mod._filigree_dir / "scanners"
        scanners_dir.mkdir(exist_ok=True)
        (scanners_dir / "echo-scanner.toml").write_text('[scanner]\nname = "echo-scanner"\ncommand = "echo"\nargs = ["{file_path}"]\n')
        try:
            target.write_text("x = 1\n")
            # First trigger should succeed
            result = _parse(
                await call_tool(
                    "trigger_scan",
                    {"approve_execution": True, "scanner": "echo-scanner", "file_path": "cooldown_test.py"},
                )
            )
            assert result.get("status") == "triggered"

            # Second trigger should be rate-limited (CONFLICT under 2.0 —
            # cooldown is a retriable conflict, not an IO failure).
            result2 = _parse(
                await call_tool(
                    "trigger_scan",
                    {"approve_execution": True, "scanner": "echo-scanner", "file_path": "cooldown_test.py"},
                )
            )
            assert result2["code"] == ErrorCode.CONFLICT
        finally:
            target.unlink(missing_ok=True)
            (scanners_dir / "echo-scanner.toml").unlink(missing_ok=True)

    async def test_spawn_failure_returns_error(self, mcp_db: FiligreeDB) -> None:
        """If process spawn fails (OSError), scan run is marked failed."""
        import filigree.mcp_server as mcp_mod

        assert mcp_mod._filigree_dir is not None
        project_root = mcp_mod._filigree_dir.parent
        target = project_root / "spawn_fail.py"
        scanners_dir = mcp_mod._filigree_dir / "scanners"
        scanners_dir.mkdir(exist_ok=True)
        (scanners_dir / "spawn-fail.toml").write_text('[scanner]\nname = "spawn-fail"\ncommand = "echo"\nargs = ["{file_path}"]\n')
        try:
            target.write_text("y = 1\n")
            with patch("filigree.scanner_runtime.subprocess.Popen", side_effect=OSError("mock spawn fail")):
                result = _parse(
                    await call_tool(
                        "trigger_scan",
                        {"approve_execution": True, "scanner": "spawn-fail", "file_path": "spawn_fail.py"},
                    )
                )
            assert result["code"] == ErrorCode.IO
        finally:
            target.unlink(missing_ok=True)
            (scanners_dir / "spawn-fail.toml").unlink(missing_ok=True)

    async def test_command_not_found_returns_error(self, mcp_db: FiligreeDB) -> None:
        """If command validation fails, no scan run is created."""
        import filigree.mcp_server as mcp_mod

        assert mcp_mod._filigree_dir is not None
        project_root = mcp_mod._filigree_dir.parent
        target = project_root / "cmd_fail.py"
        scanners_dir = mcp_mod._filigree_dir / "scanners"
        scanners_dir.mkdir(exist_ok=True)
        (scanners_dir / "bad-cmd.toml").write_text('[scanner]\nname = "bad-cmd"\ncommand = "nonexistent_binary_xyz"\n')
        try:
            target.write_text("z = 1\n")
            result = _parse(
                await call_tool(
                    "trigger_scan",
                    {"approve_execution": True, "scanner": "bad-cmd", "file_path": "cmd_fail.py"},
                )
            )
            assert result["code"] == ErrorCode.NOT_FOUND
        finally:
            target.unlink(missing_ok=True)
            (scanners_dir / "bad-cmd.toml").unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# v0.5: reopen_issue, release_claim, export, import via MCP
# ---------------------------------------------------------------------------


class TestMCPReopenIssue:
    async def test_reopen_closed_issue(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Reopen me")
        mcp_db.close_issue(issue.id, reason="done")
        result = await call_tool("reopen_issue", {"issue_id": issue.id})
        data = _parse(result)
        assert data["status"] == "open"
        assert data["closed_at"] is None

    async def test_reopen_bug_returns_to_last_non_done_status_and_clears_close_reason(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("MCP bug reopen", type="bug", fields={"severity": "major"})
        mcp_db.update_issue(issue.id, status="confirmed")
        mcp_db.update_issue(issue.id, status="fixing", fields={"root_cause": "bad assumption"})
        mcp_db.update_issue(issue.id, status="verifying", fields={"fix_verification": "regression added"})
        mcp_db.close_issue(issue.id, reason="closed too early")

        result = await call_tool("reopen_issue", {"issue_id": issue.id, "actor": "agent-gamma"})

        data = _parse(result)
        assert data["status"] == "verifying"
        assert data["closed_at"] is None
        assert data["fields"]["root_cause"] == "bad assumption"
        assert data["fields"]["fix_verification"] == "regression added"
        assert "close_reason" not in data["fields"]
        reopen_event = next(e for e in mcp_db.get_recent_events(limit=10) if e["event_type"] == "reopened")
        assert reopen_event["old_value"] == "closed"
        assert reopen_event["new_value"] == "verifying"

    async def test_reopen_with_actor(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Actor reopen")
        mcp_db.close_issue(issue.id, reason="done")
        result = await call_tool("reopen_issue", {"issue_id": issue.id, "actor": "agent-gamma"})
        data = _parse(result)
        assert data["status"] == "open"
        events = mcp_db.get_recent_events(limit=5)
        reopen_event = next(e for e in events if e["event_type"] == "reopened")
        assert reopen_event["actor"] == "agent-gamma"

    async def test_reopen_not_closed(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Still open")
        result = await call_tool("reopen_issue", {"issue_id": issue.id})
        data = _parse(result)
        assert data["code"] == ErrorCode.INVALID_TRANSITION

    async def test_reopen_not_found(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("reopen_issue", {"issue_id": "mcp-nonexistent"})
        data = _parse(result)
        assert data["code"] == ErrorCode.NOT_FOUND

    async def test_reopen_default_actor_is_mcp(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Default actor reopen")
        mcp_db.close_issue(issue.id, reason="done")
        await call_tool("reopen_issue", {"issue_id": issue.id})
        events = mcp_db.get_recent_events(limit=5)
        reopen_event = next(e for e in events if e["event_type"] == "reopened")
        assert reopen_event["actor"] == "mcp"


class TestMCPReleaseClaim:
    async def test_release_via_mcp(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("MCP release")
        mcp_db.claim_issue(issue.id, assignee="agent-1")
        result = await call_tool("release_claim", {"issue_id": issue.id})
        data = _parse(result)
        assert data["status"] == "open"  # status unchanged
        assert data["assignee"] == ""

    async def test_release_conflict_via_mcp(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Not claimed")
        result = await call_tool("release_claim", {"issue_id": issue.id})
        data = _parse(result)
        assert data["code"] == ErrorCode.CONFLICT

    async def test_release_if_held_unassigned_is_noop_via_mcp(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Not claimed")
        result = await call_tool("release_claim", {"issue_id": issue.id, "actor": "agent-1", "if_held": True})
        data = _parse(result)
        assert data["issue_id"] == issue.id
        assert data["assignee"] == ""

    async def test_release_if_held_rejects_other_assignee_via_mcp(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Other claim")
        mcp_db.claim_issue(issue.id, assignee="agent-2")

        result = await call_tool("release_claim", {"issue_id": issue.id, "actor": "agent-1", "if_held": True})

        data = _parse(result)
        assert data["code"] == ErrorCode.CONFLICT
        assert data["details"] == {"issue_id": issue.id, "observed": "agent-2", "expected": "agent-1"}
        assert mcp_db.get_issue(issue.id).assignee == "agent-2"

    async def test_release_claim_invalid_reverse_transition_via_mcp(
        self,
        mcp_db: FiligreeDB,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from filigree.types.api import InvalidTransitionError

        issue = mcp_db.create_issue("Reverse transition MCP")
        mcp_db.start_work(issue.id, assignee="agent-1", actor="agent-1")
        original_validate_transition = mcp_db.templates.validate_transition

        def fail_backward_validation(
            type_name: str,
            from_state: str,
            to_state: str,
            fields: dict[str, Any],
            *,
            backward: bool = False,
        ) -> Any:
            if backward:
                raise InvalidTransitionError(type_name, from_state, to_state=to_state, backward=True)
            return original_validate_transition(type_name, from_state, to_state, fields, backward=backward)

        monkeypatch.setattr(mcp_db.templates, "validate_transition", fail_backward_validation)

        result = await call_tool("release_claim", {"issue_id": issue.id, "actor": "agent-1"})

        data = _parse(result)
        assert data["code"] == ErrorCode.INVALID_TRANSITION

    async def test_release_not_found_via_mcp(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("release_claim", {"issue_id": "mcp-nonexistent"})
        data = _parse(result)
        assert data["code"] == ErrorCode.NOT_FOUND


class TestMCPExportImport:
    async def test_export_via_mcp(self, mcp_db: FiligreeDB) -> None:
        mcp_db.create_issue("Export me")
        result = await call_tool("export_jsonl", {"output_path": "mcp_export.jsonl"})
        data = _parse(result)
        assert data["status"] == "ok"
        assert data["records"] > 0

    async def test_import_via_mcp(self, mcp_db: FiligreeDB) -> None:
        mcp_db.create_issue("Import source")
        await call_tool("export_jsonl", {"output_path": "mcp_roundtrip.jsonl"})
        # Import into same DB with merge
        result = await call_tool("import_jsonl", {"input_path": "mcp_roundtrip.jsonl", "merge": True})
        data = _parse(result)
        assert data["status"] == "ok"

    async def test_import_via_mcp_preserves_file_domain_rows(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("File issue")
        file_rec = mcp_db.register_file("src/mcp_file.py", language="python", metadata={"owner": "mcp"})
        mcp_db.conn.execute(
            "INSERT INTO scan_findings "
            "(id, file_id, issue_id, scan_source, rule_id, severity, status, message, suggestion, "
            "scan_run_id, line_start, line_end, seen_count, first_seen, updated_at, last_seen_at, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "mcp-sf-1",
                file_rec.id,
                issue.id,
                "ruff",
                "F401",
                "medium",
                "open",
                "unused import",
                "",
                "run-mcp",
                12,
                12,
                1,
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                '{"source":"mcp"}',
            ),
        )
        mcp_db.conn.execute(
            "INSERT INTO file_associations (file_id, issue_id, assoc_type, created_at) VALUES (?, ?, ?, ?)",
            (file_rec.id, issue.id, "bug_in", "2026-01-01T00:00:00+00:00"),
        )
        mcp_db.conn.execute(
            "INSERT INTO file_events (file_id, event_type, field, old_value, new_value, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (file_rec.id, "file_metadata_update", "language", "", "python", "2026-01-01T00:00:00+00:00"),
        )
        mcp_db.conn.commit()

        export = _parse(await call_tool("export_jsonl", {"output_path": "mcp_files.jsonl"}))
        assert export["status"] == "ok"

        imported = _parse(await call_tool("import_jsonl", {"input_path": "mcp_files.jsonl", "merge": True}))
        assert imported["status"] == "ok"
        assert imported["records"] == 0
        assert mcp_db.conn.execute("SELECT COUNT(*) FROM file_records").fetchone()[0] == 1
        assert mcp_db.conn.execute("SELECT COUNT(*) FROM scan_findings").fetchone()[0] == 1
        assert mcp_db.conn.execute("SELECT COUNT(*) FROM file_associations").fetchone()[0] == 1
        assert mcp_db.conn.execute("SELECT COUNT(*) FROM file_events").fetchone()[0] == 1

    async def test_import_bad_path_via_mcp(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("import_jsonl", {"input_path": "/nonexistent/file.jsonl"})
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION


# ---------------------------------------------------------------------------
# v1.0: archive_closed, compact_events via MCP
# ---------------------------------------------------------------------------


class TestMCPV10:
    async def test_archive_closed_via_mcp(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Archive via MCP")
        mcp_db.close_issue(issue.id)
        old_date = (datetime.now(UTC) - timedelta(days=60)).isoformat()
        mcp_db.conn.execute("UPDATE issues SET closed_at = ? WHERE id = ?", (old_date, issue.id))
        mcp_db.conn.commit()

        result = await call_tool("archive_closed", {"days_old": 30})
        data = _parse(result)
        assert data["status"] == "ok"
        assert data["archived_count"] == 1

    async def test_archive_closed_can_be_scoped_to_label(self, mcp_db: FiligreeDB) -> None:
        scratch = mcp_db.create_issue("MCP scratch cleanup")
        unrelated = mcp_db.create_issue("MCP unrelated closed")
        mcp_db.add_label(scratch.id, "scratch")
        mcp_db.close_issue(scratch.id)
        mcp_db.close_issue(unrelated.id)

        result = await call_tool("archive_closed", {"days_old": 0, "label": "scratch"})

        data = _parse(result)
        assert data["status"] == "ok"
        assert data["archived_count"] == 1
        assert data["archived_ids"] == [scratch.id]
        assert mcp_db.get_issue(scratch.id).status == "archived"
        assert mcp_db.get_issue(unrelated.id).status == "closed"

    async def test_compact_events_via_mcp(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("compact_events", {})
        data = _parse(result)
        assert data["status"] == "ok"
        assert data["events_deleted"] == 0

    async def test_compact_events_rejects_negative_keep_recent(self, mcp_db: FiligreeDB) -> None:
        """Regression for filigree-fe8956fb16: negative keep_recent must not wipe history."""
        issue = mcp_db.create_issue("Negative guard via MCP")
        for i in range(10):
            mcp_db.conn.execute(
                "INSERT INTO events (issue_id, event_type, actor, created_at) VALUES (?, ?, ?, ?)",
                (issue.id, "test_event", "tester", f"2026-01-01T00:{i:02d}:00+00:00"),
            )
        mcp_db.conn.commit()
        mcp_db.close_issue(issue.id)
        old_date = (datetime.now(UTC) - timedelta(days=60)).isoformat()
        mcp_db.conn.execute("UPDATE issues SET closed_at = ? WHERE id = ?", (old_date, issue.id))
        mcp_db.conn.commit()
        mcp_db.archive_closed(days_old=30)

        before = mcp_db.conn.execute("SELECT COUNT(*) as cnt FROM events WHERE issue_id = ?", (issue.id,)).fetchone()["cnt"]
        assert before > 0

        result = await call_tool("compact_events", {"keep_recent": -1})
        data = _parse(result)
        assert data.get("code") == ErrorCode.VALIDATION, data

        after = mcp_db.conn.execute("SELECT COUNT(*) as cnt FROM events WHERE issue_id = ?", (issue.id,)).fetchone()["cnt"]
        assert after == before, f"MCP compact_events with keep_recent=-1 wiped {before - after} events"


class TestListLabels:
    async def test_list_labels_returns_namespaces(self, mcp_db: FiligreeDB) -> None:
        mcp_db.create_issue("A", labels=["cluster:broad-except"])
        result = await call_tool("list_labels", {})
        data = _parse(result)
        ns_names = {item["namespace"] for item in data["items"]}
        assert "cluster" in ns_names

    async def test_list_labels_with_namespace_filter(self, mcp_db: FiligreeDB) -> None:
        mcp_db.create_issue("A", labels=["cluster:x", "effort:m"])
        result = await call_tool("list_labels", {"namespace": "cluster"})
        data = _parse(result)
        ns_names = {item["namespace"] for item in data["items"]}
        assert "cluster" in ns_names
        assert "effort" not in ns_names

    async def test_list_labels_includes_virtual(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("list_labels", {})
        data = _parse(result)
        ns_names = {item["namespace"] for item in data["items"]}
        assert "age" in ns_names
        assert "has" in ns_names

    async def test_list_labels_renames_bare_namespace_for_public_response(self, mcp_db: FiligreeDB) -> None:
        mcp_db.create_issue("A", labels=["tech-debt"])
        result = await call_tool("list_labels", {})
        data = _parse(result)

        ns_names = {item["namespace"] for item in data["items"]}
        assert "unnamespaced" in ns_names
        assert "_bare" not in ns_names

    async def test_list_labels_accepts_public_unnamespaced_filter(self, mcp_db: FiligreeDB) -> None:
        mcp_db.create_issue("A", labels=["tech-debt", "cluster:x"])
        result = await call_tool("list_labels", {"namespace": "unnamespaced"})
        data = _parse(result)

        assert [item["namespace"] for item in data["items"]] == ["unnamespaced"]
        labels = {entry["label"] for entry in data["items"][0]["labels"]}
        assert labels == {"tech-debt"}

    async def test_list_labels_reports_truncated_groups(self, mcp_db: FiligreeDB) -> None:
        for i in range(12):
            mcp_db.create_issue(f"Issue {i}", labels=[f"bare-{i:02d}"])

        result = await call_tool("list_labels", {})
        data = _parse(result)
        unnamespaced = next(item for item in data["items"] if item["namespace"] == "unnamespaced")

        assert len(unnamespaced["labels"]) == 10
        assert unnamespaced["total"] == 12
        assert unnamespaced["truncated"] is True

    @pytest.mark.parametrize("top", ["10", -1, True])
    async def test_list_labels_invalid_top_returns_validation(self, mcp_db: FiligreeDB, top: object) -> None:
        result = await call_tool("list_labels", {"top": top})
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION


class TestGetLabelTaxonomy:
    async def test_taxonomy_returns_all_sections(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("get_label_taxonomy", {})
        data = _parse(result)
        assert "auto" in data
        assert "virtual" in data
        assert "manual_suggested" in data
        assert "bare_labels" in data

    async def test_taxonomy_rejects_priority_text_labels(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("get_label_taxonomy", {})
        data = _parse(result)
        assert data["bare_labels"]["reserved"]["patterns"] == ["P[0-4]", "priority:*"]
        assert data["bare_labels"]["reserved"]["writable"] is False
