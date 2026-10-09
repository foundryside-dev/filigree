"""Call-twice harness: what does a retried MCP call do?

An agent that loses a response (timeout, dropped transport, context reset)
retries the same call with the same arguments. For every mutating tool the
second call must land in exactly one documented outcome class:

* ``noop`` — the retry changes nothing. The second body is the
  ``{"result": "no_op", ...}`` sentinel, or a success body identical to the
  first one ignoring ``already_holding`` (the flag that tells a retrying
  caller it is being handed back its existing claim). The database snapshot
  (every ``issues``, ``events`` and ``comments`` row, ordered by ``id``) is
  identical after calls 1 and 2.
* ``dup`` — the retry is a second, distinct successful write (the snapshot
  changes again).
* ``error`` — the retry is refused with an error envelope carrying the
  expected ``code``; the refusal itself writes nothing.

``CASES`` is the 4.0 re-entry gate's "retry-safe core loop" instrument. Each
row is ``(tool, args_factory, expected_second[, error_code])``; the factory
seeds the scratch database and returns the arguments for both calls. WP-2.2
extends ``CASES`` to every mutating tool. A row whose current class is a known
defect this harness does not fix records today's class with a ``# 4.0:``
comment naming the work package that changes it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any, Literal, NamedTuple

import pytest

from filigree.core import FiligreeDB
from filigree.mcp_server import call_tool
from filigree.types.api import ErrorCode
from tests.mcp._helpers import _parse

Outcome = Literal["noop", "dup", "error"]
ArgsFactory = Callable[[FiligreeDB], dict[str, Any]]


class Case(NamedTuple):
    tool: str
    args_factory: ArgsFactory
    expected_second: Outcome
    error_code: ErrorCode | None = None


def _start_work_args(db: FiligreeDB) -> dict[str, Any]:
    issue = db.create_issue("call-twice work_start", type="task")
    return {"issue_id": issue.id, "assignee": "agent-a"}


def _start_next_args(db: FiligreeDB) -> dict[str, Any]:
    db.create_issue("call-twice start_next first", type="task", priority=0)
    db.create_issue("call-twice start_next second", type="task", priority=1)
    return {"assignee": "agent-a"}


def _claim_args(db: FiligreeDB) -> dict[str, Any]:
    issue = db.create_issue("call-twice work_claim", type="task")
    return {"issue_id": issue.id, "assignee": "agent-a"}


def _claim_next_args(db: FiligreeDB) -> dict[str, Any]:
    db.create_issue("call-twice claim_next first", type="task", priority=0)
    db.create_issue("call-twice claim_next second", type="task", priority=1)
    return {"assignee": "agent-a"}


def _release_args(db: FiligreeDB) -> dict[str, Any]:
    issue = db.create_issue("call-twice work_release", type="task")
    db.claim_issue(issue.id, assignee="agent-a", actor="agent-a")
    return {"issue_id": issue.id, "actor": "agent-a"}


def _undo_args(db: FiligreeDB) -> dict[str, Any]:
    # Two reversible events: the F1 defect was that each retry walked back one
    # more of them. A single event would make the retry a trivial no_op.
    issue = db.create_issue("call-twice undo", type="task")
    db.update_issue(issue.id, title="first rename", actor="agent-a")
    db.update_issue(issue.id, title="second rename", actor="agent-a")
    target = db.undo_candidate_event_id(issue.id)
    assert target is not None
    return {"issue_id": issue.id, "actor": "agent-a", "expected_event_id": target}


def _close_args(db: FiligreeDB) -> dict[str, Any]:
    issue = db.create_issue("call-twice close", type="task")
    return {"issue_id": issue.id, "actor": "agent-a"}


def _reopen_args(db: FiligreeDB) -> dict[str, Any]:
    issue = db.create_issue("call-twice reopen", type="task")
    db.close_issue(issue.id, actor="agent-a")
    return {"issue_id": issue.id, "actor": "agent-a"}


def _comment_args(db: FiligreeDB) -> dict[str, Any]:
    issue = db.create_issue("call-twice comment", type="task")
    return {"issue_id": issue.id, "text": "same text twice", "actor": "agent-a"}


CASES: list[Case] = [
    # 4.0: WP-2.2 decides (noop candidate) — a same-assignee re-start re-records
    # `claimed` and refreshes the lease today.
    Case("work_start", _start_work_args, "dup"),
    Case("work_start_next", _start_next_args, "noop"),
    # 4.0: WP-2.2 decides (noop candidate) — a same-assignee re-claim re-records
    # `claimed` and refreshes the lease today.
    Case("work_claim", _claim_args, "dup"),
    Case("work_claim_next", _claim_next_args, "noop"),
    Case("work_release", _release_args, "noop"),
    # A retried undo with the same expected_event_id no longer reverses the
    # next event back: the CAS on expected_event_id refuses it.
    Case("admin_undo_last", _undo_args, "error", ErrorCode.CONFLICT),
    # 4.0: becomes noop (WP-2.2) — closing a closed issue is an error today.
    Case("issue_close", _close_args, "error", ErrorCode.INVALID_TRANSITION),
    # 4.0: becomes noop (WP-2.2) — reopening an open issue is an error today.
    Case("issue_reopen", _reopen_args, "error", ErrorCode.INVALID_TRANSITION),
    # 4.0: WP-2.2 decides comment_add retry semantics; a retry is a second comment today.
    Case("comment_add", _comment_args, "dup"),
]


_SNAPSHOT_QUERIES = {
    "issues": "SELECT * FROM issues ORDER BY id",
    "events": "SELECT * FROM events ORDER BY id",
    "comments": "SELECT * FROM comments ORDER BY id",
}


def _snapshot(db: FiligreeDB) -> dict[str, list[tuple[Any, ...]]]:
    conn: sqlite3.Connection = db.conn
    return {table: [tuple(row) for row in conn.execute(sql).fetchall()] for table, sql in _SNAPSHOT_QUERIES.items()}


def _is_error(body: Any) -> bool:
    return isinstance(body, dict) and body.get("error") is not None


def _without_already_holding(body: Any) -> Any:
    if isinstance(body, dict):
        return {key: value for key, value in body.items() if key != "already_holding"}
    return body


@pytest.mark.parametrize("case", CASES, ids=[case.tool for case in CASES])
async def test_call_twice_table(mcp_db: FiligreeDB, case: Case) -> None:
    args = case.args_factory(mcp_db)

    first = _parse(await call_tool(case.tool, dict(args)))
    assert not _is_error(first), f"{case.tool}: first call must succeed, got {first}"
    after_first = _snapshot(mcp_db)

    second = _parse(await call_tool(case.tool, dict(args)))
    after_second = _snapshot(mcp_db)

    if case.expected_second == "noop":
        assert not _is_error(second), f"{case.tool}: retry must not error, got {second}"
        if isinstance(second, dict) and second.get("result") == "no_op":
            assert isinstance(second.get("reason"), str)
            assert second["reason"]
        else:
            assert _without_already_holding(second) == _without_already_holding(first), (
                f"{case.tool}: a no-op retry must return the first call's body"
            )
        assert after_second == after_first, f"{case.tool}: a no-op retry must not write"
    elif case.expected_second == "dup":
        assert not _is_error(second), f"{case.tool}: retry must succeed, got {second}"
        assert not (isinstance(second, dict) and second.get("result") == "no_op")
        assert after_second != after_first, f"{case.tool}: a dup retry is a second write"
    else:
        assert _is_error(second), f"{case.tool}: retry must be refused, got {second}"
        assert second["code"] == case.error_code
        assert after_second == after_first, f"{case.tool}: a refused retry must not write"


def test_cases_cover_the_seeded_tools() -> None:
    """The seed set the 4.0 re-entry gate starts from; WP-2.2 only adds rows."""
    seeded = {
        "work_start",
        "work_start_next",
        "work_claim",
        "work_claim_next",
        "work_release",
        "admin_undo_last",
        "issue_close",
        "issue_reopen",
        "comment_add",
    }
    assert seeded <= {case.tool for case in CASES}
