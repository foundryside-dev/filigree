"""Tests for core dependency operations — add, remove, cycle detection, critical path."""

from __future__ import annotations

import sqlite3

import pytest

from filigree.core import FiligreeDB


class TestDependencies:
    def test_add_dependency(self, db: FiligreeDB) -> None:
        a = db.create_issue("Blocked task")
        b = db.create_issue("Blocker task")
        db.add_dependency(a.id, b.id)
        refreshed = db.get_issue(a.id)
        assert b.id in refreshed.blocked_by

    def test_self_dependency_rejected(self, db: FiligreeDB) -> None:
        a = db.create_issue("Self")
        with pytest.raises(ValueError, match="self-dependency"):
            db.add_dependency(a.id, a.id)

    def test_cycle_detection(self, db: FiligreeDB) -> None:
        a = db.create_issue("A")
        b = db.create_issue("B")
        db.add_dependency(a.id, b.id)  # A depends on B
        with pytest.raises(ValueError, match="cycle"):
            db.add_dependency(b.id, a.id)  # B depends on A would cycle

    def test_ready_excludes_blocked(self, db: FiligreeDB) -> None:
        a = db.create_issue("Blocked")
        b = db.create_issue("Blocker")
        db.add_dependency(a.id, b.id)
        ready = db.get_ready()
        ready_ids = [i.id for i in ready]
        assert a.id not in ready_ids
        assert b.id in ready_ids

    def test_closing_blocker_unblocks(self, db: FiligreeDB) -> None:
        a = db.create_issue("Blocked")
        b = db.create_issue("Blocker")
        db.add_dependency(a.id, b.id)
        db.close_issue(b.id)
        ready = db.get_ready()
        ready_ids = [i.id for i in ready]
        assert a.id in ready_ids


class TestCycleDetection:
    def test_long_chain_cycle(self, db: FiligreeDB) -> None:
        """A→B→C→D, then D→A should be rejected."""
        a = db.create_issue("A")
        b = db.create_issue("B")
        c = db.create_issue("C")
        d = db.create_issue("D")
        db.add_dependency(a.id, b.id)  # A depends on B
        db.add_dependency(b.id, c.id)  # B depends on C
        db.add_dependency(c.id, d.id)  # C depends on D
        with pytest.raises(ValueError, match="cycle"):
            db.add_dependency(d.id, a.id)  # D depends on A → cycle

    def test_no_false_positive_on_diamond(self, db: FiligreeDB) -> None:
        """Diamond shape (A→B, A→C, B→D, C→D) is valid — no cycle."""
        a = db.create_issue("A")
        b = db.create_issue("B")
        c = db.create_issue("C")
        d = db.create_issue("D")
        db.add_dependency(a.id, b.id)
        db.add_dependency(a.id, c.id)
        db.add_dependency(b.id, d.id)
        db.add_dependency(c.id, d.id)  # Should not raise
        # Verify all 4 deps persisted (diamond is legal)
        dep_count = db.conn.execute("SELECT COUNT(*) FROM dependencies").fetchone()[0]
        assert dep_count == 4


class TestDependencyOperations:
    def test_remove_dependency(self, db: FiligreeDB) -> None:
        a = db.create_issue("A")
        b = db.create_issue("B")
        db.add_dependency(a.id, b.id)
        db.remove_dependency(a.id, b.id)
        refreshed = db.get_issue(a.id)
        assert b.id not in refreshed.blocked_by

    def test_get_blocked(self, db: FiligreeDB) -> None:
        a = db.create_issue("Blocked")
        b = db.create_issue("Blocker")
        db.add_dependency(a.id, b.id)
        blocked = db.get_blocked()
        assert any(i.id == a.id for i in blocked)

    def test_get_all_dependencies(self, db: FiligreeDB) -> None:
        a = db.create_issue("A")
        b = db.create_issue("B")
        db.add_dependency(a.id, b.id)
        all_deps = db.get_all_dependencies()
        assert len(all_deps) == 1
        assert all_deps[0]["from"] == a.id
        assert all_deps[0]["to"] == b.id


class TestCriticalPath:
    def test_linear_chain(self, db: FiligreeDB) -> None:
        """A→B→C should produce a path of length 3."""
        a = db.create_issue("A")
        b = db.create_issue("B")
        c = db.create_issue("C")
        db.add_dependency(a.id, b.id)  # A depends on B
        db.add_dependency(b.id, c.id)  # B depends on C
        path = db.get_critical_path()
        assert len(path) == 3
        # Path should be C→B→A (root blocker to final blocked)
        assert path[0]["id"] == c.id
        assert path[-1]["id"] == a.id

    def test_no_deps(self, db: FiligreeDB) -> None:
        """No dependency chains → empty path."""
        db.create_issue("Standalone 1")
        db.create_issue("Standalone 2")
        path = db.get_critical_path()
        assert path == []

    def test_ignores_closed(self, db: FiligreeDB) -> None:
        """Closed issues should not appear in critical path."""
        a = db.create_issue("A")
        b = db.create_issue("B")
        c = db.create_issue("C")
        db.add_dependency(a.id, b.id)
        db.add_dependency(b.id, c.id)
        db.close_issue(c.id)
        path = db.get_critical_path()
        # With C closed, only A→B remains (length 2)
        assert len(path) == 2

    def test_empty_db(self, db: FiligreeDB) -> None:
        path = db.get_critical_path()
        assert path == []

    def test_selects_longest_chain(self, db: FiligreeDB) -> None:
        """When there are multiple chains, return the longest."""
        # Chain 1: A→B (length 2)
        a = db.create_issue("A")
        b = db.create_issue("B")
        db.add_dependency(a.id, b.id)
        # Chain 2: C→D→E (length 3)
        c = db.create_issue("C")
        d = db.create_issue("D")
        e = db.create_issue("E")
        db.add_dependency(c.id, d.id)
        db.add_dependency(d.id, e.id)
        path = db.get_critical_path()
        assert len(path) == 3


class TestInvalidDepValidation:
    """Bug fix: filigree-1acc4b — create_issue dep FK crash."""

    def test_nonexistent_dep_raises_valueerror(self, db: FiligreeDB) -> None:
        """Creating an issue with deps referencing nonexistent IDs raises ValueError."""
        with pytest.raises(ValueError, match="Invalid dependency IDs"):
            db.create_issue("Bad deps", deps=["test-missing"])

    def test_nonexistent_dep_not_integrity_error(self, db: FiligreeDB) -> None:
        """The error should be ValueError, not sqlite3.IntegrityError."""
        with pytest.raises(ValueError, match="Invalid dependency IDs"):
            db.create_issue("Bad deps 2", deps=["test-abc123"])
        # Explicitly ensure it's not an IntegrityError
        try:
            db.create_issue("Bad deps 3", deps=["test-xyz789"])
        except ValueError:
            pass  # Expected
        except sqlite3.IntegrityError:
            pytest.fail("Should raise ValueError, not IntegrityError")

    def test_valid_dep_succeeds(self, db: FiligreeDB) -> None:
        """Creating an issue with valid dep IDs should work."""
        dep_issue = db.create_issue("Dep target")
        issue = db.create_issue("Has deps", deps=[dep_issue.id])
        assert dep_issue.id in issue.blocked_by


class TestCycleDetectionDepthLimit:
    """Bug fix: filigree-832676c507 — BFS has no depth limit."""

    def test_cycle_detection_respects_depth_limit(self, db: FiligreeDB) -> None:
        """BFS should not traverse beyond _MAX_TREE_DEPTH nodes."""
        # Build a chain longer than the depth limit: N0→N1→N2→...→N(depth+2)
        from filigree.db_planning import _MAX_TREE_DEPTH

        chain_len = _MAX_TREE_DEPTH + 3
        nodes = [db.create_issue(f"Node-{i}") for i in range(chain_len)]
        for i in range(chain_len - 1):
            db.add_dependency(nodes[i].id, nodes[i + 1].id)

        # Trying to add a dependency from the last node back to the first
        # would create a cycle, but the BFS should stop at depth limit
        # and raise ValueError (for depth exceeded), not silently miss the cycle
        with pytest.raises(ValueError, match="cycle"):
            db.add_dependency(nodes[-1].id, nodes[0].id)

    def test_normal_depth_cycle_still_detected(self, db: FiligreeDB) -> None:
        """Cycles within the depth limit should still be caught."""
        nodes = [db.create_issue(f"N-{i}") for i in range(5)]
        for i in range(4):
            db.add_dependency(nodes[i].id, nodes[i + 1].id)
        with pytest.raises(ValueError, match="cycle"):
            db.add_dependency(nodes[4].id, nodes[0].id)


class TestClosedDepFiltering:
    """Bug fix: keel-326c2f — Dep persists after close."""

    def test_closed_blocker_not_in_blocked_by(self, db: FiligreeDB) -> None:
        """After closing B, get_issue(A) where A depends-on B should not show B in blocked_by."""
        a = db.create_issue("Issue A")
        b = db.create_issue("Issue B")
        db.add_dependency(a.id, b.id)

        # Before closing: B should be in A's blocked_by
        a_before = db.get_issue(a.id)
        assert b.id in a_before.blocked_by

        # Close B
        db.close_issue(b.id)

        # After closing: B should NOT be in A's blocked_by
        a_after = db.get_issue(a.id)
        assert b.id not in a_after.blocked_by

    def test_closed_blocker_still_in_blocks(self, db: FiligreeDB) -> None:
        """The blocks list on B should still show A (for audit trail)."""
        a = db.create_issue("Issue A")
        b = db.create_issue("Issue B")
        db.add_dependency(a.id, b.id)
        db.close_issue(b.id)

        b_after = db.get_issue(b.id)
        assert a.id in b_after.blocks

    def test_a_becomes_ready_after_blocker_closed(self, db: FiligreeDB) -> None:
        """After closing the only blocker, the blocked issue should become ready."""
        a = db.create_issue("Issue A")
        b = db.create_issue("Issue B")
        db.add_dependency(a.id, b.id)

        a_blocked = db.get_issue(a.id)
        assert not a_blocked.is_ready

        db.close_issue(b.id)
        a_ready = db.get_issue(a.id)
        assert a_ready.is_ready


# ── M13: dependency edge cases ─────────────────────────────────────────


class TestDependencyEdgeCases:
    """Cover untested dependency edge cases (M13)."""

    def test_add_dependency_custom_dep_type(self, db: FiligreeDB) -> None:
        """Non-default dep_type is stored and returned correctly."""
        a = db.create_issue("A")
        b = db.create_issue("B")
        result = db.add_dependency(a.id, b.id, dep_type="requires")
        assert result is True

        # Verify dep_type stored in DB
        row = db.conn.execute(
            "SELECT type FROM dependencies WHERE issue_id = ? AND depends_on_id = ?",
            (a.id, b.id),
        ).fetchone()
        assert row["type"] == "requires"

    def test_add_dependency_duplicate_returns_false(self, db: FiligreeDB) -> None:
        """Adding the same dependency twice returns False (idempotent)."""
        a = db.create_issue("A")
        b = db.create_issue("B")
        assert db.add_dependency(a.id, b.id) is True
        assert db.add_dependency(a.id, b.id) is False

    def test_add_dependency_duplicate_records_no_event(self, db: FiligreeDB) -> None:
        """Duplicate add_dependency should not record a second event."""
        a = db.create_issue("A")
        b = db.create_issue("B")
        db.add_dependency(a.id, b.id)
        events_before = db.conn.execute(
            "SELECT COUNT(*) FROM events WHERE issue_id = ? AND event_type = 'dependency_added'",
            (a.id,),
        ).fetchone()[0]
        db.add_dependency(a.id, b.id)  # duplicate
        events_after = db.conn.execute(
            "SELECT COUNT(*) FROM events WHERE issue_id = ? AND event_type = 'dependency_added'",
            (a.id,),
        ).fetchone()[0]
        assert events_after == events_before

    def test_remove_dependency_nonexistent_returns_false(self, db: FiligreeDB) -> None:
        """Removing a dependency that doesn't exist returns False."""
        a = db.create_issue("A")
        b = db.create_issue("B")
        assert db.remove_dependency(a.id, b.id) is False

    @pytest.mark.parametrize("missing_side", ["from", "to"])
    def test_remove_dependency_missing_issue_raises_keyerror(self, db: FiligreeDB, missing_side: str) -> None:
        """Missing issue IDs are not the same as a missing dependency edge."""
        existing = db.create_issue("Existing")
        missing_id = f"{db.prefix}-0000000000"
        from_id = missing_id if missing_side == "from" else existing.id
        to_id = existing.id if missing_side == "from" else missing_id

        with pytest.raises(KeyError, match=missing_id):
            db.remove_dependency(from_id, to_id)

    def test_remove_dependency_nonexistent_records_no_event(self, db: FiligreeDB) -> None:
        """Removing nonexistent dependency should not record an event."""
        a = db.create_issue("A")
        b = db.create_issue("B")
        events_before = db.conn.execute(
            "SELECT COUNT(*) FROM events WHERE issue_id = ? AND event_type = 'dependency_removed'",
            (a.id,),
        ).fetchone()[0]
        db.remove_dependency(a.id, b.id)
        events_after = db.conn.execute(
            "SELECT COUNT(*) FROM events WHERE issue_id = ? AND event_type = 'dependency_removed'",
            (a.id,),
        ).fetchone()[0]
        assert events_after == events_before

    def test_remove_dependency_rolls_back_on_event_failure(self, db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
        """If _record_event fails, the DELETE must also be rolled back."""
        a = db.create_issue("A")
        b = db.create_issue("B")
        db.add_dependency(a.id, b.id)

        def _boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError("event recording failed")

        monkeypatch.setattr(db, "_record_event", _boom)
        with pytest.raises(RuntimeError, match="event recording failed"):
            db.remove_dependency(a.id, b.id)

        # Dependency must still exist — DELETE was rolled back
        deps = db.get_all_dependencies()
        assert any(d["from"] == a.id and d["to"] == b.id for d in deps)

    def test_add_dependency_duplicate_does_not_leak_transaction(self, db: FiligreeDB, tmp_path) -> None:
        """Bug filigree-a0fc2b4ecc: idempotent path skipped commit/rollback.

        ``INSERT OR IGNORE`` opens an implicit write transaction even when no
        row changes. Without an explicit rollback, the lock lingers and any
        other connection's write fails with ``database is locked``.
        """
        a = db.create_issue("A")
        b = db.create_issue("B")
        db.add_dependency(a.id, b.id)
        assert db.add_dependency(a.id, b.id) is False
        assert db.conn.in_transaction is False

        # A second connection on the same DB file must be able to write.
        second = sqlite3.connect(str(tmp_path / "filigree.db"), timeout=0.5)
        try:
            second.execute("BEGIN IMMEDIATE")
            second.commit()
        finally:
            second.close()
