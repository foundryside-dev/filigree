"""Tests for core CRUD operations — create, get, update, close, reopen, claim, export, import, archival."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from filigree.core import FiligreeDB, WrongProjectError
from filigree.types.api import ClaimConflictError, InvalidTransitionError
from tests.conftest import PopulatedDB


class TestCreateIssueValidation:
    """M10: Core-level validation for create_issue inputs."""

    @pytest.mark.parametrize("title", ["", "   "], ids=["empty", "whitespace"])
    def test_empty_title_raises(self, db: FiligreeDB, title: str) -> None:
        with pytest.raises(ValueError, match="Title cannot be empty"):
            db.create_issue(title)

    @pytest.mark.parametrize("title", [123, ["title"]], ids=["int", "list"])
    def test_non_string_title_raises_type_error_without_writing(self, db: FiligreeDB, title: object) -> None:
        before_count = db.conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0]

        with pytest.raises(TypeError, match="title must be a string"):
            db.create_issue(title)  # type: ignore[arg-type]

        after_count = db.conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0]
        assert after_count == before_count

    @pytest.mark.parametrize("priority", [-1, 5, 100], ids=["neg1", "five", "hundred"])
    def test_bad_priority_raises(self, db: FiligreeDB, priority: int) -> None:
        with pytest.raises(ValueError, match="Priority must be"):
            db.create_issue("Valid title", priority=priority)

    @pytest.mark.parametrize("key", ["", "  "], ids=["empty", "whitespace"])
    def test_empty_field_key_raises(self, db: FiligreeDB, key: str) -> None:
        with pytest.raises(ValueError, match="Field key cannot be empty"):
            db.create_issue("Valid title", fields={key: "value"})

    def test_fields_not_dict_raises(self, db: FiligreeDB) -> None:
        with pytest.raises(TypeError, match="fields must be a dict"):
            db.create_issue("Valid title", fields="not a dict")  # type: ignore[arg-type]

    def test_unique_field_checks_ignore_corrupt_existing_fields_json(self, db: FiligreeDB) -> None:
        """A corrupt fields blob on an existing row must not abort uniqueness checks."""
        corrupt = db.create_issue("Corrupt release", type="release", fields={"version": "v1.2.3"})
        db.conn.execute("UPDATE issues SET fields = ? WHERE id = ?", ("{", corrupt.id))
        db.conn.commit()

        created = db.create_issue("Valid release", type="release", fields={"version": "v2.0.0"})

        assert created.fields["version"] == "v2.0.0"

    def test_unknown_type_raises(self, db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="Unknown type"):
            db.create_issue("Valid title", type="nonexistent_type")

    @pytest.mark.parametrize("bad", ["urgent", "a"], ids=["word", "single_char"])
    def test_bare_string_labels_raises_type_error(self, db: FiligreeDB, bad: str) -> None:
        """filigree-0b4fcb6d30: bare string as labels must not be iterated char-by-char."""
        with pytest.raises(TypeError, match="labels must be a list of strings"):
            db.create_issue("Valid title", labels=bad)  # type: ignore[arg-type]

    def test_mixed_type_labels_raises_type_error(self, db: FiligreeDB) -> None:
        """filigree-0b4fcb6d30: non-string items must be rejected up front."""
        with pytest.raises(TypeError, match="labels must be a list of strings"):
            db.create_issue("Valid title", labels=["ok", 42])  # type: ignore[list-item]

    def test_bare_string_deps_raises_type_error(self, db: FiligreeDB) -> None:
        """filigree-0b4fcb6d30: bare string as deps must not be iterated char-by-char."""
        with pytest.raises(TypeError, match="deps must be a list of strings"):
            db.create_issue("Valid title", deps="abc")  # type: ignore[arg-type]

    def test_non_string_assignee_raises_type_error(self, db: FiligreeDB) -> None:
        """filigree-7c1932b74e: non-string assignee is a programming error, not a silent store."""
        with pytest.raises(TypeError, match="assignee must be a string"):
            db.create_issue("Valid title", assignee=123)  # type: ignore[arg-type]


class TestAssigneeNormalization:
    """filigree-7c1932b74e: create/update must strip assignee and never store whitespace-only."""

    def test_create_trims_assignee(self, db: FiligreeDB) -> None:
        issue = db.create_issue("T", assignee="  bob  ")
        assert issue.assignee == "bob"

    def test_create_whitespace_only_assignee_stored_empty(self, db: FiligreeDB) -> None:
        issue = db.create_issue("T", assignee="   ")
        assert issue.assignee == ""

    def test_update_trims_assignee(self, db: FiligreeDB) -> None:
        issue = db.create_issue("T")
        updated = db.update_issue(issue.id, assignee="  alice  ")
        assert updated.assignee == "alice"

    def test_update_whitespace_only_assignee_stored_empty(self, db: FiligreeDB) -> None:
        issue = db.create_issue("T", assignee="alice")
        updated = db.update_issue(issue.id, assignee="   ")
        assert updated.assignee == ""

    def test_claimable_after_create_with_whitespace_assignee(self, db: FiligreeDB) -> None:
        """Regression: whitespace-only assignee used to prevent claim (treated as already-owned)."""
        issue = db.create_issue("T", assignee="   ")
        claimed = db.claim_issue(issue.id, assignee="bob")
        assert claimed.assignee == "bob"


class TestClaimAssigneeNormalization:
    """filigree-694f7e9bf8: claim_issue must normalize assignee like create/update."""

    def test_claim_trims_assignee(self, db: FiligreeDB) -> None:
        issue = db.create_issue("T")
        claimed = db.claim_issue(issue.id, assignee="  bob  ")
        assert claimed.assignee == "bob"

    def test_claim_then_reclaim_with_canonical_form_succeeds(self, db: FiligreeDB) -> None:
        """Padded claim then canonical claim by the same identity is idempotent."""
        issue = db.create_issue("T")
        db.claim_issue(issue.id, assignee="  bob  ")
        result = db.claim_issue(issue.id, assignee="bob")
        assert result.assignee == "bob"


class TestUpdateIssueTitleValidation:
    """filigree-365dff403e: update_issue must reject empty/whitespace-only title."""

    @pytest.mark.parametrize("title", ["", "   "], ids=["empty", "whitespace"])
    def test_update_empty_title_raises(self, db: FiligreeDB, title: str) -> None:
        issue = db.create_issue("Original")
        with pytest.raises(ValueError, match="Title cannot be empty"):
            db.update_issue(issue.id, title=title)
        # And the stored title must be unchanged.
        assert db.get_issue(issue.id).title == "Original"


class TestStartWorkRollbackPreservesPriorClaim:
    """filigree-31404d228f: start_work must not erase a pre-existing same-assignee claim on rollback."""

    def test_failed_transition_preserves_prior_claim(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Already mine")
        db.claim_issue(issue.id, assignee="alice")

        with pytest.raises(ValueError, match="Invalid status"):
            db.start_work(issue.id, assignee="alice", target_status="nonexistent_status")

        assert db.get_issue(issue.id).assignee == "alice"

    def test_failed_transition_releases_newly_acquired_claim(self, db: FiligreeDB) -> None:
        """Regression guard: when start_work itself acquires the claim, rollback must still release."""
        issue = db.create_issue("Newly claimed")
        with pytest.raises(ValueError, match="Invalid status"):
            db.start_work(issue.id, assignee="alice", target_status="nonexistent_status")
        assert db.get_issue(issue.id).assignee == ""


class TestStartNextWorkRollbackPreservesPriorClaim:
    """filigree-fa01508ee2: start_next_work must not erase a pre-existing same-assignee claim on rollback.

    claim_issue is intentionally idempotent for the same identity, and claim_next
    inherits that. start_next_work previously released the claim unconditionally
    on transition failure, wiping a claim that was already held by ``assignee``
    before the call. Mirrors the start_work contract.
    """

    def test_failed_target_status_preserves_prior_claim(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Pre-owned ready", priority=1)
        db.claim_issue(issue.id, assignee="alice")

        with pytest.raises(ValueError, match="Invalid status"):
            db.start_next_work(assignee="alice", target_status="nonexistent_status")

        assert db.get_issue(issue.id).assignee == "alice"

    def test_failed_target_status_releases_newly_acquired_claim(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Unclaimed ready", priority=1)
        with pytest.raises(ValueError, match="Invalid status"):
            db.start_next_work(assignee="alice", target_status="nonexistent_status")
        assert db.get_issue(issue.id).assignee == ""


class TestStartableVsReady:
    """filigree-406e6b7ee0: "ready" != "startable" for multi-hop-to-wip types.

    ``get_ready`` lists open-category issues; bugs start at ``triage`` which
    has no single-hop transition to a wip state (triage->confirmed->fixing).
    ``start_next_work`` must skip such non-startable candidates instead of
    throwing, and ``start_work`` on a *named* triage bug must surface an
    actionable hint rather than no-op.
    """

    def test_start_next_work_skips_triage_only_ready_set(self, db: FiligreeDB) -> None:
        # type_filter="bug" makes the ready set effectively triage-only (the
        # auto-seeded "Future" release singleton is filtered out).
        bug = db.create_issue("Triage bug", type="bug", priority=1)

        result = db.start_next_work(assignee="alice", type_filter="bug")

        # No startable work: clean None, not an exception.
        assert result is None
        # The non-startable candidate is never claimed.
        assert db.get_issue(bug.id).assignee == ""

    def test_start_next_work_starts_startable_task_over_triage_bug(self, db: FiligreeDB) -> None:
        bug = db.create_issue("Triage bug", type="bug", priority=1)
        task = db.create_issue("Startable task", type="task", priority=2)

        result = db.start_next_work(assignee="alice")

        assert result is not None
        assert result.id == task.id
        assert result.status == "in_progress"
        # The skipped triage bug remains untouched and unclaimed.
        assert db.get_issue(bug.id).assignee == ""

    def test_start_work_named_triage_bug_raises_with_next_action_hint(self, db: FiligreeDB) -> None:
        bug = db.create_issue("Triage bug", type="bug", priority=1)

        with pytest.raises(InvalidTransitionError) as exc_info:
            db.start_work(bug.id, assignee="alice")

        # Names the intermediate status the agent must move through first.
        assert "confirmed" in str(exc_info.value)
        # Named start_work must not silently no-op into a claim.
        assert db.get_issue(bug.id).assignee == ""


class TestAdvanceMultiHop:
    """filigree-406e6b7ee0 Part 2: opt-in multi-hop ``advance`` walks SOFT
    transitions to the nearest working state.

    Default behaviour (no ``advance``) is unchanged — a named triage bug still
    raises. ``advance=True`` walks ``triage -> confirmed -> fixing``, surfacing
    required-field warnings rather than blocking, and never traverses a hard
    edge.
    """

    def test_start_work_advance_walks_triage_bug_to_wip(self, db: FiligreeDB) -> None:
        bug = db.create_issue("Triage bug", type="bug", priority=1)

        result = db.start_work(bug.id, assignee="alice", advance=True)

        assert result.status == "fixing"
        assert result.assignee == "alice"
        # The walk surfaces missing required fields as warnings, not blocks —
        # and aggregates EVERY hop's advisory, not just the terminal one
        # (severity is required at 'confirmed', root_cause at 'fixing').
        assert any("severity" in w for w in result.data_warnings)
        assert any("root_cause" in w for w in result.data_warnings)
        # Full audit chain: claim + both soft hops.
        events = db.get_issue_events(bug.id)
        status_changes = [(e["old_value"], e["new_value"]) for e in events if e["event_type"] == "status_changed"]
        assert ("triage", "confirmed") in status_changes
        assert ("confirmed", "fixing") in status_changes

    def test_start_work_default_still_raises_without_advance(self, db: FiligreeDB) -> None:
        bug = db.create_issue("Triage bug", type="bug", priority=1)
        with pytest.raises(InvalidTransitionError):
            db.start_work(bug.id, assignee="alice")
        assert db.get_issue(bug.id).status == "triage"
        assert db.get_issue(bug.id).assignee == ""

    def test_start_next_work_advance_starts_triage_bug(self, db: FiligreeDB) -> None:
        bug = db.create_issue("Triage bug", type="bug", priority=1)

        result = db.start_next_work(assignee="alice", type_filter="bug", advance=True)

        assert result is not None
        assert result.id == bug.id
        assert result.status == "fixing"
        assert result.assignee == "alice"


class TestStartability:
    """filigree-406e6b7ee0: TypeTemplate.startability single source of truth."""

    def test_soft_path_to_working_status(self, db: FiligreeDB) -> None:
        bug = db.templates.get_type("bug")
        task = db.templates.get_type("task")
        assert bug is not None
        assert task is not None
        assert bug.soft_path_to_working_status("triage") == ["confirmed", "fixing"]
        assert task.soft_path_to_working_status("open") == ["in_progress"]
        # Already wip -> nothing to walk.
        assert bug.soft_path_to_working_status("fixing") == []

    def test_task_is_startable_single_hop(self, db: FiligreeDB) -> None:
        tpl = db.templates.get_type("task")
        assert tpl is not None
        startable, next_action = tpl.startability("open")
        assert startable is True
        assert next_action is None

    def test_triage_bug_not_startable_next_action_is_first_soft_hop(self, db: FiligreeDB) -> None:
        tpl = db.templates.get_type("bug")
        assert tpl is not None
        startable, next_action = tpl.startability("triage")
        assert startable is False
        assert next_action == "confirmed"

    def test_already_wip_is_startable(self, db: FiligreeDB) -> None:
        tpl = db.templates.get_type("bug")
        assert tpl is not None
        startable, next_action = tpl.startability("fixing")
        assert startable is True
        assert next_action is None


class TestPriorityTypeValidation:
    """filigree-fa01508ee2: priority must reject non-int (incl. bool) before any write.

    The Issue model rejects floats at hydration but only after commit, so
    ``create_issue(priority=2.5)`` would raise while leaving a row with
    ``typeof(priority)='real'`` durably stored. ``bool`` slips past
    ``isinstance(_, int)`` because Python's bool is an int subclass, and
    update_issue's ``priority != current.priority`` short-circuit allowed
    ``update_issue(priority=True)`` on a P1 issue to silently no-op.
    """

    @pytest.mark.parametrize("priority", [2.5, 3.5, 0.0], ids=["mid_float", "high_float", "zero_float"])
    def test_create_rejects_float_and_leaves_no_row(self, db: FiligreeDB, priority: float) -> None:
        before = len(db.list_issues())
        with pytest.raises(ValueError, match="Priority must be an integer"):
            db.create_issue("Float priority", priority=priority)  # type: ignore[arg-type]
        # No partial row committed.
        assert len(db.list_issues()) == before
        rows = db.conn.execute("SELECT id FROM issues WHERE title = 'Float priority'").fetchall()
        assert rows == []

    @pytest.mark.parametrize("priority", [True, False], ids=["true", "false"])
    def test_create_rejects_bool(self, db: FiligreeDB, priority: bool) -> None:
        with pytest.raises(ValueError, match="Priority must be an integer"):
            db.create_issue("Bool priority", priority=priority)  # type: ignore[arg-type]

    @pytest.mark.parametrize("priority", [2.5, 3.5], ids=["mid_float", "high_float"])
    def test_update_rejects_float_and_leaves_row_unchanged(self, db: FiligreeDB, priority: float) -> None:
        issue = db.create_issue("Baseline", priority=2)
        with pytest.raises(ValueError, match="Priority must be an integer"):
            db.update_issue(issue.id, priority=priority)  # type: ignore[arg-type]
        # Stored value unchanged AND still typed as integer.
        row = db.conn.execute("SELECT typeof(priority), priority FROM issues WHERE id = ?", (issue.id,)).fetchone()
        assert row[0] == "integer"
        assert row[1] == 2

    def test_update_rejects_bool_even_when_equal_to_current(self, db: FiligreeDB) -> None:
        """Regression: ``priority != current.priority`` short-circuit allowed
        ``update_issue(priority=True)`` on a P1 issue to skip both validation AND
        the write — silent no-op. Validation must run for any non-None priority.
        """
        issue = db.create_issue("P1 issue", priority=1)
        with pytest.raises(ValueError, match="Priority must be an integer"):
            db.update_issue(issue.id, priority=True)  # type: ignore[arg-type]


class TestSafeFieldsJson:
    """Bug filigree-58ce3d9da4: _safe_fields_json must handle non-string raw values."""

    def test_non_string_raw_does_not_raise(self, db: FiligreeDB) -> None:
        """When raw is a non-string (e.g. int from DB corruption), log should not TypeError."""
        from filigree.db_issues import _safe_fields_json

        # Should return empty dict with corrupt flag set, without raising TypeError on the log truncation
        result = _safe_fields_json(42, "test-issue-001")  # type: ignore[arg-type]
        assert result == {}
        assert getattr(result, "_filigree_corrupt", False) is True

    def test_corrupt_json_returns_corrupt_flag(self) -> None:
        from filigree.db_issues import _safe_fields_json

        result = _safe_fields_json("{bad json", "test-issue-002")
        assert result == {}
        assert getattr(result, "_filigree_corrupt", False) is True

    def test_valid_json_returns_dict(self) -> None:
        from filigree.db_issues import _safe_fields_json

        assert _safe_fields_json('{"key": "val"}', "test-issue-003") == {"key": "val"}

    def test_none_returns_empty_dict(self) -> None:
        from filigree.db_issues import _safe_fields_json

        assert _safe_fields_json(None, "test-issue-004") == {}


class TestCreateAndGet:
    def test_create_issue(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Fix the widget")
        assert issue.title == "Fix the widget"
        assert issue.status == "open"
        assert issue.id.startswith("test-")

    def test_get_issue(self, db: FiligreeDB) -> None:
        created = db.create_issue("Something")
        fetched = db.get_issue(created.id)
        assert fetched.title == "Something"
        assert fetched.id == created.id

    def test_get_missing_issue_raises(self, db: FiligreeDB) -> None:
        with pytest.raises(KeyError):
            db.get_issue("nonexistent-abc123")


class TestUpdateAndClose:
    def test_update_status(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Do the thing")
        updated = db.update_issue(issue.id, status="in_progress")
        assert updated.status == "in_progress"

    def test_close_issue(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Close me")
        closed = db.close_issue(issue.id, reason="done")
        assert closed.status == "closed"
        assert closed.closed_at is not None

    def test_close_already_closed_raises(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Close me twice")
        db.close_issue(issue.id)
        with pytest.raises(ValueError, match="already closed"):
            db.close_issue(issue.id)


class TestLabelsAndComments:
    def test_labels_on_create(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Labeled", labels=["defect", "urgent"])
        assert set(issue.labels) == {"defect", "urgent"}

    def test_add_remove_label(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Label test")
        db.add_label(issue.id, "backend")
        refreshed = db.get_issue(issue.id)
        assert "backend" in refreshed.labels
        db.remove_label(issue.id, "backend")
        refreshed = db.get_issue(issue.id)
        assert "backend" not in refreshed.labels

    def test_create_rejects_reserved_type_label(self, db: FiligreeDB) -> None:
        with pytest.raises(ValueError, match="reserved as an issue type"):
            db.create_issue("Bad labels", labels=["bug", "urgent"])

    def test_add_label_rejects_reserved_type_label_case_insensitive(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Label test")
        with pytest.raises(ValueError, match="reserved as an issue type"):
            db.add_label(issue.id, "BuG")

    @pytest.mark.parametrize("label", ["P0", "p4", "priority:1", "Priority:high"])
    def test_add_label_rejects_priority_like_label(self, db: FiligreeDB, label: str) -> None:
        issue = db.create_issue("Label test")
        with pytest.raises(ValueError, match="priority field"):
            db.add_label(issue.id, label)

    @pytest.mark.parametrize("label", ["P1", "priority:2"])
    def test_create_rejects_priority_like_label(self, db: FiligreeDB, label: str) -> None:
        with pytest.raises(ValueError, match="priority field"):
            db.create_issue("Bad labels", labels=[label])

    def test_remove_label_allows_legacy_priority_like_label(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Label test")
        db.conn.execute("INSERT INTO labels (issue_id, label) VALUES (?, ?)", (issue.id, "P1"))
        db.conn.commit()

        removed, canonical = db.remove_label(issue.id, "P1")

        assert removed is True
        assert canonical == "P1"
        assert db.get_issue(issue.id).labels == []

    def test_add_comment(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Commentable")
        db.add_comment(issue.id, "This is a note", author="tester")
        comments = db.get_comments(issue.id)
        assert len(comments) == 1
        assert comments[0]["text"] == "This is a note"


class TestGenerateId:
    """Verify _generate_unique_id uses O(1) EXISTS check, not full-table scan."""

    def test_generate_id_returns_prefixed_id(self, db: FiligreeDB) -> None:
        issue_id = db._generate_unique_id("issues")
        assert issue_id.startswith("test-")
        assert len(issue_id) == len("test-") + 10

    def test_generate_id_avoids_collisions(self, db: FiligreeDB) -> None:
        ids = {db._generate_unique_id("issues") for _ in range(50)}
        assert len(ids) == 50

    def test_generate_id_uses_exists_check(self, db: FiligreeDB) -> None:
        """Verify the implementation queries by specific ID, not all IDs."""
        import inspect

        source = inspect.getsource(db._generate_unique_id)
        assert "SELECT 1 FROM {table} WHERE id = ?" in source
        assert "SELECT id FROM issues" not in source

    def test_generate_id_fallback_logs_error(self, db: FiligreeDB, caplog: pytest.LogCaptureFixture) -> None:
        """After 10 collisions the fallback must log an error and verify uniqueness."""
        import logging
        from unittest.mock import MagicMock, patch

        # Return predictable UUIDs: first 10 produce the same 10-char hex, 11th is unique
        collision_hex = "a" * 32
        unique_hex = "b" * 32
        mock_uuids = [MagicMock(hex=collision_hex)] * 10 + [MagicMock(hex=unique_hex)]

        # Pre-insert a row with the colliding ID
        colliding_id = f"test-{collision_hex[:10]}"
        db.conn.execute(
            "INSERT INTO issues (id, title, status, priority, type, assignee, created_at, updated_at, description, notes, fields) "
            "VALUES (?, 'collision', 'open', 2, 'task', '', '', '', '', '', '{}')",
            (colliding_id,),
        )
        db.conn.commit()

        with patch("filigree.db_issues.uuid.uuid4", side_effect=mock_uuids), caplog.at_level(logging.ERROR, logger="filigree.db_issues"):
            result = db._generate_unique_id("issues")

        # Fallback uses 16-char hex from the unique UUID
        assert result == f"test-{unique_hex[:16]}"
        assert "10 consecutive ID collisions" in caplog.text

    def test_generate_id_fallback_collision_raises(self, db: FiligreeDB) -> None:
        """If even the 16-char fallback collides, RuntimeError must be raised."""
        from unittest.mock import MagicMock, patch

        collision_hex = "c" * 32
        mock_uuids = [MagicMock(hex=collision_hex)] * 11

        # Pre-insert rows matching both the 10-char and 16-char candidates
        for length in (10, 16):
            cid = f"test-{collision_hex[:length]}"
            db.conn.execute(
                "INSERT INTO issues (id, title, status, priority, type, assignee, created_at, updated_at, description, notes, fields) "
                "VALUES (?, 'collision', 'open', 2, 'task', '', '', '', '', '', '{}')",
                (cid,),
            )
        db.conn.commit()

        with patch("filigree.db_issues.uuid.uuid4", side_effect=mock_uuids), pytest.raises(RuntimeError, match="fallback ID also collided"):
            db._generate_unique_id("issues")


class TestDescriptionNotesAuditTrail:
    """Verify description and notes changes produce audit events."""

    def test_description_changed_event(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Test issue", description="old desc")
        db.update_issue(issue.id, description="new desc", actor="tester")

        events = db.conn.execute(
            "SELECT * FROM events WHERE issue_id = ? AND event_type = 'description_changed'",
            (issue.id,),
        ).fetchall()
        assert len(events) == 1
        assert events[0]["old_value"] == "old desc"
        assert events[0]["new_value"] == "new desc"
        assert events[0]["actor"] == "tester"

    def test_notes_changed_event(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Test issue", notes="old notes")
        db.update_issue(issue.id, notes="new notes", actor="tester")

        events = db.conn.execute(
            "SELECT * FROM events WHERE issue_id = ? AND event_type = 'notes_changed'",
            (issue.id,),
        ).fetchall()
        assert len(events) == 1
        assert events[0]["old_value"] == "old notes"
        assert events[0]["new_value"] == "new notes"

    def test_no_event_when_description_unchanged(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Test issue", description="same")
        db.update_issue(issue.id, description="same")

        events = db.conn.execute(
            "SELECT * FROM events WHERE issue_id = ? AND event_type = 'description_changed'",
            (issue.id,),
        ).fetchall()
        assert len(events) == 0

    def test_no_event_when_notes_unchanged(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Test issue", notes="same")
        db.update_issue(issue.id, notes="same")

        events = db.conn.execute(
            "SELECT * FROM events WHERE issue_id = ? AND event_type = 'notes_changed'",
            (issue.id,),
        ).fetchall()
        assert len(events) == 0


class TestUpdateIssuePaths:
    def test_update_title(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Old title")
        updated = db.update_issue(issue.id, title="New title")
        assert updated.title == "New title"

    def test_update_priority(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Priority test", priority=2)
        updated = db.update_issue(issue.id, priority=0)
        assert updated.priority == 0

    def test_update_assignee(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Assign test")
        updated = db.update_issue(issue.id, assignee="alice")
        assert updated.assignee == "alice"

    def test_update_description(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Desc test")
        updated = db.update_issue(issue.id, description="new desc")
        assert updated.description == "new desc"

    def test_update_notes(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Notes test")
        updated = db.update_issue(issue.id, notes="new notes")
        assert updated.notes == "new notes"

    def test_update_fields_merge(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Fields test", fields={"a": "1", "b": "2"})
        updated = db.update_issue(issue.id, fields={"b": "updated", "c": "3"})
        assert updated.fields == {"a": "1", "b": "updated", "c": "3"}

    def test_update_issue_refuses_corrupt_fields_merge(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Corrupt fields merge")
        raw_fields = b"{bad json"
        db.conn.execute("UPDATE issues SET fields = ? WHERE id = ?", (raw_fields, issue.id))
        db.conn.commit()

        with pytest.raises(ValueError, match="Refusing to merge fields"):
            db.update_issue(issue.id, fields={"b": "2"})

        row = db.conn.execute("SELECT fields FROM issues WHERE id = ?", (issue.id,)).fetchone()
        assert row["fields"] == raw_fields
        events = db.conn.execute(
            "SELECT event_type FROM events WHERE issue_id = ? AND event_type = 'corrupt_fields_overwritten'",
            (issue.id,),
        ).fetchall()
        assert events == []

    def test_update_issue_force_overwrite_corrupt_emits_event_with_raw_bytes(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Force corrupt fields overwrite")
        raw_fields = b"{bad json"
        db.conn.execute("UPDATE issues SET fields = ? WHERE id = ?", (raw_fields, issue.id))
        db.conn.commit()

        updated = db.update_issue(issue.id, fields={"b": "2"}, actor="tester", force_overwrite_corrupt=True)

        assert updated.fields == {"b": "2"}
        event = db.conn.execute(
            "SELECT actor, old_value, new_value FROM events WHERE issue_id = ? AND event_type = 'corrupt_fields_overwritten'",
            (issue.id,),
        ).fetchone()
        assert event is not None
        assert event["actor"] == "tester"
        assert event["old_value"] == raw_fields
        assert json.loads(event["new_value"]) == {"b": "2"}
        fields_changed = db.conn.execute(
            "SELECT COUNT(*) FROM events WHERE issue_id = ? AND event_type = 'fields_changed'",
            (issue.id,),
        ).fetchone()[0]
        assert fields_changed == 0

    def test_update_no_changes(self, db: FiligreeDB) -> None:
        """Update with no actual changes should not error."""
        issue = db.create_issue("No change")
        updated = db.update_issue(issue.id)
        assert updated.title == "No change"

    def test_update_fields_identical_no_event(self, db: FiligreeDB) -> None:
        """Passing identical fields should not record an event or bump updated_at.

        Regression: fields update was always appended outside the change guard.
        """
        issue = db.create_issue("Fields stable", fields={"a": "1"})
        events_before = db.conn.execute(
            "SELECT COUNT(*) FROM events WHERE issue_id = ? AND event_type = 'fields_changed'",
            (issue.id,),
        ).fetchone()[0]
        db.update_issue(issue.id, fields={"a": "1"})
        events_after = db.conn.execute(
            "SELECT COUNT(*) FROM events WHERE issue_id = ? AND event_type = 'fields_changed'",
            (issue.id,),
        ).fetchone()[0]
        assert events_after == events_before

    def test_update_nonexistent_raises(self, db: FiligreeDB) -> None:
        with pytest.raises(KeyError):
            db.update_issue("test-nonexistent", title="nope")


class TestIssueToDictRoundTrip:
    def test_to_dict_has_all_fields(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Roundtrip", labels=["a"], fields={"x": "1"})
        d = issue.to_dict()
        expected_keys = {
            "id",
            "title",
            "status",
            "status_category",
            "priority",
            "type",
            "parent_id",
            "assignee",
            "claimed_at",
            "last_heartbeat_at",
            "claim_expires_at",
            "created_at",
            "updated_at",
            "closed_at",
            "description",
            "notes",
            "fields",
            "labels",
            "blocks",
            "blocked_by",
            "is_ready",
            "children",
            "data_warnings",
        }
        assert set(d.keys()) == expected_keys
        assert d["labels"] == ["a"]
        assert d["fields"] == {"x": "1"}

    def test_corrupt_fields_json_does_not_crash(self, db: FiligreeDB) -> None:
        """When issues.fields is corrupt JSON, get_issue should not crash."""
        issue = db.create_issue("Corrupt fields test")
        # Corrupt the fields column directly
        db.conn.execute(
            "UPDATE issues SET fields = '{bad json' WHERE id = ?",
            (issue.id,),
        )
        db.conn.commit()

        # Should not raise — gracefully degrade with empty dict + out-of-band corrupt flag.
        result = db.get_issue(issue.id)
        assert result is not None
        assert result.fields == {}
        assert getattr(result.fields, "_filigree_corrupt", False) is True

    def test_corrupt_fields_to_dict_surfaces_data_warning(self, db: FiligreeDB) -> None:
        """to_dict() should strip _fields_error and produce a data_warnings entry."""
        issue = db.create_issue("Corrupt to_dict test")
        db.conn.execute(
            "UPDATE issues SET fields = '{bad json' WHERE id = ?",
            (issue.id,),
        )
        db.conn.commit()

        result = db.get_issue(issue.id)
        d = result.to_dict()
        assert "_fields_error" not in d["fields"]
        assert len(d["data_warnings"]) == 1
        assert "corrupt" in d["data_warnings"][0].lower()

    def test_clean_fields_to_dict_has_no_warnings(self, db: FiligreeDB) -> None:
        """to_dict() should produce empty data_warnings for valid fields."""
        issue = db.create_issue("Clean fields", fields={"x": 1})
        d = issue.to_dict()
        assert d["data_warnings"] == []
        assert d["fields"] == {"x": 1}


class TestChildren:
    def test_children_populated(self, db: FiligreeDB) -> None:
        parent = db.create_issue("Parent", type="epic")
        child1 = db.create_issue("Child 1", parent_id=parent.id)
        child2 = db.create_issue("Child 2", parent_id=parent.id)
        refreshed = db.get_issue(parent.id)
        assert set(refreshed.children) == {child1.id, child2.id}


class TestCreateWithOptions:
    def test_create_with_all_options(self, db: FiligreeDB) -> None:
        issue = db.create_issue(
            "Full issue",
            type="bug",
            priority=0,
            assignee="alice",
            description="A description",
            notes="Some notes",
            fields={"severity": "critical"},
            labels=["urgent", "backend"],
            deps=None,
        )
        assert issue.type == "bug"
        assert issue.priority == 0
        assert issue.assignee == "alice"
        assert issue.description == "A description"
        assert issue.notes == "Some notes"
        assert issue.fields["severity"] == "critical"
        assert set(issue.labels) == {"urgent", "backend"}

    def test_create_with_deps(self, db: FiligreeDB) -> None:
        blocker = db.create_issue("Blocker")
        issue = db.create_issue("Blocked", deps=[blocker.id])
        assert blocker.id in issue.blocked_by

    def test_create_rejects_non_dict_fields(self, db: FiligreeDB) -> None:
        with pytest.raises(TypeError, match="fields must be a dict"):
            db.create_issue("Bad fields", fields=["not", "a", "dict"])  # type: ignore[arg-type]


class TestUpdateFieldValidation:
    def test_update_rejects_non_dict_fields(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Mutable")
        with pytest.raises(TypeError, match="fields must be a dict"):
            db.update_issue(issue.id, fields=["not", "a", "dict"])  # type: ignore[arg-type]

    @pytest.mark.parametrize("key", ["", "  "], ids=["empty", "whitespace"])
    def test_update_rejects_blank_field_key(self, db: FiligreeDB, key: str) -> None:
        issue = db.create_issue("Mutable")
        with pytest.raises(ValueError, match="Field key cannot be empty"):
            db.update_issue(issue.id, fields={key: "value"})
        assert db.get_issue(issue.id).fields == {}

    def test_update_rejects_non_string_field_key(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Mutable")
        with pytest.raises(TypeError, match="field keys must be strings"):
            db.update_issue(issue.id, fields={1: "value"})  # type: ignore[dict-item]
        assert db.get_issue(issue.id).fields == {}


class TestClaimIssue:
    def test_claim_success(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Claimable")
        claimed = db.claim_issue(issue.id, assignee="agent-1")
        assert claimed.status == "open"  # status unchanged
        assert claimed.assignee == "agent-1"

    def test_claim_step_uses_template_states(self, db: FiligreeDB) -> None:
        """Step type uses template open-category states (pending), not legacy 'open'."""
        step = db.create_issue("Step", type="step")
        assert step.status == "pending"  # step template initial state
        claimed = db.claim_issue(step.id, assignee="agent-1")
        assert claimed.status == "pending"  # status unchanged
        assert claimed.assignee == "agent-1"

    def test_claim_already_claimed(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Claimable")
        db.claim_issue(issue.id, assignee="agent-1")
        with pytest.raises(ValueError, match="already assigned to"):
            db.claim_issue(issue.id, assignee="agent-2")

    def test_claim_closed_issue(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Will close")
        db.close_issue(issue.id)
        with pytest.raises(ValueError, match="status is 'closed'"):
            db.claim_issue(issue.id, assignee="agent-1")

    def test_claim_not_found(self, db: FiligreeDB) -> None:
        with pytest.raises(KeyError, match="not found"):
            db.claim_issue("test-nonexistent", assignee="agent-1")

    def test_claim_records_event(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Claimable")
        db.claim_issue(issue.id, assignee="agent-1", actor="agent-1")
        events = db.get_recent_events(limit=5)
        claim_event = next(e for e in events if e["event_type"] == "claimed")
        assert claim_event["issue_id"] == issue.id
        assert claim_event["new_value"] == "agent-1"
        assert claim_event["actor"] == "agent-1"


class TestClaimEmptyAssignee:
    """Bug filigree-040ddb: claim_issue/claim_next must reject empty assignee."""

    @pytest.mark.parametrize("assignee", ["", "   "], ids=["empty", "whitespace"])
    def test_claim_issue_rejects_blank_assignee(self, db: FiligreeDB, assignee: str) -> None:
        issue = db.create_issue("Claimable")
        with pytest.raises(ValueError, match="Assignee cannot be empty"):
            db.claim_issue(issue.id, assignee=assignee)

    @pytest.mark.parametrize("assignee", ["", "   "], ids=["empty", "whitespace"])
    def test_claim_next_rejects_blank_assignee(self, db: FiligreeDB, assignee: str) -> None:
        db.create_issue("Ready")
        with pytest.raises(ValueError, match="Assignee cannot be empty"):
            db.claim_next(assignee)


class TestReparenting:
    def test_update_parent_id(self, db: FiligreeDB) -> None:
        parent = db.create_issue("Parent")
        child = db.create_issue("Child")
        updated = db.update_issue(child.id, parent_id=parent.id)
        assert updated.parent_id == parent.id

    def test_update_parent_id_records_event(self, db: FiligreeDB) -> None:
        parent = db.create_issue("Parent")
        child = db.create_issue("Child")
        db.update_issue(child.id, parent_id=parent.id, actor="tester")
        events = db.conn.execute(
            "SELECT * FROM events WHERE issue_id = ? AND event_type = 'parent_changed'",
            (child.id,),
        ).fetchall()
        assert len(events) == 1
        assert events[0]["new_value"] == parent.id

    def test_update_parent_id_invalid_parent_raises(self, db: FiligreeDB) -> None:
        child = db.create_issue("Child")
        with pytest.raises(ValueError, match="does not reference"):
            db.update_issue(child.id, parent_id=f"{db.prefix}-123456")

    def test_update_parent_id_foreign_prefix_raises_wrong_project_before_existence(self, db: FiligreeDB) -> None:
        child = db.create_issue("Child")
        with pytest.raises(WrongProjectError):
            db.update_issue(child.id, parent_id="foreign-123abc")
        assert db.get_issue(child.id).parent_id is None

    def test_update_parent_id_self_reference_raises(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Issue")
        with pytest.raises(ValueError, match="cannot be its own parent"):
            db.update_issue(issue.id, parent_id=issue.id)

    def test_update_parent_id_cycle_raises(self, db: FiligreeDB) -> None:
        grandparent = db.create_issue("Grandparent")
        parent = db.create_issue("Parent", parent_id=grandparent.id)
        child = db.create_issue("Child", parent_id=parent.id)
        with pytest.raises(ValueError, match="circular"):
            db.update_issue(grandparent.id, parent_id=child.id)

    def test_existing_parent_loop_is_detected_without_unbounded_walk(self, db: FiligreeDB) -> None:
        class _Cursor:
            def __init__(self, parent_id: str | None) -> None:
                self._parent_id = parent_id

            def fetchone(self) -> dict[str, str] | None:
                if self._parent_id is None:
                    return None
                return {"parent_id": self._parent_id}

        class _LoopingConn:
            def __init__(self) -> None:
                self.calls = 0

            def execute(self, _sql: str, params: tuple[Any, ...]) -> _Cursor:
                self.calls += 1
                if self.calls > 4:
                    raise AssertionError("parent-cycle traversal did not stop")
                parent_map = {"parent-a": "parent-b", "parent-b": "parent-a"}
                return _Cursor(parent_map.get(str(params[0])))

        class _FakeDB:
            conn = _LoopingConn()

        assert type(db)._would_create_parent_cycle(_FakeDB(), "child", "parent-a") is True

    def test_clear_parent_id(self, db: FiligreeDB) -> None:
        parent = db.create_issue("Parent")
        child = db.create_issue("Child", parent_id=parent.id)
        updated = db.update_issue(child.id, parent_id="")
        assert updated.parent_id is None

    def test_update_parent_id_no_change_no_event(self, db: FiligreeDB) -> None:
        parent = db.create_issue("Parent")
        child = db.create_issue("Child", parent_id=parent.id)
        db.update_issue(child.id, parent_id=parent.id)
        events = db.conn.execute(
            "SELECT * FROM events WHERE issue_id = ? AND event_type = 'parent_changed'",
            (child.id,),
        ).fetchall()
        assert len(events) == 0


class TestClaimNextFilters:
    """M8: claim_next type_filter, priority_min, priority_max must filter correctly."""

    def test_type_filter_matches(self, db: FiligreeDB) -> None:
        """claim_next with type_filter only claims matching type."""
        db.create_issue("A task", type="task", priority=1)
        bug = db.create_issue("A bug", type="bug", priority=1)
        result = db.claim_next("agent", type_filter="bug")
        assert result is not None
        assert result.id == bug.id
        assert result.type == "bug"

    def test_type_filter_no_match_returns_none(self, db: FiligreeDB) -> None:
        db.create_issue("A task", type="task", priority=1)
        result = db.claim_next("agent", type_filter="bug")
        # bug type issues may exist from get_ready (e.g. no bugs created)
        # but our task should not match
        if result is not None:
            assert result.type == "bug"

    def test_priority_min_excludes_lower(self, db: FiligreeDB) -> None:
        """priority_min filters out issues with priority < min."""
        # Claim all pre-existing ready issues first
        for existing in db.get_ready():
            db.claim_issue(existing.id, assignee="setup")
        db.create_issue("Low priority", type="task", priority=0)
        p2 = db.create_issue("Medium priority", type="task", priority=2)
        result = db.claim_next("agent", priority_min=2)
        assert result is not None
        assert result.id == p2.id
        assert result.priority >= 2

    def test_priority_max_excludes_higher(self, db: FiligreeDB) -> None:
        """priority_max filters out issues with priority > max."""
        # Claim all pre-existing ready issues first
        for existing in db.get_ready():
            db.claim_issue(existing.id, assignee="setup")
        p0 = db.create_issue("Critical", type="task", priority=0)
        db.create_issue("Low", type="task", priority=3)
        result = db.claim_next("agent", priority_max=1)
        assert result is not None
        assert result.id == p0.id
        assert result.priority <= 1

    def test_priority_range_combined(self, db: FiligreeDB) -> None:
        """priority_min + priority_max together define a range."""
        for existing in db.get_ready():
            db.claim_issue(existing.id, assignee="setup")
        db.create_issue("P0 critical", type="task", priority=0)
        p2 = db.create_issue("P2 medium", type="task", priority=2)
        db.create_issue("P4 backlog", type="task", priority=4)
        result = db.claim_next("agent", priority_min=1, priority_max=3)
        assert result is not None
        assert result.id == p2.id
        assert 1 <= result.priority <= 3

    def test_type_and_priority_combined(self, db: FiligreeDB) -> None:
        """type_filter + priority filters work together."""
        for existing in db.get_ready():
            db.claim_issue(existing.id, assignee="setup")
        db.create_issue("Low bug", type="bug", priority=3)
        db.create_issue("High task", type="task", priority=0)
        high_bug = db.create_issue("High bug", type="bug", priority=1)
        result = db.claim_next("agent", type_filter="bug", priority_max=2)
        assert result is not None
        assert result.id == high_bug.id

    @pytest.mark.parametrize("method_name", ["claim_next", "start_next_work"])
    def test_equal_priority_and_created_at_tie_breaks_by_issue_id(self, db: FiligreeDB, method_name: str) -> None:
        """Ready selection is deterministic when priority and created_at tie."""
        for existing in db.get_ready():
            db.claim_issue(existing.id, assignee="setup")
        created_at = "2026-01-01T00:00:00+00:00"
        lower_id = f"{db.prefix}-atie000001"
        higher_id = f"{db.prefix}-ztie000001"
        for issue_id, title in ((higher_id, "Higher inserted first"), (lower_id, "Lower inserted second")):
            db.conn.execute(
                "INSERT INTO issues (id, title, status, priority, type, assignee, created_at, updated_at, description, notes, fields) "
                "VALUES (?, ?, 'open', 2, 'task', '', ?, ?, '', '', '{}')",
                (issue_id, title, created_at, created_at),
            )
        db.conn.commit()

        result = db.claim_next("agent") if method_name == "claim_next" else db.start_next_work(assignee="agent")

        assert result is not None
        assert result.id == lower_id


class TestClaimNextExhaustion:
    """Bug fix: filigree-2e5383 — claim_next logs when all candidates fail."""

    def test_claim_next_no_warning_when_no_candidates(self, db: FiligreeDB) -> None:
        """When no ready issues exist, claim_next returns None without warning."""
        # Claim all pre-existing ready issues (e.g. the Future release singleton)
        for existing in db.get_ready():
            db.claim_issue(existing.id, assignee="agent1")
        issue = db.create_issue("Target")
        db.claim_issue(issue.id, assignee="agent1")

        result = db.claim_next("agent2")
        assert result is None

    def test_claim_next_logs_on_claim_conflict_exhaustion(self, db: FiligreeDB) -> None:
        """When claim_issue raises ClaimConflictError for all candidates, warn about exhaustion."""
        db.create_issue("Target")

        # Simulate claim_issue always raising ClaimConflictError (claim race)
        with (
            patch.object(
                db,
                "claim_issue",
                side_effect=ClaimConflictError("test-race", observed="other", expected="agent2"),
            ),
            patch("filigree.db_issues.logger") as mock_logger,
        ):
            result = db.claim_next("agent2")

        assert result is None
        mock_logger.warning.assert_called_once()
        assert "failed to claim" in str(mock_logger.warning.call_args)

    def test_claim_next_skips_status_mismatch_candidate(self, db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
        """A claim-phase status mismatch means this ready candidate went stale."""
        stale = db.create_issue("Stale candidate", priority=0)
        survivor = db.create_issue("Survivor candidate", priority=1)
        real_claim_issue = db.claim_issue

        def status_mismatch_once(issue_id: str, *args: object, **kwargs: object) -> object:
            if issue_id == stale.id:
                raise ValueError(f"Cannot claim {issue_id}: status is 'closed', expected open-category state or wip-category handoff state")
            return real_claim_issue(issue_id, *args, **kwargs)

        monkeypatch.setattr(db, "claim_issue", status_mismatch_once)

        result = db.claim_next("agent2")

        assert result is not None
        assert result.id == survivor.id
        assert result.assignee == "agent2"

    def test_claim_next_propagates_validation_bug_from_claim_phase(self, db: FiligreeDB) -> None:
        """Bug-class ValueError from claim_issue must not be hidden as a race."""
        db.create_issue("Target")

        with (
            patch.object(db, "claim_issue", side_effect=ValueError("invariant exploded")),
            pytest.raises(ValueError, match="invariant exploded"),
        ):
            db.claim_next("agent2")

    def test_claim_next_propagates_invalid_transition_from_claim_phase(self, db: FiligreeDB) -> None:
        """InvalidTransitionError is a ValueError subclass, but it is not a claim race."""
        db.create_issue("Target")

        with (
            patch.object(db, "claim_issue", side_effect=InvalidTransitionError("task", "open")),
            pytest.raises(InvalidTransitionError),
        ):
            db.claim_next("agent2")

    def test_claim_next_skips_deleted_issue(self, db: FiligreeDB) -> None:
        """Bug filigree-e55da01144: KeyError from deleted issue must be caught, not propagated."""
        db.create_issue("Target")

        # Simulate claim_issue raising KeyError (issue deleted between get_ready and claim)
        with (
            patch.object(db, "claim_issue", side_effect=KeyError("Issue not found: test-abc")),
            patch("filigree.db_issues.logger") as mock_logger,
        ):
            result = db.claim_next("agent2")

        assert result is None
        mock_logger.warning.assert_called_once()
        assert "failed to claim" in str(mock_logger.warning.call_args)


class TestClaimRaceCondition:
    """Bug fix: filigree-be24de — claim_issue race condition."""

    def test_claim_then_second_agent_raises(self, db: FiligreeDB) -> None:
        """Claiming an issue already assigned to another agent raises ValueError."""
        issue = db.create_issue("Race target")
        db.claim_issue(issue.id, assignee="agent1")
        with pytest.raises(ValueError, match="already assigned to 'agent1'"):
            db.claim_issue(issue.id, assignee="agent2")

    def test_claim_self_reclaim_succeeds(self, db: FiligreeDB) -> None:
        """Re-claiming an issue you already own should succeed (idempotent)."""
        issue = db.create_issue("Self claim")
        db.claim_issue(issue.id, assignee="agent1")
        # Second claim by same agent should succeed
        result = db.claim_issue(issue.id, assignee="agent1")
        assert result.assignee == "agent1"

    def test_claim_nonexistent_raises_keyerror(self, db: FiligreeDB) -> None:
        """Claiming a nonexistent issue raises KeyError."""
        with pytest.raises(KeyError):
            db.claim_issue("test-nonexistent", assignee="agent1")

    def test_claim_non_open_raises(self, db: FiligreeDB) -> None:
        """Claiming an issue not in an open-category state raises ValueError."""
        issue = db.create_issue("Close first")
        db.close_issue(issue.id)
        with pytest.raises(ValueError, match="expected open-category state"):
            db.claim_issue(issue.id, assignee="agent1")


class TestCreateIssuePartialWriteRollback:
    """Bug fix: filigree-340ce9 — create_issue leaves partial writes on dep failure."""

    def test_invalid_deps_no_orphan_issue(self, db: FiligreeDB) -> None:
        """create_issue with invalid deps must not leave an orphaned issue row."""
        issues_before = len(db.list_issues())

        with pytest.raises(ValueError, match="Invalid dependency IDs"):
            db.create_issue("Orphan candidate", deps=["test-missing-dep"])

        # Force a commit to simulate MCP's long-lived connection
        db.conn.commit()

        issues_after = len(db.list_issues())
        assert issues_after == issues_before, (
            f"Expected {issues_before} issues, got {issues_after} — orphaned issue was committed after failed create_issue"
        )

    def test_invalid_deps_no_orphan_events(self, db: FiligreeDB) -> None:
        """create_issue with invalid deps must not leave orphaned events."""
        events_before = db.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

        with pytest.raises(ValueError, match="Invalid dependency IDs"):
            db.create_issue("Event orphan candidate", deps=["test-ghost"])

        # Force commit
        db.conn.commit()

        events_after = db.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert events_after == events_before, (
            f"Expected {events_before} events, got {events_after} — orphaned 'created' event was committed after failed create_issue"
        )

    def test_invalid_deps_no_orphan_labels(self, db: FiligreeDB) -> None:
        """create_issue with labels + invalid deps must not leave orphaned labels."""
        labels_before = db.conn.execute("SELECT COUNT(*) FROM labels").fetchone()[0]

        with pytest.raises(ValueError, match="Invalid dependency IDs"):
            db.create_issue("Label orphan", labels=["defect", "urgent"], deps=["test-missing"])

        db.conn.commit()

        labels_after = db.conn.execute("SELECT COUNT(*) FROM labels").fetchone()[0]
        assert labels_after == labels_before, (
            f"Expected {labels_before} labels, got {labels_after} — orphaned labels were committed after failed create_issue"
        )


class TestUpdateIssuePartialEventRollback:
    """Bug fix: filigree-1c0a33 — update_issue persists false events on validation failure."""

    def test_invalid_priority_no_orphan_title_event(self, db: FiligreeDB) -> None:
        """update_issue with valid title + invalid priority must not leave title_changed event."""
        issue = db.create_issue("Original title")
        events_before = db.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

        with pytest.raises(ValueError, match="Priority must be"):
            db.update_issue(issue.id, title="New title", priority=99)

        # Force commit to simulate MCP long-lived connection
        db.conn.commit()

        events_after = db.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert events_after == events_before, (
            f"Expected {events_before} events, got {events_after} — orphaned title_changed event was committed after failed update_issue"
        )

        # Title should remain unchanged
        refreshed = db.get_issue(issue.id)
        assert refreshed.title == "Original title"

    def test_circular_parent_no_orphan_events(self, db: FiligreeDB) -> None:
        """update_issue with valid title + circular parent must not leave orphaned events."""
        parent = db.create_issue("Parent")
        child = db.create_issue("Child", parent_id=parent.id)
        events_before = db.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

        with pytest.raises(ValueError, match="circular parent chain"):
            db.update_issue(parent.id, title="Renamed parent", parent_id=child.id)

        db.conn.commit()

        events_after = db.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert events_after == events_before, (
            f"Expected {events_before} events, got {events_after} — orphaned events committed after failed update_issue"
        )


# ---------------------------------------------------------------------------
# Export / Import (JSONL)
# ---------------------------------------------------------------------------


class TestExportJsonl:
    def test_export_populated(self, populated_db: PopulatedDB, tmp_path: Path) -> None:
        out = tmp_path / "export.jsonl"
        count = populated_db.db.export_jsonl(out)
        assert count > 0
        lines = out.read_text().strip().split("\n")
        assert len(lines) == count

    def test_export_record_types(self, populated_db: PopulatedDB, tmp_path: Path) -> None:
        out = tmp_path / "export.jsonl"
        populated_db.db.export_jsonl(out)
        types_seen = set()
        for line in out.read_text().strip().split("\n"):
            record = json.loads(line)
            types_seen.add(record["_type"])
        assert "issue" in types_seen
        assert "dependency" in types_seen
        assert "label" in types_seen
        assert "comment" in types_seen
        assert "event" in types_seen

    def test_export_issue_fields(self, populated_db: PopulatedDB, tmp_path: Path) -> None:
        out = tmp_path / "export.jsonl"
        populated_db.db.export_jsonl(out)
        issues = []
        for line in out.read_text().strip().split("\n"):
            record = json.loads(line)
            if record["_type"] == "issue":
                issues.append(record)
        assert len(issues) == 5  # Future release + epic + A + B + C
        assert any(i["title"] == "Issue A" for i in issues)

    def test_export_empty_db(self, db: FiligreeDB, tmp_path: Path) -> None:
        out = tmp_path / "export.jsonl"
        count = db.export_jsonl(out)
        # DB has the auto-seeded Future release singleton + its created event
        assert count >= 1
        lines = [line for line in out.read_text().strip().split("\n") if line]
        issues = [json.loads(line) for line in lines if json.loads(line).get("_type") == "issue"]
        assert len(issues) == 1
        assert issues[0]["title"] == "Future"


class TestImportJsonl:
    @staticmethod
    def _seed_file_domain(db: FiligreeDB, *, file_id: str = "test-f-1", finding_id: str = "test-sf-1") -> tuple[object, object]:
        issue = db.create_issue("Bug for file")
        file_rec = db.register_file("src/example.py", language="python", metadata={"owner": "core"})
        db.conn.execute("UPDATE file_records SET id = ? WHERE id = ?", (file_id, file_rec.id))
        db.conn.execute(
            "INSERT INTO scan_findings "
            "(id, file_id, issue_id, scan_source, rule_id, severity, status, message, suggestion, "
            "scan_run_id, line_start, line_end, seen_count, first_seen, updated_at, last_seen_at, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                finding_id,
                file_id,
                issue.id,
                "ruff",
                "F401",
                "medium",
                "open",
                "unused import",
                "",
                "run-1",
                10,
                10,
                1,
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                '{"source":"test"}',
            ),
        )
        db.conn.execute(
            "INSERT INTO file_associations (file_id, issue_id, assoc_type, created_at) VALUES (?, ?, ?, ?)",
            (file_id, issue.id, "bug_in", "2026-01-01T00:00:00+00:00"),
        )
        db.conn.execute(
            "INSERT INTO file_events (file_id, event_type, field, old_value, new_value, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (file_id, "file_metadata_update", "language", "", "python", "2026-01-01T00:00:00+00:00"),
        )
        db.conn.commit()
        return issue, db.get_file(file_id)

    def test_import_roundtrip(self, populated_db: PopulatedDB, tmp_path: Path) -> None:
        """Export from populated, import into fresh — counts should match."""
        out = tmp_path / "roundtrip.jsonl"
        export_count = populated_db.db.export_jsonl(out)

        fresh = FiligreeDB(tmp_path / "fresh.db", prefix="test")
        fresh.initialize()
        result = fresh.import_jsonl(out)
        assert result["count"] == export_count
        assert result["skipped_types"] == {}
        fresh.close()

    def test_import_issues_arrive(self, populated_db: PopulatedDB, tmp_path: Path) -> None:
        out = tmp_path / "data.jsonl"
        populated_db.db.export_jsonl(out)

        fresh = FiligreeDB(tmp_path / "fresh2.db", prefix="test")
        fresh.initialize()
        fresh.import_jsonl(out)
        issues = fresh.list_issues(limit=100)
        assert len(issues) == 5
        titles = {i.title for i in issues}
        assert "Issue A" in titles
        assert "Epic E" in titles
        fresh.close()

    def test_import_roundtrip_preserves_file_domain_rows(self, db: FiligreeDB, tmp_path: Path) -> None:
        issue, file_rec = self._seed_file_domain(db)

        out = tmp_path / "file-roundtrip.jsonl"
        export_count = db.export_jsonl(out)

        fresh = FiligreeDB(tmp_path / "fresh-file.db", prefix="test")
        fresh.initialize()
        result = fresh.import_jsonl(out)

        assert result["count"] == export_count
        assert fresh.get_file(file_rec.id).path == "src/example.py"
        finding = fresh.conn.execute("SELECT file_id, issue_id FROM scan_findings WHERE id = ?", ("test-sf-1",)).fetchone()
        assert finding["file_id"] == file_rec.id
        assert finding["issue_id"] == issue.id
        assert fresh.conn.execute("SELECT COUNT(*) FROM file_associations").fetchone()[0] == 1
        assert fresh.conn.execute("SELECT COUNT(*) FROM file_events").fetchone()[0] == 1
        fresh.close()

    def test_import_roundtrip_preserves_entity_associations(self, db: FiligreeDB, tmp_path: Path) -> None:
        issue = db.create_issue("Entity-bound issue")
        attached = db.add_entity_association(
            issue.id,
            "py:func:parser.tokenize",
            content_hash="hash-a",
            actor="alice",
        )

        out = tmp_path / "entity-associations.jsonl"
        export_count = db.export_jsonl(out)

        records = [json.loads(line) for line in out.read_text().splitlines() if line]
        entity_records = [record for record in records if record.get("_type") == "entity_association"]
        assert entity_records == [
            {
                "_type": "entity_association",
                "issue_id": issue.id,
                "loomweave_entity_id": "py:func:parser.tokenize",
                "content_hash_at_attach": "hash-a",
                "attached_at": attached["attached_at"],
                "attached_by": "alice",
                "migration_orphaned_at": None,
                "entity_kind": "",
                "signature": None,
                "signoff_seq": None,
            }
        ]

        fresh = FiligreeDB(tmp_path / "fresh-entity-associations.db", prefix="test")
        fresh.initialize()
        result = fresh.import_jsonl(out)

        rows = fresh.list_entity_associations(issue.id)
        assert result["count"] == export_count
        assert rows == [attached]
        fresh.close()

    def test_import_rejects_foreign_entity_association_issue_ids(self, db: FiligreeDB, tmp_path: Path) -> None:
        jsonl = tmp_path / "foreign-entity-association.jsonl"
        jsonl.write_text(
            json.dumps(
                {
                    "_type": "entity_association",
                    "issue_id": "other-1234567890",
                    "loomweave_entity_id": "py:func:parser.tokenize",
                    "content_hash_at_attach": "hash-a",
                    "attached_at": "2026-01-01T00:00:00+00:00",
                    "attached_by": "alice",
                }
            )
            + "\n"
        )

        with pytest.raises(WrongProjectError):
            db.import_jsonl(jsonl, merge=True)

    def test_import_preserves_file_registry_metadata(self, tmp_path: Path) -> None:
        jsonl = tmp_path / "loomweave-file.jsonl"
        jsonl.write_text(
            json.dumps(
                {
                    "_type": "file_record",
                    "id": "core:file:src/imported.py",
                    "path": "src/imported.py",
                    "language": "python",
                    "file_type": "source",
                    "content_hash": "sha256:imported",
                    "registry_backend": "loomweave",
                    "first_seen": "2026-01-01T00:00:00+00:00",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                    "metadata": {"owner": "loomweave"},
                }
            )
            + "\n"
        )

        fresh = FiligreeDB(tmp_path / "fresh-loomweave-file.db", prefix="test")
        fresh.initialize()
        fresh.import_jsonl(jsonl)

        row = fresh.conn.execute(
            "SELECT id, content_hash, registry_backend, metadata FROM file_records WHERE path = ?",
            ("src/imported.py",),
        ).fetchone()
        assert dict(row) == {
            "id": "core:file:src/imported.py",
            "content_hash": "sha256:imported",
            "registry_backend": "loomweave",
            "metadata": '{"owner": "loomweave"}',
        }
        fresh.close()

    def test_import_rejects_invalid_file_registry_backend(self, db: FiligreeDB, tmp_path: Path) -> None:
        jsonl = tmp_path / "bad-file-registry.jsonl"
        jsonl.write_text(
            json.dumps(
                {
                    "_type": "file_record",
                    "id": "test-file-bad-registry",
                    "path": "src/bad_registry.py",
                    "registry_backend": "remote",
                    "first_seen": "2026-01-01T00:00:00+00:00",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                }
            )
            + "\n"
        )

        with pytest.raises(ValueError, match="Invalid registry_backend"):
            db.import_jsonl(jsonl)

    @pytest.mark.parametrize("path", ["/etc/passwd", "../outside.py", "src/../../outside.py"])
    def test_import_rejects_non_project_relative_file_path(self, db: FiligreeDB, tmp_path: Path, path: str) -> None:
        jsonl = tmp_path / "bad-file-path.jsonl"
        jsonl.write_text(
            json.dumps(
                {
                    "_type": "file_record",
                    "id": "test-file-bad-path",
                    "path": path,
                    "first_seen": "2026-01-01T00:00:00+00:00",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                }
            )
            + "\n"
        )

        with pytest.raises(ValueError, match="project-relative"):
            db.import_jsonl(jsonl)

        assert db.conn.execute("SELECT COUNT(*) FROM file_records").fetchone()[0] == 0

    def test_import_normalizes_file_path_before_merge(self, db: FiligreeDB, tmp_path: Path) -> None:
        existing = db.register_file("src/bar.py")
        jsonl = tmp_path / "normalized-file-path.jsonl"
        jsonl.write_text(
            json.dumps(
                {
                    "_type": "file_record",
                    "id": "test-file-imported-bar",
                    "path": "src/foo/../bar.py",
                    "language": "python",
                    "first_seen": "2026-01-01T00:00:00+00:00",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                }
            )
            + "\n"
        )

        result = db.import_jsonl(jsonl, merge=True)

        assert result["count"] == 0
        rows = db.conn.execute("SELECT id, path FROM file_records").fetchall()
        assert [dict(row) for row in rows] == [{"id": existing.id, "path": "src/bar.py"}]

    def test_import_legacy_file_record_defaults_registry_metadata(self, db: FiligreeDB, tmp_path: Path) -> None:
        jsonl = tmp_path / "legacy-file-record.jsonl"
        jsonl.write_text(
            json.dumps(
                {
                    "_type": "file_record",
                    "id": "test-file-legacy",
                    "path": "src/legacy.py",
                    "first_seen": "2026-01-01T00:00:00+00:00",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                }
            )
            + "\n"
        )

        db.import_jsonl(jsonl)

        row = db.conn.execute(
            "SELECT content_hash, registry_backend FROM file_records WHERE id = ?",
            ("test-file-legacy",),
        ).fetchone()
        assert dict(row) == {"content_hash": "", "registry_backend": "local"}

    def test_import_roundtrip_reconciles_seeded_future_singleton(self, db: FiligreeDB, tmp_path: Path) -> None:
        out = tmp_path / "future-roundtrip.jsonl"
        source_future = db.conn.execute(
            "SELECT id FROM issues WHERE type = 'release' AND json_extract(fields, '$.version') = 'Future'"
        ).fetchone()["id"]
        db.export_jsonl(out)

        fresh = FiligreeDB(tmp_path / "fresh-future.db", prefix="test")
        fresh.initialize()
        seeded_future = fresh.conn.execute(
            "SELECT id FROM issues WHERE type = 'release' AND json_extract(fields, '$.version') = 'Future'"
        ).fetchone()["id"]
        assert seeded_future != source_future

        fresh.import_jsonl(out)
        future_rows = fresh.conn.execute(
            "SELECT id FROM issues WHERE type = 'release' AND json_extract(fields, '$.version') = 'Future'"
        ).fetchall()

        assert len(future_rows) == 1
        assert future_rows[0]["id"] == source_future
        fresh.close()

    def test_import_dependencies_arrive(self, populated_db: PopulatedDB, tmp_path: Path) -> None:
        out = tmp_path / "data.jsonl"
        populated_db.db.export_jsonl(out)

        fresh = FiligreeDB(tmp_path / "fresh3.db", prefix="test")
        fresh.initialize()
        fresh.import_jsonl(out)
        deps = fresh.get_all_dependencies()
        assert len(deps) >= 1
        fresh.close()

    def test_import_roundtrip_with_parent_link_added_after_creation(self, db: FiligreeDB, tmp_path: Path) -> None:
        """Late-added parent links should round-trip even if child was created first."""
        child = db.create_issue("Child created first")
        parent = db.create_issue("Parent created later")
        db.update_issue(child.id, parent_id=parent.id, actor="tester")
        dependent = db.create_issue("Dependent")
        db.add_dependency(dependent.id, child.id, actor="tester")
        db.add_comment(child.id, "linked after creation", author="alice")

        out = tmp_path / "late-parent.jsonl"
        export_count = db.export_jsonl(out)

        fresh = FiligreeDB(tmp_path / "fresh-late-parent.db", prefix="test")
        fresh.initialize()
        result = fresh.import_jsonl(out)

        assert result["count"] == export_count
        assert fresh.get_issue(child.id).parent_id == parent.id
        assert any(comment["text"] == "linked after creation" for comment in fresh.get_comments(child.id))
        assert any(dep["from"] == dependent.id and dep["to"] == child.id for dep in fresh.get_all_dependencies())
        fresh.close()

    def test_import_merge_skips_existing(self, populated_db: PopulatedDB, tmp_path: Path) -> None:
        out = tmp_path / "merge.jsonl"
        populated_db.db.export_jsonl(out)
        # Import twice with merge — second import should not fail
        result2 = populated_db.db.import_jsonl(out, merge=True)
        # All records skipped since they already exist (issues by PK, deps by PK, labels by PK)
        # Events don't have PK constraint so they get duplicated
        assert result2["count"] >= 0

    def test_import_without_merge_fails_on_conflict(self, populated_db: PopulatedDB, tmp_path: Path) -> None:
        out = tmp_path / "conflict.jsonl"
        populated_db.db.export_jsonl(out)
        with pytest.raises(sqlite3.IntegrityError):
            populated_db.db.import_jsonl(out, merge=False)

    def test_import_event_uses_conflict_variable(self, db: FiligreeDB, tmp_path: Path) -> None:
        """Bug filigree-769ea4: event branch must respect merge flag, not hardcode OR IGNORE."""
        # Create an issue first so the event FK is valid
        issue = db.create_issue("Event test")

        # Write a JSONL file with one event
        jsonl = tmp_path / "events.jsonl"
        event_line = json.dumps(
            {
                "_type": "event",
                "issue_id": issue.id,
                "event_type": "status_change",
                "actor": "alice",
                "old_value": "open",
                "new_value": "closed",
                "comment": "",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        )
        jsonl.write_text(event_line + "\n")

        # First import succeeds
        result1 = db.import_jsonl(jsonl)
        assert result1["count"] == 1

        # Second import with merge=False should ABORT on the duplicate event
        with pytest.raises(sqlite3.IntegrityError):
            db.import_jsonl(jsonl, merge=False)

    def test_import_merge_event_count_accurate(self, db: FiligreeDB, tmp_path: Path) -> None:
        """Bug filigree-769ea4: merge=True must not count skipped events."""
        issue = db.create_issue("Count test")

        jsonl = tmp_path / "events.jsonl"
        event_line = json.dumps(
            {
                "_type": "event",
                "issue_id": issue.id,
                "event_type": "status_change",
                "actor": "alice",
                "old_value": "open",
                "new_value": "closed",
                "comment": "",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        )
        jsonl.write_text(event_line + "\n")

        # Import once
        db.import_jsonl(jsonl, merge=True)

        # Import again with merge — duplicate event should be skipped, count=0
        result2 = db.import_jsonl(jsonl, merge=True)
        assert result2["count"] == 0, f"Expected 0 (duplicate skipped), got {result2['count']}"

    def test_import_merge_comment_count_accurate(self, db: FiligreeDB, tmp_path: Path) -> None:
        issue = db.create_issue("Comment count test")

        jsonl = tmp_path / "comments.jsonl"
        comment_line = json.dumps(
            {
                "_type": "comment",
                "issue_id": issue.id,
                "author": "alice",
                "text": "hello",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        )
        jsonl.write_text(comment_line + "\n")

        db.import_jsonl(jsonl, merge=True)
        result2 = db.import_jsonl(jsonl, merge=True)
        assert result2["count"] == 0
        assert len(db.get_comments(issue.id)) == 1

    def test_import_merge_comment_no_created_at_consistent_timestamp(self, db: FiligreeDB, tmp_path: Path) -> None:
        """Bug filigree-ed8962ce80: _now_iso() default must be called once per record so
        the INSERT value matches the WHERE NOT EXISTS check."""
        from unittest.mock import patch

        issue = db.create_issue("Timestamp dedup test")

        jsonl = tmp_path / "comments-no-ts.jsonl"
        comment_line = json.dumps({"_type": "comment", "issue_id": issue.id, "author": "bob", "text": "hi"})
        jsonl.write_text(comment_line + "\n")

        # Mock _now_iso to return different values on each call — simulates the
        # clock advancing between the two record.get() defaults in old code
        call_count = 0

        def _advancing_clock() -> str:
            nonlocal call_count
            call_count += 1
            return f"2026-01-01T00:00:{call_count:02d}+00:00"

        with patch("filigree.db_meta._now_iso", side_effect=_advancing_clock):
            db.import_jsonl(jsonl, merge=True)

        # With the fix, _now_iso is called exactly once per record (not twice)
        assert call_count == 1, f"Expected 1 _now_iso call (fix applied), got {call_count}"

        comments = db.get_comments(issue.id)
        assert len(comments) == 1

    def test_import_merge_file_event_no_created_at_consistent_timestamp(self, db: FiligreeDB, tmp_path: Path) -> None:
        """Bug filigree-ed8962ce80: same _now_iso() single-call fix for file_events merge path."""
        from unittest.mock import patch

        frec = db.register_file("src/test.py", language="python")

        jsonl = tmp_path / "file-events-no-ts.jsonl"
        # Include the file_record so import can build the file_id_map
        file_line = json.dumps({"_type": "file_record", "id": frec.id, "path": "src/test.py", "language": "python"})
        event_line = json.dumps(
            {
                "_type": "file_event",
                "file_id": frec.id,
                "event_type": "file_metadata_update",
                "field": "language",
                "old_value": "",
                "new_value": "python",
            }
        )
        jsonl.write_text(file_line + "\n" + event_line + "\n")

        total_calls = 0

        def _advancing_clock() -> str:
            nonlocal total_calls
            total_calls += 1
            return f"2026-01-01T00:00:{total_calls:02d}+00:00"

        with patch("filigree.db_meta._now_iso", side_effect=_advancing_clock):
            db.import_jsonl(jsonl, merge=True)

        # Existing file_record merge maps by normalized path before insert; the
        # file_event merge should use exactly one timestamp call.
        assert total_calls == 1, f"Expected 1 _now_iso call for file_event merge, got {total_calls}"

    def test_import_merge_file_domain_count_accurate(self, db: FiligreeDB, tmp_path: Path) -> None:
        self._seed_file_domain(db)
        out = tmp_path / "file-merge.jsonl"
        db.export_jsonl(out)

        result2 = db.import_jsonl(out, merge=True)
        assert result2["count"] == 0
        assert db.conn.execute("SELECT COUNT(*) FROM file_records").fetchone()[0] == 1
        assert db.conn.execute("SELECT COUNT(*) FROM scan_findings").fetchone()[0] == 1
        assert db.conn.execute("SELECT COUNT(*) FROM file_associations").fetchone()[0] == 1
        assert db.conn.execute("SELECT COUNT(*) FROM file_events").fetchone()[0] == 1

    def test_import_cross_prefix_rejects_by_default(self, tmp_path: Path) -> None:
        """Regression filigree-bac0797445: by default, importing a JSONL whose
        issue IDs use a foreign prefix must fail fast rather than silently
        creating unwritable rows. Every guarded write method would later
        raise WrongProjectError on those IDs, effectively corrupting the
        destination DB.
        """
        from filigree.core import WrongProjectError

        source = FiligreeDB(tmp_path / "source-xfail.db", prefix="src")
        source.initialize()
        source.create_issue("cross-prefix seed")
        out = tmp_path / "cross-prefix-reject.jsonl"
        source.export_jsonl(out)
        source.close()

        fresh = FiligreeDB(tmp_path / "dest-xfail.db", prefix="dst")
        fresh.initialize()
        with pytest.raises(WrongProjectError, match=r"src|dst"):
            fresh.import_jsonl(out, merge=True)
        # No foreign-prefixed rows should have been inserted — the import
        # aborted before touching destination data. (The auto-seeded
        # ``Future`` release singleton uses the dst prefix and is ignored.)
        foreign_rows = fresh.conn.execute("SELECT COUNT(*) FROM issues WHERE id NOT LIKE ?", (f"{fresh.prefix}-%",)).fetchone()[0]
        assert foreign_rows == 0
        fresh.close()

    def test_import_rejects_foreign_prefix_that_extends_local_prefix(self, tmp_path: Path) -> None:
        from filigree.core import WrongProjectError

        fresh = FiligreeDB(tmp_path / "dest-prefix-extension.db", prefix="a")
        fresh.initialize()

        jsonl = tmp_path / "prefix-extension.jsonl"
        jsonl.write_text(
            json.dumps(
                {
                    "_type": "issue",
                    "id": "a-b-1234567890",
                    "title": "foreign prefix extension",
                    "status": "open",
                    "type": "task",
                    "fields": "{}",
                }
            )
            + "\n"
        )

        with pytest.raises(WrongProjectError, match=r"a-b-1234567890"):
            fresh.import_jsonl(jsonl, merge=True)

        leaked = fresh.conn.execute("SELECT COUNT(*) FROM issues WHERE id = ?", ("a-b-1234567890",)).fetchone()[0]
        assert leaked == 0
        fresh.close()

    def test_import_cross_prefix_rejects_observation_source_issue_id(self, tmp_path: Path) -> None:
        """filigree-b5c304cc75: foreign-prefix preflight must catch
        observation.source_issue_id, not just issue/event/etc IDs. Otherwise
        a foreign source_issue_id slips into observations and the row sits
        as readable-but-unwritable through prefix-guarded write paths."""
        from filigree.core import WrongProjectError

        fresh = FiligreeDB(tmp_path / "dest-obs-foreign.db", prefix="dst")
        fresh.initialize()

        bundle = (
            json.dumps(
                {
                    "_type": "observation",
                    "id": "obs-foreign",
                    "summary": "from a foreign project",
                    "detail": "",
                    "file_path": "",
                    "priority": 3,
                    "actor": "import",
                    "source_issue_id": "src-abc1234567",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "expires_at": "2030-01-01T00:00:00+00:00",
                }
            )
            + "\n"
        )
        path = tmp_path / "obs-foreign.jsonl"
        path.write_text(bundle)

        with pytest.raises(WrongProjectError, match=r"src-abc1234567"):
            fresh.import_jsonl(path, merge=True)

        # No observation row should have leaked through.
        leaked = fresh.conn.execute("SELECT COUNT(*) FROM observations WHERE id = ?", ("obs-foreign",)).fetchone()[0]
        assert leaked == 0
        fresh.close()

    def test_import_future_reconcile_ignores_corrupt_release_fields(self, db: FiligreeDB, tmp_path: Path) -> None:
        db.conn.execute(
            "INSERT INTO issues (id, title, status, priority, type, assignee, "
            "created_at, updated_at, description, notes, fields) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "test-corruptrel",
                "Corrupt release",
                "planning",
                4,
                "release",
                "",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                "",
                "",
                "not-json",
            ),
        )
        db.conn.commit()

        jsonl = tmp_path / "future-with-corrupt-existing.jsonl"
        jsonl.write_text(
            json.dumps(
                {
                    "_type": "issue",
                    "id": "test-importfuture",
                    "title": "Future",
                    "type": "release",
                    "status": "planning",
                    "priority": 4,
                    "fields": '{"version": "Future"}',
                }
            )
            + "\n"
        )

        db.import_jsonl(jsonl, merge=True)

        assert db.conn.execute("SELECT id FROM issues WHERE id = ?", ("test-importfuture",)).fetchone() is not None

    def test_import_rejects_foreign_closeout_ack_when_target_type_missing(self, db: FiligreeDB, tmp_path: Path) -> None:
        from filigree.core import WrongProjectError

        jsonl = tmp_path / "foreign-closeout-ack.jsonl"
        lines = [
            {
                "_type": "annotation",
                "id": "ann-foreign-ack",
                "file_path": "src.py",
                "note": "imported annotation",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
            },
            {
                "_type": "annotation_closeout_acknowledgement",
                "annotation_id": "ann-foreign-ack",
                "target_id": "src-foreignissue",
                "carried_to_target_id": "src-carriedissue",
                "actor": "import",
                "acknowledged_at": "2026-01-01T00:00:00+00:00",
            },
        ]
        jsonl.write_text("\n".join(json.dumps(line) for line in lines) + "\n")

        with pytest.raises(WrongProjectError, match=r"src-foreignissue"):
            db.import_jsonl(jsonl, merge=True)

        leaked = db.conn.execute(
            "SELECT COUNT(*) FROM annotation_closeout_acknowledgements WHERE target_id = ?",
            ("src-foreignissue",),
        ).fetchone()[0]
        assert leaked == 0

    def test_import_preserves_annotation_provenance_json_list_text(self, db: FiligreeDB, tmp_path: Path) -> None:
        jsonl = tmp_path / "annotation-provenance.jsonl"
        lines = [
            {
                "_type": "annotation",
                "id": "ann-provenance",
                "file_path": "src.py",
                "note": "imported annotation",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
            },
            {
                "_type": "annotation_provenance",
                "annotation_id": "ann-provenance",
                "provenance_flags": '["generated_file"]',
                "provenance_warnings": '["anchor shifted"]',
            },
        ]
        jsonl.write_text("\n".join(json.dumps(line) for line in lines) + "\n")

        db.import_jsonl(jsonl, merge=True)

        annotation = db.get_annotation("ann-provenance")
        assert annotation["provenance"]["provenance_flags"] == ["generated_file"]
        assert annotation["provenance"]["provenance_warnings"] == ["anchor shifted"]

    def test_import_merge_reconciles_file_ids_by_path(self, tmp_path: Path) -> None:
        source = FiligreeDB(tmp_path / "source.db", prefix="src")
        source.initialize()
        src_issue, _src_file = self._seed_file_domain(source, file_id="src-f1", finding_id="src-sf1")
        out = tmp_path / "merge-by-path.jsonl"
        source.export_jsonl(out)
        source.close()

        fresh = FiligreeDB(tmp_path / "dest.db", prefix="dst")
        fresh.initialize()
        dst_file = fresh.register_file("src/example.py", language="python")
        # Migration tools that deliberately preserve source IDs must opt in.
        fresh.import_jsonl(out, merge=True, allow_foreign_ids=True)

        assert fresh.conn.execute("SELECT COUNT(*) FROM file_records WHERE path = ?", ("src/example.py",)).fetchone()[0] == 1
        finding = fresh.conn.execute("SELECT file_id, issue_id FROM scan_findings WHERE id = ?", ("src-sf1",)).fetchone()
        assert finding["file_id"] == dst_file.id
        assert finding["issue_id"] == src_issue.id
        assoc = fresh.conn.execute(
            "SELECT file_id, issue_id FROM file_associations WHERE issue_id = ?",
            (src_issue.id,),
        ).fetchone()
        assert assoc["file_id"] == dst_file.id
        file_event = fresh.conn.execute(
            "SELECT file_id FROM file_events WHERE event_type = 'file_metadata_update' AND field = 'language'"
        ).fetchone()
        assert file_event["file_id"] == dst_file.id
        fresh.close()

    def test_import_merge_remaps_scan_run_file_ids_by_path(self, tmp_path: Path) -> None:
        source = FiligreeDB(tmp_path / "source-scan-run.db", prefix="src")
        source.initialize()
        self._seed_file_domain(source, file_id="src-f1", finding_id="src-sf1")
        source.conn.execute(
            "INSERT INTO scan_runs "
            "(id, scanner_name, scan_source, status, file_paths, file_ids, started_at, updated_at, findings_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "run-src-remap",
                "ruff",
                "ci",
                "completed",
                '["src/example.py"]',
                '["src-f1"]',
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                1,
            ),
        )
        source.conn.commit()
        out = tmp_path / "scan-run-remap.jsonl"
        source.export_jsonl(out)
        source.close()

        fresh = FiligreeDB(tmp_path / "dest-scan-run.db", prefix="dst")
        fresh.initialize()
        dst_file = fresh.register_file("src/example.py", language="python")
        fresh.import_jsonl(out, merge=True, allow_foreign_ids=True)

        row = fresh.conn.execute("SELECT file_ids FROM scan_runs WHERE id = ?", ("run-src-remap",)).fetchone()
        assert json.loads(row["file_ids"]) == [dst_file.id]
        fresh.close()

    def test_import_merge_remaps_finding_annotation_link_by_dedup_key(self, tmp_path: Path) -> None:
        source = FiligreeDB(tmp_path / "source-finding-link.db", prefix="src")
        source.initialize()
        self._seed_file_domain(source, file_id="src-f1", finding_id="src-sf1")
        now = "2026-01-01T00:00:00+00:00"
        source.conn.execute(
            "INSERT INTO annotations (id, file_id, file_path, note, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("src-ann-finding", "src-f1", "src/example.py", "linked to finding", now, now),
        )
        source.conn.execute(
            "INSERT INTO annotation_links "
            "(id, annotation_id, target_type, target_id, relationship, actor, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("src-annlink-finding", "src-ann-finding", "finding", "src-sf1", "evidence_for", "import", now),
        )
        source.conn.commit()
        out = tmp_path / "finding-link-remap.jsonl"
        source.export_jsonl(out)
        source.close()

        fresh = FiligreeDB(tmp_path / "dest-finding-link.db", prefix="dst")
        fresh.initialize()
        self._seed_file_domain(fresh, file_id="dst-f1", finding_id="dst-sf1")
        fresh.import_jsonl(out, merge=True, allow_foreign_ids=True)

        link = fresh.conn.execute(
            "SELECT target_id FROM annotation_links WHERE id = ?",
            ("src-annlink-finding",),
        ).fetchone()
        assert link["target_id"] == "dst-sf1"
        assert fresh.conn.execute("SELECT COUNT(*) FROM scan_findings WHERE id = ?", ("src-sf1",)).fetchone()[0] == 0
        fresh.close()

    def test_import_skips_unknown_types_and_reports_them(self, db: FiligreeDB, tmp_path: Path) -> None:
        jsonl = tmp_path / "unknown.jsonl"
        jsonl.write_text('{"_type": "alien", "data": "hello"}\n')
        result = db.import_jsonl(jsonl)
        assert result["count"] == 0
        assert result["skipped_types"] == {"alien": 1}

    def test_import_reports_multiple_unknown_types(self, db: FiligreeDB, tmp_path: Path) -> None:
        jsonl = tmp_path / "multi-unknown.jsonl"
        lines = [
            '{"_type": "alien", "data": "a"}',
            '{"_type": "alien", "data": "b"}',
            '{"_type": "ghost", "data": "c"}',
            '{"data": "no type field"}',
        ]
        jsonl.write_text("\n".join(lines) + "\n")
        result = db.import_jsonl(jsonl)
        assert result["count"] == 0
        assert result["skipped_types"] == {"alien": 2, "ghost": 1, "<missing>": 1}

    def test_import_rejects_dangling_parent_id(self, db: FiligreeDB, tmp_path: Path) -> None:
        """Bug filigree-832676c507: import_jsonl must reject parent_id referencing non-existent issue."""
        jsonl = tmp_path / "dangling_parent.jsonl"
        lines = [
            json.dumps(
                {
                    "_type": "issue",
                    "id": "test-child001",
                    "title": "Child with bad parent",
                    "parent_id": "test-nonex999",
                }
            ),
        ]
        jsonl.write_text("\n".join(lines) + "\n")
        with pytest.raises(ValueError, match="parent_id"):
            db.import_jsonl(jsonl)

    def test_import_rejects_self_parented_issue(self, db: FiligreeDB, tmp_path: Path) -> None:
        jsonl = tmp_path / "self_parent.jsonl"
        lines = [
            json.dumps(
                {
                    "_type": "issue",
                    "id": "test-selfparent",
                    "title": "Self-parented import",
                    "parent_id": "test-selfparent",
                }
            ),
        ]
        jsonl.write_text("\n".join(lines) + "\n")

        with pytest.raises(ValueError, match="own parent"):
            db.import_jsonl(jsonl)

    def test_import_rejects_circular_parent_chain(self, db: FiligreeDB, tmp_path: Path) -> None:
        jsonl = tmp_path / "circular_parent.jsonl"
        lines = [
            json.dumps(
                {
                    "_type": "issue",
                    "id": "test-grandparent",
                    "title": "Grandparent",
                    "parent_id": "test-child",
                }
            ),
            json.dumps(
                {
                    "_type": "issue",
                    "id": "test-parent",
                    "title": "Parent",
                    "parent_id": "test-grandparent",
                }
            ),
            json.dumps(
                {
                    "_type": "issue",
                    "id": "test-child",
                    "title": "Child",
                    "parent_id": "test-parent",
                }
            ),
        ]
        jsonl.write_text("\n".join(lines) + "\n")

        with pytest.raises(ValueError, match="circular parent chain"):
            db.import_jsonl(jsonl)

    def test_import_valid_parent_id_succeeds(self, db: FiligreeDB, tmp_path: Path) -> None:
        """Parent references to issues in the same import should work."""
        jsonl = tmp_path / "valid_parent.jsonl"
        lines = [
            json.dumps(
                {
                    "_type": "issue",
                    "id": "test-parent001",
                    "title": "Parent",
                }
            ),
            json.dumps(
                {
                    "_type": "issue",
                    "id": "test-child001",
                    "title": "Child",
                    "parent_id": "test-parent001",
                }
            ),
        ]
        jsonl.write_text("\n".join(lines) + "\n")
        result = db.import_jsonl(jsonl)
        assert result["count"] == 2
        child = db.get_issue("test-child001")
        assert child.parent_id == "test-parent001"

    def test_import_parent_id_referencing_existing_db_issue(self, db: FiligreeDB, tmp_path: Path) -> None:
        """Parent references to issues already in the DB should work."""
        parent = db.create_issue("Existing parent")
        jsonl = tmp_path / "existing_parent.jsonl"
        lines = [
            json.dumps(
                {
                    "_type": "issue",
                    "id": "test-child002",
                    "title": "Child referencing existing parent",
                    "parent_id": parent.id,
                }
            ),
        ]
        jsonl.write_text("\n".join(lines) + "\n")
        result = db.import_jsonl(jsonl)
        assert result["count"] == 1
        child = db.get_issue("test-child002")
        assert child.parent_id == parent.id

    def test_import_rejects_non_integer_issue_priority_before_write(self, db: FiligreeDB, tmp_path: Path) -> None:
        """import_jsonl must not persist issue rows the Issue model cannot hydrate."""
        bad_id = "test-importbadprio"
        jsonl = tmp_path / "bad_issue_priority.jsonl"
        jsonl.write_text(
            json.dumps(
                {
                    "_type": "issue",
                    "id": bad_id,
                    "title": "Bad priority",
                    "priority": 2.5,
                }
            )
            + "\n"
        )

        with pytest.raises(ValueError, match="Priority must be an integer"):
            db.import_jsonl(jsonl)

        leaked = db.conn.execute("SELECT COUNT(*) FROM issues WHERE id = ?", (bad_id,)).fetchone()[0]
        assert leaked == 0

    def test_import_rejects_invalid_issue_status_before_write(self, db: FiligreeDB, tmp_path: Path) -> None:
        """import_jsonl must validate issue status against the issue type template."""
        bad_id = "test-importbadstatus"
        jsonl = tmp_path / "bad_issue_status.jsonl"
        jsonl.write_text(
            json.dumps(
                {
                    "_type": "issue",
                    "id": bad_id,
                    "title": "Bad status",
                    "status": "teleported",
                    "type": "task",
                }
            )
            + "\n"
        )

        with pytest.raises(ValueError, match="Invalid status"):
            db.import_jsonl(jsonl)

        leaked = db.conn.execute("SELECT COUNT(*) FROM issues WHERE id = ?", (bad_id,)).fetchone()[0]
        assert leaked == 0

    def test_import_skips_blank_lines(self, db: FiligreeDB, tmp_path: Path) -> None:
        jsonl = tmp_path / "blanks.jsonl"
        jsonl.write_text('\n\n{"_type": "issue", "id": "test-aaa111", "title": "Blank test"}\n\n')
        result = db.import_jsonl(jsonl)
        assert result["count"] == 1

    def test_import_rejects_self_dependency_and_rolls_back_issues(self, db: FiligreeDB, tmp_path: Path) -> None:
        """Dependency import must preserve the normal self-edge guard and rollback prior inserts."""
        issue_id = "test-importselfdep"
        jsonl = tmp_path / "self_dependency.jsonl"
        lines = [
            {"_type": "issue", "id": issue_id, "title": "Self dependency"},
            {"_type": "dependency", "issue_id": issue_id, "depends_on_id": issue_id},
        ]
        jsonl.write_text("\n".join(json.dumps(line) for line in lines) + "\n")

        with pytest.raises(ValueError, match="self-dependency"):
            db.import_jsonl(jsonl)

        leaked_issues = db.conn.execute("SELECT COUNT(*) FROM issues WHERE id = ?", (issue_id,)).fetchone()[0]
        leaked_deps = db.conn.execute("SELECT COUNT(*) FROM dependencies WHERE issue_id = ?", (issue_id,)).fetchone()[0]
        assert leaked_issues == 0
        assert leaked_deps == 0

    def test_import_rejects_dependency_cycle_and_rolls_back_import(self, db: FiligreeDB, tmp_path: Path) -> None:
        """Dependency import must not bypass graph cycle detection."""
        first_id = "test-importcyclea"
        second_id = "test-importcycleb"
        jsonl = tmp_path / "cycle_dependency.jsonl"
        lines = [
            {"_type": "issue", "id": first_id, "title": "First"},
            {"_type": "issue", "id": second_id, "title": "Second"},
            {"_type": "dependency", "issue_id": first_id, "depends_on_id": second_id},
            {"_type": "dependency", "issue_id": second_id, "depends_on_id": first_id},
        ]
        jsonl.write_text("\n".join(json.dumps(line) for line in lines) + "\n")

        with pytest.raises(ValueError, match="would create a cycle"):
            db.import_jsonl(jsonl)

        leaked_issues = db.conn.execute("SELECT COUNT(*) FROM issues WHERE id IN (?, ?)", (first_id, second_id)).fetchone()[0]
        leaked_deps = db.conn.execute(
            "SELECT COUNT(*) FROM dependencies WHERE issue_id IN (?, ?)",
            (first_id, second_id),
        ).fetchone()[0]
        assert leaked_issues == 0
        assert leaked_deps == 0

    def test_import_rejects_invalid_scan_finding_severity(self, db: FiligreeDB, tmp_path: Path) -> None:
        """import_jsonl must reject scan_findings with invalid severity values."""
        fr = db.register_file("src/test.py")
        jsonl = tmp_path / "bad_severity.jsonl"
        lines = [
            json.dumps({"_type": "file_record", "id": fr.id, "path": "src/test.py"}),
            json.dumps(
                {
                    "_type": "scan_finding",
                    "id": "sf-bad-sev",
                    "file_id": fr.id,
                    "scan_source": "test",
                    "rule_id": "R1",
                    "severity": "banana",
                    "status": "open",
                    "message": "test finding",
                }
            ),
        ]
        jsonl.write_text("\n".join(lines) + "\n")
        with pytest.raises(ValueError, match="severity"):
            db.import_jsonl(jsonl, merge=True)

    def test_import_rejects_invalid_scan_finding_status(self, db: FiligreeDB, tmp_path: Path) -> None:
        """import_jsonl must reject scan_findings with invalid finding status values."""
        fr = db.register_file("src/test2.py")
        jsonl = tmp_path / "bad_status.jsonl"
        lines = [
            json.dumps({"_type": "file_record", "id": fr.id, "path": "src/test2.py"}),
            json.dumps(
                {
                    "_type": "scan_finding",
                    "id": "sf-bad-status",
                    "file_id": fr.id,
                    "scan_source": "test",
                    "rule_id": "R2",
                    "severity": "info",
                    "status": "potato",
                    "message": "test finding",
                }
            ),
        ]
        jsonl.write_text("\n".join(lines) + "\n")
        with pytest.raises(ValueError, match="status"):
            db.import_jsonl(jsonl, merge=True)

    def test_import_accepts_valid_scan_finding_severity_and_status(self, db: FiligreeDB, tmp_path: Path) -> None:
        """import_jsonl must accept all valid severity and status values."""
        fr = db.register_file("src/test3.py")
        jsonl = tmp_path / "good_finding.jsonl"
        lines = [
            json.dumps({"_type": "file_record", "id": fr.id, "path": "src/test3.py"}),
            json.dumps(
                {
                    "_type": "scan_finding",
                    "id": "sf-good",
                    "file_id": fr.id,
                    "scan_source": "test",
                    "rule_id": "R3",
                    "severity": "critical",
                    "status": "acknowledged",
                    "message": "valid finding",
                }
            ),
        ]
        jsonl.write_text("\n".join(lines) + "\n")
        result = db.import_jsonl(jsonl, merge=True)
        assert result["count"] >= 1

    def test_bulk_insert_issue_returns_inserted_flag(self, db: FiligreeDB) -> None:
        """bulk_insert_issue must return True when row was inserted, False when skipped."""
        result = db.bulk_insert_issue(
            {
                "id": "test-bulk-1",
                "title": "Bulk test",
                "status": "open",
                "priority": 2,
                "type": "task",
            }
        )
        assert result is True
        db.bulk_commit()

        # Duplicate should return False
        result2 = db.bulk_insert_issue(
            {
                "id": "test-bulk-1",
                "title": "Bulk test duplicate",
                "status": "open",
                "priority": 2,
                "type": "task",
            }
        )
        assert result2 is False
        db.bulk_commit()

    def test_bulk_insert_issue_rejects_blank_title_when_validate_true(self, db: FiligreeDB) -> None:
        """bulk_insert_issue(validate=True) must enforce create_issue title invariants."""
        bad_id = "test-bulk-blank-title"

        with pytest.raises(ValueError, match="Title cannot be empty"):
            db.bulk_insert_issue(
                {
                    "id": bad_id,
                    "title": "   ",
                    "status": "open",
                    "priority": 2,
                    "type": "task",
                }
            )

        leaked = db.conn.execute("SELECT COUNT(*) FROM issues WHERE id = ?", (bad_id,)).fetchone()[0]
        assert leaked == 0

    def test_bulk_insert_issue_rejects_non_integer_priority_before_write(self, db: FiligreeDB) -> None:
        """bulk_insert_issue(validate=True) must not durably store priorities the Issue model rejects."""
        bad_id = "test-bulk-bad-priority"

        with pytest.raises(ValueError, match="Priority must be an integer"):
            db.bulk_insert_issue(
                {
                    "id": bad_id,
                    "title": "Bad priority",
                    "status": "open",
                    "priority": 2.5,
                    "type": "task",
                }
            )

        leaked = db.conn.execute("SELECT COUNT(*) FROM issues WHERE id = ?", (bad_id,)).fetchone()[0]
        assert leaked == 0

    def test_bulk_insert_dependency_returns_inserted_flag(self, db: FiligreeDB) -> None:
        """bulk_insert_dependency must return True when inserted, False when skipped."""
        db.create_issue("A", fields=None)
        db.create_issue("B", fields=None)
        issues = db.list_issues(limit=2)
        a_id, b_id = issues[0].id, issues[1].id

        result = db.bulk_insert_dependency(a_id, b_id)
        assert result is True
        db.bulk_commit()

        result2 = db.bulk_insert_dependency(a_id, b_id)
        assert result2 is False
        db.bulk_commit()

    def test_bulk_insert_dependency_rejects_self_dependency(self, db: FiligreeDB) -> None:
        """bulk_insert_dependency must preserve the normal dependency self-edge guard."""
        issue = db.create_issue("Self dependency")

        with pytest.raises(ValueError, match="self-dependency"):
            db.bulk_insert_dependency(issue.id, issue.id)

        leaked = db.conn.execute("SELECT COUNT(*) FROM dependencies WHERE issue_id = ?", (issue.id,)).fetchone()[0]
        assert leaked == 0

    def test_bulk_insert_dependency_rejects_dependency_cycle(self, db: FiligreeDB) -> None:
        """bulk_insert_dependency must not bypass cycle detection."""
        first = db.create_issue("First")
        second = db.create_issue("Second")
        assert db.bulk_insert_dependency(first.id, second.id) is True
        db.bulk_commit()

        with pytest.raises(ValueError, match="would create a cycle"):
            db.bulk_insert_dependency(second.id, first.id)

        cycle_edges = db.conn.execute(
            "SELECT COUNT(*) FROM dependencies WHERE issue_id = ? AND depends_on_id = ?",
            (second.id, first.id),
        ).fetchone()[0]
        assert cycle_edges == 0

    def test_bulk_insert_event_returns_inserted_flag(self, db: FiligreeDB) -> None:
        """bulk_insert_event must return True when inserted, False when skipped."""
        issue = db.create_issue("Event test")
        result = db.bulk_insert_event(
            {
                "issue_id": issue.id,
                "event_type": "test_event",
                "actor": "test",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
        )
        assert result is True
        db.bulk_commit()


# ---------------------------------------------------------------------------
# Archival & Compaction
# ---------------------------------------------------------------------------


class TestArchival:
    def test_archive_old_closed(self, db: FiligreeDB) -> None:
        issue = db.create_issue("To archive")
        db.close_issue(issue.id)
        # Manually backdate closed_at
        old_date = (datetime.now(UTC) - timedelta(days=60)).isoformat()
        db.conn.execute("UPDATE issues SET closed_at = ? WHERE id = ?", (old_date, issue.id))
        db.conn.commit()

        archived = db.archive_closed(days_old=30)
        assert issue.id in archived
        refreshed = db.get_issue(issue.id)
        assert refreshed.status == "archived"

    def test_archive_skips_recent(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Recent close")
        db.close_issue(issue.id)
        # closed_at is now — should NOT be archived
        archived = db.archive_closed(days_old=30)
        assert issue.id not in archived

    def test_archive_skips_open(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Still open")
        archived = db.archive_closed(days_old=0)
        assert issue.id not in archived

    def test_archive_recently_closed_with_days_zero(self, db: FiligreeDB) -> None:
        """days_old=0 should archive issues closed just now."""
        issue = db.create_issue("Just closed")
        db.close_issue(issue.id)
        archived = db.archive_closed(days_old=0)
        assert issue.id in archived
        assert db.get_issue(issue.id).status == "archived"

    def test_archive_closed_can_be_scoped_to_label(self, db: FiligreeDB) -> None:
        """Review/test cleanup can archive only closed issues carrying a scratch label."""
        scratch = db.create_issue("Scratch cleanup")
        unrelated = db.create_issue("Real closed work")
        db.add_label(scratch.id, "scratch")
        db.close_issue(scratch.id)
        db.close_issue(unrelated.id)

        archived = db.archive_closed(days_old=0, label="scratch")

        assert archived == [scratch.id]
        assert db.get_issue(scratch.id).status == "archived"
        assert db.get_issue(unrelated.id).status == "closed"

    @pytest.mark.parametrize("days", [-1, -30])
    def test_archive_rejects_negative_days_old(self, db: FiligreeDB, days: int) -> None:
        """filigree-0903743222: negative days_old shifted cutoff into the future, archiving every closed issue."""
        issue = db.create_issue("Recently closed")
        db.close_issue(issue.id)
        with pytest.raises(ValueError, match="days_old must be >= 0"):
            db.archive_closed(days_old=days)
        assert db.get_issue(issue.id).status == "closed"

    def test_archive_records_event(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Archive event")
        db.close_issue(issue.id)
        old_date = (datetime.now(UTC) - timedelta(days=60)).isoformat()
        db.conn.execute("UPDATE issues SET closed_at = ? WHERE id = ?", (old_date, issue.id))
        db.conn.commit()

        db.archive_closed(days_old=30, actor="janitor")
        events = db.get_recent_events(limit=10)
        archived_events = [e for e in events if e["event_type"] == "archived"]
        assert len(archived_events) == 1
        assert archived_events[0]["actor"] == "janitor"

    def test_archive_rolls_back_on_failure(self, db: FiligreeDB, monkeypatch: pytest.MonkeyPatch) -> None:
        """H3: If _record_event fails mid-loop, no issues should be archived."""
        # Create two issues and backdate their closed_at
        a = db.create_issue("Archive A")
        b = db.create_issue("Archive B")
        db.close_issue(a.id)
        db.close_issue(b.id)
        old_date = (datetime.now(UTC) - timedelta(days=60)).isoformat()
        db.conn.execute("UPDATE issues SET closed_at = ? WHERE id = ?", (old_date, a.id))
        db.conn.execute("UPDATE issues SET closed_at = ? WHERE id = ?", (old_date, b.id))
        db.conn.commit()

        call_count = 0
        original = db._record_event

        def failing_on_second_archive(*args: object, **kwargs: object) -> None:
            nonlocal call_count
            if len(args) >= 2 and args[1] == "archived":
                call_count += 1
                if call_count == 2:
                    raise RuntimeError("Simulated failure on second archive")
            original(*args, **kwargs)

        monkeypatch.setattr(db, "_record_event", failing_on_second_archive)

        with pytest.raises(RuntimeError, match="Simulated"):
            db.archive_closed(days_old=30)

        # Neither issue should be archived — rollback should have reverted both
        assert db.get_issue(a.id).status != "archived", "First issue should not be archived after rollback"
        assert db.get_issue(b.id).status != "archived", "Second issue should not be archived after rollback"


class TestArchivedDoesNotReblockDependents:
    """filigree-42045dd065: archive_closed sets status='archived', which is
    not in any workflow's done states. Without explicit handling, readiness
    and blocked_by hydration treat archived blockers as still-blocking,
    re-blocking dependents that were ready."""

    def test_archived_blocker_keeps_dependent_ready(self, db: FiligreeDB) -> None:
        """A depends on B; close B → A ready; archive B → A still ready."""
        a = db.create_issue("Dependent A")
        b = db.create_issue("Blocker B")
        db.add_dependency(a.id, b.id)
        # While B is open, A is blocked
        assert a.id not in [r.id for r in db.get_ready()]
        db.close_issue(b.id)
        # After B closes, A is ready
        ready_ids = [r.id for r in db.get_ready()]
        assert a.id in ready_ids, f"A should be ready after B closes; got {ready_ids}"
        # Archive B (closed_at is now, days_old=0)
        archived = db.archive_closed(days_old=0)
        assert b.id in archived
        assert db.get_issue(b.id).status == "archived"
        # A must still be ready — archive must not undo the unblocking
        ready_ids_after = [r.id for r in db.get_ready()]
        assert a.id in ready_ids_after, f"A re-blocked after B was archived; ready={ready_ids_after}"

    def test_archived_blocker_not_in_blocked_by_hydration(self, db: FiligreeDB) -> None:
        """blocked_by on an issue must not list archived blockers."""
        a = db.create_issue("Dependent A")
        b = db.create_issue("Blocker B")
        db.add_dependency(a.id, b.id)
        db.close_issue(b.id)
        db.archive_closed(days_old=0)
        # Re-fetch A — its blocked_by must not contain B
        refreshed = db.get_issue(a.id)
        assert b.id not in refreshed.blocked_by, f"Archived blocker {b.id} should not appear in blocked_by; got {refreshed.blocked_by}"
        # And is_ready must reflect that
        assert refreshed.is_ready is True, "A.is_ready must be True after blocker archived"


class TestImportNormalizesNonUtcTimestamps:
    """filigree-20911dfe6d: imported timestamps with non-UTC offsets must be
    normalized to canonical UTC so SQLite TEXT comparisons (used by
    get_events_since and archive_closed) work chronologically."""

    def test_get_events_since_handles_non_utc_imported_event(self, db: FiligreeDB, tmp_path: Path) -> None:
        """Import an event whose created_at carries +02:00. A cursor at
        the same chronological instant in +00:00 must NOT return it."""
        # Seed an issue so the event has a valid issue_id
        issue = db.create_issue("Anchor")
        # Hand-write a JSONL bundle: one event whose stored timestamp uses +02:00.
        # 2026-01-01T01:00:00+02:00 == 2025-12-31T23:00:00+00:00 (chronologically earlier).
        # The CLI cursor at 2026-01-01T00:00:00+00:00 is chronologically AFTER the event,
        # so post-fix get_events_since must NOT return it. Pre-fix, lexicographic comparison
        # of the raw stored "...01:00:00+02:00" string is greater than "...00:00:00+00:00",
        # so the event would be returned.
        bundle = (
            json.dumps(
                {
                    "_type": "event",
                    "issue_id": issue.id,
                    "event_type": "title_changed",
                    "actor": "import",
                    "old_value": "old",
                    "new_value": "new",
                    "created_at": "2026-01-01T01:00:00+02:00",
                }
            )
            + "\n"
        )
        path = tmp_path / "bundle.jsonl"
        path.write_text(bundle)
        db.import_jsonl(str(path), merge=True)
        # Cursor at 2026-01-01T00:00:00+00:00 — chronologically AFTER the event
        # (event is 23:00 UTC the previous day). After normalization, the event's
        # stored created_at is "2025-12-31T23:00:00+00:00", which lexicographically
        # is BEFORE the cursor — the event must NOT be returned.
        events_after = db.get_events_since("2026-01-01T00:00:00+00:00")
        imported = [e for e in events_after if e["event_type"] == "title_changed" and e["issue_id"] == issue.id]
        assert imported == [], (
            f"Non-UTC imported event should normalize to chronologically-earlier UTC and be excluded by post-cursor query; got {imported}"
        )

    def test_get_events_since_can_resume_inside_same_timestamp_group(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Same timestamp cursor")
        tied_timestamp = "2026-01-01T00:00:00+00:00"
        cursor = db.conn.execute(
            "INSERT INTO events (issue_id, event_type, actor, created_at) VALUES (?, ?, ?, ?)",
            (issue.id, "title_changed", "import", tied_timestamp),
        ).lastrowid
        expected = db.conn.execute(
            "INSERT INTO events (issue_id, event_type, actor, created_at) VALUES (?, ?, ?, ?)",
            (issue.id, "status_changed", "import", tied_timestamp),
        ).lastrowid
        db.conn.commit()

        page = db.get_events_since(tied_timestamp, after_event_id=cursor, limit=1)

        assert [event["id"] for event in page] == [expected]

    def test_imported_observation_expires_at_normalized_so_expired_is_swept(self, db: FiligreeDB, tmp_path: Path) -> None:
        """filigree-cfada09a36: Import an observation whose expires_at is
        chronologically expired but whose ``+02:00`` text representation
        lex-sorts AFTER a canonical ``+00:00`` cursor at the same instant.

        Concretely: ``2030-01-01T01:00:00+02:00`` == ``2029-12-31T23:00:00+00:00``
        (chronologically *before* ``2030-01-01T00:00:00+00:00``), but the raw
        text ``"2030-01-01T01:00:00+02:00"`` lex-compares *greater than*
        ``"2030-01-01T00:00:00+00:00"``. Pre-fix, ``list_observations`` (which
        first sweeps via ``expires_at <= now`` then alive-filters via
        ``expires_at > now``) keeps the row alive based on raw text. Post-fix,
        the import boundary normalizes to canonical UTC and the row is
        correctly classified as expired."""
        bundle = (
            json.dumps(
                {
                    "_type": "observation",
                    "id": "obs-import-tz",
                    "summary": "imported with +02:00 expires_at",
                    "detail": "",
                    "file_path": "",
                    "priority": 3,
                    "actor": "import",
                    "created_at": "2026-01-01T01:00:00+02:00",
                    "expires_at": "2030-01-01T01:00:00+02:00",
                }
            )
            + "\n"
        )
        path = tmp_path / "obs-bundle.jsonl"
        path.write_text(bundle)
        db.import_jsonl(str(path), merge=True)

        # Freeze "now" to 2030-01-01T00:00:00+00:00 — chronologically AFTER
        # the imported expires_at instant (UTC 2029-12-31T23:00:00) but
        # lex-BEFORE the raw "+02:00" stored text.
        with patch("filigree.db_observations._now_iso", return_value="2030-01-01T00:00:00+00:00"):
            alive = db.list_observations()

        ids = [o["id"] for o in alive]
        assert "obs-import-tz" not in ids, (
            f"Imported observation with non-UTC expires_at chronologically before "
            f"the cursor must be normalized to canonical UTC at import so the alive "
            f"filter excludes it. Got ids={ids} — pre-fix, raw '+02:00' text "
            f"lex-sorted greater than '+00:00' cursor and the row stayed alive."
        )


class TestCompaction:
    def test_compact_archived_events(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Compact me")
        # Directly insert many events
        for i in range(60):
            db.conn.execute(
                "INSERT INTO events (issue_id, event_type, actor, created_at) VALUES (?, ?, ?, ?)",
                (issue.id, "test_event", "tester", f"2026-01-01T00:{i:02d}:00+00:00"),
            )
        db.conn.commit()
        db.close_issue(issue.id)
        # Backdate and archive
        old_date = (datetime.now(UTC) - timedelta(days=60)).isoformat()
        db.conn.execute("UPDATE issues SET closed_at = ? WHERE id = ?", (old_date, issue.id))
        db.conn.commit()
        db.archive_closed(days_old=30)

        # Count events before
        before = db.conn.execute("SELECT COUNT(*) as cnt FROM events WHERE issue_id = ?", (issue.id,)).fetchone()["cnt"]
        assert before > 50

        # Compact
        deleted = db.compact_events(keep_recent=10)
        assert deleted > 0

        # Count events after
        after = db.conn.execute("SELECT COUNT(*) as cnt FROM events WHERE issue_id = ?", (issue.id,)).fetchone()["cnt"]
        assert after == 10

    def test_compact_returns_actual_rowcount(self, db: FiligreeDB) -> None:
        """compact_events must return actual rows deleted, not pre-computed estimate."""
        issue = db.create_issue("Rowcount test")
        for i in range(30):
            db.conn.execute(
                "INSERT INTO events (issue_id, event_type, actor, created_at) VALUES (?, ?, ?, ?)",
                (issue.id, "test_event", "tester", f"2026-01-01T00:{i:02d}:00+00:00"),
            )
        db.conn.commit()
        db.close_issue(issue.id)
        old_date = (datetime.now(UTC) - timedelta(days=60)).isoformat()
        db.conn.execute("UPDATE issues SET closed_at = ? WHERE id = ?", (old_date, issue.id))
        db.conn.commit()
        db.archive_closed(days_old=30)

        before = db.conn.execute("SELECT COUNT(*) as cnt FROM events WHERE issue_id = ?", (issue.id,)).fetchone()["cnt"]
        deleted = db.compact_events(keep_recent=5)
        after = db.conn.execute("SELECT COUNT(*) as cnt FROM events WHERE issue_id = ?", (issue.id,)).fetchone()["cnt"]

        # Returned count must match actual rows removed
        assert deleted == before - after

    def test_compact_with_keep_recent_zero(self, db: FiligreeDB) -> None:
        """keep_recent=0 should delete ALL events for archived issues."""
        issue = db.create_issue("Compact all")
        db.update_issue(issue.id, title="v2")
        db.update_issue(issue.id, title="v3")
        db.close_issue(issue.id)
        # Backdate and archive
        old_date = (datetime.now(UTC) - timedelta(days=60)).isoformat()
        db.conn.execute("UPDATE issues SET closed_at = ? WHERE id = ?", (old_date, issue.id))
        db.conn.commit()
        db.archive_closed(days_old=30)

        before = db.conn.execute("SELECT COUNT(*) as cnt FROM events WHERE issue_id = ?", (issue.id,)).fetchone()["cnt"]
        assert before > 0

        deleted = db.compact_events(keep_recent=0)
        assert deleted > 0

        after = db.conn.execute("SELECT COUNT(*) as cnt FROM events WHERE issue_id = ?", (issue.id,)).fetchone()["cnt"]
        assert after == 0

    def test_compact_skips_non_archived(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Not archived")
        for i in range(20):
            db.update_issue(issue.id, notes=f"note {i}")
        deleted = db.compact_events(keep_recent=5)
        assert deleted == 0

    def test_compact_rollback_on_failure(self, db: FiligreeDB) -> None:
        """compact_events must rollback on mid-loop failure, not leave partial deletes.

        Creates two archived issues with many events. A trigger causes the
        second DELETE to fail. Without a rollback guard, the first issue's
        events would be silently deleted while the second's remain.
        """
        # Create two archived issues, each with 20 events
        issues = []
        for label in ("first", "second"):
            issue = db.create_issue(f"Compact {label}")
            for i in range(20):
                db.conn.execute(
                    "INSERT INTO events (issue_id, event_type, actor, created_at) VALUES (?, ?, ?, ?)",
                    (issue.id, "test_event", "tester", f"2026-01-01T00:{i:02d}:00+00:00"),
                )
            db.conn.commit()
            db.close_issue(issue.id)
            old_date = (datetime.now(UTC) - timedelta(days=60)).isoformat()
            db.conn.execute("UPDATE issues SET closed_at = ? WHERE id = ?", (old_date, issue.id))
            db.conn.commit()
            issues.append(issue)
        db.archive_closed(days_old=30)

        counts_before = {}
        for issue in issues:
            cnt = db.conn.execute("SELECT COUNT(*) as cnt FROM events WHERE issue_id = ?", (issue.id,)).fetchone()["cnt"]
            counts_before[issue.id] = cnt
            assert cnt > 10

        # Trigger fails on DELETE for the second archived issue only
        db.conn.execute(
            f"CREATE TRIGGER fail_delete BEFORE DELETE ON events "
            f"WHEN OLD.issue_id = '{issues[1].id}' BEGIN "
            f"SELECT RAISE(ABORT, 'simulated failure'); END"
        )
        with pytest.raises(Exception, match="simulated failure"):
            db.compact_events(keep_recent=5)

        # Remove the trigger and verify ALL events are intact (rollback worked)
        db.conn.execute("DROP TRIGGER fail_delete")
        for issue in issues:
            after = db.conn.execute("SELECT COUNT(*) as cnt FROM events WHERE issue_id = ?", (issue.id,)).fetchone()["cnt"]
            assert after == counts_before[issue.id], (
                f"Issue {issue.id}: expected {counts_before[issue.id]} events after rollback, got {after}"
            )

    def test_compact_rejects_negative_keep_recent(self, db: FiligreeDB) -> None:
        """Negative keep_recent would delete all archived events (regression for filigree-fe8956fb16)."""
        issue = db.create_issue("Negative keep_recent guard")
        for i in range(10):
            db.conn.execute(
                "INSERT INTO events (issue_id, event_type, actor, created_at) VALUES (?, ?, ?, ?)",
                (issue.id, "test_event", "tester", f"2026-01-01T00:{i:02d}:00+00:00"),
            )
        db.conn.commit()
        db.close_issue(issue.id)
        old_date = (datetime.now(UTC) - timedelta(days=60)).isoformat()
        db.conn.execute("UPDATE issues SET closed_at = ? WHERE id = ?", (old_date, issue.id))
        db.conn.commit()
        db.archive_closed(days_old=30)

        before = db.conn.execute("SELECT COUNT(*) as cnt FROM events WHERE issue_id = ?", (issue.id,)).fetchone()["cnt"]
        assert before > 0

        with pytest.raises(ValueError, match="keep_recent"):
            db.compact_events(keep_recent=-1)

        # Events must be untouched after the guard rejects the call
        after = db.conn.execute("SELECT COUNT(*) as cnt FROM events WHERE issue_id = ?", (issue.id,)).fetchone()["cnt"]
        assert after == before, f"negative keep_recent wiped {before - after} events"

    def test_vacuum(self, db: FiligreeDB) -> None:
        # vacuum() returns None; verify it completes without error
        db.vacuum()

    def test_analyze(self, db: FiligreeDB) -> None:
        db.analyze()


class TestCloseCommitsPending:
    """close() preserves committed writes and rolls back raw in-flight transactions."""

    def test_close_commits_pending_writes(self, tmp_path: Path) -> None:
        db = FiligreeDB(tmp_path / "commit-on-close.db", prefix="test")
        db.initialize()
        issue = db.create_issue(title="will survive close", type="task")

        # create_issue commits internally; close() should preserve that write.
        db.close()

        # Reopen and verify the issue persists.
        db2 = FiligreeDB(tmp_path / "commit-on-close.db", prefix="test")
        found = db2.get_issue(issue.id)
        assert found is not None
        assert found.title == "will survive close"
        db2.close()

    def test_context_manager_commits_on_exit(self, tmp_path: Path) -> None:
        db_path = tmp_path / "ctx-commit.db"
        with FiligreeDB(db_path, prefix="test") as db:
            db.initialize()
            issue = db.create_issue(title="ctx write", type="task")
            issue_id = issue.id

        # Reopen after context manager exit.
        db2 = FiligreeDB(db_path, prefix="test")
        found = db2.get_issue(issue_id)
        assert found is not None
        assert found.title == "ctx write"
        db2.close()

    def test_context_manager_success_rolls_back_raw_in_flight_transaction(self, tmp_path: Path) -> None:
        """Raw writes without an explicit commit are treated as incomplete work on close."""
        db_path = tmp_path / "ctx-raw-success.db"
        issue_id = "test-rawsuccess"

        with FiligreeDB(db_path, prefix="test") as db:
            db.initialize()
            db.conn.execute(
                "INSERT INTO issues (id, title, status, priority, type, assignee,"
                " created_at, updated_at, description, notes, fields)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    issue_id,
                    "raw success",
                    "open",
                    2,
                    "task",
                    "",
                    "2026-01-01T00:00:00",
                    "2026-01-01T00:00:00",
                    "",
                    "",
                    "{}",
                ),
            )

        check = FiligreeDB(db_path, prefix="test")
        try:
            row = check.conn.execute("SELECT id FROM issues WHERE id = ?", (issue_id,)).fetchone()
            assert row is None
        finally:
            check.close()

    def test_close_clears_conn_even_when_sqlite_close_raises(self, tmp_path: Path) -> None:
        """_conn must be None after close() even if Connection.close() raises."""
        db = FiligreeDB(tmp_path / "close-err.db", prefix="test")
        db.initialize()

        # Replace _conn with a mock whose close() raises
        original_conn = db._conn
        mock_conn = MagicMock(wraps=original_conn)
        mock_conn.close.side_effect = sqlite3.ProgrammingError("simulated close failure")
        db._conn = mock_conn

        try:
            with pytest.raises(sqlite3.ProgrammingError, match="simulated close failure"):
                db.close()
        finally:
            original_conn.close()

        # _conn must be cleared despite the exception
        assert db._conn is None

    def test_context_manager_clears_conn_on_close_error(self, tmp_path: Path) -> None:
        """Context manager must clear _conn even if close() raises internally."""
        db_path = tmp_path / "ctx-err.db"
        db = FiligreeDB(db_path, prefix="test")
        db.initialize()

        original_conn = db._conn
        mock_conn = MagicMock(wraps=original_conn)
        mock_conn.close.side_effect = sqlite3.ProgrammingError("simulated close failure")
        db._conn = mock_conn

        try:
            with pytest.raises(sqlite3.ProgrammingError, match="simulated close failure"):
                db.__exit__(None, None, None)
        finally:
            original_conn.close()

        assert db._conn is None

    def test_context_manager_rolls_back_on_exception(self, tmp_path: Path) -> None:
        """Bug filigree-c4bc03b6ba: __exit__ must rollback, not commit, when with-block raised.

        Simulates a future code path that does writes without explicit commit.
        Without the fix, close() would commit partial state on exception exit.
        """
        db_path = tmp_path / "ctx-rollback.db"

        with FiligreeDB(db_path, prefix="test") as db:
            db.initialize()
            db.create_issue(title="baseline", type="task")

        # Simulate uncommitted writes followed by an exception
        db2 = FiligreeDB(db_path, prefix="test")
        try:
            with db2:
                # Raw INSERT without commit — simulates a future code path
                # that does partial writes before an error
                db2.conn.execute(
                    "INSERT INTO issues (id, title, status, priority, type, assignee,"
                    " created_at, updated_at, description, notes, fields)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "test-uncommitted",
                        "uncommitted write",
                        "open",
                        2,
                        "task",
                        "",
                        "2026-01-01T00:00:00",
                        "2026-01-01T00:00:00",
                        "",
                        "",
                        "{}",
                    ),
                )
                raise RuntimeError("simulated failure mid-operation")
        except RuntimeError:
            pass

        # The uncommitted row should NOT be persisted
        db3 = FiligreeDB(db_path, prefix="test")
        row = db3.conn.execute("SELECT id FROM issues WHERE id = 'test-uncommitted'").fetchone()
        assert row is None, "uncommitted write was persisted — __exit__ should have rolled back"
        db3.close()


class TestReconnect:
    """reconnect() closes and reopens the connection with new settings."""

    def test_reconnect_changes_check_same_thread(self, tmp_path: Path) -> None:
        db = FiligreeDB(tmp_path / "reconnect.db", prefix="test")
        db.initialize()
        assert db._check_same_thread is True

        db.reconnect(check_same_thread=False)
        assert db._check_same_thread is False
        assert db._conn is None  # connection cleared, will lazily reopen

        # Accessing conn should create a new connection that works
        _ = db.conn
        assert db._conn is not None

    def test_reconnect_commits_pending_writes(self, tmp_path: Path) -> None:
        db_path = tmp_path / "reconnect-commit.db"
        db = FiligreeDB(db_path, prefix="test")
        db.initialize()
        issue = db.create_issue(title="survive reconnect", type="task")

        db.reconnect(check_same_thread=False)

        # Re-initialize and verify data persisted
        db.initialize()
        found = db.get_issue(issue.id)
        assert found.title == "survive reconnect"
        db.close()

    def test_reconnect_rolls_back_in_flight_transaction(self, tmp_path: Path) -> None:
        """reconnect() rolls back uncommitted writes when in_transaction is True."""
        db_path = tmp_path / "reconnect-rollback.db"
        db = FiligreeDB(db_path, prefix="test")
        db.initialize()
        # Committed issue — should survive
        committed = db.create_issue(title="committed", type="task")
        # Raw INSERT without commit — in_transaction becomes True
        db.conn.execute(
            "INSERT INTO issues (id, title, status, priority, type, assignee,"
            " created_at, updated_at, description, notes, fields)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "test-uncommitted",
                "uncommitted write",
                "open",
                2,
                "task",
                "",
                "2026-01-01T00:00:00",
                "2026-01-01T00:00:00",
                "",
                "",
                "{}",
            ),
        )
        assert db._conn.in_transaction

        db.reconnect(check_same_thread=False)
        db.initialize()

        # Committed row survives, uncommitted row does not
        assert db.get_issue(committed.id).title == "committed"
        row = db.conn.execute("SELECT id FROM issues WHERE id = 'test-uncommitted'").fetchone()
        assert row is None, "uncommitted write should have been rolled back"
        db.close()

    def test_reconnect_clears_conn_even_when_close_raises(self, tmp_path: Path) -> None:
        """_conn must be None after reconnect() even if Connection.close() raises."""
        db = FiligreeDB(tmp_path / "reconnect-err.db", prefix="test")
        db.initialize()

        original_conn = db._conn
        mock_conn = MagicMock(wraps=original_conn)
        mock_conn.close.side_effect = sqlite3.ProgrammingError("simulated close failure")
        db._conn = mock_conn

        try:
            with pytest.raises(sqlite3.ProgrammingError, match="simulated close failure"):
                db.reconnect(check_same_thread=False)
        finally:
            original_conn.close()

        assert db._conn is None
        assert db._check_same_thread is False

    def test_reconnect_noop_when_no_connection(self, tmp_path: Path) -> None:
        """reconnect() on a fresh DB (no conn yet) just updates the setting."""
        db = FiligreeDB(tmp_path / "reconnect-noop.db", prefix="test")
        # Don't call initialize — no connection opened yet
        assert db._conn is None

        db.reconnect(check_same_thread=False)
        assert db._conn is None
        assert db._check_same_thread is False


# ── M12: import_jsonl error paths ──────────────────────────────────────


class TestImportJsonlErrorPaths:
    """Cover untested import_jsonl error branches (M12)."""

    def test_file_id_conflict_same_id_different_path(self, db: FiligreeDB, tmp_path: Path) -> None:
        """_resolve_imported_file_id raises ValueError when same ID maps to different path."""
        # Register a file in the DB
        fr = db.register_file("src/original.py")
        file_id = fr.id

        # Build JSONL with same file ID but different path
        jsonl = tmp_path / "conflict.jsonl"
        lines = [
            json.dumps(
                {
                    "_type": "file_record",
                    "id": file_id,
                    "path": "src/different.py",
                    "language": "",
                    "first_seen": "2026-01-01T00:00:00+00:00",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                }
            ),
        ]
        jsonl.write_text("\n".join(lines) + "\n")

        with pytest.raises(ValueError, match="Import conflict for file id"):
            db.import_jsonl(jsonl, merge=True)

    def test_multiple_future_releases_in_import(self, db: FiligreeDB, tmp_path: Path) -> None:
        """Import with two Future release records raises ValueError."""
        jsonl = tmp_path / "multi_future.jsonl"
        lines = [
            json.dumps(
                {
                    "_type": "issue",
                    "id": "test-future1",
                    "title": "Future",
                    "type": "release",
                    "status": "open",
                    "priority": 4,
                    "fields": '{"version": "Future"}',
                }
            ),
            json.dumps(
                {
                    "_type": "issue",
                    "id": "test-future2",
                    "title": "Future",
                    "type": "release",
                    "status": "open",
                    "priority": 4,
                    "fields": '{"version": "Future"}',
                }
            ),
        ]
        jsonl.write_text("\n".join(lines) + "\n")

        with pytest.raises(ValueError, match="multiple Future release"):
            db.import_jsonl(jsonl)

    def test_non_reconcilable_future_with_labels(self, db: FiligreeDB, tmp_path: Path) -> None:
        """Import fails when existing Future has labels (non-reconcilable)."""
        # Create a Future release in the DB and add a label
        db.conn.execute(
            "INSERT INTO issues (id, title, status, priority, type, assignee, "
            "created_at, updated_at, description, notes, fields) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "test-existfut",
                "Future",
                "open",
                4,
                "release",
                "",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                "",
                "",
                '{"version": "Future"}',
            ),
        )
        db.conn.execute("INSERT INTO labels (issue_id, label) VALUES (?, ?)", ("test-existfut", "important"))
        db.conn.commit()

        # Import a different Future release
        jsonl = tmp_path / "new_future.jsonl"
        lines = [
            json.dumps(
                {
                    "_type": "issue",
                    "id": "test-impfut",
                    "title": "Future",
                    "type": "release",
                    "status": "open",
                    "priority": 4,
                    "fields": '{"version": "Future"}',
                }
            ),
        ]
        jsonl.write_text("\n".join(lines) + "\n")

        with pytest.raises(ValueError, match="Cannot import Future release"):
            db.import_jsonl(jsonl)

    def test_non_reconcilable_future_with_comments(self, db: FiligreeDB, tmp_path: Path) -> None:
        """Import fails when existing Future has comments."""
        db.conn.execute(
            "INSERT INTO issues (id, title, status, priority, type, assignee, "
            "created_at, updated_at, description, notes, fields) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "test-existfut",
                "Future",
                "open",
                4,
                "release",
                "",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                "",
                "",
                '{"version": "Future"}',
            ),
        )
        db.conn.execute(
            "INSERT INTO comments (issue_id, author, text, created_at) VALUES (?, ?, ?, ?)",
            ("test-existfut", "dev", "planning note", "2026-01-01T00:00:00+00:00"),
        )
        db.conn.commit()

        jsonl = tmp_path / "new_future.jsonl"
        lines = [
            json.dumps(
                {
                    "_type": "issue",
                    "id": "test-impfut",
                    "title": "Future",
                    "type": "release",
                    "status": "open",
                    "priority": 4,
                    "fields": '{"version": "Future"}',
                }
            ),
        ]
        jsonl.write_text("\n".join(lines) + "\n")

        with pytest.raises(ValueError, match="Cannot import Future release"):
            db.import_jsonl(jsonl)

    def test_non_reconcilable_future_with_children(self, db: FiligreeDB, tmp_path: Path) -> None:
        """Import fails when existing Future has child issues."""
        db.conn.execute(
            "INSERT INTO issues (id, title, status, priority, type, assignee, "
            "created_at, updated_at, description, notes, fields) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "test-existfut",
                "Future",
                "open",
                4,
                "release",
                "",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                "",
                "",
                '{"version": "Future"}',
            ),
        )
        # Add a child issue
        db.conn.execute(
            "INSERT INTO issues (id, title, status, priority, type, parent_id, assignee, "
            "created_at, updated_at, description, notes, fields) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "child-1",
                "Child task",
                "open",
                2,
                "task",
                "test-existfut",
                "",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                "",
                "",
                "{}",
            ),
        )
        db.conn.commit()

        jsonl = tmp_path / "new_future.jsonl"
        lines = [
            json.dumps(
                {
                    "_type": "issue",
                    "id": "test-impfut",
                    "title": "Future",
                    "type": "release",
                    "status": "open",
                    "priority": 4,
                    "fields": '{"version": "Future"}',
                }
            ),
        ]
        jsonl.write_text("\n".join(lines) + "\n")

        with pytest.raises(ValueError, match="Cannot import Future release"):
            db.import_jsonl(jsonl)

    def test_non_reconcilable_future_with_dependencies(self, db: FiligreeDB, tmp_path: Path) -> None:
        """Import fails when existing Future has dependencies."""
        db.conn.execute(
            "INSERT INTO issues (id, title, status, priority, type, assignee, "
            "created_at, updated_at, description, notes, fields) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "test-existfut",
                "Future",
                "open",
                4,
                "release",
                "",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                "",
                "",
                '{"version": "Future"}',
            ),
        )
        db.conn.commit()  # Close implicit DEFERRED tx — create_issue's
        # IMMEDIATE-tx decorator (2.1.0 §2.1) rejects nested BEGINs.
        other = db.create_issue("Other task")
        db.conn.execute(
            "INSERT INTO dependencies (issue_id, depends_on_id, type, created_at) VALUES (?, ?, ?, ?)",
            ("test-existfut", other.id, "blocks", "2026-01-01T00:00:00+00:00"),
        )
        db.conn.commit()

        jsonl = tmp_path / "new_future.jsonl"
        lines = [
            json.dumps(
                {
                    "_type": "issue",
                    "id": "test-impfut",
                    "title": "Future",
                    "type": "release",
                    "status": "open",
                    "priority": 4,
                    "fields": '{"version": "Future"}',
                }
            ),
        ]
        jsonl.write_text("\n".join(lines) + "\n")

        with pytest.raises(ValueError, match="Cannot import Future release"):
            db.import_jsonl(jsonl)

    def test_non_reconcilable_future_modified_title(self, db: FiligreeDB, tmp_path: Path) -> None:
        """Import fails when existing Future has modified title (not 'Future')."""
        db.conn.execute(
            "INSERT INTO issues (id, title, status, priority, type, assignee, "
            "created_at, updated_at, description, notes, fields) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "test-existfut",
                "Modified Future",
                "open",
                4,
                "release",
                "",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                "",
                "",
                '{"version": "Future"}',
            ),
        )
        db.conn.commit()

        jsonl = tmp_path / "new_future.jsonl"
        lines = [
            json.dumps(
                {
                    "_type": "issue",
                    "id": "test-impfut",
                    "title": "Future",
                    "type": "release",
                    "status": "open",
                    "priority": 4,
                    "fields": '{"version": "Future"}',
                }
            ),
        ]
        jsonl.write_text("\n".join(lines) + "\n")

        with pytest.raises(ValueError, match="Cannot import Future release"):
            db.import_jsonl(jsonl)


# ── _generate_unique_id RuntimeError ──────────────────────────────────


class TestGenerateUniqueIdCollision:
    """filigree-ca37abe44f: _generate_unique_id raises RuntimeError on persistent collisions."""

    def test_all_collisions_raises_runtime_error(self, db: FiligreeDB) -> None:
        """When uuid4 always returns the same value, both normal and fallback IDs collide."""
        # Use a fixed hex that will produce predictable IDs
        fixed_hex = "abcdef1234567890abcdef1234567890"
        normal_id = f"test-{fixed_hex[:10]}"  # first 10 chars used in loop
        fallback_id = f"test-{fixed_hex[:16]}"  # first 16 chars used in fallback

        # Pre-insert both candidate IDs into the issues table
        now = datetime.now(UTC).isoformat()
        for cid in (normal_id, fallback_id):
            db.conn.execute(
                "INSERT INTO issues (id, title, status, priority, type, assignee, "
                "created_at, updated_at, description, notes, fields) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (cid, "Collision placeholder", "open", 2, "task", "", now, now, "", "", "{}"),
            )
        db.conn.commit()

        fake_uuid = MagicMock()
        fake_uuid.hex = fixed_hex

        with patch("filigree.db_issues.uuid.uuid4", return_value=fake_uuid), pytest.raises(RuntimeError, match="ID generation failed"):
            db._generate_unique_id("issues")


class TestNamespaceReservation:
    """Reserved namespaces cannot be used as manual labels."""

    @pytest.mark.parametrize(
        "label",
        [
            "area:mcp",
            "severity:high",
            "scanner:ruff",
            "pack:core",
            "age:stale",
            "has:blockers",
            "AREA:MCP",  # case-insensitive
        ],
    )
    def test_reserved_namespace_rejected(self, db: FiligreeDB, label: str) -> None:
        issue = db.create_issue("Test")
        with pytest.raises(ValueError, match="namespace"):
            db.add_label(issue.id, label)

    @pytest.mark.parametrize(
        "label",
        [
            "cluster:broad-except",
            "effort:m",
            "review:needed",
            "wait:upstream",
            "tech-debt",
            "needs-review",
        ],
    )
    def test_manual_namespace_allowed(self, db: FiligreeDB, label: str) -> None:
        issue = db.create_issue("Test")
        added, canonical, replaced = db.add_label(issue.id, label)
        assert added is True
        assert canonical == label
        assert replaced == []

    @pytest.mark.parametrize(
        "label",
        [
            "has\nnewline",
            "has\rcarriage",
            "has\x00null",
            "has\x1fcontrol",
        ],
    )
    def test_control_characters_rejected(self, db: FiligreeDB, label: str) -> None:
        issue = db.create_issue("Test")
        with pytest.raises(ValueError, match="control characters"):
            db.add_label(issue.id, label)


class TestReviewMutualExclusivity:
    def test_adding_review_label_removes_prior(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Test")
        db.add_label(issue.id, "review:needed")
        db.add_label(issue.id, "review:done")
        refreshed = db.get_issue(issue.id)
        assert "review:done" in refreshed.labels
        assert "review:needed" not in refreshed.labels

    def test_review_labels_dont_affect_other_labels(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Test")
        db.add_label(issue.id, "tech-debt")
        db.add_label(issue.id, "review:needed")
        db.add_label(issue.id, "review:rework")
        refreshed = db.get_issue(issue.id)
        assert "review:rework" in refreshed.labels
        assert "review:needed" not in refreshed.labels
        assert "tech-debt" in refreshed.labels

    def test_idempotent_review_re_add_returns_added_false(self, db: FiligreeDB) -> None:
        """filigree-c9d223e24e: re-adding the same review:* label must report
        added=False — the mutual-exclusivity DELETE used to reinsert and
        falsely report added=True."""
        issue = db.create_issue("Test")
        added1, _, _replaced1 = db.add_label(issue.id, "review:needed")
        assert added1 is True
        added2, canonical, replaced2 = db.add_label(issue.id, "review:needed")
        assert added2 is False
        assert canonical == "review:needed"
        # Re-add of the same review label is a no-op — nothing displaced.
        assert replaced2 == []
        # State invariant: still exactly one review label
        rows = db.conn.execute(
            "SELECT label FROM labels WHERE issue_id = ? AND label LIKE 'review:%'",
            (issue.id,),
        ).fetchall()
        assert [r["label"] for r in rows] == ["review:needed"]

    def test_replacing_review_label_still_returns_added_true(self, db: FiligreeDB) -> None:
        """The idempotency short-circuit must NOT swallow real replacements."""
        issue = db.create_issue("Test")
        db.add_label(issue.id, "review:needed")
        added, canonical, replaced = db.add_label(issue.id, "review:done")
        assert added is True
        assert canonical == "review:done"
        # P2.7: real replacement now reports the displaced label so callers
        # can audit silent label changes.
        assert replaced == ["review:needed"]


class TestScanRunsExportImport:
    """filigree-6160591254: scan_runs must round-trip through JSONL export/import."""

    def test_export_includes_scan_run_records(self, db: FiligreeDB, tmp_path: Path) -> None:
        db.conn.execute(
            "INSERT INTO scan_runs (id, scanner_name, status, file_paths, file_ids, started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "run-export-1",
                "test_scanner",
                "completed",
                "[]",
                "[]",
                "2026-05-01T00:00:00+00:00",
                "2026-05-01T00:00:01+00:00",
            ),
        )
        db.conn.commit()
        out = tmp_path / "scans.jsonl"
        db.export_jsonl(out)
        types_seen = {json.loads(line)["_type"] for line in out.read_text().strip().split("\n") if line}
        assert "scan_run" in types_seen

    def test_roundtrip_preserves_scan_run_fields(self, db: FiligreeDB, tmp_path: Path) -> None:
        db.conn.execute(
            "INSERT INTO scan_runs (id, scanner_name, scan_source, status, file_paths, file_ids, "
            "pid, api_url, log_path, started_at, updated_at, completed_at, "
            "exit_code, findings_count, error_message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "run-rt-1",
                "ruff",
                "ci",
                "completed",
                '["src/foo.py"]',
                '["test-f-1"]',
                12345,
                "https://example.test",
                "/var/log/filigree/scan.log",
                "2026-05-01T00:00:00+00:00",
                "2026-05-01T00:00:05+00:00",
                "2026-05-01T00:00:05+00:00",
                0,
                3,
                "",
            ),
        )
        db.conn.commit()
        out = tmp_path / "rt.jsonl"
        db.export_jsonl(out)

        fresh = FiligreeDB(tmp_path / "fresh.db", prefix="test")
        fresh.initialize()
        fresh.import_jsonl(out)

        row = fresh.conn.execute(
            "SELECT scanner_name, scan_source, status, file_paths, file_ids, pid, "
            "api_url, log_path, completed_at, exit_code, findings_count "
            "FROM scan_runs WHERE id = ?",
            ("run-rt-1",),
        ).fetchone()
        assert row is not None, "scan_run row lost on import"
        assert row["scanner_name"] == "ruff"
        assert row["scan_source"] == "ci"
        assert row["status"] == "completed"
        assert row["file_paths"] == '["src/foo.py"]'
        assert row["pid"] == 12345
        assert row["api_url"] == "https://example.test"
        assert row["completed_at"] == "2026-05-01T00:00:05+00:00"
        assert row["exit_code"] == 0
        assert row["findings_count"] == 3
        fresh.close()

    def test_import_rejects_invalid_scan_run_status(self, db: FiligreeDB, tmp_path: Path) -> None:
        bad = tmp_path / "bad.jsonl"
        bad.write_text(
            json.dumps(
                {
                    "_type": "scan_run",
                    "id": "run-bad-1",
                    "scanner_name": "x",
                    "status": "bogus",
                    "started_at": "2026-05-01T00:00:00+00:00",
                    "updated_at": "2026-05-01T00:00:00+00:00",
                }
            )
            + "\n"
        )
        with pytest.raises(ValueError, match="Invalid scan_run status"):
            db.import_jsonl(bad, allow_foreign_ids=True)


class TestImportLabelValidation:
    """filigree-22ed219abc: import_jsonl must reject reserved-namespace labels."""

    def test_import_skips_virtual_namespace_label(self, db: FiligreeDB, tmp_path: Path) -> None:
        """A physical 'age:older_than_30d' row would shadow the computed
        virtual 'age' namespace in list_labels(). The importer must skip it."""
        issue = db.create_issue("Anchor")
        bad = tmp_path / "evil.jsonl"
        bad.write_text(json.dumps({"_type": "label", "issue_id": issue.id, "label": "age:older_than_30d"}) + "\n")
        result = db.import_jsonl(bad, merge=True)
        assert result["skipped_types"].get("<invalid_label>") == 1
        # Physical labels table is empty for this issue
        rows = db.conn.execute("SELECT label FROM labels WHERE issue_id = ?", (issue.id,)).fetchall()
        assert rows == []
        # Virtual age namespace is intact
        info = db.list_labels(namespace="age")
        assert info["namespaces"]["age"]["type"] == "virtual"
        assert any(label["label"].startswith("age:") for label in info["namespaces"]["age"]["labels"])

    def test_import_skips_auto_namespace_label(self, db: FiligreeDB, tmp_path: Path) -> None:
        """Same protection for the 'has:' auto namespace."""
        issue = db.create_issue("Anchor")
        bad = tmp_path / "evil2.jsonl"
        bad.write_text(json.dumps({"_type": "label", "issue_id": issue.id, "label": "has:fake_marker"}) + "\n")
        result = db.import_jsonl(bad, merge=True)
        assert result["skipped_types"].get("<invalid_label>") == 1
        rows = db.conn.execute("SELECT label FROM labels WHERE issue_id = ?", (issue.id,)).fetchall()
        assert rows == []

    def test_import_accepts_normal_namespaced_label(self, db: FiligreeDB, tmp_path: Path) -> None:
        """Round-trip a label that is NOT in a reserved namespace."""
        issue = db.create_issue("Anchor")
        good = tmp_path / "good.jsonl"
        good.write_text(json.dumps({"_type": "label", "issue_id": issue.id, "label": "cluster:auth"}) + "\n")
        result = db.import_jsonl(good, merge=True)
        assert result["skipped_types"] == {}
        labels = {r["label"] for r in db.conn.execute("SELECT label FROM labels WHERE issue_id = ?", (issue.id,)).fetchall()}
        assert "cluster:auth" in labels
