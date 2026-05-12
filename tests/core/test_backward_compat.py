# tests/core/test_backward_compat.py
"""Template integration tests — validates core behaviors work with templates.

These tests lock in the guarantee that existing behavior is preserved when
workflow templates are enabled. If these tests break, it means the template
system is NOT backward compatible and must be fixed before merging.

Validates: WFT-AR-011, WFT-SR-015
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest

from filigree.core import FiligreeDB
from tests._db_factory import make_db

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path: Path) -> Generator[FiligreeDB, None, None]:
    """Standard FiligreeDB with core + planning packs enabled."""
    d = make_db(tmp_path, packs=["core", "planning"])
    yield d
    d.close()


# ---------------------------------------------------------------------------
# Task type (the most critical backward compat guarantee)
# ---------------------------------------------------------------------------


class TestTaskTypeTemplates:
    """Task type must behave identically to pre-template behavior."""

    def test_task_creates_with_open(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Do something", type="task")
        assert issue.status == "open"

    def test_task_transitions_open_to_in_progress(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Do something", type="task")
        updated = db.update_issue(issue.id, status="in_progress")
        assert updated.status == "in_progress"

    def test_task_transitions_in_progress_to_closed(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Do something", type="task")
        db.update_issue(issue.id, status="in_progress")
        updated = db.update_issue(issue.id, status="closed")
        assert updated.status == "closed"
        assert updated.closed_at is not None

    def test_task_claim_assigns_only(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Do something", type="task")
        claimed = db.claim_issue(issue.id, assignee="agent")
        assert claimed.status == "open"  # status unchanged — claim only sets assignee
        assert claimed.assignee == "agent"

    def test_task_release_clears_assignee(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Do something", type="task")
        db.claim_issue(issue.id, assignee="agent")
        released = db.release_claim(issue.id)
        assert released.status == "open"  # status unchanged — release only clears assignee
        assert released.assignee == ""

    def test_task_close_produces_closed(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Do something", type="task")
        closed = db.close_issue(issue.id)
        assert closed.status == "closed"
        assert closed.closed_at is not None

    def test_task_list_by_open_includes_task(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Do something", type="task")
        issues = db.list_issues(status="open")
        ids = {i.id for i in issues}
        assert issue.id in ids

    def test_task_get_ready_includes_task(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Do something", type="task")
        ready = db.get_ready()
        ids = {i.id for i in ready}
        assert issue.id in ids


class TestEpicTypeTemplates:
    """Epic type must behave identically to pre-template behavior."""

    def test_epic_creates_with_open(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Big feature", type="epic")
        assert issue.status == "open"

    def test_epic_transitions_to_in_progress(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Big feature", type="epic")
        updated = db.update_issue(issue.id, status="in_progress")
        assert updated.status == "in_progress"

    def test_epic_close(self, db: FiligreeDB) -> None:
        issue = db.create_issue("Big feature", type="epic")
        # Epic only allows in_progress→closed; close_issue now respects this
        # (filigree-cb980eee0d, P1.4: validate-transitions policy).
        db.update_issue(issue.id, status="in_progress")
        closed = db.close_issue(issue.id)
        assert closed.status == "closed"


# ---------------------------------------------------------------------------
# to_dict() output stability
# ---------------------------------------------------------------------------


class TestToDictStability:
    """Issue.to_dict() must include all previously-existing keys."""

    def test_to_dict_keys_superset(self, db: FiligreeDB) -> None:
        """to_dict() must contain at least the original v1.0 keys."""
        issue = db.create_issue("Task", type="task")
        d = issue.to_dict()
        required_keys = {
            "id",
            "title",
            "status",
            "priority",
            "type",
            "parent_id",
            "assignee",
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
        }
        assert required_keys.issubset(set(d.keys()))

    def test_to_dict_has_status_category(self, db: FiligreeDB) -> None:
        """to_dict() now includes status_category (additive, not breaking)."""
        issue = db.create_issue("Task", type="task")
        d = issue.to_dict()
        assert "status_category" in d

    def test_to_dict_types_unchanged(self, db: FiligreeDB) -> None:
        """Core field types should be unchanged."""
        issue = db.create_issue("Task", type="task")
        d = issue.to_dict()
        assert isinstance(d["id"], str)
        assert isinstance(d["title"], str)
        assert isinstance(d["status"], str)
        assert isinstance(d["priority"], int)
        assert isinstance(d["labels"], list)
        assert isinstance(d["fields"], dict)
        assert isinstance(d["is_ready"], bool)


class TestFileRecordToDictStability:
    """FileRecord.to_dict() must include all expected keys."""

    def test_to_dict_keys(self, db: FiligreeDB) -> None:
        db.register_file("/src/main.py", language="python", file_type="source")
        files = db.list_files_paginated(limit=1)
        fr = files["results"][0]
        required_keys = {"id", "path", "language", "file_type", "first_seen", "updated_at", "metadata"}
        assert required_keys.issubset(set(fr.keys()))

    def test_to_dict_types(self, db: FiligreeDB) -> None:
        db.register_file("/src/main.py", language="python", file_type="source")
        files = db.list_files_paginated(limit=1)
        fr = files["results"][0]
        assert isinstance(fr["id"], str)
        assert isinstance(fr["path"], str)
        assert isinstance(fr["metadata"], dict)


class TestScanFindingToDictStability:
    """ScanFinding.to_dict() must include all expected keys."""

    def test_to_dict_keys(self, db: FiligreeDB) -> None:
        db.register_file("/src/main.py", language="python", file_type="source")
        db.process_scan_results(
            scan_source="test",
            findings=[
                {
                    "path": "/src/main.py",
                    "rule_id": "R001",
                    "message": "test finding",
                    "severity": "high",
                    "line_start": 1,
                    "line_end": 5,
                }
            ],
        )
        files = db.list_files_paginated(limit=1)
        file_id = files["results"][0]["id"]
        findings = db.get_findings_paginated(file_id=file_id, limit=1)
        sf = findings["results"][0]
        required_keys = {
            "id",
            "file_id",
            "severity",
            "status",
            "scan_source",
            "rule_id",
            "message",
            "suggestion",
            "scan_run_id",
            "line_start",
            "line_end",
            "issue_id",
            "seen_count",
            "first_seen",
            "updated_at",
            "last_seen_at",
            "metadata",
            "data_warnings",
        }
        assert set(sf.keys()) == required_keys

    def test_to_dict_types(self, db: FiligreeDB) -> None:
        db.register_file("/src/main.py", language="python", file_type="source")
        db.process_scan_results(
            scan_source="test",
            findings=[
                {
                    "path": "/src/main.py",
                    "rule_id": "R001",
                    "message": "test",
                    "severity": "high",
                }
            ],
        )
        files = db.list_files_paginated(limit=1)
        file_id = files["results"][0]["id"]
        findings = db.get_findings_paginated(file_id=file_id, limit=1)
        sf = findings["results"][0]
        assert isinstance(sf["id"], str)
        assert isinstance(sf["severity"], str)
        assert isinstance(sf["seen_count"], int)


# ---------------------------------------------------------------------------
# Dependencies and blocking
# ---------------------------------------------------------------------------


class TestDependencyWithTemplates:
    """Dependencies must work the same with templates enabled."""

    def test_add_dependency(self, db: FiligreeDB) -> None:
        a = db.create_issue("A", type="task")
        b = db.create_issue("B", type="task")
        db.add_dependency(b.id, a.id)
        b_fresh = db.get_issue(b.id)
        assert a.id in b_fresh.blocked_by

    def test_cycle_detection(self, db: FiligreeDB) -> None:
        a = db.create_issue("A", type="task")
        b = db.create_issue("B", type="task")
        db.add_dependency(b.id, a.id)
        with pytest.raises(ValueError, match="cycle"):
            db.add_dependency(a.id, b.id)

    def test_closing_blocker_unblocks(self, db: FiligreeDB) -> None:
        a = db.create_issue("Blocker", type="task")
        b = db.create_issue("Blocked", type="task")
        db.add_dependency(b.id, a.id)
        assert db.get_issue(b.id).is_ready is False
        db.close_issue(a.id)
        assert db.get_issue(b.id).is_ready is True


# ---------------------------------------------------------------------------
# Batch operations
# ---------------------------------------------------------------------------


class TestBatchWithTemplates:
    """Batch operations must work with templates."""

    def test_batch_close(self, db: FiligreeDB) -> None:
        a = db.create_issue("A", type="task")
        b = db.create_issue("B", type="task")
        results, errors = db.batch_close([a.id, b.id])
        assert all(r.status == "closed" for r in results)
        assert len(errors) == 0

    def test_batch_close_mixed_types_auto_resolves(self, db: FiligreeDB) -> None:
        """Mixed-type cleanup: task closes normally; phase/step/milestone in their
        initial states auto-resolve to the unique reachable done state instead of
        failing INVALID_TRANSITION. (F1 — review-h.)
        """
        t = db.create_issue("Task", type="task")
        p = db.create_issue("Phase", type="phase")
        s = db.create_issue("Step", type="step")
        m = db.create_issue("Milestone", type="milestone")
        results, errors = db.batch_close([t.id, p.id, s.id, m.id], reason="cleanup")
        assert len(errors) == 0
        by_id = {r.id: r for r in results}
        assert by_id[t.id].status == "closed"
        assert by_id[p.id].status == "skipped"
        assert by_id[s.id].status == "skipped"
        assert by_id[m.id].status == "cancelled"

    def test_batch_close_ambiguous_done_still_fails(self, db: FiligreeDB) -> None:
        """A bug in 'triage' has two reachable done states (wont_fix, not_a_bug)
        and the default 'closed' is unreachable — auto-resolve should NOT pick
        arbitrarily. The failure echoes valid_transitions so the caller can retry.
        """
        bug = db.create_issue("Bug", type="bug")
        task = db.create_issue("Task", type="task")
        results, errors = db.batch_close([bug.id, task.id])
        assert len(results) == 1
        assert results[0].id == task.id
        assert len(errors) == 1
        assert errors[0]["id"] == bug.id
        assert errors[0]["code"] == "INVALID_TRANSITION"
        assert "valid_transitions" in errors[0]

    def test_batch_update_status(self, db: FiligreeDB) -> None:
        a = db.create_issue("A", type="task")
        b = db.create_issue("B", type="task")
        results, errors = db.batch_update([a.id, b.id], status="in_progress")
        assert all(r.status == "in_progress" for r in results)
        assert len(errors) == 0
