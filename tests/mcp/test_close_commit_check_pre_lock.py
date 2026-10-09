"""The MCP close's commit-reachability check runs before the per-project tool lock (final review I4).

``call_tool`` serialises tool calls per ``FiligreeDB`` with an asyncio lock. Task
0.6 moved the git check (fetch + merge-base, up to ~10 s each) off the event
loop, but ``issue_close`` still awaited it while holding that lock, so one
agent's close-with-commit stalled every other MCP call on the project (with the
daemon-hosted ``/mcp`` every session shares one DB object). The check touches no
DB connection, so it is now computed before the lock is taken.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from filigree.commit_reachability import ReachabilityCheck
from filigree.core import FiligreeDB
from filigree.mcp_server import call_tool
from tests.mcp._helpers import _parse


@pytest.mark.asyncio
async def test_slow_commit_check_does_not_block_a_concurrent_call(mcp_db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    target = mcp_db.create_issue("close with a slow commit check", priority=2)
    other = mcp_db.create_issue("read while the close checks git", priority=2)
    started = threading.Event()
    release = threading.Event()
    real_check = mcp_db.check_close_commit

    def slow_check(commit: str) -> ReachabilityCheck:
        started.set()
        release.wait(timeout=10)
        return real_check(commit)

    monkeypatch.setattr(mcp_db, "check_close_commit", slow_check)

    close_task = asyncio.create_task(call_tool("issue_close", {"issue_id": target.id, "reason": "done", "commit": "main@deadbeefcafe"}))
    try:
        while not started.is_set():
            await asyncio.sleep(0.01)
        assert not close_task.done()

        # Under the old code this waits on the lock until the close finishes (it
        # cannot: the check is parked on ``release``), so wait_for times out.
        got = _parse(await asyncio.wait_for(call_tool("issue_get", {"issue_id": other.id}), timeout=5))

        assert got["issue_id"] == other.id
        assert not close_task.done()
    finally:
        release.set()
    closed = _parse(await close_task)
    assert closed["status_category"] == "done"
    assert closed["close_commit"] == "main@deadbeefcafe"


@pytest.mark.asyncio
async def test_close_of_missing_issue_never_runs_git(mcp_db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(commit: str) -> ReachabilityCheck:
        raise AssertionError("a close that will 404 must not run the git check")

    monkeypatch.setattr(mcp_db, "check_close_commit", boom)
    prefix = mcp_db.prefix

    data = _parse(await call_tool("issue_close", {"issue_id": f"{prefix}-0000000000", "reason": "done", "commit": "main@deadbeefcafe"}))

    assert data["code"] == "NOT_FOUND"
