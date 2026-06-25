"""MCP verb plumbing for the commit anchor (warpline seam, contract B).

``close_issue``, ``claim_issue``, and ``start_work`` accept an optional
``commit`` input that threads through to the DB layer and persists as
``close_commit`` / ``claim_commit``.
"""

from __future__ import annotations

import pytest

from filigree.core import FiligreeDB
from filigree.mcp_tools.issues import (
    _handle_claim_issue,
    _handle_close_issue,
    _handle_start_work,
)
from tests.mcp._helpers import _parse


def _anchors(db: FiligreeDB, issue_id: str) -> tuple[str | None, str | None]:
    row = db.conn.execute("SELECT claim_commit, close_commit FROM issues WHERE id = ?", (issue_id,)).fetchone()
    return row["claim_commit"], row["close_commit"]


@pytest.mark.asyncio
async def test_close_issue_persists_commit(mcp_db: FiligreeDB) -> None:
    issue = mcp_db.create_issue("close via mcp", priority=2)
    data = _parse(await _handle_close_issue({"issue_id": issue.id, "reason": "done", "commit": "main@abc123"}))
    assert "error" not in data, data
    _, close_commit = _anchors(mcp_db, issue.id)
    assert close_commit == "main@abc123"
    # Read-side exposure: the public projection carries it.
    assert data["close_commit"] == "main@abc123"


@pytest.mark.asyncio
async def test_close_issue_without_commit_leaves_null(mcp_db: FiligreeDB) -> None:
    issue = mcp_db.create_issue("close no commit", priority=2)
    data = _parse(await _handle_close_issue({"issue_id": issue.id, "reason": "done"}))
    assert "error" not in data, data
    _, close_commit = _anchors(mcp_db, issue.id)
    assert close_commit is None


@pytest.mark.asyncio
async def test_claim_issue_persists_commit(mcp_db: FiligreeDB) -> None:
    issue = mcp_db.create_issue("claim via mcp", priority=2)
    data = _parse(await _handle_claim_issue({"issue_id": issue.id, "assignee": "alice", "commit": "main@c0ffee"}))
    assert "error" not in data, data
    claim_commit, _ = _anchors(mcp_db, issue.id)
    assert claim_commit == "main@c0ffee"
    assert data["claim_commit"] == "main@c0ffee"


@pytest.mark.asyncio
async def test_start_work_persists_commit(mcp_db: FiligreeDB) -> None:
    issue = mcp_db.create_issue("start via mcp", priority=2)
    data = _parse(await _handle_start_work({"issue_id": issue.id, "assignee": "alice", "commit": "main@1234abcd"}))
    assert "error" not in data, data
    claim_commit, _ = _anchors(mcp_db, issue.id)
    assert claim_commit == "main@1234abcd"
