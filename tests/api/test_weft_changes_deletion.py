"""Weft /changes deletion-signal tests (F5, filigree-2183fea23a).

A hard-deleted issue leaves no events/issues row, so federation consumers
reconciling off ``GET /api/weft/changes`` must learn of the deletion from the
``deleted_issues`` tombstone, surfaced as a synthetic ``issue_deleted`` change
record. The record must:

- appear on the feed for a ``since`` before the deletion,
- carry the deleted issue_id and a ``created_at`` usable as the cursor,
- be absent from the normal (live-issue) event feed,
- be delivered exactly once when paging with the ``since``/``after_event_id``
  cursor.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

import filigree.dashboard as dash_module
from filigree.core import FiligreeDB
from filigree.dashboard import create_app
from tests._db_factory import make_db

_EPOCH = "2000-01-01T00:00:00+00:00"


@pytest.fixture
def changes_db(tmp_path: Path) -> Generator[FiligreeDB, None, None]:
    db = make_db(tmp_path, check_same_thread=False)
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
async def client(changes_db: FiligreeDB) -> AsyncClient:
    dash_module._db = changes_db
    app = create_app()
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        dash_module._db = None


def _delete_terminal(db: FiligreeDB, title: str, *, actor: str = "tester") -> str:
    issue = db.create_issue(title, type="task")
    db.close_issue(issue.id, force=True)
    db.delete_issue(issue.id, actor=actor)
    return issue.id


class TestGetEventsSinceDeletionMerge:
    """Seam-level tests on get_events_since (shared by MCP/CLI/HTTP)."""

    def test_deleted_issue_surfaces_as_issue_deleted(self, changes_db: FiligreeDB) -> None:
        issue_id = _delete_terminal(changes_db, "doomed", actor="alice")
        events = changes_db.get_events_since(_EPOCH, limit=100)
        deletions = [e for e in events if e["event_type"] == "issue_deleted"]
        assert len(deletions) == 1
        rec = deletions[0]
        assert rec["issue_id"] == issue_id
        assert rec["issue_title"] == "doomed"
        assert rec["actor"] == "alice"
        assert rec["created_at"]
        # The synthetic id sorts strictly above every real event id.
        max_real = max((e["id"] for e in events if e["event_type"] != "issue_deleted"), default=0)
        assert rec["id"] > max_real

    def test_absent_from_normal_feed_via_type_filter(self, changes_db: FiligreeDB) -> None:
        _delete_terminal(changes_db, "doomed")
        changes_db.create_issue("survivor", type="task")
        # A normal feed filtered to a live-issue event type must not include it.
        events = changes_db.get_events_since(_EPOCH, limit=100, event_type="created")
        assert all(e["event_type"] == "created" for e in events)
        assert not any(e["event_type"] == "issue_deleted" for e in events)

    def test_exclude_types_drops_deletions(self, changes_db: FiligreeDB) -> None:
        _delete_terminal(changes_db, "doomed")
        events = changes_db.get_events_since(_EPOCH, limit=100, exclude_types=["issue_deleted"])
        assert not any(e["event_type"] == "issue_deleted" for e in events)

    def test_type_filter_selects_only_deletions(self, changes_db: FiligreeDB) -> None:
        a = _delete_terminal(changes_db, "doomed-a")
        b = _delete_terminal(changes_db, "doomed-b")
        events = changes_db.get_events_since(_EPOCH, limit=100, event_type="issue_deleted")
        assert {e["issue_id"] for e in events} == {a, b}
        assert all(e["event_type"] == "issue_deleted" for e in events)

    def test_issue_id_filter_matches_tombstone(self, changes_db: FiligreeDB) -> None:
        target = _delete_terminal(changes_db, "doomed")
        _delete_terminal(changes_db, "other")
        events = changes_db.get_events_since(_EPOCH, limit=100, issue_id=target)
        assert [e["event_type"] for e in events] == ["issue_deleted"]
        assert events[0]["issue_id"] == target

    def test_label_filter_excludes_deletions(self, changes_db: FiligreeDB) -> None:
        """A deleted issue's labels are gone — a label filter can never match it."""
        _delete_terminal(changes_db, "doomed")
        events = changes_db.get_events_since(_EPOCH, limit=100, label="anything")
        assert not any(e["event_type"] == "issue_deleted" for e in events)

    def test_exactly_once_across_cursor_paging(self, changes_db: FiligreeDB) -> None:
        """Walking the (since, after_event_id) cursor must yield each deletion once."""
        deleted_ids = [_delete_terminal(changes_db, f"doomed-{n}") for n in range(5)]
        # Also some live events interleaved.
        changes_db.create_issue("live-1", type="task")
        changes_db.create_issue("live-2", type="task")

        seen_deletions: list[str] = []
        since = _EPOCH
        after_event_id: int | None = None
        for _ in range(50):  # generous bound to terminate
            page = changes_db.get_events_since(since, after_event_id=after_event_id, limit=2)
            if not page:
                break
            for e in page:
                if e["event_type"] == "issue_deleted":
                    seen_deletions.append(e["issue_id"])
            last = page[-1]
            since = last["created_at"]
            after_event_id = last["id"]
            if len(page) < 2:
                break
        # Every deletion seen exactly once.
        assert sorted(seen_deletions) == sorted(deleted_ids)
        assert len(seen_deletions) == len(set(seen_deletions))

    def test_same_timestamp_tiebreaker_paging(self, changes_db: FiligreeDB) -> None:
        """Force a tombstone to share a created_at with a live event, then page
        with limit=1 — both must appear exactly once, tombstone ordered last.

        Microsecond timestamps make natural collisions rare, so this exercises
        the (created_at = ? AND id > ?) tiebreaker branch explicitly.
        """
        live = changes_db.create_issue("live", type="task")
        ev = changes_db.conn.execute(
            "SELECT id, created_at FROM events WHERE issue_id = ? ORDER BY id LIMIT 1",
            (live.id,),
        ).fetchone()
        shared_ts = ev["created_at"]
        # Insert a tombstone at exactly the same created_at as the live event.
        changes_db.conn.execute(
            "INSERT INTO deleted_issues (issue_id, title, type, deleted_at, deleted_by) VALUES (?, ?, 'task', ?, 'tester')",
            ("test-collide", "collide", shared_ts),
        )
        changes_db.conn.commit()

        seen: list[tuple[str, str]] = []
        since = _EPOCH
        after_event_id: int | None = None
        for _ in range(50):
            page = changes_db.get_events_since(since, after_event_id=after_event_id, limit=1)
            if not page:
                break
            e = page[0]
            seen.append((e["event_type"], e["issue_id"]))
            since = e["created_at"]
            after_event_id = e["id"]

        # The created event and the deletion both surface exactly once.
        assert ("created", live.id) in seen
        assert ("issue_deleted", "test-collide") in seen
        assert len([s for s in seen if s[1] == "test-collide"]) == 1
        # At the shared timestamp the deletion sorts after the live event.
        idx_created = seen.index(("created", live.id))
        idx_deleted = seen.index(("issue_deleted", "test-collide"))
        assert idx_deleted > idx_created


class TestWeftChangesHttpDeletion:
    async def test_deletion_appears_on_changes_feed(self, changes_db: FiligreeDB, client: AsyncClient) -> None:
        issue_id = _delete_terminal(changes_db, "doomed", actor="alice")
        resp = await client.get("/api/weft/changes", params={"since": _EPOCH})
        assert resp.status_code == 200
        body = resp.json()
        deletions = [it for it in body["items"] if it["event_type"] == "issue_deleted"]
        assert len(deletions) == 1
        rec = deletions[0]
        assert rec["issue_id"] == issue_id
        assert rec["issue_title"] == "doomed"
        assert rec["actor"] == "alice"
        # Cursor fields advance past the deletion.
        assert "created_at" in rec
        assert "event_id" in rec

    async def test_type_filter_on_http(self, changes_db: FiligreeDB, client: AsyncClient) -> None:
        issue_id = _delete_terminal(changes_db, "doomed")
        changes_db.create_issue("survivor", type="task")
        resp = await client.get("/api/weft/changes", params={"since": _EPOCH, "type": "issue_deleted"})
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert [it["issue_id"] for it in items] == [issue_id]

    async def test_exactly_once_via_next_since_cursor(self, changes_db: FiligreeDB, client: AsyncClient) -> None:
        deleted_ids = {_delete_terminal(changes_db, f"doomed-{n}") for n in range(4)}
        seen: list[str] = []
        since = _EPOCH
        after_event_id: int | None = None
        for _ in range(50):
            params: dict[str, object] = {"since": since, "limit": 2}
            if after_event_id is not None:
                params["after_event_id"] = after_event_id
            resp = await client.get("/api/weft/changes", params=params)
            assert resp.status_code == 200
            body = resp.json()
            for it in body["items"]:
                if it["event_type"] == "issue_deleted":
                    seen.append(it["issue_id"])
            if not body["has_more"]:
                break
            since = body["next_since"]
            after_event_id = body["next_event_id"]
        deletion_hits = [s for s in seen if s in deleted_ids]
        assert sorted(set(deletion_hits)) == sorted(deleted_ids)
        assert len(deletion_hits) == len(set(deletion_hits))


class TestDeletionCarriesAffectedEntities:
    """F5 entity-association amplifier (filigree-f3bf56554c).

    ``delete_issue`` cascades ``entity_associations`` (ON DELETE CASCADE), so a
    hard delete silently drops Filigree's side of every Loomweave entity binding.
    A consumer reconciling off ``issue_deleted`` only learns the *issue* is gone;
    without the dropped bindings it cannot purge its mirrored reverse lookup
    (``list_associations_by_entity``) and surfaces a user-facing phantom issue.
    The synthetic record therefore carries ``affected_entities`` — the sorted
    ``loomweave_entity_id``s the cascade removed — captured in the tombstone before
    the rows vanish.
    """

    def test_affected_entities_on_synthetic_record(self, changes_db: FiligreeDB) -> None:
        issue = changes_db.create_issue("bound", type="task")
        # Inserted out of order; the signal must be deterministically sorted.
        changes_db.add_entity_association(issue.id, "py:func:beta", "h1", actor="tester")
        changes_db.add_entity_association(issue.id, "py:func:alpha", "h2", actor="tester")
        changes_db.close_issue(issue.id, force=True)
        changes_db.delete_issue(issue.id, actor="alice")

        events = changes_db.get_events_since(_EPOCH, limit=100)
        rec = next(e for e in events if e["event_type"] == "issue_deleted")
        assert rec["affected_entities"] == ["py:func:alpha", "py:func:beta"]

    def test_affected_entities_empty_when_no_bindings(self, changes_db: FiligreeDB) -> None:
        _delete_terminal(changes_db, "unbound")
        events = changes_db.get_events_since(_EPOCH, limit=100)
        rec = next(e for e in events if e["event_type"] == "issue_deleted")
        assert rec["affected_entities"] == []

    async def test_affected_entities_on_http_changes(self, changes_db: FiligreeDB, client: AsyncClient) -> None:
        issue = changes_db.create_issue("bound", type="task")
        changes_db.add_entity_association(issue.id, "py:func:foo", "h1", actor="tester")
        changes_db.close_issue(issue.id, force=True)
        changes_db.delete_issue(issue.id, actor="alice")

        resp = await client.get("/api/weft/changes", params={"since": _EPOCH, "type": "issue_deleted"})
        assert resp.status_code == 200
        rec = resp.json()["items"][0]
        assert rec["affected_entities"] == ["py:func:foo"]

    async def test_non_deletion_change_has_empty_affected_entities(self, changes_db: FiligreeDB, client: AsyncClient) -> None:
        """The wire shape is uniform: live-issue change records carry an empty list."""
        changes_db.create_issue("live", type="task")
        resp = await client.get("/api/weft/changes", params={"since": _EPOCH, "type": "created"})
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert items
        assert all(it["affected_entities"] == [] for it in items)


def _insert_tombstone(db: FiligreeDB, issue_id: str, deleted_at: str) -> None:
    """Insert a tombstone row directly, exercising the real production INSERT path.

    Direct-SQL like the cascade test's finding/annotation inserts — the cursor
    logic under test reads ``deleted_issues`` and is indifferent to how rows
    arrived. Routing through ``delete_issue`` would stamp ``deleted_at=now()``
    and auto-assign seq, defeating the controlled gap layout these tests need.
    """
    db.conn.execute(
        "INSERT OR REPLACE INTO deleted_issues (issue_id, title, type, deleted_at, deleted_by, reason) "
        "VALUES (?, ?, 'task', ?, 'tester', '')",
        (issue_id, issue_id, deleted_at),
    )
    db.conn.commit()


class TestTombstoneCursorVacuumStable:
    """FIX 1: the synthetic change-feed event_id is VACUUM-stable by contract.

    The cursor is derived from ``deleted_issues.seq``, an explicit
    ``INTEGER PRIMARY KEY AUTOINCREMENT``. SQLite documents that VACUUM
    *"may change the ROWIDs of entries in any tables that do not have an
    explicit INTEGER PRIMARY KEY"* — so the prior implicit-rowid design relied
    on disclaimed behavior. An explicit INTEGER PK is contractually preserved
    across VACUUM, and AUTOINCREMENT additionally guarantees seq values are
    never reused (the ``test_insert_or_replace_*`` test below covers reuse).

    Honest scope note: this is a forward-locking invariant test
    ("exactly-once across VACUUM"), NOT a rowid-vs-seq discriminator. Empirically
    SQLite 3.47.1 *preserves* the implicit rowid for this table's shape (TEXT PK
    pins rowids via its unique index), verified across delete-low/middle/max and
    page-spanning patterns — so the old rowid version does not visibly break
    today. This test guards against a future SQLite that exercises the
    documented "may change" license; the contract fix removes the dependence on
    that disclaimed behavior regardless.
    """

    def test_cursor_survives_vacuum(self, changes_db: FiligreeDB) -> None:
        ts = "2026-05-29T12:00:00+00:00"
        # 5 tombstones, all same deleted_at -> seq 1..5.
        for n in range(5):
            _insert_tombstone(changes_db, f"x{n}", ts)
        # Delete the two lowest -> survivors x2,x3,x4 sit at seq 3,4,5.
        changes_db.conn.execute("DELETE FROM deleted_issues WHERE issue_id IN ('x0', 'x1')")
        changes_db.conn.commit()

        # Page one, capturing the cursor after seeing only the first survivor.
        page1 = changes_db.get_events_since(_EPOCH, limit=1)
        assert len(page1) == 1
        assert page1[0]["event_type"] == "issue_deleted"
        first = page1[0]["issue_id"]
        cursor_since = page1[0]["created_at"]
        cursor_id = page1[0]["id"]

        # VACUUM must not perturb the seq-derived cursor (explicit INTEGER PK is
        # contractually preserved; the unseen survivors stay above the cursor).
        changes_db.vacuum()

        seen = [first]
        since = cursor_since
        after_event_id: int | None = cursor_id
        for _ in range(20):
            page = changes_db.get_events_since(since, after_event_id=after_event_id, limit=1)
            if not page:
                break
            e = page[0]
            seen.append(e["issue_id"])
            since = e["created_at"]
            after_event_id = e["id"]

        # All three survivors surface exactly once across the VACUUM boundary.
        assert sorted(seen) == ["x2", "x3", "x4"]
        assert len(seen) == len(set(seen))

    def test_insert_or_replace_keys_on_issue_id_and_bumps_seq(self, changes_db: FiligreeDB) -> None:
        """Re-deleting an id (INSERT OR REPLACE) keys on the issue_id UNIQUE
        constraint and assigns a strictly-higher seq via AUTOINCREMENT.

        Covers the re-delete-of-MAX-seq case the prior review flagged: a plain
        INTEGER PRIMARY KEY would reuse the vacated max value (max-of-remaining
        + 1), so a consumer cursored at that seq would never see the
        re-deletion. AUTOINCREMENT consults sqlite_sequence (max-ever) and hands
        out a strictly-higher seq, so the re-notify holds even for the row that
        held the previous maximum.
        """
        _insert_tombstone(changes_db, "a", "2026-05-29T10:00:00+00:00")
        _insert_tombstone(changes_db, "b", "2026-05-29T10:00:01+00:00")
        rows = changes_db.conn.execute("SELECT issue_id, seq FROM deleted_issues ORDER BY seq").fetchall()
        seq_by_id = {r["issue_id"]: r["seq"] for r in rows}
        max_seq_before = max(seq_by_id.values())
        assert seq_by_id["b"] == max_seq_before  # b holds the current max

        # Re-delete the row holding the current max seq.
        _insert_tombstone(changes_db, "b", "2026-05-29T11:00:00+00:00")

        rows_after = changes_db.conn.execute("SELECT issue_id, seq FROM deleted_issues WHERE issue_id = 'b'").fetchall()
        # INSERT OR REPLACE keyed on issue_id UNIQUE -> exactly one row for 'b'.
        assert len(rows_after) == 1
        # Strictly higher seq than any seq ever issued (AUTOINCREMENT backstop).
        assert rows_after[0]["seq"] > max_seq_before
