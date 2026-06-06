"""MCP tests for the reconciliation-debt list tool (B2 / Design A, Task 4)."""

from __future__ import annotations

import pytest

from filigree.core import FiligreeDB
from filigree.finding_issue_cascade import record_reconciliation_debt_comment
from filigree.mcp_server import call_tool  # type: ignore[attr-defined]
from tests.mcp._helpers import _parse

pytestmark = pytest.mark.asyncio


async def test_list_reconciliation_debt_lists_issue(mcp_db: FiligreeDB) -> None:
    issue = mcp_db.create_issue("Blocked", priority=2)
    record_reconciliation_debt_comment(mcp_db.conn, issue.id, "blocked by Legis")

    result = _parse(await call_tool("list_reconciliation_debt", {}))
    ids = {item["issue_id"] for item in result["items"]}
    assert issue.id in ids
    row = next(item for item in result["items"] if item["issue_id"] == issue.id)
    assert row["debt_count"] == 1


async def test_list_reconciliation_debt_served_under_new_name(mcp_db: FiligreeDB) -> None:
    issue = mcp_db.create_issue("Blocked", priority=2)
    record_reconciliation_debt_comment(mcp_db.conn, issue.id, "x")

    result = _parse(await call_tool("reconciliation_debt_list", {}))
    ids = {item["issue_id"] for item in result["items"]}
    assert issue.id in ids
