"""``heartbeat_work`` is holder-checked like ``release_claim`` (final review I5).

Before: naming the holder in ``expected_assignee`` let any actor extend the
lease, a blank actor skipped the check, and the MCP handler defaulted a missing
``actor`` to the holder. Any peer could keep a stale lease alive, which defeats
the ``stale-claims -> reclaim`` recipe. Ruling: ``actor`` must equal the live
assignee unless ``override=True`` (recorded as ``heartbeat_by_override``);
``expected_assignee`` is a compare-and-swap guard only.
"""

from __future__ import annotations

import pytest

from filigree.core import FiligreeDB
from filigree.types.api import ClaimConflictError


def _events(db: FiligreeDB, issue_id: str) -> list[tuple[str, str]]:
    rows = db.conn.execute(
        "SELECT event_type, actor FROM events WHERE issue_id = ? AND event_type LIKE 'heartbeat%' ORDER BY id", (issue_id,)
    ).fetchall()
    return [(r["event_type"], r["actor"]) for r in rows]


@pytest.fixture
def held(db: FiligreeDB) -> str:
    issue = db.create_issue("Held", type="task")
    db.claim_issue(issue.id, assignee="alice", actor="alice")
    return issue.id


class TestCore:
    def test_holder_heartbeat_succeeds(self, db: FiligreeDB, held: str) -> None:
        issue = db.heartbeat_work(held, actor="alice")

        assert issue.assignee == "alice"
        assert _events(db, held) == [("heartbeat", "alice")]

    def test_non_holder_naming_the_holder_conflicts(self, db: FiligreeDB, held: str) -> None:
        with pytest.raises(ClaimConflictError):
            db.heartbeat_work(held, actor="bob", expected_assignee="alice")
        assert _events(db, held) == []

    def test_blank_actor_is_validation(self, db: FiligreeDB, held: str) -> None:
        with pytest.raises(ValueError, match="actor is required") as exc:
            db.heartbeat_work(held, actor="", expected_assignee="alice")
        assert not isinstance(exc.value, ClaimConflictError)
        assert _events(db, held) == []

    def test_override_by_non_holder_succeeds_and_records_override_event(self, db: FiligreeDB, held: str) -> None:
        issue = db.heartbeat_work(held, actor="coordinator", override=True)

        assert issue.assignee == "alice"
        assert _events(db, held) == [("heartbeat_by_override", "coordinator")]

    def test_override_still_enforces_expected_assignee(self, db: FiligreeDB, held: str) -> None:
        with pytest.raises(ClaimConflictError):
            db.heartbeat_work(held, actor="coordinator", expected_assignee="carol", override=True)

    def test_holder_with_override_is_an_ordinary_heartbeat(self, db: FiligreeDB, held: str) -> None:
        db.heartbeat_work(held, actor="alice", override=True)

        assert _events(db, held) == [("heartbeat", "alice")]
