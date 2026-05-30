"""Tests for core batch operations — batch close, update, label, comment."""

from __future__ import annotations

import logging

import pytest

from filigree.core import FiligreeDB, WrongProjectError


class TestBatchOperations:
    def test_batch_close(self, db: FiligreeDB) -> None:
        a = db.create_issue("A")
        b = db.create_issue("B")
        results, errors = db.batch_close([a.id, b.id], reason="done")
        assert len(results) == 2
        assert len(errors) == 0
        assert all(r.status == "closed" for r in results)

    def test_batch_close_mixed_types_task_and_bug(self, db: FiligreeDB) -> None:
        """Mixed task/bug closes use each issue's own workflow template."""
        task = db.create_issue("Task")
        bug = db.create_issue("Bug", type="bug", fields={"severity": "major"})
        db.update_issue(bug.id, status="confirmed")
        db.update_issue(bug.id, status="fixing", fields={"root_cause": "bad assumption"})
        db.update_issue(bug.id, status="verifying", fields={"fix_verification": "regression passes"})

        results, errors = db.batch_close([task.id, bug.id], reason="done")

        assert errors == []
        assert {issue.id for issue in results} == {task.id, bug.id}
        assert db.get_issue(task.id).status == "closed"
        assert db.get_issue(bug.id).status == "closed"

    def test_batch_close_middle_failure_is_durable(self, db: FiligreeDB) -> None:
        """A per-item failure in the middle does not roll back neighboring successes."""
        first = db.create_issue("First task")
        failing_bug = db.create_issue("Bug still in triage", type="bug")
        last = db.create_issue("Last task")

        results, errors = db.batch_close([first.id, failing_bug.id, last.id], reason="done")

        assert {issue.id for issue in results} == {first.id, last.id}
        assert len(errors) == 1
        assert errors[0]["id"] == failing_bug.id
        assert db.get_issue(first.id).status == "closed"
        assert db.get_issue(failing_bug.id).status == "triage"
        assert db.get_issue(last.id).status == "closed"

    def test_batch_update_status(self, db: FiligreeDB) -> None:
        a = db.create_issue("A")
        b = db.create_issue("B")
        results, errors = db.batch_update([a.id, b.id], status="in_progress")
        assert len(results) == 2
        assert len(errors) == 0
        assert all(r.status == "in_progress" for r in results)

    def test_batch_update_priority(self, db: FiligreeDB) -> None:
        a = db.create_issue("A")
        b = db.create_issue("B")
        results, errors = db.batch_update([a.id, b.id], priority=0)
        assert all(r.priority == 0 for r in results)
        assert len(errors) == 0

    def test_batch_update_not_found(self, db: FiligreeDB) -> None:
        a = db.create_issue("A")
        results, errors = db.batch_update([a.id, "test-deadbeef00"], priority=0)
        assert len(results) == 1
        assert len(errors) == 1
        assert errors[0]["id"] == "test-deadbeef00"

    def test_batch_close_not_found(self, db: FiligreeDB) -> None:
        results, errors = db.batch_close(["test-deadbeef00"])
        assert len(results) == 0
        assert len(errors) == 1
        assert errors[0]["id"] == "test-deadbeef00"

    def test_batch_add_label(self, db: FiligreeDB) -> None:
        a = db.create_issue("A")
        b = db.create_issue("B")
        labeled, errors = db.batch_add_label([a.id, b.id], label="security")
        assert len(labeled) == 2
        assert len(errors) == 0
        assert all(row["status"] == "added" for row in labeled)

    def test_batch_add_label_not_found(self, db: FiligreeDB) -> None:
        labeled, errors = db.batch_add_label(["test-deadbeef00"], label="security")
        assert labeled == []
        assert len(errors) == 1
        assert errors[0]["code"] == "NOT_FOUND"

    def test_batch_add_label_validation_error(self, db: FiligreeDB) -> None:
        issue = db.create_issue("A")
        labeled, errors = db.batch_add_label([issue.id], label="bug")
        assert labeled == []
        assert len(errors) == 1
        assert errors[0]["code"] == "VALIDATION"

    @pytest.mark.parametrize("label", ["P1", "priority:1"])
    def test_batch_add_label_rejects_priority_like_labels(self, db: FiligreeDB, label: str) -> None:
        issue = db.create_issue("A")
        labeled, errors = db.batch_add_label([issue.id], label=label)

        assert labeled == []
        assert len(errors) == 1
        assert errors[0]["code"] == "VALIDATION"
        assert "priority field" in errors[0]["error"]
        assert db.get_issue(issue.id).labels == []

    def test_batch_remove_label(self, db: FiligreeDB) -> None:
        a = db.create_issue("A", labels=["security"])
        b = db.create_issue("B", labels=["security"])
        removed, errors = db.batch_remove_label([a.id, b.id], label="security")
        assert len(removed) == 2
        assert len(errors) == 0
        assert all(row["status"] == "removed" for row in removed)
        assert "security" not in db.get_issue(a.id).labels
        assert "security" not in db.get_issue(b.id).labels

    def test_batch_remove_label_not_found(self, db: FiligreeDB) -> None:
        removed, errors = db.batch_remove_label(["test-deadbeef00"], label="security")
        assert removed == []
        assert len(errors) == 1
        assert errors[0]["code"] == "NOT_FOUND"

    def test_batch_remove_label_validation_error(self, db: FiligreeDB) -> None:
        issue = db.create_issue("A")
        removed, errors = db.batch_remove_label([issue.id], label="bug")
        assert removed == []
        assert len(errors) == 1
        assert errors[0]["code"] == "VALIDATION"

    def test_batch_add_comment(self, db: FiligreeDB) -> None:
        a = db.create_issue("A")
        b = db.create_issue("B")
        commented, errors = db.batch_add_comment([a.id, b.id], text="triage complete", author="agent-1")
        assert len(commented) == 2
        assert len(errors) == 0
        assert all(isinstance(row["comment_id"], int) for row in commented)

    def test_batch_add_comment_not_found(self, db: FiligreeDB) -> None:
        commented, errors = db.batch_add_comment(["test-deadbeef00"], text="triage complete")
        assert commented == []
        assert len(errors) == 1
        assert errors[0]["code"] == "NOT_FOUND"

    def test_batch_add_comment_validation_error(self, db: FiligreeDB) -> None:
        issue = db.create_issue("A")
        commented, errors = db.batch_add_comment([issue.id], text="   ")
        assert commented == []
        assert len(errors) == 1
        assert errors[0]["code"] == "VALIDATION"


class TestBatchForeignPrefixAborts:
    """2.1.0 §0.4: every batch handler aborts envelope-level when an id
    has a foreign project prefix, rather than producing N misleading
    per-item NOT_FOUND/VALIDATION failures. The preflight check fires
    before any per-item write commits, so no partial mutation lands."""

    def test_batch_close_foreign_prefix_aborts_batch_not_per_item(self, db: FiligreeDB) -> None:
        a = db.create_issue("A")
        with pytest.raises(WrongProjectError):
            db.batch_close([a.id, "foreign-deadbeef01"])
        # Pre-flight aborts before close: the local issue must NOT be closed.
        assert db.get_issue(a.id).status != "closed"

    def test_batch_update_foreign_prefix_aborts_batch_not_per_item(self, db: FiligreeDB) -> None:
        a = db.create_issue("A")
        with pytest.raises(WrongProjectError):
            db.batch_update([a.id, "foreign-deadbeef01"], priority=0)
        assert db.get_issue(a.id).priority != 0

    def test_batch_add_label_foreign_prefix_aborts_batch(self, db: FiligreeDB) -> None:
        a = db.create_issue("A")
        with pytest.raises(WrongProjectError):
            db.batch_add_label([a.id, "foreign-deadbeef01"], label="security")
        assert "security" not in db.get_issue(a.id).labels

    def test_batch_remove_label_foreign_prefix_aborts_batch(self, db: FiligreeDB) -> None:
        a = db.create_issue("A", labels=["security"])
        with pytest.raises(WrongProjectError):
            db.batch_remove_label([a.id, "foreign-deadbeef01"], label="security")
        assert "security" in db.get_issue(a.id).labels

    def test_batch_add_comment_foreign_prefix_aborts_batch(self, db: FiligreeDB) -> None:
        a = db.create_issue("A")
        with pytest.raises(WrongProjectError):
            db.batch_add_comment([a.id, "foreign-deadbeef01"], text="note")
        assert db.get_comments(a.id) == []


class TestBatchInputValidation:
    """Bug filigree-c45430: batch_close/batch_update must validate issue_ids type."""

    def test_batch_close_string_raises(self, db: FiligreeDB) -> None:
        with pytest.raises(TypeError, match="issue_ids must be a list of strings"):
            db.batch_close("not-a-list")  # type: ignore[arg-type]

    def test_batch_close_list_of_ints_raises(self, db: FiligreeDB) -> None:
        with pytest.raises(TypeError, match="issue_ids must be a list of strings"):
            db.batch_close([1, 2, 3])  # type: ignore[list-item]

    def test_batch_update_string_raises(self, db: FiligreeDB) -> None:
        with pytest.raises(TypeError, match="issue_ids must be a list of strings"):
            db.batch_update("not-a-list", status="closed")  # type: ignore[arg-type]

    def test_batch_update_list_of_ints_raises(self, db: FiligreeDB) -> None:
        with pytest.raises(TypeError, match="issue_ids must be a list of strings"):
            db.batch_update([1, 2, 3], status="closed")  # type: ignore[list-item]

    def test_batch_add_label_string_raises(self, db: FiligreeDB) -> None:
        with pytest.raises(TypeError, match="issue_ids must be a list of strings"):
            db.batch_add_label("not-a-list", label="security")  # type: ignore[arg-type]

    def test_batch_remove_label_string_raises(self, db: FiligreeDB) -> None:
        with pytest.raises(TypeError, match="issue_ids must be a list of strings"):
            db.batch_remove_label("not-a-list", label="security")  # type: ignore[arg-type]

    def test_batch_add_comment_string_raises(self, db: FiligreeDB) -> None:
        with pytest.raises(TypeError, match="issue_ids must be a list of strings"):
            db.batch_add_comment("not-a-list", text="note")  # type: ignore[arg-type]

    def test_batch_close_valid_list_passes(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Closeable")
        closed, errors = db.batch_close([issue.id])
        assert len(closed) == 1
        assert len(errors) == 0

    def test_batch_update_valid_list_passes(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Updateable")
        updated, errors = db.batch_update([issue.id], priority=0)
        assert len(updated) == 1
        assert len(errors) == 0

    def test_batch_add_label_valid_list_passes(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Labelable")
        labeled, errors = db.batch_add_label([issue.id], label="security")
        assert len(labeled) == 1
        assert len(errors) == 0

    def test_batch_remove_label_valid_list_passes(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Labelable", labels=["security"])
        removed, errors = db.batch_remove_label([issue.id], label="security")
        assert len(removed) == 1
        assert len(errors) == 0

    def test_batch_add_comment_valid_list_passes(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Commentable")
        commented, errors = db.batch_add_comment([issue.id], text="done")
        assert len(commented) == 1
        assert len(errors) == 0


class TestBatchTransitionEnrichmentRace:
    """batch close/update should handle issue deletion between action and transition lookup."""

    def test_batch_close_already_closed_includes_valid_transitions(self, db: FiligreeDB) -> None:
        """A ValueError during close should enrich with valid_transitions."""
        issue = db.create_issue("Test")
        db.close_issue(issue.id)
        # Closing again triggers ValueError; enrichment should add valid_transitions
        _results, errors = db.batch_close([issue.id])
        assert len(errors) == 1
        assert errors[0]["code"] == "INVALID_TRANSITION"

    def test_batch_close_deleted_issue_after_valueerror(self, db: FiligreeDB) -> None:
        """If issue is deleted between ValueError and get_valid_transitions, no crash.

        Simulates a TOCTOU race: close_issue raises ValueError (already closed),
        then get_valid_transitions raises KeyError (concurrent deletion).
        """
        from unittest.mock import patch

        issue = db.create_issue("Test")
        db.close_issue(issue.id)
        # Mock get_valid_transitions to raise KeyError, simulating concurrent deletion
        with patch.object(db, "get_valid_transitions", side_effect=KeyError(issue.id)):
            _results, errors = db.batch_close([issue.id])
        assert len(errors) == 1
        assert errors[0]["code"] == "INVALID_TRANSITION"
        # Should NOT have valid_transitions key since the lookup failed
        assert "valid_transitions" not in errors[0]

    def test_batch_close_transition_enrichment_failure_warns_and_preserves_error(
        self,
        db: FiligreeDB,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Unexpected enrichment failures are warning-level, but best-effort."""

        issue = db.create_issue("Test")
        db.close_issue(issue.id)

        def fail_transition_lookup(issue_id: str) -> list[object]:
            raise RuntimeError(f"transition cache unavailable for {issue_id}")

        monkeypatch.setattr(db, "get_valid_transitions", fail_transition_lookup)

        with caplog.at_level(logging.WARNING, logger="filigree.db_issues"):
            _results, errors = db.batch_close([issue.id])

        assert len(errors) == 1
        assert errors[0]["code"] == "INVALID_TRANSITION"
        assert "valid_transitions" not in errors[0]
        assert "failed to enrich invalid-transition error" in caplog.text

    def test_batch_update_validation_valueerror_classified_as_validation(self, db: FiligreeDB) -> None:
        """Non-transition ValueErrors (e.g. field validation) must be VALIDATION, not INVALID_TRANSITION.

        _batch_with_transition_errors previously labelled every ValueError as
        INVALID_TRANSITION, but update_issue also raises validation-class errors
        ("Field validation failed: ...", "Priority must be between 0 and 4").
        Those are not state-machine rejections and must surface as VALIDATION so
        clients can distinguish "fix your input" from "pick a different transition".
        """
        from unittest.mock import patch

        issue = db.create_issue("Test")
        with patch.object(
            db,
            "update_issue",
            side_effect=ValueError("Field validation failed: priority: must be positive"),
        ):
            _results, errors = db.batch_update([issue.id], priority=2)
        assert len(errors) == 1
        assert errors[0]["code"] == "VALIDATION"
        # Validation-class failures should not be enriched with valid_transitions
        assert "valid_transitions" not in errors[0]

    def test_batch_close_transition_valueerror_still_enriched(self, db: FiligreeDB) -> None:
        """Transition ValueErrors (contain 'status'/'transition'/'state') keep enrichment.

        Classifying doesn't change behaviour for the common already-closed case;
        it still surfaces INVALID_TRANSITION with valid_transitions attached.
        """
        issue = db.create_issue("Test")
        db.close_issue(issue.id)
        _results, errors = db.batch_close([issue.id])
        assert len(errors) == 1
        assert errors[0]["code"] == "INVALID_TRANSITION"
        assert "valid_transitions" in errors[0]
