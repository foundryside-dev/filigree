"""MCP error handling regression tests.

Covers: filigree-d6c3f6 (comment/label error handling)
"""

from __future__ import annotations

import json

import filigree.mcp_server as mcp_mod
from filigree.core import FiligreeDB
from filigree.mcp_server import call_tool
from filigree.types.api import ErrorCode
from tests.mcp._helpers import _parse


class TestMCPCommentErrors:
    """MCP add_comment and get_comments on missing issues."""

    async def test_add_comment_missing_issue(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool(
            "add_comment",
            {"issue_id": "mcp-nonexistent", "text": "Hello"},
        )
        data = _parse(result)
        assert data["code"] == ErrorCode.NOT_FOUND
        assert "mcp-nonexistent" in data["error"]

    async def test_get_comments_missing_issue(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool(
            "get_comments",
            {"issue_id": "mcp-nonexistent"},
        )
        data = _parse(result)
        assert data["code"] == ErrorCode.NOT_FOUND
        assert "mcp-nonexistent" in data["error"]

    async def test_add_comment_still_works(self, mcp_db: FiligreeDB) -> None:
        """Verify normal add_comment still works after the fix."""
        issue = mcp_db.create_issue("Commentable")
        result = await call_tool(
            "add_comment",
            {"issue_id": issue.id, "text": "Hello"},
        )
        data = _parse(result)
        assert data["issue_id"] == issue.id
        assert data["status"] == "open"
        assert "comment_id" in data


class TestMCPLabelErrors:
    """MCP add_label and remove_label on missing issues."""

    async def test_add_label_missing_issue(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool(
            "add_label",
            {"issue_id": "mcp-nonexistent", "label": "bug"},
        )
        data = _parse(result)
        assert data["code"] == ErrorCode.NOT_FOUND
        assert "mcp-nonexistent" in data["error"]

    async def test_remove_label_missing_issue(self, mcp_db: FiligreeDB) -> None:
        result = await call_tool(
            "remove_label",
            {"issue_id": "mcp-nonexistent", "label": "bug"},
        )
        data = _parse(result)
        assert data["code"] == ErrorCode.NOT_FOUND
        assert "mcp-nonexistent" in data["error"]

    async def test_add_label_still_works(self, mcp_db: FiligreeDB) -> None:
        """Verify normal add_label still works after the fix."""
        issue = mcp_db.create_issue("Labelable")
        result = await call_tool(
            "add_label",
            {"issue_id": issue.id, "label": "urgent"},
        )
        data = _parse(result)
        assert data["issue_id"] == issue.id
        assert data["status"] == "open"
        assert data["label_result"] == "added"
        assert "urgent" in data["labels"]


def test_meta_errors_use_error_code_enum(mcp_client_for_empty_project) -> None:
    """Every error emitted from meta.py should have a code that is a
    valid ErrorCode member."""
    client = mcp_client_for_empty_project
    # add_comment on a non-existent issue
    result = client.call_tool("add_comment", {"issue_id": "nope-123", "text": "x"})
    payload = json.loads(result.content[0].text)
    assert payload["code"] in {e.value for e in ErrorCode}


class TestMCPDispatchSchemaValidation:
    async def test_rejects_bad_primitive_type_before_handler(self, monkeypatch) -> None:
        async def unreachable(_arguments):
            raise AssertionError("handler should not run after schema validation failure")

        monkeypatch.setitem(mcp_mod._all_handlers, "report_finding", unreachable)

        data = _parse(
            await call_tool(
                "report_finding",
                {
                    "file_path": "src/app.py",
                    "rule_id": "bad-severity",
                    "message": "Severity has the wrong JSON type",
                    "severity": 123,
                },
            )
        )

        assert data["code"] == ErrorCode.VALIDATION
        assert "severity" in data["error"]
        assert "string" in data["error"]

    async def test_rejects_bad_array_type_before_handler(self, monkeypatch) -> None:
        async def unreachable(_arguments):
            raise AssertionError("handler should not run after schema validation failure")

        monkeypatch.setitem(mcp_mod._all_handlers, "batch_close", unreachable)

        data = _parse(await call_tool("batch_close", {"issue_ids": "issue-1"}))

        assert data["code"] == ErrorCode.VALIDATION
        assert "issue_ids" in data["error"]
        assert "array" in data["error"]
