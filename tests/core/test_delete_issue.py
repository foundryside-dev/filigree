"""Core acceptance tests for FiligreeDB.delete_issue (F5, filigree-2183fea23a).

Covers cascade correctness across every dependent relation, the three guard
refusals (non-terminal, has-children, has-inbound-deps), force behaviours
(orphan children, cascade inbound deps), the deletion tombstone, NOT_FOUND on
a missing issue, and force-bool validation.
"""

from __future__ import annotations

import pytest

from filigree.core import FiligreeDB


def _count(db: FiligreeDB, sql: str, *params: object) -> int:
    return int(db.conn.execute(sql, params).fetchone()[0])


def _terminal(db: FiligreeDB, issue_id: str) -> None:
    """Drive an issue into a done-category state.

    ``force=True`` uses the template's declared escape edge so this works for
    types (like ``bug``) whose initial status has no direct transition to a
    done state.
    """
    db.close_issue(issue_id, force=True)


class TestDeleteIssueGuards:
    def test_refuses_non_terminal(self, db: FiligreeDB) -> None:
        i = db.create_issue("open task", type="task")
        with pytest.raises(ValueError, match="Cannot delete") as exc:
            db.delete_issue(i.id)
        assert "not terminal" in str(exc.value)
        # Issue still present.
        assert db.get_issue(i.id).id == i.id

    def test_refuses_has_children(self, db: FiligreeDB) -> None:
        parent = db.create_issue("parent", type="task")
        db.create_issue("child", type="task", parent_id=parent.id)
        _terminal(db, parent.id)
        with pytest.raises(ValueError, match="child issue") as exc:
            db.delete_issue(parent.id)
        assert "1 child issue" in str(exc.value)

    def test_refuses_inbound_dependents_with_count(self, db: FiligreeDB) -> None:
        blocker = db.create_issue("blocker", type="task")
        dep1 = db.create_issue("dependent 1", type="task")
        dep2 = db.create_issue("dependent 2", type="task")
        db.add_dependency(dep1.id, blocker.id)
        db.add_dependency(dep2.id, blocker.id)
        _terminal(db, blocker.id)
        with pytest.raises(ValueError, match="blocked by it") as exc:
            db.delete_issue(blocker.id)
        assert "2 issues blocked by it" in str(exc.value)

    def test_archived_is_deletable(self, db: FiligreeDB) -> None:
        i = db.create_issue("to archive", type="task")
        db.close_issue(i.id)
        # Simulate archive_closed setting status='archived'.
        db.conn.execute("UPDATE issues SET status = 'archived' WHERE id = ?", (i.id,))
        db.conn.commit()
        result = db.delete_issue(i.id)
        assert result["status"] == "deleted"
        with pytest.raises(KeyError):
            db.get_issue(i.id)

    def test_force_bool_validation(self, db: FiligreeDB) -> None:
        i = db.create_issue("x", type="task")
        _terminal(db, i.id)
        with pytest.raises(ValueError, match="force must be a boolean"):
            db.delete_issue(i.id, force="yes")  # type: ignore[arg-type]

    def test_missing_issue_raises_keyerror(self, db: FiligreeDB) -> None:
        with pytest.raises(KeyError):
            db.delete_issue("test-doesnotexist")


class TestDeleteIssueCascade:
    def test_each_relation_removed(self, db: FiligreeDB) -> None:
        target = db.create_issue("target", type="task", labels=["a", "b"])
        # comment + events accumulate
        db.add_comment(target.id, "a comment", author="tester")
        # outbound dependency: target depends on blocker
        blocker = db.create_issue("blocker", type="task")
        db.add_dependency(target.id, blocker.id)
        # inbound dependency: dependent depends on target
        dependent = db.create_issue("dependent", type="task")
        db.add_dependency(dependent.id, target.id)
        # file association
        fr = db.register_file("src/x.py", actor="tester")
        db.add_file_association(fr.id, target.id, "bug_in", actor="tester")
        # scan finding linked to target (orphans on delete)
        db.conn.execute(
            "INSERT INTO scan_findings (id, file_id, issue_id, first_seen, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("test-finding-1", fr.id, target.id, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        # observation link
        obs = db.create_observation("a smell", file_path="src/x.py")
        db.link_observation_to_issue(obs["id"], target.id, disposition="evidence", actor="tester")
        # entity association (ON DELETE CASCADE)
        db.add_entity_association(target.id, "py:func:foo", "hash1", actor="tester")
        # annotation + annotation link (target_type='issue', non-FK polymorphic).
        # Inserted directly to avoid annotate_file's on-disk file requirement.
        db.conn.execute(
            "INSERT INTO annotations (id, file_path, note, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("test-ann-1", "src/x.py", "note", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        db.conn.execute(
            "INSERT INTO annotation_links (id, annotation_id, target_type, target_id, relationship, created_at) "
            "VALUES (?, ?, 'issue', ?, 'relevant_to', ?)",
            ("test-annlink-1", "test-ann-1", target.id, "2026-01-01T00:00:00+00:00"),
        )
        # closeout ack (non-FK)
        db.conn.execute(
            "INSERT INTO annotation_closeout_acknowledgements "
            "(annotation_id, target_type, target_id, acknowledged_at) VALUES (?, 'issue', ?, ?)",
            ("test-ann-1", target.id, "2026-01-01T00:00:00+00:00"),
        )
        db.conn.commit()

        # Sanity: everything present.
        assert _count(db, "SELECT COUNT(*) FROM events WHERE issue_id = ?", target.id) > 0
        assert _count(db, "SELECT COUNT(*) FROM labels WHERE issue_id = ?", target.id) == 2

        _terminal(db, target.id)
        result = db.delete_issue(target.id, force=True)

        # Issue and every dependent relation gone.
        with pytest.raises(KeyError):
            db.get_issue(target.id)
        assert _count(db, "SELECT COUNT(*) FROM events WHERE issue_id = ?", target.id) == 0
        assert _count(db, "SELECT COUNT(*) FROM comments WHERE issue_id = ?", target.id) == 0
        assert _count(db, "SELECT COUNT(*) FROM labels WHERE issue_id = ?", target.id) == 0
        assert _count(db, "SELECT COUNT(*) FROM dependencies WHERE issue_id = ?", target.id) == 0
        assert _count(db, "SELECT COUNT(*) FROM dependencies WHERE depends_on_id = ?", target.id) == 0
        assert _count(db, "SELECT COUNT(*) FROM file_associations WHERE issue_id = ?", target.id) == 0
        assert _count(db, "SELECT COUNT(*) FROM observation_links WHERE issue_id = ?", target.id) == 0
        assert _count(db, "SELECT COUNT(*) FROM entity_associations WHERE issue_id = ?", target.id) == 0
        assert _count(db, "SELECT COUNT(*) FROM annotation_links WHERE target_type='issue' AND target_id = ?", target.id) == 0
        assert (
            _count(
                db,
                "SELECT COUNT(*) FROM annotation_closeout_acknowledgements WHERE target_type='issue' AND target_id = ?",
                target.id,
            )
            == 0
        )
        # scan finding orphaned (ON DELETE SET NULL), not deleted.
        finding = db.conn.execute("SELECT issue_id FROM scan_findings WHERE id = ?", ("test-finding-1",)).fetchone()
        assert finding is not None
        assert finding["issue_id"] is None

        # Result envelope counts.
        assert result["status"] == "deleted"
        assert result["deleted_comments"] >= 1
        assert result["deleted_labels"] == 2
        assert result["deleted_dependencies_out"] == 1
        assert result["deleted_dependencies_in"] == 1
        assert result["deleted_file_associations"] == 1
        assert result["deleted_observation_links"] == 1
        assert result["deleted_entity_associations"] == 1
        assert result["deleted_annotation_links"] == 1
        assert result["deleted_annotation_closeout_acks"] == 1
        assert result["orphaned_findings"] == 1
        assert result["deleted_events"] > 0

    def test_provenance_breadcrumbs_left_intact(self, db: FiligreeDB) -> None:
        """observations.source_issue_id / observation_links.source_issue_id are text
        breadcrumbs with no FK — intentionally NOT cleaned on delete."""
        target = db.create_issue("source", type="task")
        # An observation whose source_issue_id points at the deleted issue.
        db.conn.execute(
            "INSERT INTO observations (id, summary, source_issue_id, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            ("test-obs-prov", "from deleted", target.id, "2026-01-01T00:00:00+00:00", "2026-12-01T00:00:00+00:00"),
        )
        db.conn.commit()
        _terminal(db, target.id)
        db.delete_issue(target.id, force=True)
        row = db.conn.execute("SELECT source_issue_id FROM observations WHERE id = ?", ("test-obs-prov",)).fetchone()
        assert row is not None
        assert row["source_issue_id"] == target.id


class TestDeleteIssueForceBehaviors:
    def test_children_orphan_to_roots(self, db: FiligreeDB) -> None:
        parent = db.create_issue("parent", type="task")
        c1 = db.create_issue("c1", type="task", parent_id=parent.id)
        c2 = db.create_issue("c2", type="task", parent_id=parent.id)
        _terminal(db, parent.id)
        result = db.delete_issue(parent.id, force=True)
        assert result["orphaned_children"] == 2
        assert db.get_issue(c1.id).parent_id is None
        assert db.get_issue(c2.id).parent_id is None

    def test_inbound_deps_cascade_and_unblock(self, db: FiligreeDB) -> None:
        blocker = db.create_issue("blocker", type="task")
        dependent = db.create_issue("dependent", type="task")
        db.add_dependency(dependent.id, blocker.id)
        # dependent is blocked while blocker is open.
        assert _count(db, "SELECT COUNT(*) FROM dependencies WHERE depends_on_id = ?", blocker.id) == 1
        _terminal(db, blocker.id)
        result = db.delete_issue(blocker.id, force=True)
        assert result["deleted_dependencies_in"] == 1
        # dependent's blocking edge is gone -> silently unblocked.
        assert _count(db, "SELECT COUNT(*) FROM dependencies WHERE depends_on_id = ?", blocker.id) == 0
        assert db.get_issue(dependent.id).id == dependent.id


class TestDeleteIssueTombstone:
    def test_tombstone_written(self, db: FiligreeDB) -> None:
        i = db.create_issue("doomed", type="bug")
        _terminal(db, i.id)
        db.delete_issue(i.id, actor="alice")
        row = db.conn.execute("SELECT * FROM deleted_issues WHERE issue_id = ?", (i.id,)).fetchone()
        assert row is not None
        assert row["title"] == "doomed"
        assert row["type"] == "bug"
        assert row["deleted_by"] == "alice"
        assert row["deleted_at"]

    def test_tombstone_records_affected_entity_ids(self, db: FiligreeDB) -> None:
        """The tombstone captures the cascaded entity bindings (sorted JSON) so the
        ``issue_deleted`` signal can name them after the rows vanish (filigree-f3bf56554c)."""
        import json

        i = db.create_issue("bound", type="task")
        db.add_entity_association(i.id, "py:func:beta", "h1", actor="alice")
        db.add_entity_association(i.id, "py:func:alpha", "h2", actor="alice")
        _terminal(db, i.id)
        db.delete_issue(i.id, actor="alice")
        row = db.conn.execute("SELECT entity_ids FROM deleted_issues WHERE issue_id = ?", (i.id,)).fetchone()
        assert json.loads(row["entity_ids"]) == ["py:func:alpha", "py:func:beta"]

    def test_tombstone_entity_ids_empty_without_bindings(self, db: FiligreeDB) -> None:
        import json

        i = db.create_issue("unbound", type="task")
        _terminal(db, i.id)
        db.delete_issue(i.id, actor="alice")
        row = db.conn.execute("SELECT entity_ids FROM deleted_issues WHERE issue_id = ?", (i.id,)).fetchone()
        assert json.loads(row["entity_ids"]) == []

    def test_terminal_simple_delete_no_force_needed(self, db: FiligreeDB) -> None:
        i = db.create_issue("simple", type="task")
        _terminal(db, i.id)
        result = db.delete_issue(i.id)
        assert result["status"] == "deleted"
        with pytest.raises(KeyError):
            db.get_issue(i.id)
