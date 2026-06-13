"""MCP-layer tests for the warpline reverify-worklist consumer (federation Seam 2A).

Exercises ``warpline_worklist_ingest`` via call_tool() — the same in-process MCP
shape every other MCP test uses.
"""

from __future__ import annotations

from typing import Any

import pytest

from filigree.core import FiligreeDB
from filigree.mcp_server import call_tool  # type: ignore[attr-defined]
from filigree.types.api import ErrorCode
from tests.mcp._helpers import _parse

pytestmark = pytest.mark.asyncio


def _worklist(sei: str | None, *, locator: str = "pkg.mod.fn", priority: str = "unknown") -> dict[str, Any]:
    return {
        "completeness": "FULL",
        "items": [
            {
                "entity": {"locator": locator, "sei": sei},
                "priority": priority,
                "reason": "changed",
                "depth": 0,
                "why": [],
                "suggested_verification": [{"kind": "test", "command": "run tests"}],
                "enrichment": {"work": [], "risk": [], "governance": [], "requirements": []},
            }
        ],
        "next_actions": {"filigree": []},
    }


class TestIngestWarplineWorklistMCP:
    async def test_preview_default_no_write(self, mcp_db: FiligreeDB) -> None:
        result = _parse(await call_tool("warpline_worklist_ingest", {"worklist": _worklist("loomweave:eid:A1")}))
        assert result["applied"] is False
        assert result["summary"]["filed"] == 1
        assert mcp_db.list_associations_by_entity("loomweave:eid:A1") == []

    async def test_apply_files_and_binds(self, mcp_db: FiligreeDB) -> None:
        result = _parse(
            await call_tool("warpline_worklist_ingest", {"worklist": _worklist("loomweave:eid:A2", priority="P1"), "apply": True})
        )
        assert result["applied"] is True
        filed = result["results"][0]
        assert filed["action"] == "filed"
        issue = mcp_db.get_issue(filed["issue_id"])
        assert issue.priority == 1
        assert "warpline" in issue.labels
        assert [a["issue_id"] for a in mcp_db.list_associations_by_entity("loomweave:eid:A2")] == [filed["issue_id"]]

    async def test_links_existing_open_issue(self, mcp_db: FiligreeDB) -> None:
        existing = mcp_db.create_issue("tracked", priority=2)
        mcp_db.add_entity_association(existing.id, "loomweave:eid:A3", content_hash="h")
        result = _parse(await call_tool("warpline_worklist_ingest", {"worklist": _worklist("loomweave:eid:A3"), "apply": True}))
        assert result["results"][0]["action"] == "linked"
        assert result["results"][0]["linked_issue_ids"] == [existing.id]

    async def test_worklist_must_be_object(self, mcp_db: FiligreeDB) -> None:
        result = _parse(await call_tool("warpline_worklist_ingest", {"worklist": "nope"}))
        assert result["code"] == ErrorCode.VALIDATION

    async def test_priority_out_of_range_rejected(self, mcp_db: FiligreeDB) -> None:
        result = _parse(await call_tool("warpline_worklist_ingest", {"worklist": _worklist("loomweave:eid:A4"), "priority": 9}))
        assert result["code"] == ErrorCode.VALIDATION
