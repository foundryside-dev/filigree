"""MCP ``work_heartbeat`` holder check (final review I5): ``actor`` is required, no default to the holder."""

from __future__ import annotations

import pytest

from filigree.core import FiligreeDB
from filigree.mcp_server import call_tool
from tests.core.test_heartbeat_holder_check import _events
from tests.mcp._helpers import _parse


@pytest.mark.asyncio
class TestMcp:
    async def test_missing_actor_is_validation(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Held MCP")
        mcp_db.claim_issue(issue.id, assignee="alice", actor="alice")

        data = _parse(await call_tool("work_heartbeat", {"issue_id": issue.id}))

        assert data["code"] == "VALIDATION"
        assert _events(mcp_db, issue.id) == []

    async def test_non_holder_naming_the_holder_conflicts(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Held MCP")
        mcp_db.claim_issue(issue.id, assignee="alice", actor="alice")

        data = _parse(await call_tool("work_heartbeat", {"issue_id": issue.id, "actor": "bob", "expected_assignee": "alice"}))

        assert data["code"] == "CONFLICT"

    async def test_override_records_override_event(self, mcp_db: FiligreeDB) -> None:
        issue = mcp_db.create_issue("Held MCP")
        mcp_db.claim_issue(issue.id, assignee="alice", actor="alice")

        data = _parse(await call_tool("work_heartbeat", {"issue_id": issue.id, "actor": "coordinator", "override": True}))

        assert data["assignee"] == "alice"
        assert _events(mcp_db, issue.id) == [("heartbeat_by_override", "coordinator")]
