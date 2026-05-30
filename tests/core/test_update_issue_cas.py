"""§0.1 — update_issue atomic CAS on assignee.

Pins the SQL-level compare-and-swap guard. When an assignee was observed
at SELECT time, the UPDATE adds ``AND assignee = ?`` so a concurrent
reassignment between read and write fails the UPDATE atomically and
raises ``ClaimConflictError`` instead of silently overwriting the new
claimant's audit-trail attribution.

The race surface is real: two agents calling ``update_issue`` against
the same held issue under multi-agent load can otherwise interleave
their reads and writes such that the second write wins silently, with
the audit-trail event recorded under the wrong actor.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest

from filigree.core import FiligreeDB
from filigree.types.api import ClaimConflictError, ErrorCode
from tests._db_factory import make_db


@pytest.fixture
def db(tmp_path: Path) -> Generator[FiligreeDB, None, None]:
    d = make_db(tmp_path)
    try:
        yield d
    finally:
        d.close()


class TestUpdateIssueCAS:
    def test_update_issue_cas_rejects_concurrent_reassign(self, concurrent_db_workers: list[FiligreeDB]) -> None:
        """Two writers race: agent A reads with observed assignee alice,
        agent B reassigns to mallory under A's feet, A's UPDATE fails
        with ClaimConflictError instead of silently winning."""
        a_db, b_db = concurrent_db_workers
        issue = a_db.create_issue("Task", type="task")
        a_db.claim_issue(issue.id, assignee="alice", actor="alice")

        # B (concurrent writer) reassigns directly via SQL — mid-race steal.
        b_db.conn.execute(
            "UPDATE issues SET assignee = 'mallory' WHERE id = ?",
            (issue.id,),
        )
        b_db.conn.commit()

        # A's stale view still shows alice; the SQL CAS catches the race.
        with pytest.raises(ClaimConflictError) as exc:
            a_db.update_issue(issue.id, title="hijacked", actor="alice")
        assert exc.value.expected == "alice"
        assert exc.value.observed == "mallory"

        # Title was not durably written under the wrong actor.
        assert a_db.get_issue(issue.id).title == "Task"

    def test_close_issue_inherits_cas(self, concurrent_db_workers: list[FiligreeDB]) -> None:
        """close_issue routes through update_issue, so the CAS guard
        propagates: a steal between SELECT and the close UPDATE
        produces CONFLICT rather than a silent close under the wrong
        actor's audit row."""
        a_db, b_db = concurrent_db_workers
        issue = a_db.create_issue("Task", type="task")
        a_db.claim_issue(issue.id, assignee="alice", actor="alice")

        b_db.conn.execute(
            "UPDATE issues SET assignee = 'mallory' WHERE id = ?",
            (issue.id,),
        )
        b_db.conn.commit()

        with pytest.raises(ClaimConflictError):
            a_db.close_issue(issue.id, actor="alice")

        # Issue must not have been durably closed by the racing call.
        assert a_db.get_issue(issue.id).status != "closed"

    def test_batch_close_per_item_cas(self, concurrent_db_workers: list[FiligreeDB]) -> None:
        """In a batch_close, a middle item losing the CAS race surfaces
        as a per-item CONFLICT — items before and after still commit.
        Pins the per-item failure semantic that the design's silent-
        failure C1 finding called out."""
        a_db, b_db = concurrent_db_workers
        a = a_db.create_issue("A", type="task")
        b = a_db.create_issue("B", type="task")
        c = a_db.create_issue("C", type="task")
        a_db.claim_issue(b.id, assignee="alice", actor="alice")

        # B is stolen by mallory before a_db can close it.
        b_db.conn.execute(
            "UPDATE issues SET assignee = 'mallory' WHERE id = ?",
            (b.id,),
        )
        b_db.conn.commit()

        closed, failures = a_db.batch_close([a.id, b.id, c.id], actor="alice")
        closed_ids = [issue.id for issue in closed]
        assert a.id in closed_ids
        assert c.id in closed_ids
        assert len(failures) == 1
        assert failures[0]["id"] == b.id
        assert failures[0]["code"] == ErrorCode.CONFLICT

    def test_update_issue_no_observed_assignee_no_cas_guard(self, db: FiligreeDB) -> None:
        """Unassigned issues do not trigger the CAS guard (ADR-008
        read-tolerance for unheld issues). Otherwise every update of
        an unclaimed issue would race against any concurrent claim,
        making bulk metadata edits on a fresh project pointlessly
        fragile."""
        issue = db.create_issue("Task", type="task")
        # No claim; observed assignee is empty.
        updated = db.update_issue(issue.id, title="renamed")
        assert updated.title == "renamed"

    def test_update_issue_with_observed_assignee_succeeds_when_no_race(self, db: FiligreeDB) -> None:
        """Sanity: the CAS guard is invisible to the happy path —
        a held issue updated by its rightful holder still succeeds."""
        issue = db.create_issue("Task", type="task")
        db.claim_issue(issue.id, assignee="alice", actor="alice")
        updated = db.update_issue(issue.id, title="renamed", actor="alice")
        assert updated.title == "renamed"
