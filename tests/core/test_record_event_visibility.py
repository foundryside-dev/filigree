"""§0.2 — _record_event collision visibility.

Pins the v16 behaviour:

- ``event_seq`` extends the dedup tuple, so same-actor same-second
  emissions land distinct rows (heartbeat bursts, batch ops that share
  a single ``_now_iso()``).
- ``_record_event`` uses plain ``INSERT`` rather than ``INSERT OR
  IGNORE``: a true duplicate row (every column, including ``event_seq``,
  identical to an existing row) raises ``IntegrityError`` so the
  caller's transaction rolls back instead of silently swallowing the
  event.
- ``event_seq`` is monotonic per issue_id.

Closes the silent-failure C3 finding from the 2.1.0 panel review.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from pathlib import Path

import pytest

from filigree.core import FiligreeDB
from tests._db_factory import make_db


@pytest.fixture
def db(tmp_path: Path) -> Generator[FiligreeDB, None, None]:
    d = make_db(tmp_path)
    try:
        yield d
    finally:
        d.close()


class TestRecordEventVisibility:
    def test_heartbeat_bursts_record_every_event(self, db: FiligreeDB) -> None:
        """Fire 10 heartbeats in a tight loop; assert every event lands.

        Pre-v16 the ISO-second created_at + INSERT OR IGNORE collapsed
        same-second emissions silently. With event_seq extending the
        dedup tuple, every heartbeat is durable.
        """
        issue = db.create_issue("Task", type="task")
        db.claim_issue(issue.id, assignee="alice", actor="alice")
        for _ in range(10):
            db.heartbeat_work(issue.id, actor="alice", expected_assignee="alice")
        # 11 heartbeat events expected: 10 from the loop + the heartbeat
        # the initial claim records implicitly is "claimed", not "heartbeat".
        rows = db.conn.execute(
            "SELECT COUNT(*) FROM events WHERE issue_id = ? AND event_type = 'heartbeat'",
            (issue.id,),
        ).fetchone()
        assert rows[0] == 10

    def test_record_event_sequence_monotonic_per_issue(self, db: FiligreeDB) -> None:
        """event_seq increments per issue, never repeats within one issue's stream."""
        issue = db.create_issue("Task", type="task")
        db.claim_issue(issue.id, assignee="alice", actor="alice")
        for _ in range(5):
            db.heartbeat_work(issue.id, actor="alice", expected_assignee="alice")
        seqs = [
            row[0]
            for row in db.conn.execute(
                "SELECT event_seq FROM events WHERE issue_id = ? ORDER BY id",
                (issue.id,),
            ).fetchall()
        ]
        # Strictly increasing; first event for an issue starts at 0.
        assert seqs == sorted(seqs)
        assert seqs == list(range(len(seqs)))

    def test_record_event_sequence_independent_across_issues(self, db: FiligreeDB) -> None:
        """Two issues' event_seq streams are independent — each starts at 0."""
        a = db.create_issue("A", type="task")
        b = db.create_issue("B", type="task")
        # Each issue's create_issue already records one "created" event.
        a_seqs = [
            row[0]
            for row in db.conn.execute(
                "SELECT event_seq FROM events WHERE issue_id = ? ORDER BY id",
                (a.id,),
            ).fetchall()
        ]
        b_seqs = [
            row[0]
            for row in db.conn.execute(
                "SELECT event_seq FROM events WHERE issue_id = ? ORDER BY id",
                (b.id,),
            ).fetchall()
        ]
        assert a_seqs[0] == 0
        assert b_seqs[0] == 0

    def test_record_event_true_duplicate_raises_integrity_error(self, db: FiligreeDB) -> None:
        """A genuine duplicate INSERT (same composite key including event_seq)
        raises IntegrityError so the caller can roll back. Pre-v16 the
        same scenario was silently swallowed by INSERT OR IGNORE.

        Exercised by manually re-inserting a row with the same composite —
        the natural _record_event path can't reach this state because
        event_seq is always MAX+1 for the issue.
        """
        issue = db.create_issue("Task", type="task")
        row = db.conn.execute(
            "SELECT issue_id, event_type, actor, old_value, new_value, comment, created_at, event_seq FROM events WHERE issue_id = ?",
            (issue.id,),
        ).fetchone()
        assert row is not None
        with pytest.raises(sqlite3.IntegrityError):
            db.conn.execute(
                "INSERT INTO events (issue_id, event_type, actor, old_value, new_value, comment, created_at, event_seq) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(row),
            )
        db.conn.rollback()
