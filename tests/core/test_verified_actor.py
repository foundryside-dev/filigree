"""Tests for transport-bound verified-actor plumbing (ADR-012, schema v24)."""

from __future__ import annotations

from pathlib import Path

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


def test_observation_stamps_and_exposes_verified_actor(db: FiligreeDB) -> None:
    db.set_verified_actor("alice")
    obs = db.create_observation(summary="smell in foo.py", actor="agent-x")
    assert obs["verified_actor"] == "alice"
    # Read-back via list also carries it.
    listed = db.list_observations()
    assert listed[0]["verified_actor"] == "alice"


def test_observation_verified_actor_null_when_unset(db: FiligreeDB) -> None:
    obs = db.create_observation(summary="another smell", actor="agent-x")
    assert obs["verified_actor"] is None


def test_file_event_stamps_verified_actor(db: FiligreeDB) -> None:
    db.set_verified_actor("alice")
    # Re-registering a file with changed metadata emits a file_metadata_update
    # file_event (Site B of the two stamped INSERTs).
    db.register_file("foo.py")
    db.register_file("foo.py", metadata={"k": "v"})
    row = db.conn.execute("SELECT verified_actor FROM file_events WHERE event_type = 'file_metadata_update'").fetchone()
    assert row is not None
    assert row["verified_actor"] == "alice"


def test_file_event_verified_actor_null_when_unset(db: FiligreeDB) -> None:
    db.register_file("bar.py")
    db.register_file("bar.py", metadata={"k": "v"})
    row = db.conn.execute("SELECT verified_actor FROM file_events WHERE event_type = 'file_metadata_update'").fetchone()
    assert row is not None
    assert row["verified_actor"] is None


def test_annotation_event_stamps_verified_actor(db: FiligreeDB, tmp_path: Path) -> None:
    db.project_root = tmp_path
    (tmp_path / "foo.py").write_text("one\ntwo\nthree\n")
    db.set_verified_actor("alice")
    db.annotate_file("foo.py", "smell here", line_start=1, actor="agent-x")
    row = db.conn.execute("SELECT verified_actor FROM annotation_events ORDER BY rowid DESC LIMIT 1").fetchone()
    assert row is not None
    assert row["verified_actor"] == "alice"


def test_annotation_event_verified_actor_null_when_unset(db: FiligreeDB, tmp_path: Path) -> None:
    db.project_root = tmp_path
    (tmp_path / "bar.py").write_text("one\ntwo\nthree\n")
    db.annotate_file("bar.py", "smell here", line_start=1, actor="agent-x")
    row = db.conn.execute("SELECT verified_actor FROM annotation_events ORDER BY rowid DESC LIMIT 1").fetchone()
    assert row is not None
    assert row["verified_actor"] is None


def test_export_import_round_trips_verified_actor(tmp_path: Path) -> None:
    src_dir = tmp_path / "src" / ".filigree"
    src_dir.mkdir(parents=True)
    src = FiligreeDB.from_filigree_dir(src_dir)
    src.set_verified_actor("alice")
    issue = src.create_issue(title="t", actor="agent-x")
    src.add_comment(issue.id, "hello", author="agent-x")
    export_path = tmp_path / "dump.jsonl"
    src.export_jsonl(export_path)

    dst_dir = tmp_path / "dst" / ".filigree"
    dst_dir.mkdir(parents=True)
    dst = FiligreeDB.from_filigree_dir(dst_dir)
    # Import must NOT stamp the importer's identity; it restores the recorded one.
    dst.set_verified_actor("bob")
    dst.import_jsonl(export_path, allow_foreign_ids=True)

    ev = dst.conn.execute(
        "SELECT verified_actor FROM events WHERE issue_id = ? AND event_type = 'created'",
        (issue.id,),
    ).fetchone()
    assert ev["verified_actor"] == "alice"
    cm = dst.conn.execute("SELECT verified_author FROM comments WHERE issue_id = ?", (issue.id,)).fetchone()
    assert cm["verified_author"] == "alice"
