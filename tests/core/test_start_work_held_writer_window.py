"""§2.2 statement trace — held-writer window for start_work / start_next_work.

Pins the design's intent: candidate discovery and template lookups run
lock-free, only the claim+update composite acquires a writer lock. Uses
``sqlite3.Connection.set_trace_callback`` to inspect the SQL statements
between ``BEGIN IMMEDIATE`` and the next ``COMMIT`` / ``ROLLBACK``.
"""

from __future__ import annotations

import pytest

from filigree.core import FiligreeDB


class _LockWindowTracker:
    """Records every statement inside a BEGIN IMMEDIATE → COMMIT/ROLLBACK span."""

    def __init__(self) -> None:
        self.windows: list[list[str]] = []
        self._current: list[str] | None = None

    def __call__(self, sql: str) -> None:
        head = sql.lstrip().upper()
        if head.startswith("BEGIN IMMEDIATE"):
            self._current = []
        elif head.startswith(("COMMIT", "ROLLBACK")) and self._current is not None:
            self.windows.append(self._current)
            self._current = None
        elif self._current is not None:
            self._current.append(sql)


def _assert_no_discovery_or_template_sql(statements: list[str]) -> None:
    """The critical section should not include candidate or template reads."""
    window = "\n".join(statements).lower()
    assert "type_templates" not in window
    assert " from packs" not in window
    assert "select i.id from issues i" not in window
    assert "order by i.priority" not in window


@pytest.mark.parametrize("with_explicit_target", [True, False])
def test_start_work_held_writer_window_excludes_template_lookup(
    db: FiligreeDB,
    with_explicit_target: bool,
) -> None:
    """``start_work`` holds the writer lock only across the claim+update
    composite, not the template lookup that resolves ``target_status``."""
    issue = db.create_issue("contended", priority=1)
    target = "in_progress" if with_explicit_target else None

    tracker = _LockWindowTracker()
    db.conn.set_trace_callback(tracker)
    try:
        db.start_work(issue.id, assignee="alice", target_status=target)
    finally:
        db.conn.set_trace_callback(None)

    # Exactly one writer-lock window opened during start_work
    # (the _start_work_locked critical section).
    assert len(tracker.windows) == 1, f"expected 1 BEGIN/COMMIT pair, got {tracker.windows}"
    _assert_no_discovery_or_template_sql(tracker.windows[0])


def test_start_work_advance_multihop_holds_single_window(db: FiligreeDB) -> None:
    """filigree-406e6b7ee0 Part 2: an ``advance`` multi-hop walk (triage ->
    confirmed -> fixing) still opens exactly one writer-lock window and reads no
    templates inside it — the extra hops are in-memory template lookups."""
    bug = db.create_issue("advance-window", type="bug", priority=1)

    tracker = _LockWindowTracker()
    db.conn.set_trace_callback(tracker)
    try:
        result = db.start_work(bug.id, assignee="alice", advance=True)
    finally:
        db.conn.set_trace_callback(None)

    assert result.status == "fixing"
    assert len(tracker.windows) == 1, f"expected 1 BEGIN/COMMIT pair, got {tracker.windows}"
    _assert_no_discovery_or_template_sql(tracker.windows[0])


def test_start_next_work_iteration_runs_outside_writer_lock(db: FiligreeDB) -> None:
    """``start_next_work`` iterates ``get_ready()`` candidates outside any
    writer lock; only the per-candidate claim+update enters BEGIN IMMEDIATE."""
    issues = [db.create_issue(f"ready-{i}", priority=2) for i in range(3)]

    tracker = _LockWindowTracker()
    db.conn.set_trace_callback(tracker)
    try:
        result = db.start_next_work(assignee="alice")
    finally:
        db.conn.set_trace_callback(None)

    assert result is not None
    assert result.id in {i.id for i in issues}
    # One writer-lock window per successful start (the first candidate).
    assert len(tracker.windows) == 1
    _assert_no_discovery_or_template_sql(tracker.windows[0])


@pytest.mark.parametrize("method_name", ["start_work", "start_next_work"])
def test_start_work_allows_audit_actor_to_differ_from_assignee(db: FiligreeDB, method_name: str) -> None:
    """Coordinator actors may start work for a named assignee.

    The claim holder remains the assignee; the actor is only the audit
    identity for the composed operation.
    """
    issue = db.create_issue(f"{method_name} split actor", type="task")

    if method_name == "start_work":
        result = db.start_work(issue.id, assignee="alice", actor="scheduler")
    else:
        result = db.start_next_work(assignee="alice", actor="scheduler", type_filter="task")

    assert result is not None
    assert result.id == issue.id
    assert result.assignee == "alice"
    events = db.get_issue_events(issue.id)
    start_events = [event for event in events if event["event_type"] in {"claimed", "status_changed"}]
    assert {event["event_type"] for event in start_events} == {"claimed", "status_changed"}
    assert {event["actor"] for event in start_events} == {"scheduler"}
