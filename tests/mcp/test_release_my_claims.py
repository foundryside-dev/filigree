"""MCP release_my_claims handler coverage and response-shape regressions."""

from __future__ import annotations

from typing import Any

import pytest

from filigree.core import FiligreeDB
from filigree.mcp_server import call_tool  # type: ignore[attr-defined]
from filigree.mcp_tools.issues import _handle_release_my_claims
from filigree.types.api import ErrorCode
from tests.mcp._helpers import _parse


@pytest.mark.asyncio
class TestReleaseMyClaims:
    async def test_slim_default_releases_current_actor_claims(self, mcp_db: FiligreeDB) -> None:
        mine = mcp_db.create_issue("Mine")
        other = mcp_db.create_issue("Other")
        mcp_db.claim_issue(mine.id, assignee="agent-1")
        mcp_db.claim_issue(other.id, assignee="agent-2")

        data = _parse(await call_tool("work_release_mine", {"actor": "agent-1", "reason": "session ended"}))

        assert data["succeeded"] == [
            {
                "issue_id": mine.id,
                "title": "Mine",
                "status": "open",
                "priority": 2,
                "type": "task",
            }
        ]
        assert data["failed"] == []
        assert "dry_run" not in data
        assert mcp_db.get_issue(mine.id).assignee == ""
        assert mcp_db.get_issue(other.id).assignee == "agent-2"

    async def test_full_dry_run_filters_by_label_prefix_without_mutating_claims(self, mcp_db: FiligreeDB) -> None:
        scoped = mcp_db.create_issue("Scoped", labels=["cluster:session"])
        unrelated = mcp_db.create_issue("Unrelated")
        mcp_db.claim_issue(scoped.id, assignee="agent-1")
        mcp_db.claim_issue(unrelated.id, assignee="agent-1")

        data = _parse(
            await call_tool(
                "work_release_mine",
                {
                    "actor": "agent-1",
                    "label_prefix": "cluster:",
                    "dry_run": True,
                    "response_detail": "full",
                },
            )
        )

        assert data["dry_run"] is True
        assert [issue["issue_id"] for issue in data["succeeded"]] == [scoped.id]
        assert data["succeeded"][0]["labels"] == ["cluster:session"]
        assert data["failed"] == []
        assert mcp_db.get_issue(scoped.id).assignee == "agent-1"
        assert mcp_db.get_issue(unrelated.id).assignee == "agent-1"

    @pytest.mark.parametrize(
        ("arguments", "message"),
        [
            ({"actor": "agent-1", "label": 1}, "label must be a string"),
            ({"actor": "agent-1", "label_prefix": 1}, "label_prefix must be a string"),
            ({"actor": "agent-1", "dry_run": "yes"}, "dry_run must be a boolean"),
            ({"actor": "agent-1", "revert_status": "yes"}, "revert_status must be a boolean"),
            ({"actor": "agent-1", "reason": 1}, "reason must be a string"),
            ({"actor": "agent-1", "response_detail": "verbose"}, "Invalid value for response_detail"),
        ],
    )
    async def test_validates_optional_arguments(
        self,
        mcp_db: FiligreeDB,
        arguments: dict[str, Any],
        message: str,
    ) -> None:
        data = _parse(await _handle_release_my_claims(arguments))

        assert data["code"] == ErrorCode.VALIDATION
        assert message in data["error"]
