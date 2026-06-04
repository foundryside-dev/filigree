"""Tests for transport-bound verified-actor plumbing (ADR-012, schema v24)."""

from __future__ import annotations

from filigree.core import FiligreeDB


def test_constructor_defaults_verified_actor_to_none(db: FiligreeDB) -> None:
    assert db._verified_actor is None


def test_set_verified_actor_updates_field(db: FiligreeDB) -> None:
    db.set_verified_actor("alice")
    assert db._verified_actor == "alice"
    db.set_verified_actor(None)
    assert db._verified_actor is None


def test_borrow_for_worker_thread_propagates_verified_actor(db: FiligreeDB) -> None:
    db.set_verified_actor("alice")
    with db.borrow_for_worker_thread() as clone:
        assert clone._verified_actor == "alice"


def _create_issue(db: FiligreeDB) -> str:
    issue = db.create_issue(title="t", actor="agent-x")
    return issue.id


def test_event_stamps_verified_actor_when_set(db: FiligreeDB) -> None:
    db.set_verified_actor("alice")
    issue_id = _create_issue(db)
    row = db.conn.execute(
        "SELECT verified_actor FROM events WHERE issue_id = ? AND event_type = 'created'",
        (issue_id,),
    ).fetchone()
    assert row["verified_actor"] == "alice"


def test_event_verified_actor_null_when_unset(db: FiligreeDB) -> None:
    # No set_verified_actor call — unverified surface.
    issue_id = _create_issue(db)
    row = db.conn.execute(
        "SELECT verified_actor FROM events WHERE issue_id = ? AND event_type = 'created'",
        (issue_id,),
    ).fetchone()
    assert row["verified_actor"] is None


def test_event_record_read_path_exposes_verified_actor(db: FiligreeDB) -> None:
    db.set_verified_actor("alice")
    issue_id = _create_issue(db)
    events = db.get_issue_events(issue_id)
    created = next(e for e in events if e["event_type"] == "created")
    assert created["verified_actor"] == "alice"


def test_recent_events_read_path_exposes_verified_actor(db: FiligreeDB) -> None:
    db.set_verified_actor("alice")
    issue_id = _create_issue(db)
    events = db.get_recent_events()
    created = next(e for e in events if e["event_type"] == "created" and e["issue_id"] == issue_id)
    assert created["verified_actor"] == "alice"


def test_add_comment_stamps_verified_author(db: FiligreeDB) -> None:
    db.set_verified_actor("alice")
    issue_id = _create_issue(db)
    db.add_comment(issue_id, "hello", author="agent-x")
    row = db.conn.execute("SELECT verified_author FROM comments WHERE issue_id = ?", (issue_id,)).fetchone()
    assert row["verified_author"] == "alice"


def test_get_comments_exposes_verified_author(db: FiligreeDB) -> None:
    db.set_verified_actor("alice")
    issue_id = _create_issue(db)
    db.add_comment(issue_id, "hello", author="agent-x")
    comments = db.get_comments(issue_id)
    assert comments[0]["verified_author"] == "alice"


def test_comment_verified_author_null_when_unset(db: FiligreeDB) -> None:
    issue_id = _create_issue(db)
    db.add_comment(issue_id, "hello", author="agent-x")
    comments = db.get_comments(issue_id)
    assert comments[0]["verified_author"] is None
