"""Tests for the undo_last mechanism."""

from __future__ import annotations

import pytest

from filigree.core import FiligreeDB


class TestUndoStatus:
    """Undo status_changed events."""

    def test_undo_status_change(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Test")
        db.update_issue(issue.id, status="in_progress", actor="t")
        result = db.undo_last(issue.id, actor="t")
        assert result["undone"] is True
        assert result["event_type"] == "status_changed"
        assert result["issue"]["status"] == "open"

    def test_undo_close_clears_closed_at(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Test")
        db.close_issue(issue.id, actor="t")
        closed = db.get_issue(issue.id)
        assert closed.closed_at is not None

        result = db.undo_last(issue.id, actor="t")
        assert result["undone"] is True
        assert result["issue"]["closed_at"] is None


class TestUndoTitle:
    def test_undo_title_change(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Original title")
        db.update_issue(issue.id, title="Changed title", actor="t")
        result = db.undo_last(issue.id, actor="t")
        assert result["undone"] is True
        assert result["issue"]["title"] == "Original title"


class TestUndoPriority:
    def test_undo_priority_change(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Test", priority=2)
        db.update_issue(issue.id, priority=0, actor="t")
        result = db.undo_last(issue.id, actor="t")
        assert result["undone"] is True
        assert result["issue"]["priority"] == 2


class TestUndoAssignee:
    def test_undo_assignee_change(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Test")
        db.update_issue(issue.id, assignee="alice", actor="t")
        result = db.undo_last(issue.id, actor="t")
        assert result["undone"] is True
        assert result["issue"]["assignee"] == ""


class TestUndoClaim:
    def test_undo_claim(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Test")
        db.claim_issue(issue.id, assignee="alice", actor="t")
        claimed = db.get_issue(issue.id)
        assert claimed.status == "open"  # claim does not change status
        assert claimed.assignee == "alice"

        result = db.undo_last(issue.id, actor="t")
        assert result["undone"] is True
        assert result["event_type"] == "claimed"
        assert result["issue"]["status"] == "open"  # status still unchanged
        assert result["issue"]["assignee"] == ""

    def test_undo_claim_clears_assignee_only(self, db: FiligreeDB) -> None:
        """Undo claim just clears assignee — status was never changed by claim."""
        bug = db.create_issue("Bug", type="bug")
        assert bug.status == "triage"
        # Move to confirmed (still open-category, but not initial)
        db.update_issue(bug.id, status="confirmed", actor="t")
        confirmed = db.get_issue(bug.id)
        assert confirmed.status == "confirmed"

        db.claim_issue(bug.id, assignee="alice", actor="t")
        claimed = db.get_issue(bug.id)
        assert claimed.status == "confirmed"  # claim does not change status

        result = db.undo_last(bug.id, actor="t")
        assert result["undone"] is True
        assert result["event_type"] == "claimed"
        # Status stays at "confirmed" — claim never changed it
        assert result["issue"]["status"] == "confirmed"
        assert result["issue"]["assignee"] == ""

    def test_undo_claim_restores_prior_assignee(self, db: FiligreeDB) -> None:
        """Bug filigree-a8e7cf: undo claim must restore prior assignee, not blank."""
        issue = db.create_issue("Test")
        # Alice claims first
        db.claim_issue(issue.id, assignee="alice", actor="t")
        assert db.get_issue(issue.id).assignee == "alice"

        # Release and let bob claim (alice's claim is released, bob claims fresh)
        db.release_claim(issue.id, actor="t")
        db.claim_issue(issue.id, assignee="bob", actor="t")
        assert db.get_issue(issue.id).assignee == "bob"

        # Undo bob's claim — should restore to "" (what was there before bob claimed)
        result = db.undo_last(issue.id, actor="t")
        assert result["undone"] is True
        assert result["event_type"] == "claimed"
        assert result["issue"]["assignee"] == ""


class TestUndoDependency:
    def test_undo_dependency_added(self, db: FiligreeDB) -> None:
        a = db.create_issue("A")
        b = db.create_issue("B")
        db.add_dependency(a.id, b.id, actor="t")

        # a is blocked by b
        a_before = db.get_issue(a.id)
        assert b.id in a_before.blocked_by

        result = db.undo_last(a.id, actor="t")
        assert result["undone"] is True
        assert result["event_type"] == "dependency_added"

        a_after = db.get_issue(a.id)
        assert b.id not in a_after.blocked_by

    def test_undo_dependency_removed(self, db: FiligreeDB) -> None:
        a = db.create_issue("A")
        b = db.create_issue("B")
        db.add_dependency(a.id, b.id, actor="t")
        db.remove_dependency(a.id, b.id, actor="t")

        a_before = db.get_issue(a.id)
        assert b.id not in a_before.blocked_by

        result = db.undo_last(a.id, actor="t")
        assert result["undone"] is True
        assert result["event_type"] == "dependency_removed"

        a_after = db.get_issue(a.id)
        assert b.id in a_after.blocked_by


class TestUndoDescription:
    def test_undo_description_change(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Test", description="original")
        db.update_issue(issue.id, description="changed", actor="t")
        result = db.undo_last(issue.id, actor="t")
        assert result["undone"] is True
        assert result["event_type"] == "description_changed"
        assert result["issue"]["description"] == "original"


class TestUndoNotes:
    def test_undo_notes_change(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Test", notes="original")
        db.update_issue(issue.id, notes="changed", actor="t")
        result = db.undo_last(issue.id, actor="t")
        assert result["undone"] is True
        assert result["event_type"] == "notes_changed"
        assert result["issue"]["notes"] == "original"


class TestUndoEdgeCases:
    def test_undo_created_only_fails(self, db: FiligreeDB) -> None:
        """Issue with only a 'created' event has no reversible events."""
        issue = db.create_issue("Test")
        result = db.undo_last(issue.id, actor="t")
        assert result["undone"] is False
        assert "No reversible events" in result["reason"]

    def test_undo_skips_transition_warning(self, db: FiligreeDB) -> None:
        """transition_warning events should be skipped when finding last event."""
        issue = db.create_issue("Test", type="bug")
        # Move through triage -> confirmed without severity (generates warning)
        db.update_issue(issue.id, status="confirmed", actor="t")

        # The most recent non-skip event should be status_changed, not transition_warning
        result = db.undo_last(issue.id, actor="t")
        assert result["undone"] is True
        assert result["event_type"] == "status_changed"

    def test_double_undo_returns_no_reversible_events(self, db: FiligreeDB) -> None:
        """Cannot undo the same reversible event twice. Per filigree-a849860f2e
        the candidate-selection query filters out already-undone events, so
        when there is no earlier reversible event to fall back to the result
        is "no reversible events" rather than "already undone"."""
        issue = db.create_issue("Test")
        db.update_issue(issue.id, status="in_progress", actor="t")
        db.undo_last(issue.id, actor="t")
        result = db.undo_last(issue.id, actor="t")
        assert result["undone"] is False
        assert "No reversible events" in result["reason"]

    def test_undo_reaches_past_non_reversible_events(self, db: FiligreeDB) -> None:
        """Undo should skip non-reversible events and find earlier reversible ones."""
        issue = db.create_issue("Test")
        db.update_issue(issue.id, status="in_progress", actor="t")
        # Record a non-reversible event (simulate by inserting directly)
        db.conn.execute(
            "INSERT INTO events (issue_id, event_type, actor, created_at) VALUES (?, ?, ?, ?)",
            (issue.id, "released", "system", "2099-01-01T00:00:00+00:00"),
        )
        db.conn.commit()
        # Undo should skip 'released' and find 'status_changed'
        result = db.undo_last(issue.id, actor="t")
        assert result["undone"] is True
        assert result["event_type"] == "status_changed"
        assert result["issue"]["status"] == "open"

    def test_undo_nonexistent_issue_raises(self, db: FiligreeDB) -> None:
        with pytest.raises(KeyError):
            db.undo_last("nonexistent-abc123")

    def test_undone_event_recorded(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Test")
        db.update_issue(issue.id, status="in_progress", actor="t")
        db.undo_last(issue.id, actor="undoer")

        events = db.get_issue_events(issue.id)
        undone_events = [e for e in events if e["event_type"] == "undone"]
        assert len(undone_events) == 1
        assert undone_events[0]["actor"] == "undoer"
        assert undone_events[0]["old_value"] == "status_changed"


class TestGetIssueEvents:
    def test_returns_events_newest_first(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Test")
        db.update_issue(issue.id, title="Changed")
        events = db.get_issue_events(issue.id)
        assert len(events) >= 2
        assert events[0]["event_type"] == "title_changed"
        assert events[1]["event_type"] == "created"

    def test_respects_limit(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Test")
        db.update_issue(issue.id, title="T2")
        db.update_issue(issue.id, title="T3")
        events = db.get_issue_events(issue.id, limit=1)
        assert len(events) == 1

    def test_raises_on_nonexistent(self, db: FiligreeDB) -> None:
        with pytest.raises(KeyError):
            db.get_issue_events("nonexistent-abc123")


class TestUndoRollback:
    """H1: undo_last must rollback on exception, not leave partial writes."""

    def test_undo_rolls_back_on_record_event_failure(self, db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
        """If _record_event raises, the status change should be rolled back."""
        issue = db.create_issue("Test")
        db.update_issue(issue.id, status="in_progress", actor="t")

        original = db._record_event

        def failing_record_event(*args: object, **kwargs: object) -> None:
            if kwargs.get("old_value") == "status_changed" or (args and len(args) >= 2 and args[1] == "undone"):
                raise RuntimeError("Simulated _record_event failure")
            original(*args, **kwargs)

        monkeypatch.setattr(db, "_record_event", failing_record_event)

        with pytest.raises(RuntimeError, match="Simulated"):
            db.undo_last(issue.id, actor="t")

        # The status should NOT have been changed — rollback should have reverted it
        after = db.get_issue(issue.id)
        assert after.status == "in_progress", "Status should be unchanged after failed undo (rollback)"


class TestUndoNullGuards:
    """H2: undo_last must handle NULL old_value/new_value gracefully."""

    def test_undo_status_changed_null_old_value(self, db: FiligreeDB) -> None:
        """status_changed with NULL old_value should return undone=False, not crash."""
        issue = db.create_issue("Test")
        # Inject a status_changed event with NULL old_value directly
        db.conn.execute(
            "INSERT INTO events (issue_id, event_type, actor, old_value, new_value, comment, created_at) "
            "VALUES (?, 'status_changed', 'test', NULL, 'in_progress', '', '2099-01-01T00:00:00+00:00')",
            (issue.id,),
        )
        db.conn.commit()

        result = db.undo_last(issue.id)
        assert result["undone"] is False
        assert "no old_value" in result["reason"].lower()

    def test_undo_title_changed_null_old_value(self, db: FiligreeDB) -> None:
        """title_changed with NULL old_value should return undone=False, not crash."""
        issue = db.create_issue("Test")
        db.conn.execute(
            "INSERT INTO events (issue_id, event_type, actor, old_value, new_value, comment, created_at) "
            "VALUES (?, 'title_changed', 'test', NULL, 'New Title', '', '2099-01-01T00:00:00+00:00')",
            (issue.id,),
        )
        db.conn.commit()

        result = db.undo_last(issue.id)
        assert result["undone"] is False
        assert "no old_value" in result["reason"].lower()

    def test_undo_priority_changed_corrupt_old_value(self, db: FiligreeDB) -> None:
        """priority_changed with corrupt old_value should return undone=False, not crash."""
        issue = db.create_issue("Test")
        db.conn.execute(
            "INSERT INTO events (issue_id, event_type, actor, old_value, new_value, comment, created_at) "
            "VALUES (?, 'priority_changed', 'test', 'not-a-number', '1', '', '2099-01-01T00:00:00+00:00')",
            (issue.id,),
        )
        db.conn.commit()

        result = db.undo_last(issue.id)
        assert result["undone"] is False
        assert "not a valid priority" in result["reason"].lower()

    def test_undo_priority_changed_null_old_value(self, db: FiligreeDB) -> None:
        """priority_changed with NULL old_value should return undone=False, not crash."""
        issue = db.create_issue("Test")
        db.conn.execute(
            "INSERT INTO events (issue_id, event_type, actor, old_value, new_value, comment, created_at) "
            "VALUES (?, 'priority_changed', 'test', NULL, '1', '', '2099-01-01T00:00:00+00:00')",
            (issue.id,),
        )
        db.conn.commit()

        result = db.undo_last(issue.id)
        assert result["undone"] is False
        assert "no old_value" in result["reason"].lower()

    def test_undo_dependency_removed_null_old_value(self, db: FiligreeDB) -> None:
        """dependency_removed with NULL old_value should return undone=False, not silently no-op."""
        issue = db.create_issue("Test")
        # Inject a dependency_removed event with NULL old_value
        db.conn.execute(
            "INSERT INTO events (issue_id, event_type, actor, old_value, new_value, comment, created_at) "
            "VALUES (?, 'dependency_removed', 'test', NULL, NULL, '', '2099-01-01T00:00:00+00:00')",
            (issue.id,),
        )
        db.conn.commit()

        result = db.undo_last(issue.id)
        assert result["undone"] is False
        assert "no old_value" in result["reason"].lower()


class TestUndoCloseConsistency:
    """Bug fix: keel-3e899d — undo_last closed_at consistency."""

    def test_undo_close_clears_closed_at(self, db: FiligreeDB) -> None:
        """Closing an issue then undoing should clear closed_at."""
        issue = db.create_issue("Undo close test")
        db.close_issue(issue.id)

        # Verify closed_at is set
        closed = db.get_issue(issue.id)
        assert closed.closed_at is not None

        # Undo the close
        result = db.undo_last(issue.id)
        assert result["undone"] is True

        # closed_at should be cleared
        undone = db.get_issue(issue.id)
        assert undone.closed_at is None
        assert undone.status_category != "done"

    def test_undo_to_done_sets_closed_at(self, db: FiligreeDB) -> None:
        """Undoing from a non-done state back to a done state should set closed_at."""
        issue = db.create_issue("Undo to done test")

        # Close the issue (status -> closed, closed_at set)
        db.close_issue(issue.id)
        closed = db.get_issue(issue.id)
        assert closed.closed_at is not None

        # Reopen the issue (status -> open, closed_at cleared)
        db.reopen_issue(issue.id)
        reopened = db.get_issue(issue.id)
        assert reopened.closed_at is None

        # Now undo the reopen — this should restore the closed status
        # The most recent reversible event should be the status_changed from reopen
        result = db.undo_last(issue.id)
        assert result["undone"] is True

        # closed_at should be set because we're back in a done state
        restored = db.get_issue(issue.id)
        assert restored.status_category == "done"
        assert restored.closed_at is not None

    def test_undo_close_restores_correct_status(self, db: FiligreeDB) -> None:
        """Test that undoing a close restores the previous status and clears closed_at."""
        issue = db.create_issue("Undo close status test")

        # Move to in_progress first
        db.update_issue(issue.id, status="in_progress")

        # Close
        db.close_issue(issue.id)
        closed = db.get_issue(issue.id)
        assert closed.closed_at is not None
        assert closed.status_category == "done"

        # Undo close -> should restore to in_progress
        result = db.undo_last(issue.id)
        assert result["undone"] is True
        after_undo = db.get_issue(issue.id)
        assert after_undo.status == "in_progress"
        assert after_undo.closed_at is None
        assert after_undo.status_category != "done"

    def test_undo_close_with_reason_reverts_in_one_call(self, db: FiligreeDB) -> None:
        """Closing with a reason and then undoing should fully revert in ONE
        ``undo_last`` call.

        Regression guard for senior-user MCP review-f finding F2: previously
        ``close_issue(reason=...)`` wrote two events (``status_changed`` for
        the close + ``fields_changed`` for the synthetic ``close_reason``
        field). The first ``undo_last`` would then reverse only the field
        change — returning ``undone: True`` while the issue stayed closed
        — and a second ``undo_last`` was needed to actually reopen.
        """
        issue = db.create_issue("Undo close-with-reason")
        db.close_issue(issue.id, reason="duplicate of foo", actor="t")

        closed = db.get_issue(issue.id)
        assert closed.status_category == "done"
        assert closed.closed_at is not None
        assert closed.fields.get("close_reason") == "duplicate of foo"

        # Single undo must reverse status, closed_at, AND close_reason.
        result = db.undo_last(issue.id, actor="t")
        assert result["undone"] is True
        assert result["event_type"] == "status_changed"

        restored = db.get_issue(issue.id)
        assert restored.status_category != "done", f"still in done category: {restored.status}"
        assert restored.closed_at is None
        assert "close_reason" not in restored.fields, f"close_reason should be cleared when reopening, got fields={restored.fields}"

    def test_close_with_reason_writes_single_event(self, db: FiligreeDB) -> None:
        """``close_issue(reason=...)`` must record exactly one reversible event.

        The reason is preserved in the issue's ``fields`` column AND attached
        as the ``status_changed`` event's ``comment`` for audit. Two events
        would re-introduce the F2 footgun.
        """
        issue = db.create_issue("One-event close")
        db.close_issue(issue.id, reason="dup", actor="t")

        events = db.get_issue_events(issue.id)
        # Filter to events emitted *by the close* — i.e. since the create event.
        close_events = [e for e in events if e["event_type"] in {"status_changed", "fields_changed"}]
        assert len(close_events) == 1, f"expected one composite close event, got {[e['event_type'] for e in close_events]}"
        assert close_events[0]["event_type"] == "status_changed"
        assert close_events[0]["new_value"] == "closed"
        assert close_events[0]["comment"] == "dup", "close reason must travel on the status_changed event's comment"


class TestUndoFields:
    """H4: Field changes must be recorded as events and be undoable."""

    def test_field_change_records_event(self, db: FiligreeDB) -> None:
        """Updating fields should produce a 'fields_changed' event."""
        issue = db.create_issue("Test", fields={"a": "1"})
        db.update_issue(issue.id, fields={"b": "2"}, actor="tester")
        events = db.get_issue_events(issue.id)
        field_events = [e for e in events if e["event_type"] == "fields_changed"]
        assert len(field_events) == 1
        assert field_events[0]["actor"] == "tester"

    def test_field_change_no_event_when_unchanged(self, db: FiligreeDB) -> None:
        """Setting fields to the same values should not record an event."""
        issue = db.create_issue("Test", fields={"a": "1"})
        db.update_issue(issue.id, fields={"a": "1"}, actor="tester")
        events = db.get_issue_events(issue.id)
        field_events = [e for e in events if e["event_type"] == "fields_changed"]
        assert len(field_events) == 0

    def test_undo_field_change(self, db: FiligreeDB) -> None:
        """Undoing a field change should restore the previous fields."""
        issue = db.create_issue("Test", fields={"a": "1", "b": "2"})
        db.update_issue(issue.id, fields={"b": "changed", "c": "3"}, actor="t")
        result = db.undo_last(issue.id, actor="t")
        assert result["undone"] is True
        assert result["event_type"] == "fields_changed"
        restored = db.get_issue(issue.id)
        assert restored.fields == {"a": "1", "b": "2"}

    def test_undo_field_change_from_empty(self, db: FiligreeDB) -> None:
        """Undoing fields added to an issue that started with no fields."""
        issue = db.create_issue("Test")
        db.update_issue(issue.id, fields={"key": "val"}, actor="t")
        result = db.undo_last(issue.id, actor="t")
        assert result["undone"] is True
        restored = db.get_issue(issue.id)
        assert restored.fields == {}

    def test_double_undo_same_event_by_event_id(self, db: FiligreeDB) -> None:
        """Two rapid undo calls must not both succeed for the same event.

        Regression: old check used timestamp comparison which was racy.
        New check uses event ID linkage (undone event's new_value = target event ID).
        """
        issue = db.create_issue("Test")
        db.update_issue(issue.id, status="in_progress", actor="t")
        first = db.undo_last(issue.id, actor="t")
        assert first["undone"] is True
        # The undone event's new_value should be the event_id of the status_changed event
        undone_events = db.conn.execute(
            "SELECT new_value FROM events WHERE issue_id = ? AND event_type = 'undone'",
            (issue.id,),
        ).fetchall()
        assert len(undone_events) == 1
        assert undone_events[0]["new_value"] == str(first["event_id"])
        # Second undo must be blocked. Post filigree-a849860f2e, the
        # candidate-selection NOT EXISTS clause filters out the already-undone
        # event, so the reason becomes "No reversible events to undo" rather
        # than "already undone".
        second = db.undo_last(issue.id, actor="t")
        assert second["undone"] is False
        assert "No reversible events" in second["reason"]

    def test_undo_field_change_with_corrupt_json(self, db: FiligreeDB) -> None:
        """Undoing a field change with corrupt stored JSON returns failure, not exception."""
        issue = db.create_issue("Test", fields={"a": "1"})
        db.update_issue(issue.id, fields={"b": "2"}, actor="t")
        # Corrupt the old_value in the fields_changed event
        db.conn.execute(
            "UPDATE events SET old_value = 'not-valid-json{' WHERE issue_id = ? AND event_type = 'fields_changed'",
            (issue.id,),
        )
        db.conn.commit()
        result = db.undo_last(issue.id, actor="t")
        assert result["undone"] is False
        assert "corrupt" in result["reason"].lower()


class TestUndoFallsBackPastAlreadyUndone:
    """filigree-a849860f2e: undo_last must reach older reversible events
    when the newest reversible event is already covered by an 'undone'
    marker. The CLI semantics promise to undo "the most recent reversible
    action", not "the most recent action that has not yet been undone"."""

    def test_double_undo_reaches_earlier_reversible_event(self, db: FiligreeDB) -> None:
        """title-change → priority-change → undo (priority restored) →
        undo again should restore the title."""
        issue = db.create_issue("Original title", priority=2)
        db.update_issue(issue.id, title="Changed title", actor="t")
        db.update_issue(issue.id, priority=0, actor="t")

        first = db.undo_last(issue.id, actor="t")
        assert first["undone"] is True
        assert first["event_type"] == "priority_changed"
        assert db.get_issue(issue.id).priority == 2
        # Title still in its post-change state
        assert db.get_issue(issue.id).title == "Changed title"

        second = db.undo_last(issue.id, actor="t")
        assert second["undone"] is True, f"second undo failed: {second}"
        assert second["event_type"] == "title_changed"
        assert db.get_issue(issue.id).title == "Original title"


class TestUndoParentChanged:
    """filigree-fc6bb28c23: parent_changed must be undoable."""

    def test_undo_parent_set(self, db: FiligreeDB) -> None:
        """Setting a parent then undoing must clear it back to None."""
        parent = db.create_issue("Parent")
        child = db.create_issue("Child")
        db.update_issue(child.id, parent_id=parent.id, actor="t")
        assert db.get_issue(child.id).parent_id == parent.id

        result = db.undo_last(child.id, actor="t")
        assert result["undone"] is True, f"undo failed: {result}"
        assert result["event_type"] == "parent_changed"
        assert db.get_issue(child.id).parent_id is None

    def test_undo_parent_change(self, db: FiligreeDB) -> None:
        """Reparenting to a different parent and undoing must restore the original."""
        parent_a = db.create_issue("Parent A")
        parent_b = db.create_issue("Parent B")
        child = db.create_issue("Child")
        db.update_issue(child.id, parent_id=parent_a.id, actor="t")
        db.update_issue(child.id, parent_id=parent_b.id, actor="t")
        assert db.get_issue(child.id).parent_id == parent_b.id

        result = db.undo_last(child.id, actor="t")
        assert result["undone"] is True
        assert db.get_issue(child.id).parent_id == parent_a.id

    def test_undo_parent_clear(self, db: FiligreeDB) -> None:
        """Clearing a parent and undoing must restore the prior parent."""
        parent = db.create_issue("Parent")
        child = db.create_issue("Child")
        db.update_issue(child.id, parent_id=parent.id, actor="t")
        # Clear by passing empty string (per db_issues update path)
        db.update_issue(child.id, parent_id="", actor="t")
        assert db.get_issue(child.id).parent_id is None

        result = db.undo_last(child.id, actor="t")
        assert result["undone"] is True
        assert db.get_issue(child.id).parent_id == parent.id


class TestUndoParentChangedCycleGuard:
    """filigree-0a8c3d38d7: undo of parent_changed must reject restorations
    that would create a circular parent chain, mirroring update_issue's
    invariant. Without the guard, an interleaved reparent sequence followed
    by undo could produce A->C->A."""

    def test_undo_refuses_to_create_parent_cycle(self, db: FiligreeDB) -> None:
        a = db.create_issue("A")
        b = db.create_issue("B")
        c = db.create_issue("C")
        # C parented under A, then re-parented to B (records old=A, new=B).
        db.update_issue(c.id, parent_id=a.id, actor="t")
        db.update_issue(c.id, parent_id=b.id, actor="t")
        # A is reparented under C (valid: C->B is the chain root).
        db.update_issue(a.id, parent_id=c.id, actor="t")

        # Undoing C's last parent_changed would set C.parent=A, but A->C
        # already exists, so this would create a cycle. Must be refused.
        result = db.undo_last(c.id, actor="t")
        assert result["undone"] is False
        assert "cycle" in result["reason"].lower() or "circular" in result["reason"].lower()
        # Ensure no cycle was actually written.
        assert db.get_issue(c.id).parent_id == b.id
        assert db.get_issue(a.id).parent_id == c.id


class TestUndoFieldsChangedSchemaGuard:
    """filigree-cb4cd68f80: fields_changed undo must reject NULL or non-dict
    JSON in old_value rather than silently writing an empty dict or a list.
    The model invariant is that issues.fields is always a JSON object."""

    def test_undo_refuses_null_old_value(self, db: FiligreeDB) -> None:
        issue = db.create_issue("X", fields={"foo": "bar"})
        # Inject a corrupted fields_changed event with old_value=NULL as the
        # most recent reversible event for this issue.
        db.conn.execute(
            "INSERT INTO events (issue_id, event_type, actor, old_value, new_value, comment, created_at) "
            "VALUES (?, 'fields_changed', 'inject', NULL, ?, '', ?)",
            (issue.id, '{"foo":"baz"}', "2999-01-01T00:00:00+00:00"),
        )
        db.conn.commit()
        result = db.undo_last(issue.id, actor="t")
        assert result["undone"] is False
        # Fields must remain untouched.
        assert db.get_issue(issue.id).fields == {"foo": "bar"}

    def test_undo_refuses_non_dict_old_value(self, db: FiligreeDB) -> None:
        issue = db.create_issue("X", fields={"foo": "bar"})
        db.conn.execute(
            "INSERT INTO events (issue_id, event_type, actor, old_value, new_value, comment, created_at) "
            "VALUES (?, 'fields_changed', 'inject', '[]', ?, '', ?)",
            (issue.id, '{"foo":"baz"}', "2999-01-01T00:00:00+00:00"),
        )
        db.conn.commit()
        result = db.undo_last(issue.id, actor="t")
        assert result["undone"] is False
        row = db.conn.execute("SELECT fields FROM issues WHERE id = ?", (issue.id,)).fetchone()
        # Stored fields column must still be a JSON object, not '[]'.
        assert row["fields"] != "[]"


class TestUndoConcurrentRace:
    """filigree-f38d4e2874: concurrent undo_last() calls on the same issue
    via separate connections must not both write 'undone' markers for the
    same target event. BEGIN IMMEDIATE serializes the read-check-write."""

    def test_concurrent_undo_writes_at_most_one_marker(self, tmp_path) -> None:
        import threading

        db_path = tmp_path / "filigree.db"
        seed = FiligreeDB(db_path, prefix="test", check_same_thread=False)
        seed.initialize()
        issue = seed.create_issue("X")
        seed.update_issue(issue.id, title="Y", actor="t")
        seed.close()

        db1 = FiligreeDB(db_path, prefix="test", check_same_thread=False)
        db1.initialize()
        db2 = FiligreeDB(db_path, prefix="test", check_same_thread=False)
        db2.initialize()

        results: list[dict | None] = [None, None]
        barrier = threading.Barrier(2)

        def worker(idx: int, dbref: FiligreeDB) -> None:
            barrier.wait()
            results[idx] = dbref.undo_last(issue.id, actor=f"a{idx}")

        t1 = threading.Thread(target=worker, args=(0, db1))
        t2 = threading.Thread(target=worker, args=(1, db2))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        successes = [r for r in results if r and r.get("undone")]
        assert len(successes) == 1, f"expected exactly one undo to succeed, got {results}"

        target_id = successes[0]["event_id"]
        markers = db1.conn.execute(
            "SELECT * FROM events WHERE event_type = 'undone' AND new_value = ?",
            (str(target_id),),
        ).fetchall()
        assert len(markers) == 1, f"expected one undone marker, got {len(markers)}"
        db1.close()
        db2.close()


class TestUndoDependencyTypeWithColon:
    """filigree-2cd923c1d8: dep_type containing ':' must round-trip
    correctly through dependency_added / dependency_removed undo. The
    parse used split(':', 1) which fails when dep_type itself has a colon."""

    def test_undo_dependency_added_with_colon_in_type(self, db: FiligreeDB) -> None:
        a = db.create_issue("A")
        b = db.create_issue("B")
        # add_dependency takes dep_type as a kwarg; choose one that contains ':'
        db.add_dependency(a.id, b.id, dep_type="namespaced:type", actor="t")
        assert any(d["from"] == a.id and d["to"] == b.id for d in db.get_all_dependencies())

        result = db.undo_last(a.id, actor="t")
        assert result["undone"] is True
        assert result["event_type"] == "dependency_added"
        # The dependency must be gone now (undo removes it)
        deps = db.get_all_dependencies()
        assert not any(d["from"] == a.id and d["to"] == b.id for d in deps), f"dependency_added undo did not remove the edge; deps={deps}"

    def test_undo_dependency_removed_with_colon_in_type(self, db: FiligreeDB) -> None:
        a = db.create_issue("A")
        b = db.create_issue("B")
        db.add_dependency(a.id, b.id, dep_type="namespaced:type", actor="t")
        db.remove_dependency(a.id, b.id, actor="t")
        # Sanity: edge gone
        assert not any(d["from"] == a.id and d["to"] == b.id for d in db.get_all_dependencies())

        result = db.undo_last(a.id, actor="t")
        assert result["undone"] is True, f"undo failed: {result}"
        assert result["event_type"] == "dependency_removed"
        deps = db.get_all_dependencies()
        edge = next((d for d in deps if d["from"] == a.id and d["to"] == b.id), None)
        assert edge is not None, "dependency_removed undo did not restore the edge"
        # The original dep_type must round-trip
        assert edge["type"] == "namespaced:type", f"dep_type was lost on undo; got {edge['type']!r}"
