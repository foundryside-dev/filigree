"""MCP boundary validation tests for priority and actor."""

from __future__ import annotations

from filigree.core import FiligreeDB
from filigree.mcp_server import call_tool
from filigree.types.api import ErrorCode
from tests.mcp._helpers import _parse


class TestMCPActorValidation:
    """Actor validation across MCP handlers."""

    async def test_create_issue_empty_actor(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("create_issue", {"title": "Test", "actor": ""})
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION
        assert "empty" in data["error"]

    async def test_create_issue_control_char_actor(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("create_issue", {"title": "Test", "actor": "\x00evil"})
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION
        assert "control" in data["error"].lower()


class TestMCPUnknownParameterValidation:
    async def test_unknown_parameter_rejected_before_handler(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("create_issue", {"title": "Test", "priority_min": 1})

        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION
        assert "create_issue" in data["error"]
        assert "priority_min" in data["error"]

    async def test_multiple_unknown_parameters_are_named(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("list_issues", {"limit": 5, "priority_min": 1, "include_files": True})

        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION
        assert "list_issues" in data["error"]
        assert "include_files" in data["error"]
        assert "priority_min" in data["error"]

    async def test_unknown_parameter_does_not_mask_unknown_tool(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("not_a_tool", {"surprise": True})

        data = _parse(result)
        assert data["code"] == ErrorCode.NOT_FOUND

    async def test_create_issue_strips_actor(self, mcp_db: FiligreeDB) -> None:
        """Valid actor with whitespace should succeed (stripped)."""
        result = await call_tool("create_issue", {"title": "Stripped", "actor": "  bot  "})
        data = _parse(result)
        assert "error" not in data
        assert data["title"] == "Stripped"

    async def test_create_issue_default_actor(self, mcp_db: FiligreeDB) -> None:
        """No actor provided — defaults to 'mcp', should succeed."""
        result = await call_tool("create_issue", {"title": "Default Actor"})
        data = _parse(result)
        assert "error" not in data

    async def test_update_issue_empty_actor(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Target")
        result = await call_tool("update_issue", {"issue_id": issue.id, "title": "New", "actor": ""})
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION

    async def test_close_issue_bom_actor(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Target")
        result = await call_tool("close_issue", {"issue_id": issue.id, "actor": "\ufeff"})
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION

    async def test_add_dependency_control_actor(self, mcp_db: FiligreeDB) -> None:
        a = mcp_db.create_issue("A")
        b = mcp_db.create_issue("B")
        result = await call_tool(
            "add_dependency",
            {"from_issue_id": a.id, "to_issue_id": b.id, "actor": "\nbad"},
        )
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION

    async def test_add_comment_empty_actor(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Target")
        result = await call_tool(
            "add_comment",
            {"issue_id": issue.id, "text": "hello", "actor": ""},
        )
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION

    async def test_undo_last_long_actor(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Target")
        result = await call_tool(
            "undo_last",
            {"issue_id": issue.id, "actor": "a" * 129},
        )
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION
        assert "128" in data["error"]

    async def test_reopen_issue_control_char_actor(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Target")
        mcp_db.close_issue(issue.id, reason="done")
        result = await call_tool("reopen_issue", {"issue_id": issue.id, "actor": "\x00evil"})
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION
        assert "control" in data["error"].lower()

    async def test_reopen_issue_empty_actor(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Target")
        mcp_db.close_issue(issue.id, reason="done")
        result = await call_tool("reopen_issue", {"issue_id": issue.id, "actor": ""})
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION

    async def test_release_claim_control_char_actor(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Target")
        mcp_db.claim_issue(issue.id, assignee="agent-1")
        result = await call_tool("release_claim", {"issue_id": issue.id, "actor": "\nbad"})
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION

    async def test_release_claim_empty_actor(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Target")
        mcp_db.claim_issue(issue.id, assignee="agent-1")
        result = await call_tool("release_claim", {"issue_id": issue.id, "actor": ""})
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION

    async def test_batch_add_label_empty_actor(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Label target")
        result = await call_tool(
            "batch_add_label",
            {"issue_ids": [issue.id], "label": "security", "actor": ""},
        )
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION
        assert "empty" in data["error"]

    async def test_batch_add_label_control_char_actor(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Label target")
        result = await call_tool(
            "batch_add_label",
            {"issue_ids": [issue.id], "label": "security", "actor": "\x00bad"},
        )
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION
        assert "control" in data["error"].lower()

    async def test_batch_add_label_valid_actor(self, mcp_db: FiligreeDB) -> None:
        """Valid actor should succeed without errors."""
        issue = mcp_db.create_issue("Label target")
        result = await call_tool(
            "batch_add_label",
            {"issue_ids": [issue.id], "label": "reviewed", "actor": "ci-bot"},
        )
        data = _parse(result)
        assert "error" not in data
        assert len(data["succeeded"]) == 1


class TestMCPPriorityValidation:
    """Priority range validation in MCP issue handlers."""

    async def test_create_issue_priority_too_high(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("create_issue", {"title": "Bad", "priority": 5})
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION

    async def test_create_issue_priority_too_low(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("create_issue", {"title": "Bad", "priority": -1})
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION

    async def test_create_issue_priority_boundary_0(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("create_issue", {"title": "Low bound", "priority": 0})
        data = _parse(result)
        assert "error" not in data
        assert data["priority"] == 0

    async def test_create_issue_priority_boundary_4(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("create_issue", {"title": "High bound", "priority": 4})
        data = _parse(result)
        assert "error" not in data
        assert data["priority"] == 4

    async def test_update_issue_priority_out_of_range(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Target")
        result = await call_tool("update_issue", {"issue_id": issue.id, "priority": 99})
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION

    async def test_list_issues_priority_filter_out_of_range(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("list_issues", {"priority": -1})
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION

    async def test_batch_update_priority_out_of_range(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Target")
        result = await call_tool("batch_update", {"issue_ids": [issue.id], "priority": 5})
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION

    async def test_claim_next_priority_min_out_of_range(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("claim_next", {"assignee": "bot", "priority_min": -1})
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION

    async def test_claim_next_priority_max_out_of_range(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("claim_next", {"assignee": "bot", "priority_max": 5})
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION

    async def test_create_issue_priority_bool_true(self, mcp_db: FiligreeDB) -> None:
        """bool is a subclass of int — True should be rejected, not silently become 1."""
        result = await call_tool("create_issue", {"title": "Bad", "priority": True})
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION

    async def test_create_issue_priority_bool_false(self, mcp_db: FiligreeDB) -> None:
        """bool is a subclass of int — False should be rejected, not silently become 0."""
        result = await call_tool("create_issue", {"title": "Bad", "priority": False})
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION

    async def test_create_plan_milestone_priority_out_of_range(self, mcp_db: FiligreeDB) -> None:
        """Invalid priority on milestone should be rejected at boundary."""
        result = await call_tool(
            "create_plan",
            {
                "milestone": {"title": "Bad", "priority": 99},
                "phases": [{"title": "P1", "steps": [{"title": "S1"}]}],
            },
        )
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION
        assert "milestone.priority" in data["error"]

    async def test_create_plan_phase_priority_out_of_range(self, mcp_db: FiligreeDB) -> None:
        """Invalid priority on a phase should be rejected at boundary."""
        result = await call_tool(
            "create_plan",
            {
                "milestone": {"title": "M"},
                "phases": [{"title": "P1", "priority": -1, "steps": [{"title": "S1"}]}],
            },
        )
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION
        assert "phases[0].priority" in data["error"]

    async def test_create_plan_step_priority_out_of_range(self, mcp_db: FiligreeDB) -> None:
        """Invalid priority on a step should be rejected at boundary."""
        result = await call_tool(
            "create_plan",
            {
                "milestone": {"title": "M"},
                "phases": [{"title": "P1", "steps": [{"title": "S1", "priority": 5}]}],
            },
        )
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION
        assert "phases[0].steps[0].priority" in data["error"]

    async def test_create_plan_step_priority_bool(self, mcp_db: FiligreeDB) -> None:
        """Bool priority on a nested step should be rejected."""
        result = await call_tool(
            "create_plan",
            {
                "milestone": {"title": "M"},
                "phases": [{"title": "P1", "steps": [{"title": "S1", "priority": True}]}],
            },
        )
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION

    async def test_create_plan_valid_priorities(self, mcp_db: FiligreeDB) -> None:
        """Valid priorities (0-4) should succeed."""
        result = await call_tool(
            "create_plan",
            {
                "milestone": {"title": "M", "priority": 0},
                "phases": [{"title": "P1", "priority": 4, "steps": [{"title": "S1", "priority": 2}]}],
            },
        )
        data = _parse(result)
        assert "error" not in data

    async def test_update_issue_priority_none_allowed(self, mcp_db: FiligreeDB) -> None:
        """Not providing priority should be fine (optional)."""
        issue = mcp_db.create_issue("Target")
        result = await call_tool("update_issue", {"issue_id": issue.id, "title": "New"})
        data = _parse(result)
        assert "error" not in data
        assert data.get("title") == "New"


class TestMCPStringValidation:
    """_validate_str boundary tests for MCP file-tool filter fields."""

    async def test_list_files_language_non_string(self, mcp_db: FiligreeDB) -> None:
        """Non-string language filter should be rejected."""
        result = await call_tool("list_files", {"language": 123})
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION
        assert "string" in data["error"]

    async def test_list_files_path_prefix_non_string(self, mcp_db: FiligreeDB) -> None:
        """Non-string path_prefix filter should be rejected."""
        result = await call_tool("list_files", {"path_prefix": True})
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION

    async def test_list_files_scan_source_non_string(self, mcp_db: FiligreeDB) -> None:
        """Non-string scan_source filter should be rejected."""
        result = await call_tool("list_files", {"scan_source": ["test"]})
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION


class TestMCPArchiveClosedDaysOld:
    """filigree-0903743222: archive_closed must reject negative days_old via MCP."""

    async def test_archive_closed_negative_days_old_rejected(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool("archive_closed", {"days_old": -1})
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION
        assert "days_old" in data["error"]

    async def test_archive_closed_days_old_zero_requires_label(self, mcp_db: FiligreeDB) -> None:
        """days_old<7 requires a label filter to prevent project-wide sweeps
        of recently-closed issues (filigree-cb980eee0d, P3.17)."""
        result = await call_tool("archive_closed", {"days_old": 0})
        data = _parse(result)
        assert data["code"] == ErrorCode.VALIDATION
        assert "label filter" in data["error"]

    async def test_archive_closed_days_old_zero_with_label_ok(self, mcp_db: FiligreeDB) -> None:
        """0 with a label filter is valid — scoped to the label."""
        result = await call_tool("archive_closed", {"days_old": 0, "label": "test-scope"})
        data = _parse(result)
        assert "error" not in data
