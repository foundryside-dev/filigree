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
