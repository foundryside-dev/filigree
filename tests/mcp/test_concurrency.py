"""Regression tests for filigree-33a938b515.

The MCP SDK dispatches tool calls concurrently; without serialisation two
coroutines share one cached ``sqlite3.Connection`` on ``FiligreeDB`` and
``call_tool``'s ``finally`` rollback in one coroutine erases the sibling's
uncommitted writes.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from mcp.types import TextContent

from filigree.core import FiligreeDB
from filigree.mcp_server import call_tool


async def test_concurrent_raising_tool_does_not_corrupt_sibling(mcp_db: FiligreeDB) -> None:
    """A raising tool call must not wipe a sibling coroutine's pending writes.

    One coroutine writes then yields (simulating any ``await`` between
    mutations and commit). A second coroutine's handler raises. Without
    serialisation the second call's ``finally`` rolls back the first's
    uncommitted insert.
    """
    proceed = asyncio.Event()
    slow_commit_called = asyncio.Event()

    async def _slow_handler(arguments: dict[str, Any]) -> list[TextContent]:
        mcp_db.conn.execute(
            "INSERT INTO issues (id, title, status, priority, type, "
            "created_at, updated_at, description, notes, fields) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "mcp-slowwrite",
                "Slow write",
                "open",
                2,
                "task",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                "",
                "",
                "{}",
            ),
        )
        await proceed.wait()
        mcp_db.conn.commit()
        slow_commit_called.set()
        return [TextContent(type="text", text='{"ok": true}')]

    async def _boom(arguments: dict[str, Any]) -> list[TextContent]:
        raise RuntimeError("simulated handler crash")

    import filigree.mcp_server as mcp_mod

    mcp_mod._all_handlers["__slow"] = _slow_handler
    mcp_mod._all_handlers["__boom"] = _boom
    try:
        slow_task = asyncio.create_task(call_tool("__slow", {}))
        # Let the slow handler reach its ``await proceed.wait()`` point with
        # an in-flight transaction.
        for _ in range(5):
            await asyncio.sleep(0)
            if mcp_db.conn.in_transaction:
                break
        assert mcp_db.conn.in_transaction, "precondition: slow handler opened a transaction"

        boom_task = asyncio.create_task(call_tool("__boom", {}))
        # Give the event loop a chance to run the boom handler if the lock
        # is not in place. With the fix, boom_task is blocked on the lock.
        for _ in range(5):
            await asyncio.sleep(0)

        proceed.set()
        await slow_task
        with pytest.raises(RuntimeError, match="simulated handler crash"):
            await boom_task
    finally:
        mcp_mod._all_handlers.pop("__slow", None)
        mcp_mod._all_handlers.pop("__boom", None)

    assert slow_commit_called.is_set(), "slow handler did not reach commit"
    row = mcp_db.conn.execute("SELECT id FROM issues WHERE id = 'mcp-slowwrite'").fetchone()
    assert row is not None, "sibling's finally rollback erased an in-flight write on the shared connection"


async def test_concurrent_mutations_both_persist(mcp_db: FiligreeDB) -> None:
    """Two concurrent ``create_issue`` calls must both persist."""

    async def _create(title: str) -> None:
        result = await call_tool("issue_create", {"title": title})
        text = result[0].text
        assert '"error":' not in text, text

    await asyncio.gather(_create("concurrent-A"), _create("concurrent-B"))

    rows = mcp_db.conn.execute("SELECT title FROM issues WHERE title LIKE 'concurrent-%' ORDER BY title").fetchall()
    titles = [r["title"] for r in rows]
    assert titles == ["concurrent-A", "concurrent-B"], f"expected both concurrent issues to persist, got: {titles}"
