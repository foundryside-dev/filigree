"""§0.3 — typed ClaimConflictError pin tests.

Every CAS-failure path in the data layer raises ``ClaimConflictError``
(subclass of ``ValueError``), and every dispatch site routes via
``isinstance`` rather than message-text matching. These tests pin the
contract so future message rewording cannot silently downgrade CONFLICT
to VALIDATION (the 2.1.0 design's silent-failure C1 / python-engineer C1
finding).
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


# ---------------------------------------------------------------------------
# Raise sites: every CAS-failure path raises ClaimConflictError
# ---------------------------------------------------------------------------


class TestRaiseSites:
    def test_check_expected_assignee_raises_typed(self, db: FiligreeDB) -> None:
        """_check_expected_assignee (via update_issue) raises ClaimConflictError."""
        issue = db.create_issue("Task", type="task")
        db.claim_issue(issue.id, assignee="alice", actor="alice")
        with pytest.raises(ClaimConflictError) as exc:
            db.update_issue(issue.id, title="hijacked", actor="bob", expected_assignee="bob")
        assert exc.value.observed == "alice"
        assert exc.value.expected == "bob"

    def test_release_claim_wrong_holder_raises_typed(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Task", type="task")
        db.claim_issue(issue.id, assignee="alice", actor="alice")
        with pytest.raises(ClaimConflictError):
            db.release_claim(issue.id, actor="bob", if_held=True)

    def test_heartbeat_wrong_holder_raises_typed(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Task", type="task")
        db.claim_issue(issue.id, assignee="alice", actor="alice")
        with pytest.raises(ClaimConflictError):
            db.heartbeat_work(issue.id, actor="bob", expected_assignee="bob")

    def test_reclaim_wrong_expected_raises_typed(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Task", type="task")
        db.claim_issue(issue.id, assignee="alice", actor="alice")
        with pytest.raises(ClaimConflictError):
            db.reclaim_issue(
                issue.id,
                assignee="carol",
                actor="carol",
                expected_assignee="bob",
                reason="stuck",
            )

    def test_claim_conflict_error_is_value_error_subclass(self) -> None:
        """Backward compatibility: pre-typed-exception callers still catch via ValueError."""
        err = ClaimConflictError("x-001", observed="alice", expected="bob")
        assert isinstance(err, ValueError)
        message = str(err)
        assert "alice" in message
        assert "bob" in message
        assert err.issue_id == "x-001"


# ---------------------------------------------------------------------------
# Dispatch sites: batch handlers route ClaimConflictError → CONFLICT
# ---------------------------------------------------------------------------


class TestBatchDispatch:
    def test_batch_close_reports_conflict_for_claim_conflict(self, db: FiligreeDB) -> None:
        """batch_close's _batch_with_transition_errors emits CONFLICT, not VALIDATION."""
        issue = db.create_issue("Task", type="task")
        db.claim_issue(issue.id, assignee="alice", actor="alice")
        _closed, failures = db.batch_close([issue.id], actor="bob", expected_assignee="bob")
        assert len(failures) == 1
        assert failures[0]["id"] == issue.id
        assert failures[0]["code"] == ErrorCode.CONFLICT

    def test_batch_add_label_reports_conflict_for_claim_conflict(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Task", type="task")
        db.claim_issue(issue.id, assignee="alice", actor="alice")
        _ok, errors = db.batch_add_label([issue.id], label="urgent", actor="bob", expected_assignee="bob")
        assert len(errors) == 1
        assert errors[0]["code"] == ErrorCode.CONFLICT

    def test_batch_remove_label_reports_conflict_for_claim_conflict(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Task", type="task")
        db.add_label(issue.id, "urgent")
        db.claim_issue(issue.id, assignee="alice", actor="alice")
        _ok, errors = db.batch_remove_label([issue.id], label="urgent", actor="bob", expected_assignee="bob")
        assert len(errors) == 1
        assert errors[0]["code"] == ErrorCode.CONFLICT

    def test_batch_add_comment_reports_conflict_for_claim_conflict(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Task", type="task")
        db.claim_issue(issue.id, assignee="alice", actor="alice")
        _ok, errors = db.batch_add_comment([issue.id], text="note", author="bob", expected_assignee="bob")
        assert len(errors) == 1
        assert errors[0]["code"] == ErrorCode.CONFLICT

    def test_release_claim_reports_conflict_for_wrong_holder(self, db: FiligreeDB) -> None:
        """release_claim's CAS path raises ClaimConflictError when assignee
        was stolen between the SELECT and UPDATE — pins the line-1290
        dispatch contract via direct invocation (release_my_claims would
        skip a row that no longer matches its discovery filter)."""
        issue = db.create_issue("Task", type="task")
        db.claim_issue(issue.id, assignee="alice", actor="alice")
        db.conn.execute("UPDATE issues SET assignee = 'mallory' WHERE id = ?", (issue.id,))
        db.conn.commit()
        with pytest.raises(ClaimConflictError):
            db.release_claim(issue.id, actor="alice", if_held=True)
